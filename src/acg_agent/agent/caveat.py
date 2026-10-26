"""Agent 回答后的自动说明：推荐的作品有条件无法确认或不满足时，由代码补一句话（TECH_DEBT 第 51 项）。

模型会自己放宽条件，回答里却说“符合要求”。放宽有两种：用 explore 模式；或者干脆把条件去掉
（d06：第二次搜索不再带“不要后宫”）。能不能确认是数据库算得出来的事，所以不靠模型自觉，
而是用这句话里搜索用过的所有条件重新判断一遍，把“未知”和“冲突”如实写出来。
"""

import sqlite3

from acg_agent.search import repository as repo
from acg_agent.verdict.models import Condition, ConditionResult, FormatIn, TagExcluded, TagRequired, Verdict, YearRange
from acg_agent.verdict.rules import evaluate

MAX_TITLES = 3  # 每个条件最多列几部作品，其余写“等 N 部”
FORMAT_NAMES = {"tv": "TV", "web": "WEB", "ova": "OVA", "movie": "剧场版", "motion_comic": "动态漫画", "other": "其他"}


def condition_label(c: Condition) -> str:
    """把条件写成作品的性质，接在作品名后面读：《冰雪女王》没有「后宫」。"""
    match c:
        case YearRange(min_year=lo, max_year=hi):
            if lo is not None and hi is not None:
                return f"是 {lo}–{hi} 年的" if lo != hi else f"是 {lo} 年的"
            return f"是 {lo} 年以后的" if lo is not None else f"是 {hi} 年及以前的"
        case FormatIn(formats=fs):
            return "是" + "/".join(FORMAT_NAMES.get(f, f) for f in sorted(fs))
        case TagRequired(concept=x):
            return f"有「{x}」"
        case TagExcluded(concept=x):
            return f"没有「{x}」"


def _titles(names: list[str]) -> str:
    shown = "、".join(f"《{n}》" for n in names[:MAX_TITLES])
    return shown + (f"等 {len(names)} 部" if len(names) > MAX_TITLES else "")


def build_caveat(conn: sqlite3.Connection, work_ids: list[int], conditions: list[Condition], relaxed: bool) -> str | None:
    """动画：返回要附在回答后面的说明；推荐的作品条件都有证据支持时返回 None。

    conditions：这句话里搜索用过的所有条件；relaxed：用过 explore 模式，或最后一次搜索去掉了之前的条件。
    """
    if not work_ids or not conditions:
        return None
    rows = repo.display_rows(conn, work_ids)
    results = {r.work_id: r for r in evaluate(conn, work_ids, conditions)}
    # 按推荐顺序列作品名
    return summarize([(rows[w]["title_zh"] or rows[w]["title_original"], results[w].results) for w in work_ids], relaxed)


def summarize(entries: list[tuple[str, list[ConditionResult]]], relaxed: bool) -> str | None:
    """（作品名, 各条件的判断）→ 说明文字。动画和 galgame 共用。"""
    unknown: dict[str, list[str]] = {}
    conflict: dict[str, list[str]] = {}
    for title, results in entries:
        for cr in results:
            bucket = {Verdict.UNKNOWN: unknown, Verdict.CONFLICT: conflict}.get(cr.verdict)
            if bucket is not None:
                bucket.setdefault(condition_label(cr.condition), []).append(title)
    if not unknown and not conflict:
        return None
    parts = []
    if unknown:
        parts.append("没能确认" + "；".join(f"{_titles(ts)}{label}" for label, ts in unknown.items()))
    if conflict:
        parts.append("；".join(f"{_titles(ts)}不满足“{label}”" for label, ts in conflict.items()))
    head = "说明：这次放宽了条件，" if relaxed else "说明："
    return head + "，".join(parts) + "。详情见卡片上的判断依据。"
