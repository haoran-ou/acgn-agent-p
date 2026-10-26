"""Bangumi Archive 导入流程：校验 → 复制旧库 → 在副本上更新 → 校验 → 原子切换（atomic swap）。

任何一步失败，临时库都会被删除，正式库保持原样。复制和切换的机制在 common.py，平台导入也用它。
"""

import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from acg_agent.domain.models import Snapshot
from acg_agent.importer import repository as repo
from acg_agent.importer.archive import ArchiveReader, verify_sha256
from acg_agent.importer.codes import load_codes
from acg_agent.importer.common import ErrorLog, ImportFailed, find_imported, swap_import, utc_now
from acg_agent.importer.transform import (
    ANIME_TYPE,
    SOURCE,
    parse_person,
    parse_relation,
    parse_subject,
)

log = logging.getLogger(__name__)

SNAPSHOT_SOURCE = "bangumi_archive"
# 每部作品最多导出的用户标签数（V-research 01 实测）。导入时会检查，数据超过这个数就报错，提醒人来更新
TAG_CAP = 11
DEFAULT_MAX_ERROR_RATIO = 0.01


@dataclass
class ImportStats:
    anime_seen: int = 0
    works_imported: int = 0
    work_errors: int = 0
    relations_seen: int = 0
    relations_imported: int = 0
    relation_errors: int = 0
    persons_imported: int = 0
    person_errors: int = 0
    links_seen: int = 0
    links_imported: int = 0
    link_errors: int = 0
    works_removed: int = 0
    max_user_tags: int = 0
    seconds: float = 0.0


@dataclass
class ImportResult:
    status: Literal["imported", "skipped"]
    snapshot_id: int
    stats: ImportStats = field(default_factory=ImportStats)
    error_report: Path | None = None


def run_import(
    zip_path: Path,
    db_path: Path,
    expected_sha256: str,
    codes_dir: Path,
    errors_dir: Path,
    max_error_ratio: float = DEFAULT_MAX_ERROR_RATIO,
) -> ImportResult:
    started = time.monotonic()
    log.info("校验 sha256：%s", zip_path.name)
    sha256 = verify_sha256(zip_path, expected_sha256)

    done = find_imported(db_path, SNAPSHOT_SOURCE, sha256)
    if done:
        log.info("这个数据包已经导入过（snapshot %s），跳过", done)
        return ImportResult(status="skipped", snapshot_id=done)

    errors = ErrorLog()
    error_report = errors_dir / f"{zip_path.stem}.jsonl"

    def work(conn: sqlite3.Connection) -> tuple[ImportStats, int]:
        stats, snapshot_id = _import_into(conn, zip_path, sha256, codes_dir, errors)
        _validate(conn, stats, max_error_ratio)
        repo.set_snapshot_status(conn, snapshot_id, "ready")
        return stats, snapshot_id

    stats, snapshot_id = swap_import(db_path, errors, error_report, work)

    stats.seconds = round(time.monotonic() - started, 1)
    report = errors.write(error_report) if errors.entries else None
    log.info("导入完成：%s", asdict(stats))
    return ImportResult("imported", snapshot_id, stats, report)


