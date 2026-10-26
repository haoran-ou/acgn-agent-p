"""VNDB 数据包导入（G-01）：uv run acg-import-vndb data/vndb/vndb-db-latest.tar.zst

只读需要的 11 张表，在内存里汇总后整库写入 data/galgame.db（每次重建，原子切换）。
用户列表、逐图投票、角色等大表不读（ulist_vns 一张就有 600 MB）。

作品对应的动画（G-05）：VNDB 的 vn_anime（AniDB 编号）+ bangumi-data 的 AniDB → Bangumi 对照；
再加上 Bangumi 里动画条目和游戏条目之间的“改编”关系。都在导入时算好，存进 vn_anime 表。

VNDB 里有中文名的作品只有约 7%。传入 Bangumi Archive 数据包时，用 Bangumi 游戏条目的中文名补上：
日文原名规范化后完全相同、且只对应一个中文名才补（你的决定，2026-10-25），有歧义的跳过。
"""

import json
import logging
import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from acg_agent.domain.models import Snapshot
from acg_agent.domain.text import normalize_title, search_key
from acg_agent.galgame.copyfmt import stream_tables
from acg_agent.galgame.db import apply_galgame_schema
from acg_agent.importer import repository as repo
from acg_agent.importer.archive import ArchiveReader, sha256_of, verify_sha256
from acg_agent.importer.common import ErrorLog, find_imported, swap_import, utc_now

log = logging.getLogger(__name__)

SNAPSHOT_SOURCE = "vndb_dump"
DEFAULT_MAX_ERROR_RATIO = 0.01
# 涉及未成年角色的性内容：这几个标签和它们的全部子标签（Lolicon / Shotacon 下面还有 Straight、Lesbian、Gay 等细分）。
# 只用于一条底线：这类作品的非安全级封面一律不显示（PLAN“边界与合规”，2026-10-25）。作品本身照常检索
MINOR_SEXUAL_ROOTS = frozenset({"g156", "g184", "g2023", "g1337"})  # Lolicon · Shotacon · Sex Involving Children · Sexual Child Abuse
ZH_LANGS = ("zh-Hans", "zh-Hant", "zh")  # 简体优先
TBA = 99999999  # VNDB 的发售日：尚未公布

_SPOILER_RE = re.compile(r"\[spoiler\].*?\[/spoiler\]", re.S | re.I)
_URL_RE = re.compile(r"\[url=[^\]]*\](.*?)\[/url\]", re.S | re.I)
_TAG_RE = re.compile(r"\[/?[a-z]+(?:=[^\]]*)?\]", re.I)


@dataclass
class VndbStats:
    vn_seen: int = 0
    vn_imported: int = 0
    vn_errors: int = 0
    with_zh_title: int = 0
    zh_from_bangumi: int = 0  # 其中由 Bangumi 补上的
    zh_ambiguous: int = 0  # 日文名对上了多个不同的中文名，跳过
    with_release_date: int = 0
    with_ero: int = 0
    minor_sexual: int = 0
    with_egs: int = 0
    tags: int = 0
    tag_links: int = 0
    tag_votes: int = 0
    relations: int = 0
    anime_links: int = 0  # 作品 ↔ 动画的对应（G-05）
    vns_with_anime: int = 0
    seconds: float = 0.0


@dataclass
class VndbImportResult:
    status: Literal["imported", "skipped"]
    snapshot_id: int
    stats: VndbStats = field(default_factory=VndbStats)
    error_report: Path | None = None


# ---------- 纯函数：一个字段 / 一条记录怎么转换 ----------

def parse_released(value: int) -> tuple[str | None, int | None]:
    """VNDB 的发售日是 YYYYMMDD 整数；月、日不知道时写 99，没公布是 99999999，0 是未知。"""
    if value in (0, TBA):
        return None, None
    year, month, day = value // 10000, value // 100 % 100, value % 100
    if not 1970 <= year <= 2100:
        return None, None
    try:
        return date(year, month, day).isoformat(), year
    except ValueError:
        return None, year  # 只知道年份（或年月）


