"""galgame 搜索（G-02）：预筛选 → 判断 → 排序 → 分页 → 放宽提示；按作品名查找。

两条底线在这里强制执行，调用方（接口、以后的 Agent）绕不过去：
- 成人开关：请求里没有 adult_confirmed=True 就拒绝（你的决定：打开开关后才能检索 galgame）
- 封面：默认只给安全级封面的地址；show_all_covers 时，被标“涉及未成年角色的性内容”的作品仍然只给安全级封面
"""

import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from acg_agent.domain.text import search_key
from acg_agent.galgame.concepts import TagIndex
from acg_agent.galgame.links import AnimeRef
from acg_agent.galgame.verdict import VnResult, evaluate
from acg_agent.verdict.models import ACCEPTED, Condition, ConditionResult, Mode, TagRequired, Verdict, YearRange

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50
SAFE_COVER_BELOW = 0.5  # 色情程度投票平均低于这个数算“安全”（VNDB：0 安全 · 1 擦边 · 2 露骨）
VNDB_URL = "https://vndb.org/{}"
EGS_URL = "https://erogamescape.dyndns.org/~ap2/ero/toukei_kaiseki/game.php?game={}"
BANGUMI_URL = "https://bgm.tv/subject/{}"
SUMMARY_CHARS = 400


class AdultNotConfirmed(PermissionError):
    pass


class GalgameNotice(BaseModel):
    source: str = "VNDB"
    license: str = "ODbL 1.0（数据库）· DbCL 1.0（内容）；作品简介另有来源"
    zh_names: str = "部分中文名来自 Bangumi 番组计划（CC BY-SA 3.0）"
    egs: str = "批評空間链接来自 VNDB 的外部链接，评分和评论请到批評空間查看"


class GalgameSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conditions: list[Condition] = Field(min_length=1)
    mode: Mode = "strict"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    adult_confirmed: bool = False  # 用户在设置里确认已满 18 岁并打开了成人内容开关
    show_all_covers: bool = False  # 设置里的“显示非安全级封面”


class GalgameItem(BaseModel):
    vn_id: str
    vndb_url: str
    title_original: str
    title_zh: str | None
    title_zh_source: str | None  # 'vndb' 或 'bangumi'（来自 Bangumi 时要署名）
    bangumi_url: str | None
    egs_url: str | None
    release_date: str | None
    release_year: int | None
    min_age: int | None
    has_ero: bool
    has_all_ages: bool
    rating: float | None
    vote_count: int
    length_minutes: int | None
    summary: str | None
    cover_url: str | None
    cover_hidden: str | None  # None：有封面或没有封面；'not_safe'：非安全级、没打开封面开关；'minor_rule'：底线规则
    overall: Verdict | None
    weak_evidence: bool
    conditions: list[ConditionResult]
    anime: list[AnimeRef] = Field(default_factory=list)  # 改编动画（G-05）


class GalgameDropOption(BaseModel):
    condition_index: int
    condition: Condition
    strict_total_without: int


class GalgameRelaxHint(BaseModel):
    explore_total: int
    drop_one: list[GalgameDropOption]


class GalgameSearchResponse(BaseModel):
    mode: Mode
    total: int
    page: int
    page_size: int
    items: list[GalgameItem]
    verdict_counts: dict[Verdict, int]
    relax_hint: GalgameRelaxHint | None = None
    notice: GalgameNotice = GalgameNotice()


class GalgameTitleMatch(BaseModel):
    vn_id: str
    vndb_url: str
    title_original: str
    title_zh: str | None
    matched_alias: str
    release_year: int | None
    rating: float | None
    vote_count: int


def cover_url(cover_id: str) -> str:
    """VNDB 图片地址：cv20339 → https://t.vndb.org/cv/39/20339.jpg（目录是编号的最后两位）。"""
    num = int(cover_id.removeprefix("cv"))
    return f"https://t.vndb.org/cv/{num % 100:02d}/{num}.jpg"


def cover_policy(row: sqlite3.Row, show_all: bool) -> tuple[str | None, str | None]:
    if not row["cover_id"]:
        return None, None
    safe = row["cover_sexual"] is not None and row["cover_sexual"] < SAFE_COVER_BELOW
    if safe:
        return cover_url(row["cover_id"]), None
    if row["minor_sexual"]:
        return None, "minor_rule"  # 底线：开关也打不开
    if show_all:
        return cover_url(row["cover_id"]), None
    return None, "not_safe"


