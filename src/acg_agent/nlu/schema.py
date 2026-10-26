"""一句话解析出来的结果。模型和规则解析器输出同一种结构。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from acg_agent.verdict.models import Condition

# search：是在找作品；unclear：看不懂；off_topic：和找作品无关（包括试图改变任务的输入）
Intent = Literal["search", "unclear", "off_topic"]
MAX_TEXT_CHARS = 200


class ParsedCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: Condition
    inferred: bool = Field(
        default=False,
        description="原话没有直接说、由模型推断出来的条件（例如“轻松一点的”推断成“日常”），设为 true",
    )
    source_text: str = Field(default="", max_length=40, description="这个条件对应的原话片段")


class ParsedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    conditions: list[ParsedCondition] = Field(default_factory=list, max_length=10)
    unmatched: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="没能变成条件的原话片段，例如“像《物语》那样的”、词表里没有的标签",
    )
