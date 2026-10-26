"""分流（routing）：一句话交给固定流程还是 Agent（V1.5 的完成标准）。

依据 2026-10-25 的对比评估：普通条件固定流程更准、更快、更省；只有参照作品、剧情描述这类
变不成条件的说法，Agent 才更好。这类说法在解析结果里表现为两种样子：
- 列进了“没理解的部分”（unmatched），例如“像物语那样”
- 被模型推断成了标签（inferred），例如把“互相救赎”推断成“催泪”。同一类说法解析器有时列进 unmatched、
  有时推断成标签，不稳定（TECH_DEBT 第 53 项），所以两种都交给 Agent
规则由代码判断，不再多问一次模型。
"""

from dataclasses import dataclass
from typing import Literal

from acg_agent.nlu.schema import ParsedQuery

Target = Literal["workflow", "agent"]


@dataclass(frozen=True)
class Route:
    target: Target
    reason: str


def choose_route(query: ParsedQuery | None, has_history: bool = False) -> Route:
    """query 为 None 表示没有先解析（多轮对话时）。"""
    if has_history:
        return Route("agent", "继续和代理人的对话")
    if query is None:
        raise ValueError("没有对话记录时，必须先解析")
    if query.intent == "off_topic":
        return Route("workflow", "和找动画无关")
    if query.unmatched:
        return Route("agent", "有没理解的部分：" + "、".join(query.unmatched))
    # 只算标签：推断出来的年份、类型（例如把“恋爱番”的“番”推断成 TV）不是剧情、氛围类说法
    inferred = [pc.source_text or pc.condition.concept for pc in query.conditions
                if pc.inferred and pc.condition.kind in ("tag_required", "tag_excluded")]
    if inferred:
        return Route("agent", "有推断出来的条件：" + "、".join(inferred))
    return Route("workflow", "条件都理解了")
