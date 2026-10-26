"""搜索用到的 SQL：预筛选候选作品、读取展示用的作品信息。"""

import json
import sqlite3
from collections.abc import Sequence

from acg_agent.verdict.concepts import tag_names
from acg_agent.verdict.models import (
    Condition,
    FormatIn,
    Mode,
    TagExcluded,
    TagRequired,
    VerdictConfig,
    YearRange,
)


def candidate_ids(
    conn: sqlite3.Connection, conditions: Sequence[Condition], mode: Mode, config: VerdictConfig
) -> list[int]:
    """用 SQL 先筛掉一定不会被接受的作品，再交给三态判断。

    规则：筛选结果必须是“会被接受的作品”的超集，宁可多留，不能多删。
    所以只删掉“一定是冲突”的，以及严格模式下“一定是未知”的。
    """
    where = ["w.removed_at IS NULL", "w.nsfw = 0"]  # 成人内容默认排除（MVP 不提供开关）
    params: list = []
    strict = mode == "strict"
    for c in conditions:
        match c:
            case YearRange():
                bounds = []
                if c.min_year is not None:
                    bounds.append("w.release_date >= ?")
                    params.append(f"{c.min_year:04d}-01-01")
                if c.max_year is not None:
                    bounds.append("w.release_date <= ?")
                    params.append(f"{c.max_year:04d}-12-31")
                in_range = " AND ".join(bounds)
                # 没有日期 = 未知：严格模式不要，探索模式要保留
                where.append(f"({in_range})" if strict else f"(w.release_date IS NULL OR ({in_range}))")
            case FormatIn():
                marks = ", ".join("?" for _ in c.formats)
                in_set = f"w.format IN ({marks})"
                where.append(in_set if strict else f"(w.format IS NULL OR {in_set})")
                params.extend(sorted(c.formats))
            case TagRequired():
                if strict:  # 严格模式要求至少有这个标签；探索模式里“没出现”是未知，要保留
                    names = tag_names(c.concept)
                    marks = ", ".join("?" for _ in names)
                    where.append(
                        f"EXISTS (SELECT 1 FROM work_tag t WHERE t.work_id = w.id AND t.name IN ({marks}))"
                    )
                    params.extend(names)
            case TagExcluded():
                # 两种模式都删掉“一定冲突”的：公共标签有，或用户标签人数达到冲突门槛
                names = tag_names(c.concept)
                marks = ", ".join("?" for _ in names)
                where.append(
                    "NOT EXISTS (SELECT 1 FROM work_tag t WHERE t.work_id = w.id"
                    f" AND t.name IN ({marks}) AND (t.kind = 'meta' OR t.count >= ?))"
                )
                params.extend([*names, config.exclude_conflict_min_count])
    sql = f"SELECT w.id FROM work AS w WHERE {' AND '.join(where)}"
    return [row[0] for row in conn.execute(sql, params)]


def mean_score(conn: sqlite3.Connection) -> float:
    """全部有评分作品的平均分，贝叶斯平均里的“先验”（prior）。"""
    value = conn.execute("SELECT AVG(score) FROM work WHERE score IS NOT NULL").fetchone()[0]
    return value if value is not None else 0.0


def sort_keys(conn: sqlite3.Connection, work_ids: Sequence[int]) -> dict[int, tuple]:
    """排序要用的字段：(score, rating_count)。"""
    return _by_id(conn, "SELECT id, score, rating_count FROM work WHERE id IN ({})", work_ids,
                  lambda r: (r[1], r[2]))


def display_rows(conn: sqlite3.Connection, work_ids: Sequence[int]) -> dict[int, sqlite3.Row]:
    return _by_id(
        conn,
        "SELECT w.id, e.source_id AS bangumi_id, w.title_original, w.title_zh, w.format,"
        " w.release_date, w.score, w.rating_count, w.summary"
        " FROM work AS w LEFT JOIN external_id AS e ON e.work_id = w.id AND e.source = 'bangumi'"
        " WHERE w.id IN ({})",
        work_ids,
        lambda r: r,
    )


def listed_platforms(conn: sqlite3.Connection, work_ids: Sequence[int]) -> dict[int, list[dict]]:
    """作品曾上线的正版平台。排序：中国大陆的在前 → 来源没声明地区的 → 其他地区，同一组按平台名。"""
    out: dict[int, list[dict]] = {wid: [] for wid in work_ids}
    rows = _by_id_many(
        conn,
        "SELECT wp.work_id, wp.site, p.title, wp.url, wp.regions, wp.begin"
        " FROM work_platform AS wp JOIN platform AS p ON p.source = wp.source AND p.site = wp.site"
        " WHERE wp.work_id IN ({})",
        work_ids,
    )
    for work_id, site, title, url, regions, begin in rows:
        out[work_id].append({"site": site, "name": title, "url": url,
                             "regions": json.loads(regions) if regions else None, "begin": begin})

    def order(p: dict):
        regions = p["regions"]
        group = 0 if regions and "CN" in regions else 1 if regions is None else 2
        return (group, p["name"])

    for platforms in out.values():
        platforms.sort(key=order)
    return out


_BATCH = 900


def _by_id_many(conn, sql_template, work_ids) -> list[tuple]:
    rows = []
    ids = list(work_ids)
    for start in range(0, len(ids), _BATCH):
        batch = ids[start : start + _BATCH]
        rows += conn.execute(sql_template.format(", ".join("?" for _ in batch)), batch).fetchall()
    return rows


def _by_id(conn, sql_template, work_ids, to_value) -> dict:
    out = {}
    ids = list(work_ids)
    for start in range(0, len(ids), _BATCH):
        batch = ids[start : start + _BATCH]
        sql = sql_template.format(", ".join("?" for _ in batch))
        for row in conn.execute(sql, batch):
            out[row[0]] = to_value(row)
    return out
