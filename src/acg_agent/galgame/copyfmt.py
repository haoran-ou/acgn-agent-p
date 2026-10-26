"""读 VNDB 数据包：一个 zstd 压缩的 tar，里面每张表一个 PostgreSQL COPY 格式的文件。

COPY 格式就是 TSV 加几条转义规则（PostgreSQL 文档“COPY · Text Format”）：
- 字段用 Tab 分隔，一行一条记录
- \\N 表示 NULL
- 反斜杠转义：\\\\ \\t \\n \\r \\b \\f \\v
规则只有这几条，自己写十几行比引入一个库更轻。解压用 zstandard（BSD 许可）。
"""

import re
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path

import zstandard

_ESCAPES = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r", "b": "\b", "f": "\f", "v": "\v"}
_ESCAPE_RE = re.compile(r"\\(.)")


def decode_field(raw: str) -> str | None:
    if raw == r"\N":
        return None
    if "\\" not in raw:
        return raw
    return _ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), raw)


def parse_line(line: bytes) -> list[str | None]:
    return [decode_field(f) for f in line.decode("utf-8").rstrip("\n").split("\t")]


def stream_tables(path: Path, handlers: dict[str, Callable[[Iterator[list[str | None]]], None]]) -> str | None:
    """按顺序读一遍数据包，把每张需要的表交给对应的 handler；返回 TIMESTAMP 文件的内容。

    tar 用流式模式（r|）读取，不需要先把整个包解压到磁盘；不需要的表直接跳过。
    """
    timestamp = None
    with path.open("rb") as fh:
        reader = zstandard.ZstdDecompressor().stream_reader(fh)
        with tarfile.open(fileobj=reader, mode="r|") as tar:
            for member in tar:
                if member.name == "TIMESTAMP":
                    timestamp = tar.extractfile(member).read().decode().strip()
                    continue
                name = member.name.removeprefix("db/")
                handler = handlers.get(name)
                if handler is None or not member.isfile():
                    continue
                handler(parse_line(line) for line in tar.extractfile(member))
    return timestamp