def clean_description(text: str | None) -> str | None:
    """去掉剧透段落和 BBCode：[url=/c411]Hakuoro[/url] → Hakuoro。"""
    if not text:
        return None
    text = _SPOILER_RE.sub("", text)
    text = _URL_RE.sub(r"\1", text)
    text = _TAG_RE.sub("", text).strip()
    return text or None


def descendants(roots: frozenset[str], parents: dict[str, set[str]]) -> set[str]:
    """roots 和它们的所有子孙标签。parents：标签 → 它的父标签。"""
    children = defaultdict(set)
    for tag, ps in parents.items():
        for p in ps:
            children[p].add(tag)
    found, stack = set(roots), list(roots)
    while stack:
        for child in children[stack.pop()]:
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


@dataclass(slots=True)  # 汇总约 100 万对（作品, 标签），slots 省内存
class TagAgg:
    votes: int = 0
    positive: int = 0
    negative: int = 0
    total: int = 0
    spoiler_sum: int = 0
    spoiler_n: int = 0

    def add(self, vote: int, spoiler: int | None) -> None:
        self.votes += 1
        self.total += vote
        self.positive += vote > 0
        self.negative += vote < 0
        if spoiler is not None:
            self.spoiler_sum += spoiler
            self.spoiler_n += 1

    @property
    def score(self) -> float:
        return round(self.total / self.votes, 2)

    @property
    def spoiler(self) -> float | None:
        return round(self.spoiler_sum / self.spoiler_n, 2) if self.spoiler_n else None


# ---------- 读数据包：每张表一个 handler，结果放在 Dump 里 ----------

@dataclass
class Dump:
    egs_links: dict[str, str] = field(default_factory=dict)  # extlinks.id → 批評空間作品 ID
    covers: dict[str, tuple[float, int]] = field(default_factory=dict)  # cv 图片 → (色情程度平均, 投票数)
    releases: dict[str, dict] = field(default_factory=dict)
    release_egs: dict[str, str] = field(default_factory=dict)
    release_vns: dict[str, list[tuple[str, str]]] = field(default_factory=lambda: defaultdict(list))
    tags: dict[str, dict] = field(default_factory=dict)
    tag_parents: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    vn_tags: dict[tuple[str, str], TagAgg] = field(default_factory=lambda: defaultdict(TagAgg))
    vns: list[dict] = field(default_factory=list)
    relations: list[tuple[str, str, str, int]] = field(default_factory=list)
    titles: dict[str, dict[str, tuple[str, str | None, bool]]] = field(default_factory=lambda: defaultdict(dict))
    vn_anime: list[tuple[str, str]] = field(default_factory=list)  # (vn_id, AniDB 动画编号)
    timestamp: str | None = None


