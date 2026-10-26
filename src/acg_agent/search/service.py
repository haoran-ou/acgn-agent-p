"""搜索服务：预筛选 → 三态判断 → 排序 → 按系列折叠 → 分页；严格模式结果为空时给出放宽提示。"""

import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from acg_agent.galgame.links import GameRef
from acg_agent.search import repository as repo
from acg_agent.search.series import SeriesIndex, build_series_index
from acg_agent.verdict.models import (
    ACCEPTED,
    DEFAULT_CONFIG,
    SEVERITY,
    Condition,
    ConditionResult,
    Mode,
    Verdict,
    VerdictConfig,
    WorkResult,
)
from acg_agent.verdict.rules import evaluate

DEFAULT_PAGE_SIZE = 10
# 贝叶斯平均（Bayesian average）的先验票数：评分人数远少于它时，分数会被拉向全站平均分。
# 取 50，略高于评分人数的中位数 24（2026-10-20 快照）
PRIOR_VOTES = 50
MAX_PAGE_SIZE = 50
BANGUMI_SUBJECT_URL = "https://bgm.tv/subject/{}"
# 封面：Bangumi 官方接口，会 302 跳转到图片。浏览器显示卡片时按需加载，服务器不下载、不保存图片；
# 没有封面的作品会跳转到 Bangumi 的“暂无图片”占位图。条目信息（含封面）遵循 CC BY-SA 3.0
BANGUMI_COVER_URL = "https://api.bgm.tv/v0/subjects/{}/image?type=medium"


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conditions: list[Condition] = Field(min_length=1)
    mode: Mode = "strict"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    group_series: bool = True  # 同一系列只显示一张卡片


class ListedPlatform(BaseModel):
    """作品曾在这个正版平台上线。不代表现在还能看，界面要写“曾在该平台上线”。"""

    site: str
    name: str
    url: str
    regions: list[str] | None  # 例如 ["CN"]；None 表示来源没有声明，不等于全球可看
    begin: str | None


class PlatformNotice(BaseModel):
    source: str = "bangumi-data"
    license: str = "CC BY 4.0"
    note: str = "曾在该平台上线的记录，未核实当前是否可看"


class SeriesMember(BaseModel):
    work_id: int
    title: str
    release_date: str | None
    format: str | None
    overall: Verdict


class SeriesInfo(BaseModel):
    """这张卡片所属的系列。卡片上显示的是系列里符合条件、而且最早播出的一部。"""

    size: int  # 系列一共几部（不管是否符合条件）
    others: list[SeriesMember]  # 同系列里其他也符合条件的作品，按播出顺序，最多 10 部
    others_total: int
    entry_title: str | None  # 卡片上的作品不是系列第一部时，第一部的名字（“这是续作，系列从《X》开始”）


class SearchItem(BaseModel):
    work_id: int
    bangumi_id: str | None
    bangumi_url: str | None
    cover_url: str | None
    title_original: str
    title_zh: str | None
    format: str | None
    release_date: str | None
    score: float | None
    rating_count: int
    summary: str | None
    overall: Verdict | None  # 代理人推荐、但没有搜索条件可对照时为 None
    weak_evidence: bool
    conditions: list[ConditionResult]
    listed_platforms: list[ListedPlatform]
    series: SeriesInfo | None = None  # 不按系列折叠时为 None
    original_games: list[GameRef] = Field(default_factory=list)  # 原作游戏（G-05）；只在成人开关打开时填


class RelaxOption(BaseModel):
    condition_index: int
    condition: Condition
    strict_total_without: int  # 去掉这一个条件后，严格模式能得到多少部


class RelaxHint(BaseModel):
    """严格模式一部都没有时给出的提示。系统不会自己放宽，由用户决定。

    explore_total：切换到探索模式能看到多少部（多数是“未知”）
    drop_one：分别去掉每个条件后的严格结果数，按结果多少排序，只列出能得到结果的
    """

    explore_total: int
    drop_one: list[RelaxOption]


