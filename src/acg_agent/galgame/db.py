"""galgame 库的建表。连接仍用 acg_agent.db.connect（同样打开外键检查）。"""

import sqlite3
from pathlib import Path

GALGAME_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_GALGAME_DB = Path("data/galgame.db")


def apply_galgame_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(GALGAME_SCHEMA_PATH.read_text(encoding="utf-8"))
