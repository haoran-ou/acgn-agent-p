"""bangumi-data 导入：作品曾在哪些正版平台上线。

只用 siteMeta 里 type = 'onair' 的站点（放送平台）。type = 'resource' 的是 BT 资源索引站，
按项目的合规规则一律不导入；type = 'info' 的是资讯站，这里也用不到。
数据许可证 CC BY 4.0，展示时必须注明来源 bangumi-data。
"""

import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from acg_agent.domain.models import Snapshot
from acg_agent.importer import repository as repo
from acg_agent.importer.archive import verify_sha256
from acg_agent.importer.common import ErrorLog, ImportFailed, find_imported, swap_import, utc_now

log = logging.getLogger(__name__)

SNAPSHOT_SOURCE = "bangumi_data"
PLATFORM_SOURCE = "bangumi_data"
ONAIR = "onair"
DEFAULT_MAX_ERROR_RATIO = 0.01


@dataclass(frozen=True)
class ParsedPlatform:
    site: str
    title: str
    url_template: str
    default_regions: tuple[str, ...] | None


@dataclass(frozen=True)
class ParsedLink:
    bangumi_id: str
    site: str
    url: str
    regions: tuple[str, ...] | None
    begin: str | None


@dataclass
class PlatformStats:
    items_seen: int = 0
    items_without_bangumi_id: int = 0  # 正常情况：bangumi-data 里少数条目没有 Bangumi ID
    items_unmatched: int = 0  # 正常情况：Bangumi ID 不在本地库里
    platforms: int = 0
    links_seen: int = 0
    links_imported: int = 0
    links_duplicate: int = 0  # 同一部作品、同一平台出现多次，只留第一条
    link_errors: int = 0
    seconds: float = 0.0


@dataclass
class PlatformImportResult:
    status: Literal["imported", "skipped"]
    snapshot_id: int
    stats: PlatformStats = field(default_factory=PlatformStats)
    error_report: Path | None = None


def _check_url(url: str) -> str:
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"链接不是 http(s)：{url}")
    return url


def parse_platforms(site_meta: dict) -> dict[str, ParsedPlatform]:
    platforms = {}
    for site, meta in site_meta.items():
        if meta.get("type") != ONAIR:
            continue  # resource（BT 资源站）和 info（资讯站）都不要
        template = _check_url(meta["urlTemplate"])
        regions = tuple(meta["regions"]) if meta.get("regions") else None
        platforms[site] = ParsedPlatform(site, meta["title"].strip(), template, regions)
    return platforms


def parse_links(item: dict, platforms: dict[str, ParsedPlatform]) -> tuple[str | None, list[ParsedLink]]:
    """返回 (Bangumi ID, 这部番组的正版平台链接)。不在 platforms 里的站点直接忽略。"""
    bangumi_id = next((s["id"] for s in item["sites"] if s["site"] == "bangumi"), None)
    links = []
    for entry in item["sites"]:
        platform = platforms.get(entry["site"])
        if platform is None:
            continue
        # 有 url 字段时优先用它（模板拼不出来的特殊链接），否则把 id 填进模板
        url = entry.get("url") or platform.url_template.replace("{{id}}", entry["id"])
        # 作品自己的地区设置优先；空列表视为没有设置
        regions = tuple(entry["regions"]) if entry.get("regions") else platform.default_regions
        links.append(ParsedLink(str(bangumi_id), entry["site"], _check_url(url), regions,
                                entry.get("begin") or None))
    return (str(bangumi_id) if bangumi_id else None), links


def run_platform_import(
    json_path: Path,
    db_path: Path,
    expected_sha256: str,
    errors_dir: Path,
    max_error_ratio: float = DEFAULT_MAX_ERROR_RATIO,
) -> PlatformImportResult:
    started = time.monotonic()
    sha256 = verify_sha256(json_path, expected_sha256)
    if not db_path.exists():
        raise ImportFailed(f"数据库不存在：{db_path}。平台数据要对应到作品，请先运行 acg-import")
    done = find_imported(db_path, SNAPSHOT_SOURCE, sha256)
    if done:
        log.info("这个文件已经导入过（snapshot %s），跳过", done)
        return PlatformImportResult("skipped", done)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    errors = ErrorLog()
    error_report = errors_dir / f"{json_path.stem}.jsonl"

    def work(conn: sqlite3.Connection) -> tuple[PlatformStats, int]:
        stats = PlatformStats()
        snapshot_id = repo.insert_snapshot(conn, Snapshot(
            source=SNAPSHOT_SOURCE, file_name=json_path.name, sha256=sha256,
            size_bytes=json_path.stat().st_size, status="importing", imported_at=utc_now(),
        ))
        platforms = parse_platforms(data["siteMeta"])
        stats.platforms = len(platforms)
        work_ids = repo.work_ids_by_source_id(conn, "bangumi")

        rows: dict[tuple[int, str], ParsedLink] = {}
        for item in data["items"]:
            stats.items_seen += 1
            try:
                bangumi_id, links = parse_links(item, platforms)
            except (KeyError, ValueError) as e:
                stats.link_errors += 1
                errors.add("platform_link", item.get("title"), e)
                continue
            stats.links_seen += len(links)
            if bangumi_id is None:
                stats.items_without_bangumi_id += 1
                continue
            work_id = work_ids.get(bangumi_id)
            if work_id is None:
                stats.items_unmatched += 1
                errors.add("unmatched", bangumi_id, f"Bangumi ID 不在本地库里：{item.get('title')}")
                continue
            for link in links:
                if (work_id, link.site) in rows:
                    stats.links_duplicate += 1
                else:
                    rows[(work_id, link.site)] = link

        repo.replace_platforms(conn, PLATFORM_SOURCE, platforms.values(), rows, snapshot_id)
        stats.links_imported = len(rows)
        _validate(conn, stats, max_error_ratio)
        repo.set_snapshot_status(conn, snapshot_id, "ready")
        return stats, snapshot_id

    stats, snapshot_id = swap_import(db_path, errors, error_report, work)
    stats.seconds = round(time.monotonic() - started, 1)
    report = errors.write(error_report) if errors.entries else None
    log.info("平台导入完成：%s", asdict(stats))
    return PlatformImportResult("imported", snapshot_id, stats, report)


def _validate(conn: sqlite3.Connection, stats: PlatformStats, max_error_ratio: float) -> None:
    problems = repo.check_integrity(conn)
    if stats.platforms == 0:
        problems.append("没有任何 onair 平台，文件格式可能变了")
    if stats.links_imported == 0:
        problems.append("一条平台链接都没有导入")
    total = stats.links_seen + stats.link_errors
    if total and stats.link_errors / total > max_error_ratio:
        problems.append(f"坏记录占 {stats.link_errors / total:.1%}，超过上限 {max_error_ratio:.0%}")
    if problems:
        raise ImportFailed("校验没有通过：" + "；".join(problems))
