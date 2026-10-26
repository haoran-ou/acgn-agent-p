"""Agent 可以调用的工具。全部只读，不能执行任意 SQL，也不能写数据。

四个查动画的工具，加上 G-05 的“查原作游戏”（只在成人开关打开时出现在工具清单里）。

工具返回的内容尽量精简（省 token），而且其中的简介等文字来自数据库，是“数据”，不是给模型的指令。
"""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.tools import ToolDefinition

from acg_agent.galgame.links import games_for_works
from acg_agent.search import repository as repo
from acg_agent.search.series import SeriesIndex
from acg_agent.search.service import SearchRequest, search
from acg_agent.search.titles import lookup
from acg_agent.verdict.models import Condition, TagExcluded, TagRequired

SUMMARY_CHARS = 200
MAX_RESULTS = 8
REQUEST_LIMIT = 6  # 每句话最多几轮模型调用；最后一轮收起所有工具，只能作答
# 每句话里两个最费调用的工具各能用几次。提示词里也写了，但模型不一定遵守（TS-036），所以用代码强制
TOOL_BUDGETS = {"search_works": 2, "get_work_details": 3}
FORMAT_NAMES = {"tv": "TV", "web": "WEB", "ova": "OVA", "movie": "剧场版", "motion_comic": "动态漫画", "other": "其他"}


@dataclass
class AgentDeps:
    conn: sqlite3.Connection
    series: SeriesIndex
    vocabulary: list[str]
    calls: dict[str, int] = field(default_factory=dict)  # 已经做过的调用，用来发现重复
    used: dict[str, int] = field(default_factory=dict)  # 每个工具真正执行了几次，和 TOOL_BUDGETS 比较
    seen_ids: set[int] = field(default_factory=set)  # 本轮工具返回过的作品
    last_conditions: list[Condition] = field(default_factory=list)  # 最后一次搜索用的条件，卡片的依据按它算
    last_mode: Literal["strict", "explore"] = "strict"  # 最后一次搜索的模式，用于自动说明（caveat）
    searched_conditions: list[Condition] = field(default_factory=list)  # 这句话里搜索用过的所有条件（去重），用于自动说明
    trace: list[dict] = field(default_factory=list)  # 调用记录：给网页展示，也写进日志
    galgame_db: Path | None = None  # 成人开关打开时才给：可以查原作游戏（G-05）


def _repeated(ctx: RunContext, tool: str, args: dict) -> dict | None:
    """同样的参数第二次调用时，直接告诉模型已经查过了，防止原地打转。"""
    key = tool + json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    ctx.deps.calls[key] = ctx.deps.calls.get(key, 0) + 1
    if ctx.deps.calls[key] > 1:
        ctx.deps.trace.append({"tool": tool, "args": args, "result": "重复调用，已拒绝"})
        return {"note": "这个查询刚才已经做过了，结果和上次一样。不要重复查询，请换一个思路或直接给出回答。"}
    return None


def _over_budget(ctx: RunContext, tool: str, args: dict, budgets: dict[str, int] = TOOL_BUDGETS) -> dict | None:
    """预算用完后不报错（报错会让整句话退回固定流程），而是让模型根据已有结果作答。

    正常情况下 _hide_when_spent 已经把工具从清单里拿掉了；这里拦的是同一轮里一次调用多个的情况。
    """
    if ctx.deps.used.get(tool, 0) < budgets[tool]:
        return None
    ctx.deps.trace.append({"tool": tool, "args": args, "result": "预算已用完，已拒绝"})
    return {"note": f"{tool} 的次数已经用完了，不要再调用。请根据已经查到的结果给出回答；没有合适的就如实说明，并告诉用户可以放宽哪个条件。"}


def _spend(ctx: RunContext, tool: str) -> None:
    ctx.deps.used[tool] = ctx.deps.used.get(tool, 0) + 1


def make_hide_when_spent(budgets: dict[str, int]):
    """生成 Pydantic AI 工具的 prepare 钩子：每轮调用模型前都会问一次，返回 None 表示这一轮不把这个工具给模型。

    两种情况收起工具：这个工具的预算用完了；已经到了最后一轮（只剩作答的机会）。
    只限制部分工具时，模型会换用没限制的工具，照样碰到轮数上限（TS-036）。
    动画和 galgame 的代理人各有一份预算，共用这套规则。
    """
    async def hide(ctx: RunContext, tool_def: ToolDefinition) -> ToolDefinition | None:
        if ctx.usage.requests >= REQUEST_LIMIT - 1:
            return None
        budget = budgets.get(tool_def.name)
        if budget is not None and ctx.deps.used.get(tool_def.name, 0) >= budget:
            return None
        return tool_def
    return hide


_hide_when_spent = make_hide_when_spent(TOOL_BUDGETS)