def read_dump(path: Path) -> Dump:
    d = Dump()

    def extlinks(rows):
        for link_id, site, value in rows:
            if site == "egs":
                d.egs_links[link_id] = value

    def images(rows):
        for img, _w, _h, votes, sexual, *_ in rows:
            if img.startswith("cv"):
                d.covers[img] = (int(sexual) / 100, int(votes))

    def releases(rows):
        for r in rows:
            d.releases[r[0]] = {"released": int(r[3]), "minage": None if r[7] is None else int(r[7]),
                                "has_ero": r[17] == "t", "patch": r[18] == "t", "official": r[21] == "t"}

    def releases_extlinks(rows):
        for rid, link_id in rows:
            if link_id in d.egs_links:
                d.release_egs.setdefault(rid, d.egs_links[link_id])

    def releases_vn(rows):
        for rid, vid, rtype in rows:
            d.release_vns[vid].append((rid, rtype))

    def tags(rows):
        for tid, cat, _spoil, searchable, applicable, name, alias, _desc in rows:
            d.tags[tid] = {"id": tid, "name": name, "cat": cat, "searchable": int(searchable == "t"),
                           "applicable": int(applicable == "t"),
                           "aliases": [a for a in (alias or "").split("\n") if a]}

    def tags_parents(rows):
        for tid, parent, _main in rows:
            d.tag_parents[tid].add(parent)

    def tags_vn(rows):
        for _date, tag, vid, _uid, vote, spoiler, ignore, _lie, _notes in rows:
            if ignore == "t":
                continue  # 被管理员忽略的票
            d.vn_tags[(vid, tag)].add(int(vote), None if spoiler is None else int(spoiler))

    def vn(rows):
        for r in rows:
            d.vns.append({"id": r[0], "image": r[1], "olang": r[3], "votecount": int(r[4]),
                          "rating": None if r[5] is None else int(r[5]), "length": None if r[7] is None else int(r[7]),
                          "alias": r[11], "description": r[12]})

    def vn_relations(rows):
        for vid, other, relation, official in rows:
            d.relations.append((vid, other, relation, int(official == "t")))

    def vn_titles(rows):
        for vid, lang, official, title, latin in rows:
            d.titles[vid][lang] = (title, latin, official == "t")

    def vn_anime(rows):
        for vid, aid in rows:
            d.vn_anime.append((vid, aid))

    d.timestamp = stream_tables(path, {
        "extlinks": extlinks, "images": images, "releases": releases, "releases_extlinks": releases_extlinks,
        "releases_vn": releases_vn, "tags": tags, "tags_parents": tags_parents, "tags_vn": tags_vn,
        "vn": vn, "vn_relations": vn_relations, "vn_titles": vn_titles, "vn_anime": vn_anime,
    })
    return d


# ---------- Bangumi 游戏条目的中文名 ----------

BangumiNames = dict[str, dict[str, str]]  # 规范化的日文原名 → {中文名: Bangumi 条目 ID}
BANGUMI_ADAPTATION = 1  # Bangumi 关联类型“改编”（动画条目 → 游戏条目）


@dataclass
class BangumiInfo:
    names: BangumiNames
    anime_game: list[tuple[str, str]]  # (动画条目 ID, 游戏条目 ID)：两者之间有“改编”关系


def load_bangumi(zip_path: Path) -> BangumiInfo:
    """Bangumi Archive：有中文名的游戏条目（type = 4），以及动画（type = 2）和游戏之间的“改编”关系。"""
    names: BangumiNames = defaultdict(dict)
    types: dict[int, int] = {}
    pairs: list[tuple[str, str]] = []
    with ArchiveReader(zip_path) as archive:
        for rec in archive.iter_records("subject.jsonlines"):
            types[int(rec["id"])] = int(rec.get("type") or 0)
            if str(rec.get("type")) != "4" or not rec.get("name_cn") or not rec.get("name"):
                continue
            key = normalize_title(rec["name"])
            if key:
                sid = str(rec["id"])
                old = names[key].get(rec["name_cn"].strip())
                names[key][rec["name_cn"].strip()] = min(old, sid, key=int) if old else sid  # 同名多个条目取最早的
        for rel in archive.iter_records("subject-relations.jsonlines"):
            a, b = int(rel["subject_id"]), int(rel["related_subject_id"])
            if int(rel["relation_type"]) == BANGUMI_ADAPTATION and types.get(a) == 2 and types.get(b) == 4:
                pairs.append((str(a), str(b)))
    return BangumiInfo(names=names, anime_game=pairs)


def load_bangumi_game_names(zip_path: Path) -> BangumiNames:
    return load_bangumi(zip_path).names


def load_anidb_to_bangumi(json_path: Path) -> dict[str, list[str]]:
    """bangumi-data（CC BY 4.0）：AniDB 动画编号 → Bangumi 条目 ID。"""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    mapping: dict[str, list[str]] = defaultdict(list)
    for item in data.get("items", []):
        sites = {s.get("site"): s.get("id") for s in item.get("sites", [])}
        if sites.get("anidb") and sites.get("bangumi"):
            mapping[str(sites["anidb"])].append(str(sites["bangumi"]))
    return mapping


