"""名字的规范化（normalization），用于别名匹配。"""

import unicodedata
from functools import lru_cache

from opencc import OpenCC

# 匹配时忽略的字符类别：标点（P*）、符号（S*）、空白（Z*）
_IGNORED_CATEGORIES = ("P", "S", "Z")


def normalize_title(text: str) -> str:
    """把名字变成用于比较的形式。

    - NFKC：全角字母数字转半角（Ｆａｔｅ → Fate），兼容字符统一
    - casefold：比 lower() 更彻底的大小写折叠
    - 去掉标点、符号和空白：「Re:ゼロ」与「Re ゼロ」视为同一个名字

    不做简繁转换（检索时的简繁转换见下面的 search_key）。
    名字全由标点或符号组成时（例如「……」），去掉后会变成空串，这时只保留空白以外的字符。
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(
        ch for ch in folded if not unicodedata.category(ch).startswith(_IGNORED_CATEGORIES)
    )
    return kept or "".join(ch for ch in folded if not ch.isspace())


@lru_cache(maxsize=1)
def _t2s() -> OpenCC:
    """繁体 → 简体转换器（OpenCC t2s），第一次用到时才加载词典。"""
    return OpenCC("t2s")


def to_simplified(text: str) -> str:
    return _t2s().convert(text)


def search_key(text: str) -> str:
    """检索用的键：先转简体，再规范化。入库的别名和用户的查询都用它，两边才能对上。

    例：“物語”、“物语” → “物语”；“進擊的巨人”（繁体）→ “进击的巨人”。
    局限：日本新字体（撃、錬、桜 等）OpenCC 不认识，不会转换。
    """
    return normalize_title(to_simplified(text))
