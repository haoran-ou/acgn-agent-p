"""从 Bangumi infobox（原始 wiki 字符串）里解析别名。

只解析“别名”一个字段，够 V0 用。实测的四种写法：

    |别名={            多行，每行一个方括号
    [名字]             最常见
    [罗马字|Name]       带类别：取竖线后面的部分
    [剧场版 [前篇]]     括号嵌套：只去掉最外层
    }
    |别名= 名字          单行写法
    |别名= 甲 / 乙       单行写多个时用“空格/空格”分隔（不带空格的斜杠如 Fate/stay night 不拆）
"""

_FIELD = "别名"
_INLINE_SEPARATOR = " / "


def parse_aliases(infobox: str | None) -> list[str]:
    if not infobox:
        return []
    lines = infobox.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        key, sep, value = line.lstrip("|").partition("=")
        if not line.startswith("|") or not sep or key.strip() != _FIELD:
            continue
        value = value.strip()
        if value != "{":
            return [name.strip() for name in value.split(_INLINE_SEPARATOR) if name.strip()]
        return _parse_block(lines[i + 1 :])
    return []


def _parse_block(lines: list[str]) -> list[str]:
    names = []
    for line in lines:
        item = line.strip()
        if item == "}":
            break
        name = _unwrap(item)
        if name:
            names.append(name)
    return names


def _unwrap(item: str) -> str:
    if not (item.startswith("[") and item.endswith("]")):
        return item
    inner = item[1:-1]
    label, sep, rest = inner.partition("|")
    # 竖线前面是类别（不含方括号）时，取竖线后面；否则整个当作名字
    if sep and "[" not in label:
        return rest.strip()
    return inner.strip()