def require_adult(confirmed: bool) -> None:
    if not confirmed:
        raise AdultNotConfirmed("galgame 检索需要先在设置里确认已满 18 岁，并打开成人内容开关")


# ---------- 搜索 ----------

def candidate_ids(conn: sqlite3.Connection, conditions: list[Condition], mode: Mode, index: TagIndex) -> list[str]:
    """预筛选：只用一定会被淘汰的条件缩小范围，绝不能漏掉会被接受的作品。

    严格模式下“想要某标签”要有平均分 > 0 的票、年份要在范围内；探索模式也接受“未知”，所以只按年份排除确定不符的。
    “排除某标签”不预筛选（冲突的作品判断后再去掉）。
    """
    where, params = [], []
    for c in conditions:
        if isinstance(c, YearRange):
            parts = []
            if c.min_year is not None:
                parts.append("release_year >= ?")
                params.append(c.min_year)
            if c.max_year is not None:
                parts.append("release_year <= ?")
                params.append(c.max_year)
            clause = " AND ".join(parts)
            where.append(f"({clause})" if mode == "strict" else f"(release_year IS NULL OR ({clause}))")
        elif isinstance(c, TagRequired) and mode == "strict":
            tags = sorted(index.resolve(c.concept))
            marks = ", ".join("?" for _ in tags)
            where.append(f"id IN (SELECT vn_id FROM vn_tag WHERE score > 0 AND tag_id IN ({marks}))")
            params += tags
    sql = "SELECT id FROM vn" + (f" WHERE {' AND '.join(where)}" if where else "")
    return [r[0] for r in conn.execute(sql, params)]


def _evaluate(conn, conditions, mode, index) -> list[VnResult]:
    ids = candidate_ids(conn, conditions, mode, index)
    return evaluate(conn, ids, conditions, index) if ids else []


def _rows(conn: sqlite3.Connection, ids: list[str]) -> dict[str, sqlite3.Row]:
    out = {}
    for start in range(0, len(ids), 900):
        batch = ids[start:start + 900]
        marks = ", ".join("?" for _ in batch)
        for r in conn.execute(f"SELECT * FROM vn WHERE id IN ({marks})", batch):
            out[r["id"]] = r
    return out


MAJOR_SCORE = 2.0  # VNDB 标签平均分 ≥ 2：这是作品的主要要素（而不只是“有一点”）


def relevance_tier(r: VnResult) -> int:
    """“想要某标签”有多贴题：0 全部是主要要素 · 1 至少是明确要素 · 2 其他。没有“想要”条件时都是 0。

    只按评分排的话，“百合”第一名是《樱之诗》（有百合要素，但不是百合作品），所以先按贴题程度分档。
    """
    scores = [res.evidence[0].detail.get("score") for res in r.results if isinstance(res.condition, TagRequired)]
    scores = [s for s in scores if isinstance(s, (int, float))]
    if not scores:
        return 0
    low = min(scores)
    return 0 if low >= MAJOR_SCORE else 1 if low >= 1 else 2


def _rank(results: list[VnResult], rows: dict[str, sqlite3.Row]) -> list[VnResult]:
    # 证据弱的排最后；再按贴题程度分档；同一档里按 VNDB 的贝叶斯评分（已经考虑了投票人数），没有评分的排后面
    def key(r: VnResult):
        row = rows[r.vn_id]
        return (r.weak_evidence, relevance_tier(r), row["rating"] is None, -(row["rating"] or 0),
                -row["vote_count"], r.vn_id)
    return sorted(results, key=key)


def _item(row: sqlite3.Row, r: VnResult | None, show_all: bool) -> GalgameItem:
    url, hidden = cover_policy(row, show_all)
    summary = row["description"]
    if summary and len(summary) > SUMMARY_CHARS:
        summary = summary[:SUMMARY_CHARS] + "…"
    return GalgameItem(
        vn_id=row["id"], vndb_url=VNDB_URL.format(row["id"]), title_original=row["title_original"],
        title_zh=row["title_zh"], title_zh_source=row["title_zh_source"],
        bangumi_url=BANGUMI_URL.format(row["bangumi_id"]) if row["bangumi_id"] else None,
        egs_url=EGS_URL.format(row["egs_id"]) if row["egs_id"] else None,
        release_date=row["release_date"], release_year=row["release_year"], min_age=row["min_age"],
        has_ero=bool(row["has_ero"]), has_all_ages=bool(row["has_all_ages"]), rating=row["rating"],
        vote_count=row["vote_count"], length_minutes=row["length_minutes"], summary=summary,
        cover_url=url, cover_hidden=hidden,
        overall=r.overall if r else None, weak_evidence=r.weak_evidence if r else False,
        conditions=r.results if r else [],
    )


