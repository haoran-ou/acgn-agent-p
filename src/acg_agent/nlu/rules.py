"""规则解析器：模型不可用时的后备。只认得出明确的说法，认不出的如实列进 unmatched。

认得出：
- 年份：2005到2015年、2010年以后、2000年以前、90年代、2012年
- 类型：TV / 剧场版 / 电影 / OVA / WEB，以及“不要剧场版”这种否定
- 标签：词表里的概念（含同义词），前面同一分句里出现否定词就是“排除”，否则是“想要”
规则解析器从不推断（inferred 永远是 False）。
"""

import re
import unicodedata
from datetime import date

from acg_agent.domain.text import search_key, to_simplified
from acg_agent.nlu.schema import MAX_TEXT_CHARS, ParsedCondition, ParsedQuery
from acg_agent.verdict.concepts import TAG_SYNONYMS
from acg_agent.verdict.models import FormatIn, TagExcluded, TagRequired, YearRange

_NEGATIONS = ("不想看", "不想要", "不喜欢", "不要", "不含", "不带", "没有", "排除", "除了", "讨厌", "拒绝", "别")
_CLAUSE_BREAK = re.compile(r"[，,。；;！!？?\n]")
# 认出条件以后剩下的、不算“没理解”的虚词
_FILLERS = ("我想看", "想看", "推荐", "一些", "几部", "动画", "番剧", "作品", "的", "要", "有", "和", "与",
            "还有", "但是", "但", "而且", "并且", "请", "给我", "找", "吗", "呢", "吧", "最好", "可以", "也", "或者")

_YEAR_PATTERNS = [
    # 顺序重要：先匹配范围，再匹配单个年份
    ("range", re.compile(r"(\d{4})\s*年?\s*(?:到|至|-|~|—)\s*(\d{4})\s*年?")),
    ("after", re.compile(r"(\d{4})\s*年?\s*(?:以后|之后|以来|起|开始|后)")),
    ("before", re.compile(r"(\d{4})\s*年?\s*(?:以前|之前|前)")),
    ("decade", re.compile(r"(\d{2}|\d{4})\s*年代")),
    ("single", re.compile(r"(\d{4})\s*年")),
]

_FORMAT_WORDS = {
    "TV": "tv", "电视动画": "tv", "剧场版": "movie", "电影": "movie", "OVA": "ova", "OAD": "ova",
    "WEB": "web", "网络动画": "web",
}
_ALL_FORMATS = frozenset({"tv", "web", "ova", "movie", "motion_comic", "other"})


def parse_with_rules(text: str, vocabulary: list[str]) -> ParsedQuery:
    text = _prepare(text)
    spans: list[tuple[int, int]] = []  # 已经认出的片段，最后用来找“没理解的部分”
    conditions: list[ParsedCondition] = []

    for cond, (start, end) in _years(text):
        conditions.append(ParsedCondition(condition=cond, source_text=text[start:end][:40]))
        spans.append((start, end))

    excluded_formats, wanted_formats = set(), set()
    for word, fmt in _FORMAT_WORDS.items():
        for m in re.finditer(re.escape(word), text, flags=re.IGNORECASE):
            (excluded_formats if _negated(text, m.start()) else wanted_formats).add(fmt)
            spans.append(m.span())
    if wanted_formats:
        conditions.append(ParsedCondition(condition=FormatIn(formats=frozenset(wanted_formats))))
    elif excluded_formats:
        conditions.append(ParsedCondition(condition=FormatIn(formats=_ALL_FORMATS - excluded_formats)))

    for concept, (start, end), negated in _concepts(text, vocabulary, spans):
        cond = TagExcluded(concept=concept) if negated else TagRequired(concept=concept)
        conditions.append(ParsedCondition(condition=cond, source_text=text[start:end]))
        spans.append((start, end))

    unmatched = _leftovers(text, spans)
    intent = "search" if conditions else "unclear"
    return ParsedQuery(intent=intent, conditions=_dedupe(conditions), unmatched=unmatched[:5])