def match_bangumi_zh(work: dict, names: BangumiNames) -> tuple[str, str] | None | Literal["ambiguous"]:
    """只给原语言是日语、VNDB 里没有中文名的作品补；返回 (中文名, Bangumi 条目 ID)。"""
    if work["title_zh"] or work["olang"] != "ja":
        return None
    candidates = names.get(normalize_title(work["title_ja"] or work["title_original"]))
    if not candidates:
        return None
    if len(candidates) > 1:
        return "ambiguous"
    return next(iter(candidates.items()))


# ---------- 汇总成一部作品 ----------

def build_vn(raw: dict, d: Dump) -> tuple[dict, list[tuple[str, str]]]:
    vid = raw["id"]
    titles = d.titles.get(vid, {})
    original = titles.get(raw["olang"]) or next(iter(titles.values()), None)
    if original is None:
        raise ValueError("没有任何标题")
    zh = next((titles[lang][0] for lang in ZH_LANGS if lang in titles), None)

    # 发行版：试玩版和补丁不算；非官方的（如同人汉化）也计入年龄分级和色情内容，但发售日只看官方的
    rels = [(d.releases[rid], rtype) for rid, rtype in d.release_vns.get(vid, []) if rid in d.releases]
    full = [r for r, rtype in rels if rtype != "trial" and not r["patch"]]
    dated = [parse_released(r["released"]) for r in full if r["official"]] or [parse_released(r["released"]) for r in full]
    dates = sorted(x for x, _ in dated if x)
    years = sorted(y for _, y in dated if y)
    ages = [r["minage"] for r in full if r["minage"] is not None]
    egs = Counter(d.release_egs[rid] for rid, _ in d.release_vns.get(vid, []) if rid in d.release_egs)

    cover = raw["image"]
    sexual, votes = d.covers.get(cover, (None, None)) if cover else (None, None)
    work = {
        "id": vid,
        "title_original": original[0],
        "title_zh": zh,
        "title_zh_source": "vndb" if zh else None,
        "bangumi_id": None,
        "title_ja": titles["ja"][0] if "ja" in titles else None,
        "title_latin": original[1],
        "olang": raw["olang"],
        "release_date": dates[0] if dates else None,
        "release_year": years[0] if years else None,
        "min_age": max(ages) if ages else None,
        "has_ero": int(any(r["has_ero"] for r in full)),
        "has_all_ages": int(any(not r["has_ero"] for r in full)),
        "rating": round(raw["rating"] / 100, 2) if raw["rating"] and raw["votecount"] else None,
        "vote_count": raw["votecount"],
        "length_minutes": raw["length"] or None,
        "description": clean_description(raw["description"]),
        "cover_id": cover,
        "cover_sexual": sexual,
        "cover_votes": votes,
        "tag_count": 0,  # 写库时再算
        "minor_sexual": 0,  # 写库时再算
        "egs_id": egs.most_common(1)[0][0] if egs else None,
    }

    names: list[tuple[str, str]] = [(original[0], "original")]
    if zh:
        names.append((zh, "zh"))
    for title, latin, _official in titles.values():
        names.append((title, "title"))
        if latin:
            names.append((latin, "latin"))
    names += [(a, "alias") for a in (raw["alias"] or "").split("\n") if a.strip()]
    return work, names


# ---------- 写库 ----------

VN_COLUMNS = ("id", "title_original", "title_zh", "title_zh_source", "bangumi_id", "title_ja", "title_latin", "olang", "release_date", "release_year",
              "min_age", "has_ero", "has_all_ages", "rating", "vote_count", "length_minutes", "description",
              "cover_id", "cover_sexual", "cover_votes", "tag_count", "minor_sexual", "egs_id", "snapshot_id")


