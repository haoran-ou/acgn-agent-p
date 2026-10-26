"""解析编排：先用模型，任何失败都退回规则；最后统一做一遍校验，并写本地日志。"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelServerError,
    ModelTimeoutError,
    ModelUnavailableError,
    UnexpectedModelBehavior,
)

from acg_agent.config import Settings
from acg_agent.nlu.llm import build_prompt
from acg_agent.nlu.rules import parse_with_rules
from acg_agent.nlu.schema import ParsedCondition, ParsedQuery
from acg_agent.verdict.models import FormatIn, TagExcluded, TagRequired, YearRange

# 外层超时比模型自己的超时多一点：模型的超时管单次 HTTP，外层管整次调用（含一次重试）
_OUTER_TIMEOUT_MARGIN_S = 1.0
_MIN_YEAR, _MAX_YEAR = 1900, datetime.now(UTC).year + 2
_ALL_FORMATS = frozenset({"tv", "web", "ova", "movie", "motion_comic", "other"})

# 服务器临时出错（5xx、过载）时等一下再试一次。限流（429）和额度用尽不重试：重试只会更快耗尽额度
_RETRY_DELAY_S = 0.5
_RETRYABLE_STATUS = {500, 502, 503, 504}

Parser = Literal["llm", "rules"]


@dataclass
class ParseOutcome:
    query: ParsedQuery
    parser: Parser
    fallback_reason: str | None  # 为什么没用模型；用了模型就是 None
    latency_ms: int
    usage: dict = field(default_factory=dict)  # input_tokens / output_tokens / requests
    retried: bool = False  # 模型第一次出现服务器错误，重试过一次
    rule_overrides: list[str] = field(default_factory=list)  # 哪些条件类型以规则为准覆盖了模型的结果


async def parse_text(
    text: str,
    vocabulary: list[str],
    settings: Settings,
    agent: Agent | None,
    force_rules: bool = False,
) -> ParseOutcome:
    started = time.perf_counter()
    reason: str | None = None
    usage: dict = {}
    query: ParsedQuery | None = None
    attempts: list[int] = []  # 记录调用了几次模型，用来判断是否重试过

    if force_rules:
        reason = "forced"
    elif agent is None:
        reason = "no_api_key"
    else:
        try:
            # 外层超时管的是“整次调用”，包括可能的一次重试
            result = await asyncio.wait_for(
                _run_with_retry(agent, build_prompt(text, vocabulary), attempts),
                timeout=settings.parse_timeout_s + _OUTER_TIMEOUT_MARGIN_S,
            )
            query = result.output
            u = result.usage
            usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "requests": u.requests}
        except (TimeoutError, ModelTimeoutError):
            reason = "timeout"
        except (ModelRateLimitError, ModelQuotaExceededError):
            reason = "rate_limited"
        except (UnexpectedModelBehavior, ValidationError):
            reason = "invalid_output"
        except ModelHTTPError as e:
            reason = f"http_{e.status_code}"
        except ModelAPIError as e:
            reason = f"api_error:{type(e).__name__}"
        except Exception as e:  # noqa: BLE001  模型这条路出任何意外，都退回规则，接口不能因为模型而报错
            reason = f"unexpected:{type(e).__name__}"

    parser: Parser = "llm" if query is not None else "rules"
    rules_query = parse_with_rules(text, vocabulary)
    overrides: list[str] = []
    if query is None:
        query = rules_query
    else:
        query, overrides = apply_rule_overrides(query, rules_query)
    query = sanitize(query, vocabulary)
    elapsed = int((time.perf_counter() - started) * 1000)
    # 按名字传参：字段顺序调整时不会把值塞错位置（真实踩过）
    return ParseOutcome(query=query, parser=parser, fallback_reason=reason, latency_ms=elapsed,
                        usage=usage, rule_overrides=overrides, retried=len(attempts) > 1)


def _retryable(error: Exception) -> bool:
    if isinstance(error, (ModelServerError, ModelUnavailableError)):
        return True
    return isinstance(error, ModelHTTPError) and error.status_code in _RETRYABLE_STATUS


async def _run_with_retry(agent: Agent, prompt: str, attempts: list[int]):
    attempts.append(1)
    try:
        return await agent.run(prompt)
    except Exception as e:
        if not _retryable(e):
            raise
    await asyncio.sleep(_RETRY_DELAY_S)
    attempts.append(2)
    return await agent.run(prompt)


# 年份和类型是明确的事实说法，规则能准确认出；能用代码确定的事，不交给 LLM。
# 实测：模型把“不要剧场版”解析成了“只要剧场版”。
_RULE_AUTHORITATIVE = ("year", "format")


def apply_rule_overrides(llm: ParsedQuery, rules: ParsedQuery) -> tuple[ParsedQuery, list[str]]:
    """规则认出了年份或类型时，以规则为准，替换模型给出的同类条件。标签仍以模型为准。

    替换发生在原来的位置；两边结果一样时，保留模型的那一条（带着它的 source_text）。
    """
    overrides = []
    conditions = list(llm.conditions)
    for kind in _RULE_AUTHORITATIVE:
        from_rules = [pc for pc in rules.conditions if pc.condition.kind == kind]
        if not from_rules:
            continue
        positions = [i for i, pc in enumerate(conditions) if pc.condition.kind == kind]
        if [conditions[i].condition for i in positions] == [pc.condition for pc in from_rules]:
            continue
        overrides.append(kind)
        insert_at = positions[0] if positions else len(conditions)
        kept = [pc for pc in conditions if pc.condition.kind != kind]
        before = sum(1 for i in range(insert_at) if conditions[i].condition.kind != kind)
        conditions = kept[:before] + from_rules + kept[before:]
    intent = "search" if overrides and llm.intent == "unclear" else llm.intent
    return llm.model_copy(update={"conditions": conditions, "intent": intent}), overrides


def sanitize(query: ParsedQuery, vocabulary: list[str]) -> ParsedQuery:
    """模型的输出也不完全可信：词表外的标签、离谱的年份，都移进 unmatched，而不是悄悄丢掉。"""
    allowed = set(vocabulary)
    kept: list[ParsedCondition] = []
    unmatched = list(query.unmatched)
    seen = set()
    for pc in query.conditions:
        cond = pc.condition
        if isinstance(cond, (TagRequired, TagExcluded)) and cond.concept not in allowed:
            unmatched.append(pc.source_text or cond.concept)
            continue
        if isinstance(cond, YearRange) and not all(
            y is None or _MIN_YEAR <= y <= _MAX_YEAR for y in (cond.min_year, cond.max_year)
        ):
            unmatched.append(pc.source_text or f"{cond.min_year}–{cond.max_year}")
            continue
        if isinstance(cond, FormatIn) and cond.formats >= _ALL_FORMATS:
            continue  # 包含全部类型 = 不限类型，这个条件没有意义（实测模型会这样输出）
        key = cond.model_dump_json()
        if key in seen:
            continue
        seen.add(key)
        kept.append(pc)
    intent = query.intent
    if intent == "search" and not kept:
        intent = "unclear"  # 说是在找作品，但一个条件都没留下：按“没理解”处理，不能随便给结果
    unmatched = list(dict.fromkeys(u[:40] for u in unmatched if u))[:5]
    return ParsedQuery(intent=intent, conditions=kept, unmatched=unmatched)


def log_call(log_dir: Path, text: str, outcome: ParseOutcome) -> None:
    """本地日志：只在 data/logs/ 下，不提交。记原句是为了以后做评估集（你的决定）。绝不记 Key。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "text": text,
        "parser": outcome.parser,
        "fallback_reason": outcome.fallback_reason,
        "latency_ms": outcome.latency_ms,
        "usage": outcome.usage,
        "rule_overrides": outcome.rule_overrides,
        "retried": outcome.retried,
        "query": outcome.query.model_dump(mode="json"),
    }
    with (log_dir / "llm.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
