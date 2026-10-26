"""三态判断（three-state verdict）的数据结构。

判断有四个取值。“很可能满足”是从“未知”里按证据强度细分出来的，所以仍然叫三态判断：
    satisfied         满足：有正面证据
    likely_satisfied  很可能满足：没有正面证据，但间接证据足够强
    unknown           未知：信息缺失或证据太弱
    conflict          冲突：有证据表明违反条件
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from acg_agent.domain.models import Format


class Verdict(StrEnum):
    SATISFIED = "satisfied"
    LIKELY = "likely_satisfied"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


# 合并多个条件时取最差的那个：数字越大越差
SEVERITY = {Verdict.SATISFIED: 0, Verdict.LIKELY: 1, Verdict.UNKNOWN: 2, Verdict.CONFLICT: 3}

Mode = Literal["strict", "explore"]
# 严格模式只要有证据支持的；探索模式额外接受“未知”，但界面上要标注
ACCEPTED = {
    "strict": {Verdict.SATISFIED, Verdict.LIKELY},
    "explore": {Verdict.SATISFIED, Verdict.LIKELY, Verdict.UNKNOWN},
}


@dataclass(frozen=True)
class VerdictConfig:
    """判断用的阈值。都是可以调的参数，将来用冒烟测试集校准。"""

    # 用于排除时，打标人数达到这个数才算冲突；更少的只算未知（避免一个人随手打的标签就排除作品）
    exclude_conflict_min_count: int = 3
    # 相关标签打标人数少于这个数，就是“证据薄弱”：想要的标签只能算很可能满足；排序时排到最后
    weak_count_below: int = 10
    # 想排除的标签没出现时，标签列表必须满额，且没列出的标签人数上限至少这么多，才算很可能满足
    likely_min_upper_bound: int = 50


DEFAULT_CONFIG = VerdictConfig()


class _Condition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class YearRange(_Condition):
    kind: Literal["year"] = "year"
    min_year: int | None = None
    max_year: int | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.min_year is None and self.max_year is None:
            raise ValueError("年份范围至少要有一端")
        if self.min_year is not None and self.max_year is not None and self.min_year > self.max_year:
            raise ValueError("起始年份不能晚于结束年份")
        return self


class FormatIn(_Condition):
    kind: Literal["format"] = "format"
    formats: frozenset[Format] = Field(min_length=1)


class TagRequired(_Condition):
    kind: Literal["tag_required"] = "tag_required"
    concept: str = Field(min_length=1)


class TagExcluded(_Condition):
    kind: Literal["tag_excluded"] = "tag_excluded"
    concept: str = Field(min_length=1)


# 辨别联合（discriminated union）：按 kind 字段决定是哪一种条件，解析 JSON 时也能用
Condition = Annotated[YearRange | FormatIn | TagRequired | TagExcluded, Field(discriminator="kind")]


class Evidence(BaseModel):
    """判断依据。code 是语言无关的键，界面再按语言渲染成句子。"""

    model_config = ConfigDict(frozen=True)

    code: str
    detail: dict[str, str | int | float | None] = {}  # float：VNDB 标签的平均分


class ConditionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    condition: Condition
    verdict: Verdict
    evidence: list[Evidence]
    weak: bool = False  # 证据薄弱：相关标签打标人数少于 weak_count_below


class WorkResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_id: int
    overall: Verdict
    results: list[ConditionResult]
    weak_evidence: bool  # 任何一个条件证据薄弱，就为 True；排序时排到最后

    def accepted(self, mode: Mode) -> bool:
        return self.overall in ACCEPTED[mode]