def write_dump(conn: sqlite3.Connection, d: Dump, snapshot_id: int, stats: VndbStats, errors: ErrorLog,
               bangumi_names: BangumiNames | None = None, bangumi: BangumiInfo | None = None,
               anidb_to_bangumi: dict[str, list[str]] | None = None) -> None:
    if bangumi is not None and bangumi_names is None:
        bangumi_names = bangumi.names
    conn.executemany(
        "INSERT INTO tag (id, name, cat, searchable, applicable, aliases) VALUES (?, ?, ?, ?, ?, ?)",
        [(t["id"], t["name"], t["cat"], t["searchable"], t["applicable"], repo._json_or_none(t["aliases"]) or "[]")
         for t in d.tags.values()],
    )
    conn.executemany("INSERT INTO tag_parent VALUES (?, ?)",
                     [(t, p) for t, ps in d.tag_parents.items() for p in ps if t in d.tags and p in d.tags])
    stats.tags = len(d.tags)
    minor_tags = descendants(MINOR_SEXUAL_ROOTS, d.tag_parents)

    # 有正面证据（平均 > 0）的“涉及未成年角色的性内容”标签，不管剧透等级
    minor_vns = {vid for (vid, tag), agg in d.vn_tags.items() if tag in minor_tags and agg.score > 0}
    tag_counts = Counter(vid for (vid, tag), agg in d.vn_tags.items() if agg.score > 0 and tag in d.tags)

    placeholders = ", ".join("?" for _ in VN_COLUMNS)
    alias_rows = []
    imported = set()
    for raw in d.vns:
        stats.vn_seen += 1
        try:
            work, names = build_vn(raw, d)
        except Exception as e:  # noqa: BLE001  坏记录计入错误预算
            stats.vn_errors += 1
            errors.add("vn", raw.get("id"), e)
            continue
        work["minor_sexual"] = int(work["id"] in minor_vns)
        work["tag_count"] = tag_counts[work["id"]]
        if bangumi_names:
            match = match_bangumi_zh(work, bangumi_names)
            if match == "ambiguous":
                stats.zh_ambiguous += 1
            elif match:
                work["title_zh"], work["bangumi_id"] = match
                work["title_zh_source"] = "bangumi"
                names.append((work["title_zh"], "zh"))
                stats.zh_from_bangumi += 1
        work["snapshot_id"] = snapshot_id
        conn.execute(f"INSERT INTO vn ({', '.join(VN_COLUMNS)}) VALUES ({placeholders})",
                     [work[c] for c in VN_COLUMNS])
        imported.add(work["id"])
        seen = set()
        for text, kind in names:
            text = text.strip()
            norm = normalize_title(text) if text else ""
            if not norm or norm in seen:
                continue
            seen.add(norm)
            alias_rows.append((work["id"], text, norm, search_key(text), kind))
        stats.vn_imported += 1
        stats.with_zh_title += work["title_zh"] is not None
        stats.with_release_date += work["release_year"] is not None
        stats.with_ero += work["has_ero"]
        stats.minor_sexual += work["minor_sexual"]
        stats.with_egs += work["egs_id"] is not None

    conn.executemany("INSERT INTO vn_alias (vn_id, text, normalized, search_key, kind) VALUES (?, ?, ?, ?, ?)",
                     alias_rows)
    conn.execute("INSERT INTO vn_alias_fts (vn_alias_fts) VALUES ('rebuild')")

    tag_rows = [(vid, tag, a.votes, a.positive, a.negative, a.score, a.spoiler)
                for (vid, tag), a in d.vn_tags.items() if vid in imported and tag in d.tags]
    conn.executemany("INSERT INTO vn_tag VALUES (?, ?, ?, ?, ?, ?, ?)", tag_rows)
    stats.tag_links = len(tag_rows)
    stats.tag_votes = sum(a.votes for a in d.vn_tags.values())
    rel_rows = [r for r in d.relations if r[0] in imported]
    conn.executemany("INSERT INTO vn_relation VALUES (?, ?, ?, ?)", rel_rows)
    stats.relations = len(rel_rows)

    # 作品 ↔ 动画（G-05）：先用 VNDB + bangumi-data，再用 Bangumi 的“改编”关系补；同一对只留第一条
    links = []
    if anidb_to_bangumi:
        links += [(vid, bid, "vndb_anidb") for vid, aid in d.vn_anime if vid in imported
                  for bid in anidb_to_bangumi.get(aid, [])]
    if bangumi is not None:
        game_to_vn = {bid: vid for vid, bid in conn.execute("SELECT id, bangumi_id FROM vn WHERE bangumi_id IS NOT NULL")}
        links += [(game_to_vn[game], anime, "bangumi_relation") for anime, game in bangumi.anime_game if game in game_to_vn]
    conn.executemany("INSERT OR IGNORE INTO vn_anime VALUES (?, ?, ?)", links)
    stats.anime_links = conn.execute("SELECT COUNT(*) FROM vn_anime").fetchone()[0]
    stats.vns_with_anime = conn.execute("SELECT COUNT(DISTINCT vn_id) FROM vn_anime").fetchone()[0]


