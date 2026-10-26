"""各个导入命令共用的部分：错误报告，以及“复制旧库 → 在副本上更新 → 原子切换”。"""

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from acg_agent.db import apply_schema, connect
from acg_agent.importer import repository as repo

T = TypeVar("T")


class ImportFailed(Exception):
    """导入没有完成，正式库没有任何变化。"""


class ErrorLog:
    """坏记录清单，最后写成 JSON Lines 文件。"""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def add(self, stage: str, source_id: object, error: Exception | str) -> None:
        self.entries.append({"stage": stage, "source_id": str(source_id), "error": str(error)})

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for entry in self.entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return path


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class closing:
    """sqlite3.Connection 的 with 语句只管事务、不关闭连接，这里补上关闭。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self.conn

    def __exit__(self, *exc) -> None:
        self.conn.close()


def find_imported(db_path: Path, source: str, sha256: str) -> int | None:
    """只读打开正式库，查这个文件是否已经导入过。不往正式库写任何东西。"""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = repo.find_ready_snapshot(conn, source, sha256)
    except sqlite3.OperationalError:
        return None  # 库里还没有 snapshot 表
    finally:
        conn.close()
    return row["id"] if row else None


def copy_database(src: Path, dst: Path) -> None:
    """复制旧库作为起点，作品 ID 因此保持不变。用 SQLite 的 backup 接口，比直接复制文件安全。"""
    if not src.exists():
        return
    with closing(sqlite3.connect(src)) as source, closing(sqlite3.connect(dst)) as target:
        source.backup(target)


def swap_import(
    db_path: Path,
    errors: ErrorLog,
    error_report: Path,
    work: Callable[[sqlite3.Connection], T],
    schema: Callable[[sqlite3.Connection], None] = apply_schema,
    copy_old: bool = True,
) -> T:
    """在旧库的副本上执行 work(conn)，提交后原子切换成正式库。

    schema：建表函数（galgame 库有自己的表）；copy_old=False 表示从空库开始整库重建（galgame 每次都重建）。

    work 负责写数据和校验；校验不通过就抛异常。任何异常都会删除临时库、写出错误报告，
    再统一包装成 ImportFailed，正式库保持原样。
    """
    tmp_path = db_path.with_name(db_path.name + ".importing")
    tmp_path.unlink(missing_ok=True)  # 上次异常退出留下的临时库
    try:
        if copy_old:
            copy_database(db_path, tmp_path)
        with closing(connect(tmp_path)) as conn:
            schema(conn)
            result = work(conn)
            conn.commit()
        # os.replace 在同一个文件系统内是原子操作：要么还是旧库，要么已经是新库，不存在中间状态
        os.replace(tmp_path, db_path)
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        errors.write(error_report)
        raise ImportFailed(f"导入失败，正式库没有变化：{e}") from e
    return result
