"""galgame 的三态判断：沿用动画的条件类型和结果结构，只是证据换成 VNDB 的标签投票。

VNDB 和 Bangumi 的标签最大的不同：VNDB 可以投反对票（-3 到 -1 表示“没有这个要素”）。所以
- “排除某标签”有了正面证据：有人明确投了“没有” → 满足（动画那边最多只能“很可能满足”）
- “想要某标签”有了反面证据：平均分 ≤ 0 → 冲突
标签没人投票时仍然是“缺少证据 ≠ 不含该内容”：只有作品标签很齐全（tag_count 达标）才算很可能没有。
"""

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from acg_agent.galgame.concepts import TagIndex
from acg_agent.verdict.models import (
    SEVERITY,
    Condition,
    ConditionResult,
    Evidence,
    TagExcluded,
    TagRequired,
    Verdict,
    YearRange,
)

STRONG_SCORE = 1.0     # 想要的标签平均分达到这个数、且至少 MIN_VOTES 票，才算满足；否则只是很可能满足
MIN_VOTES = 2          # 一个人投的票不足以排除作品，也不足以确认
WELL_TAGGED = 30       # 作品有这么多个有效标签，才认为标签齐全（投票 ≥100 的作品大多 ≥30）
SPOILER_AT = 1.0       # 剧透等级平均达到这个数，依据里标“含剧透”


class UnsupportedCondition(ValueError):
    pass


class VnResult(BaseModel):
    """和动画的 WorkResult 一样，只是作品 ID 是 VNDB 的字符串 ID（如 'v17'）。"""

    vn_id: str
    overall: Verdict
    results: list[ConditionResult]
    weak_evidence: bool


@dataclass
class VnFacts:
    vn_id: str
    release_year: int | None
    tag_count: int
    tags: dict[str, tuple[float, int, int, float | None]] = field(default_factory=dict)  # 标签 → (平均分, 票数, 反对票, 剧透)


def load_facts(conn: sqlite3.Connection, vn_ids: Iterable[str], tag_ids: set[str]) -> dict[str, VnFacts]:
    ids = list(vn_ids)
    facts: dict[str, VnFacts] = {}
    for start in range(0, len(ids), 900):  # SQLite 一条语句的参数个数有上限
        batch = ids[start:start + 900]
        marks = ", ".join("?" for _ in batch)
        for vid, year, count in conn.execute(f"SELECT id, release_year, tag_count FROM vn WHERE id IN ({marks})", batch):
            facts[vid] = VnFacts(vid, year, count)
        if tag_ids:
            tag_marks = ", ".join("?" for _ in tag_ids)
            for vid, tid, score, votes, neg, spoiler in conn.execute(
                f"SELECT vn_id, tag_id, score, votes, negative, spoiler FROM vn_tag"
                f" WHERE vn_id IN ({marks}) AND tag_id IN ({tag_marks})", [*batch, *tag_ids]
            ):
                facts[vid].tags[tid] = (score, votes, neg, spoiler)
    return {vid: facts[vid] for vid in ids if vid in facts}


def _result(c, verdict, code, weak=False, **detail) -> ConditionResult:
    return ConditionResult(condition=c, verdict=verdict, evidence=[Evidence(code=code, detail=detail)], weak=weak)


def _tag_detail(index: TagIndex, tid: str, stats) -> dict:
    score, votes, neg, spoiler = stats
    return {"tag": index.names.get(tid, tid), "score": round(score, 2), "votes": votes, "negative": neg,
            "spoiler": int(spoiler is not None and spoiler >= SPOILER_AT)}


def _year(c: YearRange, f: VnFacts) -> ConditionResult:
    if f.release_year is None:
        return _result(c, Verdict.UNKNOWN, "date_missing")
    if (c.min_year is not None and f.release_year < c.min_year) or (c.max_year is not None and f.release_year > c.max_year):
        return _result(c, Verdict.CONFLICT, "year_out_of_range", year=f.release_year)
    return _result(c, Verdict.SATISFIED, "year_in_range", year=f.release_year)


def _tag_required(c: TagRequired, f: VnFacts, tags: frozenset[str], index: TagIndex) -> ConditionResult:
    voted = {t: f.tags[t] for t in tags if t in f.tags}
    if not voted:
        return _result(c, Verdict.UNKNOWN, "vndb_tag_absent", tag_count=f.tag_count)
    best = max(voted, key=lambda t: (voted[t][0], voted[t][1]))
    score, votes = voted[best][0], voted[best][1]
    detail = _tag_detail(index, best, voted[best])
    if score >= STRONG_SCORE and votes >= MIN_VOTES:
        return _result(c, Verdict.SATISFIED, "vndb_tag_present", **detail)
    if score > 0:
        return _result(c, Verdict.LIKELY, "vndb_tag_weak", weak=True, **detail)
    return _result(c, Verdict.CONFLICT, "vndb_tag_denied", **detail)  # 投票的人多数认为没有


def _tag_excluded(c: TagExcluded, f: VnFacts, tags: frozenset[str], index: TagIndex) -> ConditionResult:
    voted = {t: f.tags[t] for t in tags if t in f.tags}
    positive = {t: s for t, s in voted.items() if s[0] > 0}
    if positive:
        best = max(positive, key=lambda t: (positive[t][1], positive[t][0]))
        detail = _tag_detail(index, best, positive[best])
        if positive[best][1] >= MIN_VOTES:
            return _result(c, Verdict.CONFLICT, "vndb_tag_present", **detail)
        return _result(c, Verdict.UNKNOWN, "vndb_tag_weak", weak=True, **detail)  # 只有一个人说有
    if voted:  # 有人投了票，而且平均分都 ≤ 0：明确说“没有”
        best = max(voted, key=lambda t: voted[t][2])
        return _result(c, Verdict.SATISFIED, "vndb_tag_denied", **_tag_detail(index, best, voted[best]))
    if f.tag_count >= WELL_TAGGED:
        return _result(c, Verdict.LIKELY, "vndb_tag_absent_well_tagged", tag_count=f.tag_count)
    return _result(c, Verdict.UNKNOWN, "vndb_tag_absent", tag_count=f.tag_count)


def evaluate(conn: sqlite3.Connection, vn_ids: Iterable[str], conditions: Sequence[Condition],
             index: TagIndex) -> list[VnResult]:
    if not conditions:
        raise ValueError("至少要有一个条件")
    resolved: list[frozenset[str] | None] = []
    for c in conditions:
        if isinstance(c, (TagRequired, TagExcluded)):
            resolved.append(index.resolve(c.concept))
        elif isinstance(c, YearRange):
            resolved.append(None)
        else:
            raise UnsupportedCondition(f"galgame 不支持“{c.kind}”条件（只支持年份和标签）")
    all_tags = set().union(*(r for r in resolved if r))
    facts = load_facts(conn, vn_ids, all_tags)
    out = []
    for f in facts.values():
        results = []
        for c, tags in zip(conditions, resolved, strict=True):
            if isinstance(c, YearRange):
                results.append(_year(c, f))
            elif isinstance(c, TagRequired):
                results.append(_tag_required(c, f, tags, index))
            else:
                results.append(_tag_excluded(c, f, tags, index))
        if len(results) != len(conditions):  # 每个条件都必须有判断结果
            raise RuntimeError("有条件没有得到判断结果")
        out.append(VnResult(vn_id=f.vn_id, overall=max((r.verdict for r in results), key=SEVERITY.__getitem__),
                            results=results, weak_evidence=any(r.weak for r in results)))
    return out
