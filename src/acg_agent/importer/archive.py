"""读取 Bangumi Archive 的 zip 数据包：校验 sha256、逐行读取 JSON Lines。"""

import hashlib
import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

REQUIRED_MEMBERS = (
    "subject.jsonlines",
    "subject-relations.jsonlines",
    "subject-persons.jsonlines",
    "person.jsonlines",
)

_CHUNK_BYTES = 1024 * 1024


class ArchiveError(Exception):
    """数据包本身有问题：校验和不一致、缺文件、某一行不是合法 JSON。"""


def sha256_of(path: Path) -> str:
    """分块计算，不把 447 MB 的文件一次读进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> str:
    actual = sha256_of(path)
    expected = expected.lower().removeprefix("sha256:")
    if actual != expected:
        raise ArchiveError(f"sha256 不一致：期望 {expected}，实际 {actual}")
    return actual


class ArchiveReader:
    """直接从 zip 里逐行读取，不解压到磁盘。"""

    def __init__(self, path: Path):
        self._zip = zipfile.ZipFile(path)
        missing = [m for m in REQUIRED_MEMBERS if m not in self._zip.namelist()]
        if missing:
            self._zip.close()
            raise ArchiveError(f"数据包缺少文件：{', '.join(missing)}")

    def __enter__(self) -> "ArchiveReader":
        return self

    def __exit__(self, *exc) -> None:
        self._zip.close()

    def iter_records(self, member: str) -> Iterator[dict]:
        with self._zip.open(member) as raw:
            for line_no, line in enumerate(io.TextIOWrapper(raw, encoding="utf-8"), start=1):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    # 一行坏 JSON 说明文件本身损坏，不是单条记录的问题，所以整体失败
                    raise ArchiveError(f"{member} 第 {line_no} 行不是合法 JSON：{e}") from e