class SearchResponse(BaseModel):
    mode: Mode
    total: int  # 结果数：按系列折叠时是系列数，否则是作品数
    works_total: int  # 符合条件的作品数
    page: int
    page_size: int
    items: list[SearchItem]
    verdict_counts: dict[Verdict, int]  # 预筛选后的候选里，各判断结果的数量
    relax_hint: RelaxHint | None = None
    platform_notice: PlatformNotice = PlatformNotice()  # listed_platforms 的来源与说明，展示时必须带上


_MAX_SERIES_OTHERS = 10


def search(
    conn: sqlite3.Connection,
    req: SearchRequest,
    config: VerdictConfig = DEFAULT_CONFIG,
    series: SeriesIndex | None = None,  # 不传就现算（约 0.4 秒）；接口会在启动后缓存一份
) -> SearchResponse:
    results = _evaluate(conn, req, req.mode, config)
    accepted = [r for r in results if r.overall in ACCEPTED[req.mode]]
    ranked = _rank(conn, accepted)

    if req.group_series:
        index = series or build_series_index(conn)
        groups = _group_by_series(ranked, index)
    else:
        index, groups = None, [(r, []) for r in ranked]

    start = (req.page - 1) * req.page_size
    page = groups[start : start + req.page_size]
    shown = [rep.work_id for rep, _ in page]
    extra = [m.work_id for _, others in page for m in others[:_MAX_SERIES_OTHERS]]
    entries = [index.entry(w) for w in shown] if index else []
    rows = repo.display_rows(conn, list(dict.fromkeys(shown + extra + entries)))
    platforms = repo.listed_platforms(conn, shown)

    hint = None
    if req.mode == "strict" and not accepted:
        hint = _relax_hint(conn, req, config)

    items = []
    for rep, others in page:
        item = _item(rows[rep.work_id], rep, platforms[rep.work_id])
        if index:
            item.series = _series_info(rep, others, index, rows)
        items.append(item)
    return SearchResponse(
        mode=req.mode,
        total=len(groups),
        works_total=len(ranked),
        page=req.page,
        page_size=req.page_size,
        items=items,
        verdict_counts={v: sum(r.overall == v for r in results) for v in Verdict},
        relax_hint=hint,
    )


def _group_by_series(ranked: list[WorkResult], index: SeriesIndex) -> list[tuple[WorkResult, list[WorkResult]]]:
    """按系列折叠。系列的位置 = 系列里排名最靠前的那部；卡片显示 = 系列里符合条件、最早播出的那部。"""
    groups: dict[int, list[WorkResult]] = {}
    for r in ranked:  # dict 保持插入顺序，所以系列的先后就是“系列里最靠前那部”的先后
        groups.setdefault(index.series(r.work_id), []).append(r)
    out = []
    for members in groups.values():
        representative = min(members, key=lambda r: index.entry_order(r.work_id))  # 正篇优先，再按播出时间
        others = sorted((r for r in members if r is not representative), key=lambda r: index.order(r.work_id))
        out.append((representative, others))
    return out


def _series_info(rep: WorkResult, others: list[WorkResult], index: SeriesIndex, rows) -> SeriesInfo:
    entry = index.entry(rep.work_id)
    entry_title = None
    if entry != rep.work_id and entry in rows:
        entry_title = rows[entry]["title_zh"] or rows[entry]["title_original"]
    return SeriesInfo(
        size=index.size(rep.work_id),
        others=[SeriesMember(work_id=o.work_id, title=rows[o.work_id]["title_zh"] or rows[o.work_id]["title_original"],
                             release_date=rows[o.work_id]["release_date"], format=rows[o.work_id]["format"],
                             overall=o.overall)
                for o in others[:_MAX_SERIES_OTHERS]],
        others_total=len(others),
        entry_title=entry_title,
    )


