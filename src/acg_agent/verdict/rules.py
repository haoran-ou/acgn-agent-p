"""判断规则：对一部作品的每个条件给出 verdict 和依据。

这里全是纯函数：输入是 WorkFacts，不碰数据库，方便测试。
核心原则：缺少证据不等于有反面证据（absence of evidence is not evidence of absence）。
所以“排除某标签”永远不会判为 satisfied，最好也只能是 likely_satisfied。
"""

import sqlite3
from collections.abc import Iterable, Sequence

from acg_agent.verdict.concepts import tag_names
from acg_agent.verdict.facts import WorkFacts, load_facts
from acg_agent.verdict.models import (
    DEFAULT_CONFIG,
    SEVERITY,
    Condition,
    ConditionResult,
    Evidence,
    FormatIn,
    TagExcluded,
    TagRequired,
    Verdict,
    VerdictConfig,
    WorkResult,
    YearRange,
)


def evaluate(
    conn: sqlite3.Connection,
    work_ids: Iterable[int],
    conditions: Sequence[Condition],
    config: VerdictConfig = DEFAULT_CONFIG,
) -> list[WorkResult]:
    facts = load_facts(conn, work_ids)
    return [evaluate_work(f, conditions, config) for f in facts.values()]


def evaluate_work(
    facts: WorkFacts, conditions: Sequence[Condition], config: VerdictConfig = DEFAULT_CONFIG
) -> WorkResult:
    if not conditions:
        raise ValueError("至少要有一个条件")
    results = [evaluate_condition(c, facts, config) for c in conditions]
    # 每个条件都必须有判断结果，不能悄悄丢掉任何一个（CLAUDE.md 的规则）。
    # 不用 assert：python -O 会把 assert 整个去掉
    if len(results) != len(conditions):
        raise RuntimeError("有条件没有得到判断结果")
    overall = max((r.verdict for r in results), key=SEVERITY.__getitem__)
    return WorkResult(
        work_id=facts.work_id,
        overall=overall,
        results=results,
        weak_evidence=any(r.weak for r in results),
    )


def evaluate_condition(
    condition: Condition, facts: WorkFacts, config: VerdictConfig = DEFAULT_CONFIG
) -> ConditionResult:
    match condition:
        case YearRange():
            return _year(condition, facts)
        case FormatIn():
            return _format(condition, facts)
        case TagRequired():
            return _tag_required(condition, facts, config)
        case TagExcluded():
            return _tag_excluded(condition, facts, config)
    raise TypeError(f"不认识的条件类型：{type(condition).__name__}")


def _result(condition, verdict, code, weak=False, **detail) -> ConditionResult:
    return ConditionResult(
        condition=condition, verdict=verdict, evidence=[Evidence(code=code, detail=detail)], weak=weak
    )


def _year(c: YearRange, f: WorkFacts) -> ConditionResult:
    if f.release_date is None:
        return _result(c, Verdict.UNKNOWN, "date_missing")
    year = int(f.release_date[:4])
    too_early = c.min_year is not None and year < c.min_year
    too_late = c.max_year is not None and year > c.max_year
    if too_early or too_late:
        return _result(c, Verdict.CONFLICT, "year_out_of_range", year=year)
    return _result(c, Verdict.SATISFIED, "year_in_range", year=year)


def _format(c: FormatIn, f: WorkFacts) -> ConditionResult:
    if f.format is None:
        return _result(c, Verdict.UNKNOWN, "format_missing")
    if f.format in c.formats:
        return _result(c, Verdict.SATISFIED, "format_match", format=f.format)
    return _result(c, Verdict.CONFLICT, "format_mismatch", format=f.format)


def _strongest_user_tag(names: tuple[str, ...], f: WorkFacts) -> tuple[str | None, int]:
    """同义词里打标人数最多的那个用户标签。"""
    best_name, best_count = None, 0
    for name in names:
        count = f.user_tags.get(name, 0)
        if count > best_count:
            best_name, best_count = name, count
    return best_name, best_count


def _meta_hit(names: tuple[str, ...], f: WorkFacts) -> str | None:
    return next((n for n in names if n in f.meta_tags), None)


def _tag_required(c: TagRequired, f: WorkFacts, cfg: VerdictConfig) -> ConditionResult:
    names = tag_names(c.concept)
    if meta := _meta_hit(names, f):
        return _result(c, Verdict.SATISFIED, "meta_tag_present", tag=meta)
    name, count = _strongest_user_tag(names, f)
    if count >= cfg.weak_count_below:
        return _result(c, Verdict.SATISFIED, "user_tag_present", tag=name, count=count)
    if count > 0:
        return _result(c, Verdict.LIKELY, "user_tag_weak", weak=True, tag=name, count=count)
    return _result(
        c, Verdict.UNKNOWN, "tag_absent",
        list_is_full=int(f.list_is_full), upper_bound=f.unlisted_tag_max_count,
    )


def _tag_excluded(c: TagExcluded, f: WorkFacts, cfg: VerdictConfig) -> ConditionResult:
    names = tag_names(c.concept)
    if meta := _meta_hit(names, f):
        return _result(c, Verdict.CONFLICT, "meta_tag_present", tag=meta)
    name, count = _strongest_user_tag(names, f)
    weak = 0 < count < cfg.weak_count_below
    if count >= cfg.exclude_conflict_min_count:
        return _result(c, Verdict.CONFLICT, "user_tag_present", weak=weak, tag=name, count=count)
    if count > 0:
        # 只有一两个人打：不足以排除，但也不能说不含
        return _result(c, Verdict.UNKNOWN, "user_tag_weak", weak=True, tag=name, count=count)
    # 标签没出现：只有标签列表满额、而且上界足够高，才算很可能满足（上界证据，upper-bound evidence）
    if f.list_is_full and f.unlisted_tag_max_count >= cfg.likely_min_upper_bound:
        return _result(
            c, Verdict.LIKELY, "tag_absent_strong_upper_bound", upper_bound=f.unlisted_tag_max_count
        )
    code = "tag_absent_low_upper_bound" if f.list_is_full else "tag_absent_few_tags"
    return _result(
        c, Verdict.UNKNOWN, code,
        user_tag_count=f.user_tag_count, upper_bound=f.unlisted_tag_max_count,
    )
