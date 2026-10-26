"""打开 SQLite 连接并建表。"""

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str | Path) -> sqlite3.Connection:
    """打开数据库。path 传 ":memory:" 得到只存在于内存中的库（测试用）。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # SQLite 默认不检查外键，而且这个设置只对当前连接有效，所以每次打开都要执行
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """建表。schema.sql 全部使用 IF NOT EXISTS，重复执行不会出错。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