def _brief(row) -> dict:
    return {
        "work_id": row["id"],
        "title": row["title_zh"] or row["title_original"],
        "year": row["release_date"][:4] if row["release_date"] else None,
        "format": FORMAT_NAMES.get(row["format"], row["format"]),
        "score": row["score"],
    }


async def find_works_by_title(ctx: RunContext[AgentDeps], title: str) -> list[dict] | dict:
    """按作品名查找作品，返回候选作品（work_id、名字、年份、类型、评分）。

    用途：用户提到具体作品时（例如“像《物语》那样的”），先用它找到这部作品的 work_id。

    Args:
        title: 作品名，中文、日文、罗马字、别名都可以
    """
    if (hit := _repeated(ctx, "find_works_by_title", {"title": title})) is not None:
        return hit
    matches = lookup(ctx.deps.conn, title, limit=5)
    ctx.deps.seen_ids.update(m.work_id for m in matches)
    out = [{"work_id": m.work_id, "title": m.title_zh or m.title_original, "matched": m.matched_alias,
            "year": m.release_date[:4] if m.release_date else None,
            "format": FORMAT_NAMES.get(m.format, m.format), "score": m.score} for m in matches]
    ctx.deps.trace.append({"tool": "find_works_by_title", "args": {"title": title}, "result": f"{len(out)} 个候选"})
    return out


async def search_works(
    ctx: RunContext[AgentDeps],
    conditions: list[Condition],
    mode: Literal["strict", "explore"] = "strict",
) -> dict:
    """按条件搜索动画，返回排好序的结果（同系列已合并），以及每部作品是否满足条件。

    条件类型：year（年份范围）、format（类型：tv/web/ova/movie/motion_comic/other）、
    tag_required（想要的标签）、tag_excluded（要排除的标签）。标签只能用系统给出的可用标签。

    Args:
        conditions: 搜索条件，至少一个
        mode: strict 只返回有证据支持的；explore 也返回证据不足（unknown）的
    """
    args = {"conditions": [c.model_dump(mode="json") for c in conditions], "mode": mode}
    if (hit := _repeated(ctx, "search_works", args) or _over_budget(ctx, "search_works", args)) is not None:
        return hit
    allowed = set(ctx.deps.vocabulary)
    unknown = [c.concept for c in conditions if isinstance(c, (TagRequired, TagExcluded)) and c.concept not in allowed]
    if unknown:
        # 让模型自己改：这次调用不算数，模型会收到这段话再试一次
        raise ModelRetry(f"这些标签不在可用标签里：{'、'.join(unknown)}。请换成可用标签里的词，或者去掉这个条件。")
    if not conditions:
        raise ModelRetry("至少需要一个条件。")

    _spend(ctx, "search_works")
    resp = search(ctx.deps.conn, SearchRequest(conditions=conditions, mode=mode, page_size=MAX_RESULTS),
                  series=ctx.deps.series)
    ctx.deps.last_conditions = list(conditions)
    ctx.deps.last_mode = mode
    ctx.deps.searched_conditions = list(dict.fromkeys([*ctx.deps.searched_conditions, *conditions]))
    ctx.deps.seen_ids.update(i.work_id for i in resp.items)
    out = {
        "total_series": resp.total,
        "results": [{"work_id": i.work_id, "title": i.title_zh or i.title_original,
                     "year": i.release_date[:4] if i.release_date else None,
                     "format": FORMAT_NAMES.get(i.format, i.format), "score": i.score,
                     "verdict": i.overall.value, "weak_evidence": i.weak_evidence}
                    for i in resp.items],
    }
    if resp.relax_hint:
        out["relax_hint"] = [{"drop_condition": o.condition.model_dump(mode="json"),
                              "results_if_dropped": o.strict_total_without}
                             for o in resp.relax_hint.drop_one]
    ctx.deps.trace.append({"tool": "search_works", "args": args, "result": f"{resp.total} 个系列"})
    return out


