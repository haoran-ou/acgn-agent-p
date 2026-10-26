"""把 Bangumi Archive 的原始记录转成领域模型。

这里全是纯函数（pure function）：只做转换，不碰数据库，所以测试时直接喂字典就行。
转换失败统一抛 ValueError（Pydantic 的 ValidationError 也是 ValueError 的子类），由 pipeline 记进错误报告。
"""

from dataclasses import dataclass

from acg_agent.domain.models import Alias, Person, Work, WorkRelation, WorkTag
from acg_agent.domain.text import normalize_title, search_key
from acg_agent.importer.infobox import parse_aliases

SOURCE = "bangumi"
ANIME_TYPE = 2

# Bangumi 动画的 platform 代码 → 我们的 format（来自 bangumi/common 的 subject_platforms.yml）
ANIME_FORMATS = {1: "tv", 5: "web", 2: "ova", 3: "movie", 2006: "motion_comic", 0: "other"}

# Bangumi 人物 type → 我们的 kind（0 在数据里出现过 1 次，含义不明）
PERSON_KINDS = {0: "unknown", 1: "individual", 2: "company", 3: "group"}


@dataclass(frozen=True)
class ParsedWork:
    source_id: str
    work: Work
    aliases: list[Alias]
    tags: list[WorkTag]


def _blank_to_none(value: str | None) -> str | None:
    """Bangumi 用空字符串表示“没有”，入库一律换成 None。"""
    if value is None:
        return None
    value = value.strip()
    return value or None


def parse_subject(raw: dict) -> ParsedWork:
    if raw.get("type") != ANIME_TYPE:
        raise ValueError(f"不是动画条目（type={raw.get('type')}）")
    platform = raw.get("platform")
    if platform not in ANIME_FORMATS:
        raise ValueError(f"未知的动画 platform 代码：{platform}")

    rating_count = sum((raw.get("score_details") or {}).values())
    work = Work(
        media_type="anime",
        format=ANIME_FORMATS[platform],
        title_original=raw["name"].strip(),
        title_zh=_blank_to_none(raw.get("name_cn")),
        release_date=_blank_to_none(raw.get("date")),
        nsfw=bool(raw.get("nsfw")),
        # score = 0 是“没人评分”的哨兵值（sentinel value），不是 0 分
        score=raw.get("score") if rating_count > 0 else None,
        rating_count=rating_count,
        summary=_blank_to_none(raw.get("summary")),
    )
    return ParsedWork(
        source_id=str(raw["id"]),
        work=work,
        aliases=_aliases(work, raw.get("infobox")),
        tags=_tags(raw),
    )


def _aliases(work: Work, infobox: str | None) -> list[Alias]:
    candidates = [(work.title_original, "original")]
    if work.title_zh:
        candidates.append((work.title_zh, "zh"))
    candidates += [(name, "alias") for name in parse_aliases(infobox)]

    seen: set[str] = set()
    aliases = []
    for text, kind in candidates:
        normalized = normalize_title(text)
        # 同一部作品里规范化后相同的名字只留第一个，顺序是 原名 > 中文名 > 别名
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        aliases.append(Alias(text=text, normalized=normalized, search_key=search_key(text), kind=kind))
    return aliases


def _tags(raw: dict) -> list[WorkTag]:
    tags = []
    seen_user: set[str] = set()
    # 原始顺序就是按打标人数降序，位置从 1 开始
    for tag in raw.get("tags") or []:
        name = tag["name"].strip()
        if name and name not in seen_user:
            seen_user.add(name)
            tags.append(WorkTag(kind="user", name=name, count=tag["count"], position=len(seen_user)))
    for name in dict.fromkeys(m.strip() for m in raw.get("meta_tags") or []):
        if name:
            tags.append(WorkTag(kind="meta", name=name))
    return tags


def parse_relation(raw: dict, subject_types: dict[int, int]) -> tuple[str, WorkRelation]:
    """返回 (作品的来源 ID, 关系)。目标条目的类型从全量条目类型表里查。"""
    target = raw["related_subject_id"]
    if target not in subject_types:
        raise ValueError(f"关系指向的条目 {target} 不在本期数据里")
    return str(raw["subject_id"]), WorkRelation(
        related_source=SOURCE,
        related_source_id=str(target),
        related_source_type=subject_types[target],
        relation_code=raw["relation_type"],
    )


def parse_person(raw: dict) -> Person:
    kind = PERSON_KINDS.get(raw.get("type"))
    if kind is None:
        raise ValueError(f"未知的人物 type：{raw.get('type')}")
    return Person(source=SOURCE, source_id=str(raw["id"]), name=raw["name"].strip(), kind=kind)
