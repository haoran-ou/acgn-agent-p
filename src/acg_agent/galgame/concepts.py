"""中文概念 → VNDB 标签（手工维护，2026-10-25 你过目确认）。

VNDB 的标签是英文的，而且有层级：查一个标签时连同它的所有子标签一起算（和 VNDB 网站的搜索一样）。
表里没有的说法，再按 VNDB 标签的英文名或别名精确匹配（给以后的 Agent 用）；都对不上就报错，不悄悄忽略。
VNDB 没有“治愈”“热血”“萌”这类标签，这些说法不收录。

“后宫”的含义和动画不同：galgame 几乎每部都有多个女主角的路线，所以这里的“后宫”指“有全收结局”（Harem Ending）。
"""

import json
import sqlite3
from collections import defaultdict

CONCEPT_TAGS: dict[str, tuple[str, ...]] = {
    "催泪": ("g596",),        # Nakige
    "致郁": ("g693",),        # Utsuge
    "恋爱": ("g96",),         # Romance
    "百合": ("g97",),         # Girl x Girl Romance
    "耽美": ("g98",),         # Boy x Boy Romance
    "乙女向": ("g542",),      # Otome Game
    "拔作": ("g214",),        # Nukige
    "后宫": ("g434",),        # Harem Ending
    "NTR": ("g513",),         # Netorare
    "搞笑": ("g104",),        # Comedy
    "日常": ("g454",),        # Slice of Life
    "校园": ("g47",),         # School
    "悬疑": ("g19",),         # Mystery
    "推理": ("g153",),        # Murder Mystery
    "全年龄": ("g235",),      # No Sexual Content
    "异世界": ("g2180",),     # Protagonist from a Different World
    "恐怖": ("g7",),          # Horror
    "奇幻": ("g2",),          # Fantasy
    "魔法": ("g4",),          # Magic
    "科幻": ("g105",),        # Science Fiction
    "赛博朋克": ("g682",),    # Cyberpunk
    "战斗": ("g12",),         # Action
    "剧情向": ("g147",),      # Drama
    "时间旅行": ("g249",),    # Time Travel
    "轮回": ("g349",),        # Time Loop
    "猎奇": ("g161",),        # Gore
    "电波": ("g960",),        # Denpa
    "吸血鬼": ("g5",),        # Vampire
    "女主人公": ("g134",),    # Female Protagonist
    "多结局": ("g148",),      # Multiple Endings
    "无选项": ("g709",),      # Kinetic Novel
    "修罗场": ("g336",),      # Love Triangle
}

# 同一个概念的其他说法（含繁体）
CONCEPT_ALIASES: dict[str, str] = {
    "催淚": "催泪", "泣系": "催泪", "泣きゲー": "催泪", "致鬱": "致郁", "鬱": "致郁", "郁作": "致郁",
    "戀愛": "恋爱", "百合向": "百合", "BL": "耽美", "乙女": "乙女向", "乙女游戏": "乙女向", "拔": "拔作", "拔作向": "拔作",
    "後宮": "后宫", "全收": "后宫", "ntr": "NTR", "寝取": "NTR", "牛头人": "NTR", "喜剧": "搞笑", "校園": "校园",
    "懸疑": "悬疑", "推理向": "推理", "杀人事件": "推理", "无H": "全年龄", "清水": "全年龄", "異世界": "异世界",
    "恐怖向": "恐怖", "动作": "战斗", "戰鬥": "战斗", "剧情": "剧情向", "穿越": "时间旅行", "循环": "轮回",
    "輪迴": "轮回", "血腥": "猎奇", "獵奇": "猎奇", "電波": "电波", "女主视角": "女主人公", "多结局向": "多结局",
    "动态小说": "无选项", "三角恋": "修罗场", "修羅場": "修罗场",
}


class UnknownConcept(ValueError):
    pass


class TagIndex:
    """标签名、别名、父子关系。启动后缓存一份（3,014 个标签）。"""

    def __init__(self, conn: sqlite3.Connection):
        self.names: dict[str, str] = {}
        self.by_name: dict[str, str] = {}
        for tid, name, aliases in conn.execute("SELECT id, name, aliases FROM tag"):
            self.names[tid] = name
            for n in [name, *json.loads(aliases)]:
                self.by_name.setdefault(n.casefold(), tid)
        self.children: dict[str, set[str]] = defaultdict(set)
        for tid, parent in conn.execute("SELECT tag_id, parent_id FROM tag_parent"):
            self.children[parent].add(tid)

    def expand(self, roots: tuple[str, ...]) -> frozenset[str]:
        found, stack = set(roots), list(roots)
        while stack:
            for child in self.children[stack.pop()]:
                if child not in found:
                    found.add(child)
                    stack.append(child)
        return frozenset(found)

    def resolve(self, concept: str) -> frozenset[str]:
        """概念 → 标签 ID 集合（含子标签）。"""
        c = concept.strip()
        c = CONCEPT_ALIASES.get(c, c)
        if c in CONCEPT_TAGS:
            return self.expand(CONCEPT_TAGS[c])
        tid = self.by_name.get(c.casefold())
        if tid:
            return self.expand((tid,))
        raise UnknownConcept(f"galgame 不支持的标签：{concept}。可用：{'、'.join(CONCEPT_TAGS)}")