async def get_work_details(ctx: RunContext[AgentDeps], work_id: int) -> dict:
    """查一部作品的详细信息：标签（含打标人数）、公共标签、简介片段、所属系列、曾上线的平台。

    用途：判断一部作品是否符合用户的剧情、氛围类要求时，读它的标签和简介。

    Args:
        work_id: 作品 ID（来自其他工具的结果）
    """
    args = {"work_id": work_id}
    if (hit := _repeated(ctx, "get_work_details", args) or _over_budget(ctx, "get_work_details", args)) is not None:
        return hit
    rows = repo.display_rows(ctx.deps.conn, [work_id])
    if work_id not in rows or not is_visible(ctx.deps.conn, work_id):
        raise ModelRetry(f"没有 work_id={work_id} 这部作品。只能用工具返回过的 work_id。")
    _spend(ctx, "get_work_details")
    row = rows[work_id]
    tags = ctx.deps.conn.execute(
        "SELECT kind, name, count FROM work_tag WHERE work_id = ? ORDER BY kind DESC, position", (work_id,)
    ).fetchall()
    entry = ctx.deps.series.entry(work_id)
    entry_title = None
    if entry != work_id:
        entry_row = repo.display_rows(ctx.deps.conn, [entry]).get(entry)
        entry_title = entry_row and (entry_row["title_zh"] or entry_row["title_original"])
    platforms = repo.listed_platforms(ctx.deps.conn, [work_id])[work_id]
    ctx.deps.seen_ids.add(work_id)
    out = _brief(row) | {
        "rating_count": row["rating_count"],
        "user_tags": [f"{t['name']}({t['count']})" for t in tags if t["kind"] == "user"],
        "meta_tags": [t["name"] for t in tags if t["kind"] == "meta"],
        "summary": (row["summary"] or "")[:SUMMARY_CHARS],
        "series_size": ctx.deps.series.size(work_id),
        "series_starts_with": entry_title,
        "platforms": [p["name"] for p in platforms],
    }
    ctx.deps.trace.append({"tool": "get_work_details", "args": {"work_id": work_id}, "result": out["title"]})
    return out


async def get_related_works(ctx: RunContext[AgentDeps], work_id: int) -> list[dict] | dict:
    """查一部作品的相关动画：前传、续集、番外篇、改编、相同世界观等。

    Args:
        work_id: 作品 ID（来自其他工具的结果）
    """
    if (hit := _repeated(ctx, "get_related_works", {"work_id": work_id})) is not None:
        return hit
    if not is_visible(ctx.deps.conn, work_id):
        raise ModelRetry(f"没有 work_id={work_id} 这部作品。只能用工具返回过的 work_id。")
    rows = ctx.deps.conn.execute(
        "SELECT w.id, w.title_zh, w.title_original, w.release_date, w.format, w.score, sc.label_zh AS relation"
        " FROM work_relation AS r"
        " JOIN external_id AS e ON e.source = r.related_source AND e.source_id = r.related_source_id"
        " JOIN work AS w ON w.id = e.work_id AND w.removed_at IS NULL AND w.nsfw = 0"
        " LEFT JOIN source_code AS sc ON sc.source = 'bangumi' AND sc.category = 'relation'"
        "  AND sc.scope = r.related_source_type AND sc.code = r.relation_code"
        " WHERE r.work_id = ? ORDER BY w.release_date LIMIT 20",
        (work_id,),
    ).fetchall()
    ctx.deps.seen_ids.update(r["id"] for r in rows)
    out = [_brief(r) | {"relation": r["relation"]} for r in rows]
    ctx.deps.trace.append({"tool": "get_related_works", "args": {"work_id": work_id}, "result": f"{len(out)} 部"})
    return out


async def get_original_games(ctx: RunContext[AgentDeps], work_id: int) -> list[dict] | dict:
    """查一部动画的相关游戏（通常就是原作 galgame）：名字、年份、年龄分级。

    用途：用户问到“这部动画的游戏版 / 原作游戏”时。只能在 reply 里提到，不能把游戏放进 work_ids。

    Args:
        work_id: 动画的作品 ID（来自其他工具的结果）
    """
    if (hit := _repeated(ctx, "get_original_games", {"work_id": work_id})) is not None:
        return hit
    if not is_visible(ctx.deps.conn, work_id):
        raise ModelRetry(f"没有 work_id={work_id} 这部作品。只能用工具返回过的 work_id。")
    games = games_for_works(ctx.deps.conn, ctx.deps.galgame_db, [work_id]).get(work_id, [])
    out = [{"vn_id": g.vn_id, "title": g.title, "year": g.year, "age": g.min_age} for g in games[:5]]
    ctx.deps.trace.append({"tool": "get_original_games", "args": {"work_id": work_id}, "result": f"{len(out)} 部"})
    return out or {"note": "没有记录到原作游戏。"}


async def _hide_games(ctx: RunContext[AgentDeps], tool_def: ToolDefinition) -> ToolDefinition | None:
    """成人开关没打开（没给 galgame 库）时，这个工具不出现在清单里。"""
    if ctx.deps.galgame_db is None:
        return None
    return await _hide_when_spent(ctx, tool_def)


def is_visible(conn: sqlite3.Connection, work_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM work WHERE id = ? AND removed_at IS NULL AND nsfw = 0", (work_id,)
    ).fetchone() is not None


TOOLS = tuple(Tool(f, prepare=_hide_when_spent)
              for f in (find_works_by_title, search_works, get_work_details, get_related_works)) \
    + (Tool(get_original_games, prepare=_hide_games),)