def items_for_works(
    conn: sqlite3.Connection,
    work_ids: list[int],
    conditions: list[Condition],
    series: SeriesIndex,
    config: VerdictConfig = DEFAULT_CONFIG,
) -> list[SearchItem]:
    """给指定的作品生成卡片（代理人推荐用）。有搜索条件时附上三态判断和依据；没有就只给作品信息。"""
    if not work_ids:
        return []
    verdicts = {r.work_id: r for r in evaluate(conn, work_ids, conditions, config)} if conditions else {}
    rows = repo.display_rows(conn, work_ids + [series.entry(w) for w in work_ids])
    platforms = repo.listed_platforms(conn, work_ids)
    items = []
    for wid in work_ids:
        if wid not in rows:
            continue
        r = verdicts.get(wid)
        stub = r or WorkResult(work_id=wid, overall=Verdict.SATISFIED, results=[], weak_evidence=False)
        item = _item(rows[wid], stub, platforms[wid])
        if r is None:
            item.overall = None
        item.series = _series_info(stub, [], series, rows)
        items.append(item)
    return items


def _evaluate(conn, req: SearchRequest, mode: Mode, config: VerdictConfig) -> list[WorkResult]:
    ids = repo.candidate_ids(conn, req.conditions, mode, config)
    return evaluate(conn, ids, req.conditions, config) if ids else []


def _rank(conn, results: list[WorkResult]) -> list[WorkResult]:
    """排序：证据薄弱的排最后 → 判断结果（满足、很可能满足、未知）→ 贝叶斯平均分高的在前 → 没人评分的在后。"""
    keys = repo.sort_keys(conn, [r.work_id for r in results])
    prior = repo.mean_score(conn)

    def key(r: WorkResult):
        score, votes = keys[r.work_id]
        return (r.weak_evidence, SEVERITY[r.overall], score is None,
                -bayesian_score(score, votes, prior), -votes, r.work_id)

    return sorted(results, key=key)


def bayesian_score(score: float | None, votes: int, prior: float, prior_votes: int = PRIOR_VOTES) -> float:
    """(votes × score + prior_votes × prior) / (votes + prior_votes)。只有 1 个人打 10 分的作品不会排到最前。"""
    if score is None or votes == 0:
        return prior
    return (votes * score + prior_votes * prior) / (votes + prior_votes)


def _relax_hint(conn, req: SearchRequest, config: VerdictConfig) -> RelaxHint:
    explore = _evaluate(conn, req, "explore", config)
    options = []
    if len(req.conditions) > 1:  # 只有一个条件时，“去掉它”就等于不加任何条件，没有意义
        for i, cond in enumerate(req.conditions):
            rest = req.conditions[:i] + req.conditions[i + 1 :]
            reduced = req.model_copy(update={"conditions": rest})
            total = sum(r.overall in ACCEPTED["strict"] for r in _evaluate(conn, reduced, "strict", config))
            if total:
                options.append(RelaxOption(condition_index=i, condition=cond, strict_total_without=total))
    options.sort(key=lambda o: -o.strict_total_without)
    return RelaxHint(
        explore_total=sum(r.overall in ACCEPTED["explore"] for r in explore), drop_one=options
    )


def _item(row: sqlite3.Row, r: WorkResult, platforms: list[dict]) -> SearchItem:
    bangumi_id = row["bangumi_id"]
    return SearchItem(
        work_id=r.work_id,
        bangumi_id=bangumi_id,
        bangumi_url=BANGUMI_SUBJECT_URL.format(bangumi_id) if bangumi_id else None,
        cover_url=BANGUMI_COVER_URL.format(bangumi_id) if bangumi_id else None,
        title_original=row["title_original"],
        title_zh=row["title_zh"],
        format=row["format"],
        release_date=row["release_date"],
        score=row["score"],
        rating_count=row["rating_count"],
        summary=row["summary"],
        overall=r.overall,
        weak_evidence=r.weak_evidence,
        conditions=r.results,
        listed_platforms=[ListedPlatform(**p) for p in platforms],
    )