def _prepare(text: str) -> str:
    # 全角数字、字母转半角；再转简体，让词表能对上
    return to_simplified(unicodedata.normalize("NFKC", text)[:MAX_TEXT_CHARS])


def _years(text: str) -> list[tuple[YearRange, tuple[int, int]]]:
    found, taken = [], []
    for kind, pattern in _YEAR_PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            a = int(m.group(1))
            if kind == "range":
                lo, hi = sorted((a, int(m.group(2))))
                cond = YearRange(min_year=lo, max_year=hi)
            elif kind == "after":
                cond = YearRange(min_year=a)
            elif kind == "before":
                cond = YearRange(max_year=a - 1)
            elif kind == "decade":
                start = a if a >= 1000 else (1900 + a if a >= 30 else 2000 + a)
                cond = YearRange(min_year=start, max_year=start + 9)
            else:
                cond = YearRange(min_year=a, max_year=a)
            if not 1900 <= (cond.min_year or cond.max_year) <= date.today().year + 2:
                continue  # 不像年份的四位数（例如“1000部”），不当年份
            found.append((cond, m.span()))
            taken.append(m.span())
    return found


# 否定的作用范围到这些词为止：“不要后宫的恋爱番”只否定“后宫”
_NEGATION_STOPS = ("的", "但", "要", "想看")


def _negated(text: str, pos: int) -> bool:
    """位置 pos 前面、同一分句里有否定词，而且否定词和 pos 之间没有出现“的”等截断词。

    “不要后宫和百合” → 后宫、百合都被否定；“不要后宫的恋爱番” → 只有后宫被否定。
    """
    clause_start = max((m.end() for m in _CLAUSE_BREAK.finditer(text, 0, pos)), default=0)
    before = text[clause_start:pos]
    last_neg_end = max((before.rfind(neg) + len(neg) for neg in _NEGATIONS if neg in before), default=-1)
    if last_neg_end < 0:
        return False
    between = before[last_neg_end:]
    return not any(stop in between for stop in _NEGATION_STOPS)


def _terms(vocabulary: list[str]) -> list[tuple[str, str]]:
    """(在文本里找的词, 对应的概念)，长词优先，避免“轻小说改”被“小说改”抢先匹配。"""
    pairs = {}
    for concept in vocabulary:
        for term in TAG_SYNONYMS.get(concept, (concept,)):
            pairs[term] = concept
    return sorted(pairs.items(), key=lambda kv: -len(kv[0]))


def _concepts(text: str, vocabulary: list[str], taken: list[tuple[int, int]]):
    found = []
    taken = list(taken)
    lowered = text.casefold()
    for term, concept in _terms(vocabulary):
        needle = term.casefold()
        # 纯拉丁字母的短词（如 SF、BL、3D）必须是独立的词，不能是别的单词的一部分
        latin = re.fullmatch(r"[0-9a-z.\-]+", needle) is not None
        pattern = rf"(?<![0-9a-z]){re.escape(needle)}(?![0-9a-z])" if latin else re.escape(needle)
        for m in re.finditer(pattern, lowered):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append(m.span())
            found.append((concept, m.span(), _negated(text, m.start())))
    found.sort(key=lambda f: f[1][0])
    return found


def _leftovers(text: str, spans: list[tuple[int, int]]) -> list[str]:
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    rest = "".join(chars)
    for word in sorted((*_NEGATIONS, *_FILLERS), key=len, reverse=True):
        rest = rest.replace(word, " ")
    chunks = [c.strip() for c in re.split(r"[\s，,。；;！!？?、]+", rest)]
    # 只剩一个字的多半是虚词残留；规范化后为空的是纯标点
    return [c[:40] for c in chunks if len(c) >= 2 and search_key(c)]


def _dedupe(conditions: list[ParsedCondition]) -> list[ParsedCondition]:
    seen, out = set(), []
    for pc in conditions:
        key = pc.condition.model_dump_json()
        if key not in seen:
            seen.add(key)
            out.append(pc)
    return out
