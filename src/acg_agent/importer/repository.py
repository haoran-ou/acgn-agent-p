"""导入用到的全部 SQL。其他模块不直接写 SQL，换数据库时只需要改这里。"""

import json
import sqlite3
from collections.abc import Iterable, Mapping

from acg_agent.domain.models import Person, Snapshot, WorkRelation
from acg_agent.importer.codes import CodeRow
from acg_agent.importer.transform import ParsedWork


def find_ready_snapshot(conn: sqlite3.Connection, source: str, sha256: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshot WHERE source = ? AND sha256 = ? AND status = 'ready'",
        (source, sha256),
    ).fetchone()


def insert_snapshot(conn: sqlite3.Connection, snapshot: Snapshot) -> int:
    return conn.execute(
        "INSERT INTO snapshot (source, file_name, sha256, size_bytes, tag_cap, status, imported_at)"
        " VALUES (:source, :file_name, :sha256, :size_bytes, :tag_cap, :status, :imported_at)",
        snapshot.model_dump(),
    ).lastrowid


def set_snapshot_status(conn: sqlite3.Connection, snapshot_id: int, status: str) -> None:
    conn.execute("UPDATE snapshot SET status = ? WHERE id = ?", (status, snapshot_id))


def upsert_work(conn: sqlite3.Connection, source: str, parsed: ParsedWork, snapshot_id: int) -> int:
    """已有就更新（ID 不变），没有就新建。别名和标签整体替换成这一期的内容。"""
    w = parsed.work
    values = {
        "media_type": w.media_type,
        "format": w.format,
        "title_original": w.title_original,
        "title_zh": w.title_zh,
        "release_date": w.release_date.isoformat() if w.release_date else None,
        "nsfw": int(w.nsfw),
        "score": w.score,
        "rating_count": w.rating_count,
        "summary": w.summary,
        "snapshot_id": snapshot_id,
    }
    row = conn.execute(
        "SELECT work_id FROM external_id WHERE source = ? AND source_id = ?",
        (source, parsed.source_id),
    ).fetchone()
    if row:
        work_id = row["work_id"]
        assignments = ", ".join(f"{col} = :{col}" for col in values)
        # removed_at 清空：以前被打过墓碑标记的作品，这一期又出现了
        conn.execute(
            f"UPDATE work SET {assignments}, removed_at = NULL WHERE id = :id",
            values | {"id": work_id},
        )
        conn.execute("DELETE FROM alias WHERE work_id = ?", (work_id,))
        conn.execute("DELETE FROM work_tag WHERE work_id = ?", (work_id,))
    else:
        cols = ", ".join(values)
        marks = ", ".join(f":{col}" for col in values)
        work_id = conn.execute(f"INSERT INTO work ({cols}) VALUES ({marks})", values).lastrowid
        conn.execute(
            "INSERT INTO external_id (source, source_id, work_id) VALUES (?, ?, ?)",
            (source, parsed.source_id, work_id),
        )

    conn.executemany(
        "INSERT INTO alias (work_id, text, normalized, search_key, kind) VALUES (?, ?, ?, ?, ?)",
        [(work_id, a.text, a.normalized, a.search_key, a.kind) for a in parsed.aliases],
    )
    conn.executemany(
        "INSERT INTO work_tag (work_id, kind, name, count, position) VALUES (?, ?, ?, ?, ?)",
        [(work_id, t.kind, t.name, t.count, t.position) for t in parsed.tags],
    )
    return work_id


def work_ids_by_source_id(conn: sqlite3.Connection, source: str) -> dict[str, int]:
    rows = conn.execute("SELECT source_id, work_id FROM external_id WHERE source = ?", (source,))
    return {r["source_id"]: r["work_id"] for r in rows}


def replace_relations(
    conn: sqlite3.Connection, source: str, relations: Iterable[tuple[int, WorkRelation]]
) -> None:
    conn.execute(
        "DELETE FROM work_relation WHERE work_id IN"
        " (SELECT work_id FROM external_id WHERE source = ?)",
        (source,),
    )
    # INSERT OR IGNORE：原始数据里偶有完全重复的关系行，重复的只留一条
    conn.executemany(
        "INSERT OR IGNORE INTO work_relation VALUES (?, ?, ?, ?, ?)",
        (
            (work_id, r.related_source, r.related_source_id, r.related_source_type, r.relation_code)
            for work_id, r in relations
        ),
    )


