"""从数据库批量读取判断需要的事实（facts）。判断规则本身不碰数据库，见 rules.py。"""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field

# SQLite 单条语句的参数个数有上限，分批查询更稳妥
_BATCH = 900


@dataclass
class WorkFacts:
    work_id: int
    release_date: str | None
    format: str | None
    user_tags: dict[str, int] = field(default_factory=dict)  # 标签名 → 打标人数
    meta_tags: set[str] = field(default_factory=set)
    user_tag_count: int = 0
    list_is_full: bool = False
    unlisted_tag_max_count: int = 0  # 没列出的标签，打标人数的上限（上界证据）


def load_facts(conn: sqlite3.Connection, work_ids: Iterable[int]) -> dict[int, WorkFacts]:
    ids = list(dict.fromkeys(work_ids))
    facts: dict[int, WorkFacts] = {}
    for start in range(0, len(ids), _BATCH):
        batch = ids[start : start + _BATCH]
        marks = ", ".join("?" for _ in batch)
        for row in conn.execute(
            "SELECT w.id, w.release_date, w.format, e.user_tag_count, e.list_is_full,"
            "       e.unlisted_tag_max_count"
            " FROM work AS w JOIN work_tag_evidence AS e ON e.work_id = w.id"
            f" WHERE w.id IN ({marks})",
            batch,
        ):
            facts[row[0]] = WorkFacts(
                work_id=row[0],
                release_date=row[1],
                format=row[2],
                user_tag_count=row[3],
                list_is_full=bool(row[4]),
                unlisted_tag_max_count=row[5] or 0,
            )
        for work_id, kind, name, count in conn.execute(
            f"SELECT work_id, kind, name, count FROM work_tag WHERE work_id IN ({marks})", batch
        ):
            if kind == "user":
                facts[work_id].user_tags[name] = count
            else:
                facts[work_id].meta_tags.add(name)
    return facts