def _validate(conn: sqlite3.Connection, stats: VndbStats, max_error_ratio: float) -> None:
    if stats.vn_seen == 0:
        raise ValueError("数据包里没有作品")
    if stats.vn_errors > stats.vn_seen * max_error_ratio:
        raise ValueError(f"坏记录 {stats.vn_errors} 条，超过错误预算（{max_error_ratio:.0%}）")
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise ValueError(f"外键检查失败：{len(problems)} 处")


def run_vndb_import(
    dump_path: Path,
    db_path: Path,
    expected_sha256: str | None,
    errors_dir: Path,
    max_error_ratio: float = DEFAULT_MAX_ERROR_RATIO,
    bangumi_zip: Path | None = None,
    force: bool = False,
    bangumi_data_json: Path | None = None,
) -> VndbImportResult:
    """VNDB 不公布校验值：不传 expected_sha256 就只记录实际的 sha256。

    bangumi_zip：用来补中文名的 Bangumi Archive 数据包；force：同一个数据包也重新导入（比如换了 Bangumi 数据包）。
    """
    started = time.perf_counter()
    if not dump_path.exists():
        raise FileNotFoundError(dump_path)
    sha = verify_sha256(dump_path, expected_sha256) if expected_sha256 else sha256_of(dump_path)
    existing = find_imported(db_path, SNAPSHOT_SOURCE, sha)
    if existing is not None and not force:
        return VndbImportResult(status="skipped", snapshot_id=existing)

    log.info("读取数据包 %s", dump_path.name)
    dump = read_dump(dump_path)
    bangumi = load_bangumi(bangumi_zip) if bangumi_zip is not None else None
    if bangumi:
        log.info("Bangumi：%d 个游戏日文原名有中文名，%d 条动画→游戏改编关系", len(bangumi.names), len(bangumi.anime_game))
    anidb_map = load_anidb_to_bangumi(bangumi_data_json) if bangumi_data_json is not None else None
    log.info("读取完成：%d 部作品，%d 条标签投票汇总", len(dump.vns), len(dump.vn_tags))
    stats = VndbStats()
    errors = ErrorLog()
    report = errors_dir / f"vndb-{utc_now().replace(':', '')}.jsonl"

    def work(conn: sqlite3.Connection) -> int:
        snapshot = Snapshot(source=SNAPSHOT_SOURCE, file_name=dump_path.name, sha256=sha,
                            size_bytes=dump_path.stat().st_size, tag_cap=None, status="importing",
                            imported_at=utc_now())
        snapshot_id = repo.insert_snapshot(conn, snapshot)
        conn.execute("UPDATE snapshot SET dumped_at = ?, bangumi_file = ?, bangumi_data_file = ? WHERE id = ?",
                     (dump.timestamp, bangumi_zip.name if bangumi_zip else None,
                      bangumi_data_json.name if bangumi_data_json else None, snapshot_id))
        write_dump(conn, dump, snapshot_id, stats, errors, bangumi=bangumi, anidb_to_bangumi=anidb_map)
        _validate(conn, stats, max_error_ratio)
        repo.set_snapshot_status(conn, snapshot_id, "ready")
        return snapshot_id

    snapshot_id = swap_import(db_path, errors, report, work, schema=apply_galgame_schema, copy_old=False)
    stats.seconds = round(time.perf_counter() - started, 1)
    error_report = errors.write(report) if errors.entries else None
    log.info("导入完成：%s", asdict(stats))
    return VndbImportResult(status="imported", snapshot_id=snapshot_id, stats=stats, error_report=error_report)
