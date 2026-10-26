"""动画 ↔ galgame 的对应（G-05）：对应关系存在 galgame 库的 vn_anime 表，用 Bangumi 条目 ID 连到动画库。

两个库是分开的文件，这里每次只读打开另一个库查一下（几毫秒）。
动画这边只在成人开关打开时才显示“原作游戏”（你的决定：很多原作是 18 禁游戏，开关关着时不提）。
"""

import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import BaseModel

VNDB_URL = "https://vndb.org/{}"
# VNDB 的“对应动画”不只记录原作：同人游戏、客串角色的作品也挂在动画下面（动画《CLANNAD》下面挂着 6 部，5 部是
# 同人或联动作品；轻小说改编的《凉宫春日的忧郁》下面挂着十几部同人游戏，TS-046）。从动画这边看时，来自 VNDB 的对应要同时满足：
#   发售年份不晚于这部动画（才可能是原作）、至少 GAME_MIN_VOTES 票、投票数至少达到最热门那部的 RELATED_SHARE
# Bangumi 人工整理的“改编”关系更可靠，不受这些限制
RELATED_SHARE = 0.1
GAME_MIN_VOTES = 50
BANGUMI_URL = "https://bgm.tv/subject/{}"


class AnimeRef(BaseModel):
    """galgame 卡片上的“改编动画”。"""

    work_id: int
    bangumi_url: str
    title: str
    year: int | None
    format: str | None


class GameRef(BaseModel):
    """动画卡片上的“相关游戏”（通常就是原作）。"""

    vn_id: str
    vndb_url: str
    title: str
    year: int | None
    min_age: int | None


def _open_ro(path: Path | None) -> sqlite3.Connection | None:
    if path is None or not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _marks(values) -> str:
    return ", ".join("?" for _ in values)


def anime_for_vns(galgame_conn: sqlite3.Connection, anime_db: Path | None, vn_ids: list[str]) -> dict[str, list[AnimeRef]]:
    if not vn_ids:
        return {}
    # 调用方传进来的连接不一定设了 row_factory，所以按位置取值。
    # 同人游戏也挂在原作动画下面（《CLANN》→ CLANNAD）：来自 VNDB 的对应，游戏要早于或同年于动画、
    # 并且至少 GAME_MIN_VOTES 票，才算“改编动画”（能改编成动画的游戏不会只有几票）
    links = [tuple(r) for r in galgame_conn.execute(
        "SELECT a.vn_id, a.bangumi_id, a.via, v.release_year, v.vote_count FROM vn_anime AS a JOIN vn AS v ON v.id = a.vn_id"
        f" WHERE a.vn_id IN ({_marks(vn_ids)})", vn_ids)]
    conn = _open_ro(anime_db)
    if conn is None or not links:
        return {}
    with closing(conn):
        bids = sorted({link[1] for link in links})
        rows = {r["source_id"]: r for r in conn.execute(
            "SELECT e.source_id, w.id, w.title_zh, w.title_original, w.release_date, w.format FROM external_id AS e"
            " JOIN work AS w ON w.id = e.work_id AND w.removed_at IS NULL AND w.nsfw = 0"
            f" WHERE e.source = 'bangumi' AND e.source_id IN ({_marks(bids)})", bids)}
    out: dict[str, list[AnimeRef]] = {}
    for vn_id, bid, via, vn_year, votes in links:
        r = rows.get(bid)
        anime_year = int(r["release_date"][:4]) if r and r["release_date"] else None
        source_first = (vn_year is not None and anime_year is not None and vn_year <= anime_year
                        and votes >= GAME_MIN_VOTES)
        if r and (via == "bangumi_relation" or source_first):  # 动画库里没有（或已下架、成人内容）的不显示
            out.setdefault(vn_id, []).append(AnimeRef(
                work_id=r["id"], bangumi_url=BANGUMI_URL.format(bid),
                title=r["title_zh"] or r["title_original"],
                year=int(r["release_date"][:4]) if r["release_date"] else None, format=r["format"]))
    for refs in out.values():
        refs.sort(key=lambda a: (a.year or 9999, a.work_id))
    return out


def games_for_works(anime_conn: sqlite3.Connection, galgame_db: Path | None, work_ids: list[int]) -> dict[int, list[GameRef]]:
    if not work_ids:
        return {}
    bgm, anime_year = {}, {}
    for sid, wid, date in anime_conn.execute(
        "SELECT e.source_id, e.work_id, w.release_date FROM external_id AS e JOIN work AS w ON w.id = e.work_id"
        f" WHERE e.source = 'bangumi' AND e.work_id IN ({_marks(work_ids)})", work_ids):
        bgm[sid] = wid
        anime_year[wid] = int(date[:4]) if date else None
    conn = _open_ro(galgame_db)
    if conn is None or not bgm:
        return {}
    with closing(conn):
        rows = conn.execute(
            "SELECT a.bangumi_id, a.via, v.id, v.title_zh, v.title_original, v.release_year, v.min_age, v.vote_count"
            f" FROM vn_anime AS a JOIN vn AS v ON v.id = a.vn_id WHERE a.bangumi_id IN ({_marks(bgm)})"
            " ORDER BY v.vote_count DESC", list(bgm)).fetchall()
    grouped: dict[int, list] = {}
    for r in rows:  # 已按投票数从多到少排好
        grouped.setdefault(bgm[r["bangumi_id"]], []).append(r)
    out: dict[int, list[GameRef]] = {}
    for work_id, cands in grouped.items():
        floor = max(GAME_MIN_VOTES, cands[0]["vote_count"] * RELATED_SHARE)
        year = anime_year.get(work_id)
        seen = set()
        for r in cands:
            curated = r["via"] == "bangumi_relation"
            early_enough = year is not None and r["release_year"] is not None and r["release_year"] <= year
            if r["id"] in seen or not (curated or (early_enough and r["vote_count"] >= floor)):
                continue
            seen.add(r["id"])
            out.setdefault(work_id, []).append(GameRef(
                vn_id=r["id"], vndb_url=VNDB_URL.format(r["id"]), title=r["title_zh"] or r["title_original"],
                year=r["release_year"], min_age=r["min_age"]))
    return out
