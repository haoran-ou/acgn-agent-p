"""读取 bangumi/common 的代码对照表（YAML），转成 source_code 表的行。"""

from pathlib import Path
from typing import NamedTuple

import yaml

# (文件名, 顶层键, category, 中文名字段, 英文名字段)
_FILES = (
    ("subject_relations.yml", "relations", "relation", "cn", "en"),
    ("subject_staffs.yml", "staffs", "staff", "cn", "en"),
    ("subject_platforms.yml", "platforms", "platform", "type_cn", "type"),
)


class CodeRow(NamedTuple):
    source: str
    category: str
    scope: int
    code: int
    label_zh: str
    label_en: str | None


def load_codes(codes_dir: Path) -> list[CodeRow]:
    rows = []
    for file_name, top_key, category, zh_key, en_key in _FILES:
        path = codes_dir / file_name
        if not path.exists():
            raise FileNotFoundError(f"缺少代码表 {path}（见 docs/CREDITS.md 的 bangumi/common 一行）")
        tree = yaml.safe_load(path.read_text(encoding="utf-8"))[top_key]
        for scope, entries in tree.items():
            # platforms 里混有 'book_series' 这类字符串键的分组，不是条目类型，跳过
            if not isinstance(scope, int):
                continue
            for code, entry in entries.items():
                label_zh = (entry.get(zh_key) or "").strip()
                if not label_zh:
                    continue
                label_en = (entry.get(en_key) or "").strip() or None
                rows.append(CodeRow("bangumi", category, scope, int(code), label_zh, label_en))
    return rows