def search(conn: sqlite3.Connection, req: GalgameSearchRequest, index: TagIndex) -> GalgameSearchResponse:
    require_adult(req.adult_confirmed)
    results = _evaluate(conn, req.conditions, req.mode, index)
    accepted = [r for r in results if r.overall in ACCEPTED[req.mode]]
    rows = _rows(conn, [r.vn_id for r in accepted])
    ranked = _rank(accepted, rows)
    start = (req.page - 1) * req.page_size
    page = ranked[start:start + req.page_size]
    hint = _relax_hint(conn, req, index) if req.mode == "strict" and not accepted else None
    return GalgameSearchResponse(
        mode=req.mode, total=len(ranked), page=req.page, page_size=req.page_size,
        items=[_item(rows[r.vn_id], r, req.show_all_covers) for r in page],
        verdict_counts={v: sum(r.overall == v for r in results) for v in Verdict},
        relax_hint=hint,
    )


def _relax_hint(conn, req: GalgameSearchRequest, index: TagIndex) -> GalgameRelaxHint:
    explore = _evaluate(conn, req.conditions, "explore", index)
    options = []
    if len(req.conditions) > 1:
        for i, cond in enumerate(req.conditions):
            rest = req.conditions[:i] + req.conditions[i + 1:]
            total = sum(r.overall in ACCEPTED["strict"] for r in _evaluate(conn, rest, "strict", index))
            if total:
                options.append(GalgameDropOption(condition_index=i, condition=cond, strict_total_without=total))
    options.sort(key=lambda o: -o.strict_total_without)
    return GalgameRelaxHint(explore_total=sum(r.overall in ACCEPTED["explore"] for r in explore), drop_one=options)


def items_for_vns(conn: sqlite3.Connection, vn_ids: list[str], show_all_covers: bool,
                  conditions: list[Condition] | None = None, index: TagIndex | None = None) -> list[GalgameItem]:
    """代理人推荐的作品的卡片。给了条件就附上判断和依据（按代理人最后一次搜索的条件），封面规则照旧。"""
    rows = _rows(conn, vn_ids)
    results = {r.vn_id: r for r in evaluate(conn, vn_ids, conditions, index)} if conditions and index else {}
    return [_item(rows[v], results.get(v), show_all_covers) for v in vn_ids if v in rows]


# ---------- 按作品名查找 ----------

_FTS_MIN_CHARS = 3


def lookup(conn: sqlite3.Connection, q: str, adult_confirmed: bool, limit: int = 10) -> list[GalgameTitleMatch]:
    require_adult(adult_confirmed)
    key = search_key(q)
    if not key:
        return []
    if len(key) >= _FTS_MIN_CHARS:
        rows = conn.execute(
            "SELECT a.vn_id, a.text, a.search_key FROM vn_alias_fts JOIN vn_alias AS a ON a.id = vn_alias_fts.rowid"
            " WHERE vn_alias_fts MATCH ?", ('"' + key.replace('"', '""') + '"',)).fetchall()  # 整体当作一个短语
    else:
        rows = conn.execute("SELECT vn_id, text, search_key FROM vn_alias WHERE search_key LIKE ?",
                            (f"%{key}%",)).fetchall()
    best: dict[str, tuple[int, str]] = {}
    for vid, text, alias_key in rows:
        rank = 0 if alias_key == key else 1 if alias_key.startswith(key) else 2
        if vid not in best or rank < best[vid][0]:
            best[vid] = (rank, text)
    vns = _rows(conn, list(best))
    order = sorted(best, key=lambda v: (best[v][0], -vns[v]["vote_count"], v))[:limit]
    return [GalgameTitleMatch(vn_id=v, vndb_url=VNDB_URL.format(v), title_original=vns[v]["title_original"],
                              title_zh=vns[v]["title_zh"], matched_alias=best[v][1],
                              release_year=vns[v]["release_year"], rating=vns[v]["rating"],
                              vote_count=vns[v]["vote_count"]) for v in order]