def upsert_persons(conn: sqlite3.Connection, persons: Iterable[Person], snapshot_id: int) -> None:
    conn.executemany(
        "INSERT INTO person (source, source_id, name, kind, snapshot_id) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (source, source_id) DO UPDATE SET"
        " name = excluded.name, kind = excluded.kind, snapshot_id = excluded.snapshot_id",
        ((p.source, p.source_id, p.name, p.kind, snapshot_id) for p in persons),
    )


def person_ids_by_source_id(conn: sqlite3.Connection, source: str) -> dict[str, int]:
    rows = conn.execute("SELECT source_id, id FROM person WHERE source = ?", (source,))
    return {r["source_id"]: r["id"] for r in rows}


def replace_work_persons(
    conn: sqlite3.Connection, source: str, links: Iterable[tuple[int, int, int]]
) -> None:
    conn.execute(
        "DELETE FROM work_person WHERE work_id IN"
        " (SELECT work_id FROM external_id WHERE source = ?)",
        (source,),
    )
    conn.executemany("INSERT OR IGNORE INTO work_person VALUES (?, ?, ?)", links)


def replace_source_codes(conn: sqlite3.Connection, source: str, rows: list[CodeRow]) -> None:
    conn.execute("DELETE FROM source_code WHERE source = ?", (source,))
    conn.executemany("INSERT INTO source_code VALUES (?, ?, ?, ?, ?, ?)", rows)


def mark_removed(
    conn: sqlite3.Connection, source: str, seen_source_ids: set[str], removed_at: str
) -> int:
    """墓碑记录（tombstone）：这一期数据包里没出现的作品打上 removed_at，不物理删除。返回新标记的数量。"""
    conn.execute("CREATE TEMP TABLE seen_source_id (source_id TEXT PRIMARY KEY)")
    try:
        conn.executemany(
            "INSERT INTO seen_source_id VALUES (?)", ((sid,) for sid in seen_source_ids)
        )
        return conn.execute(
            "UPDATE work SET removed_at = ? WHERE removed_at IS NULL AND id IN ("
            " SELECT e.work_id FROM external_id AS e"
            " WHERE e.source = ? AND e.source_id NOT IN (SELECT source_id FROM seen_source_id))",
            (removed_at, source),
        ).rowcount
    finally:
        conn.execute("DROP TABLE seen_source_id")


def replace_platforms(
    conn: sqlite3.Connection,
    source: str,
    platforms: Iterable,
    links: Mapping[tuple[int, str], object],
    snapshot_id: int,
) -> None:
    """整体替换某个来源的平台和作品-平台链接。先删链接再删平台（外键顺序）。"""
    conn.execute("DELETE FROM work_platform WHERE source = ?", (source,))
    conn.execute("DELETE FROM platform WHERE source = ?", (source,))
    conn.executemany(
        "INSERT INTO platform VALUES (?, ?, ?, ?, ?)",
        ((source, p.site, p.title, p.url_template, _json_or_none(p.default_regions)) for p in platforms),
    )
    conn.executemany(
        "INSERT INTO work_platform VALUES (?, ?, ?, ?, ?, ?, ?)",
        ((work_id, source, site, link.url, _json_or_none(link.regions), link.begin, snapshot_id)
         for (work_id, site), link in links.items()),
    )


def _json_or_none(values) -> str | None:
    return json.dumps(list(values)) if values else None


def rebuild_title_index(conn: sqlite3.Connection) -> None:
    """按 alias 表的当前内容重建全文索引。导入时别名是整体替换的，最后统一重建最简单。"""
    conn.execute("INSERT INTO alias_fts (alias_fts) VALUES ('rebuild')")


def count_active_works(conn: sqlite3.Connection, source: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM work AS w JOIN external_id AS e ON e.work_id = w.id"
        " WHERE e.source = ? AND w.removed_at IS NULL",
        (source,),
    ).fetchone()[0]


def check_integrity(conn: sqlite3.Connection) -> list[str]:
    """返回发现的问题；空列表表示通过。"""
    problems = []
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        problems.append(f"integrity_check: {result}")
    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        problems.append(f"foreign_key_check: {len(broken)} 行外键指向不存在的记录")
    return problems