def _import_into(
    conn: sqlite3.Connection, zip_path: Path, sha256: str, codes_dir: Path, errors: ErrorLog
) -> tuple[ImportStats, int]:
    stats = ImportStats()
    now = utc_now()
    snapshot_id = repo.insert_snapshot(
        conn,
        Snapshot(
            source=SNAPSHOT_SOURCE,
            file_name=zip_path.name,
            sha256=sha256,
            size_bytes=zip_path.stat().st_size,
            tag_cap=TAG_CAP,
            status="importing",
            imported_at=now,
        ),
    )

    with ArchiveReader(zip_path) as archive:
        # 第一遍：记下所有条目的类型（关系表要用），同时导入动画
        log.info("读取条目")
        subject_types: dict[int, int] = {}
        anime_ids: set[str] = set()
        imported_ids: set[str] = set()
        for raw in archive.iter_records("subject.jsonlines"):
            subject_types[raw["id"]] = raw["type"]
            if raw["type"] != ANIME_TYPE:
                continue
            # 先记下“这一期出现过”，再解析。解析失败的作品仍然在数据包里，不能被当成“消失”打墓碑
            anime_ids.add(str(raw["id"]))
            stats.anime_seen += 1
            try:
                parsed = parse_subject(raw)
            except ValueError as e:
                stats.work_errors += 1
                errors.add("subject", raw.get("id"), e)
                continue
            stats.max_user_tags = max(
                stats.max_user_tags, sum(t.kind == "user" for t in parsed.tags)
            )
            repo.upsert_work(conn, SOURCE, parsed, snapshot_id)
            imported_ids.add(parsed.source_id)
            stats.works_imported += 1

        # 只处理这一期解析成功的作品。库里还有以前导入、这一期已经消失的作品，不能拿它们去匹配本期的关系
        work_ids = {
            sid: wid
            for sid, wid in repo.work_ids_by_source_id(conn, SOURCE).items()
            if sid in imported_ids
        }

        log.info("读取作品关系")
        relations = []
        for raw in archive.iter_records("subject-relations.jsonlines"):
            work_id = work_ids.get(str(raw["subject_id"]))
            if work_id is None:
                continue  # 不是已导入的动画
            stats.relations_seen += 1
            try:
                _, relation = parse_relation(raw, subject_types)
            except ValueError as e:
                stats.relation_errors += 1
                errors.add("relation", raw.get("subject_id"), e)
                continue
            relations.append((work_id, relation))
        repo.replace_relations(conn, SOURCE, relations)
        stats.relations_imported = len(relations)

        log.info("读取 Staff 关联")
        raw_links = []
        for raw in archive.iter_records("subject-persons.jsonlines"):
            work_id = work_ids.get(str(raw["subject_id"]))
            if work_id is not None:
                raw_links.append((work_id, str(raw["person_id"]), raw["position"]))
        stats.links_seen = len(raw_links)
        needed = {person_id for _, person_id, _ in raw_links}

        log.info("读取人物（需要 %d 个）", len(needed))
        persons = []
        for raw in archive.iter_records("person.jsonlines"):
            if str(raw["id"]) not in needed:
                continue
            try:
                persons.append(parse_person(raw))
            except ValueError as e:
                stats.person_errors += 1
                errors.add("person", raw.get("id"), e)
        repo.upsert_persons(conn, persons, snapshot_id)
        stats.persons_imported = len(persons)

        person_ids = repo.person_ids_by_source_id(conn, SOURCE)
        links = []
        for work_id, person_source_id, position in raw_links:
            person_id = person_ids.get(person_source_id)
            if person_id is None:
                stats.link_errors += 1
                errors.add("link", person_source_id, ValueError("人物不在本期数据里"))
                continue
            links.append((work_id, person_id, position))
        repo.replace_work_persons(conn, SOURCE, links)
        stats.links_imported = len(links)

    log.info("导入代码表")
    repo.replace_source_codes(conn, SOURCE, load_codes(codes_dir))
    stats.works_removed = repo.mark_removed(conn, SOURCE, anime_ids, now)
    log.info("重建作品名全文索引")
    repo.rebuild_title_index(conn)
    return stats, snapshot_id


def _validate(conn: sqlite3.Connection, stats: ImportStats, max_error_ratio: float) -> None:
    problems = repo.check_integrity(conn)
    if stats.works_imported == 0:
        problems.append("一部动画都没有导入成功")
    if stats.max_user_tags > TAG_CAP:
        problems.append(
            f"有作品的用户标签数达到 {stats.max_user_tags}，超过 TAG_CAP={TAG_CAP}，"
            "导出规则可能变了，请核对后更新 TAG_CAP"
        )
    for name, bad, total in (
        ("条目", stats.work_errors, stats.anime_seen),
        ("关系", stats.relation_errors, stats.relations_seen),
        ("人物", stats.person_errors, stats.persons_imported + stats.person_errors),
        ("Staff 关联", stats.link_errors, stats.links_seen),
    ):
        if total and bad / total > max_error_ratio:
            problems.append(f"{name}的坏记录占 {bad / total:.1%}，超过上限 {max_error_ratio:.0%}")
    active = repo.count_active_works(conn, SOURCE)
    if not stats.works_imported <= active <= stats.anime_seen:
        problems.append(
            f"有效作品数 {active} 不在 [{stats.works_imported}, {stats.anime_seen}] 范围内"
        )
    if problems:
        raise ImportFailed("校验没有通过：" + "；".join(problems))
