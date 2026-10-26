"""按作品名查找作品：中文名、日文原名、罗马字、别名都可以，支持繁体输入。"""

import sqlite3
from enum import IntEnum

from pydantic import BaseModel

from acg_agent.domain.text import search_key
from acg_agent.search import repository as repo
from acg_agent.search.service import BANGUMI_SUBJECT_URL, bayesian_score

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
# trigram 分词器要求查询至少 3 个字符，更短的用 LIKE
_FTS_MIN_CHARS = 3


class MatchType(IntEnum):
    """数字越小越靠前。"""

    EXACT = 0   # 规范化后完全相同
    PREFIX = 1  # 以查询词开头
    CONTAINS = 2


class TitleMatch(BaseModel):
    work_id: int
    bangumi_id: str | None
    bangumi_url: str | None
    title_original: str
    title_zh: str | None
    matched_alias: str  # 命中的是哪个名字，方便用户确认
    match_type: str
    format: str | None
    release_date: str | None
    score: float | None
    rating_count: int


def lookup(conn: sqlite3.Connection, q: str, limit: int = DEFAULT_LIMIT) -> list[TitleMatch]:
    key = search_key(q)  # 和入库时同样处理：转简体、规范化
    best: dict[int, tuple[MatchType, str]] = {}  # work_id → (最好的匹配类型, 命中的名字)
    for work_id, text, alias_key in _candidates(conn, key):
        match = _match_type(alias_key, key)
        if work_id not in best or match < best[work_id][0]:
            best[work_id] = (match, text)
    if not best:
        return []

    rows = repo.display_rows(conn, list(best))
    prior = repo.mean_score(conn)

    def rank(work_id: int):
        row = rows[work_id]
        return (best[work_id][0], -bayesian_score(row["score"], row["rating_count"], prior),
                -row["rating_count"], work_id)

    return [_to_match(rows[wid], *best[wid]) for wid in sorted(best, key=rank)[:limit]]


def _candidates(conn: sqlite3.Connection, key: str) -> list[tuple[int, str, str]]:
    # 和搜索保持一致：不返回已下架和成人内容
    visible = "JOIN work AS w ON w.id = a.work_id WHERE w.removed_at IS NULL AND w.nsfw = 0"
    if len(key) >= _FTS_MIN_CHARS:
        # 用双引号包成短语，避免查询词里的特殊字符被当成 FTS5 语法
        phrase = '"' + key.replace('"', '""') + '"'
        sql = (
            "SELECT a.work_id, a.text, a.search_key FROM alias_fts"
            f" JOIN alias AS a ON a.id = alias_fts.rowid {visible} AND alias_fts MATCH ?"
        )
        return conn.execute(sql, (phrase,)).fetchall()
    # 1–2 个字：LIKE 子串匹配。转义 % 和 _，否则它们会被当成通配符
    escaped = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    sql = f"SELECT a.work_id, a.text, a.search_key FROM alias AS a {visible} AND a.search_key LIKE ? ESCAPE '\\'"
    return conn.execute(sql, (f"%{escaped}%",)).fetchall()


def _match_type(alias_key: str, key: str) -> MatchType:
    if alias_key == key:
        return MatchType.EXACT
    if alias_key.startswith(key):
        return MatchType.PREFIX
    return MatchType.CONTAINS


def _to_match(row: sqlite3.Row, match: MatchType, text: str) -> TitleMatch:
    bangumi_id = row["bangumi_id"]
    return TitleMatch(
        work_id=row["id"],
        bangumi_id=bangumi_id,
        bangumi_url=BANGUMI_SUBJECT_URL.format(bangumi_id) if bangumi_id else None,
        title_original=row["title_original"],
        title_zh=row["title_zh"],
        matched_alias=text,
        match_type=match.name.lower(),
        format=row["format"],
        release_date=row["release_date"],
        score=row["score"],
        rating_count=row["rating_count"],
    )
