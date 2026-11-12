"""模型Profile的可选字段类型，以及思考强度的端点映射与一致性校验（ADR 0146）。

思考强度是给用户的档位（低、中、高），不是Token数：模型按档位自己规划思考的长短。只在核实过的
思考协议上生效；显式给出思考Token上限时改发上限、不发档位（有的模型不接受两者同时出现）。
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import Field

ThinkingEffort = Literal["low", "medium", "high"]


def _unset(value: object) -> bool:
    return value is None


# 模型上下文窗口（真实Token）；声明后Context上限与压缩阈值按窗口换算，未声明沿用固定上限。
ContextWindowTokens = Annotated[
    int | None, Field(ge=8192, le=2_097_152, strict=True, exclude_if=_unset)
]
# 思考强度档位；未设置时不发送任何强度参数（沿用服务端默认）。
ThinkingEffortOption = Annotated[ThinkingEffort | None, Field(exclude_if=_unset)]
# 思考Token上限（高级选项）：到达后模型停止思考并作答；与思考强度只能设置一个。
ThinkingBudgetTokens = Annotated[
    int | None, Field(ge=256, le=262_144, strict=True, exclude_if=_unset)
]

# 核实过接受强度档位的思考协议，以及各档在线上的取值。None表示不发送档位：
# 百炼qwen3.8系列不发送时即服务端默认，也就是最强的一档。
_EFFORT_WIRE_VALUES: dict[str, dict[str, str | None]] = {
    "dashscope_enable_thinking": {"low": "low", "medium": "medium", "high": None},
}


class _ThinkingProfile(Protocol):
    """校验只读取Profile的这几个字段。"""

    @property
    def thinking(self) -> str | None: ...
    @property
    def thinking_effort(self) -> str | None: ...
    @property
    def thinking_budget_tokens(self) -> int | None: ...
    @property
    def max_output_tokens(self) -> int: ...


def effort_wire_value(protocol: str | None, effort: str | None) -> str | None:
    """强度档位在该思考协议下发送的取值；协议不支持或该档不发送时为None。"""
    return _EFFORT_WIRE_VALUES.get(protocol or "", {}).get(effort or "")


def validate_profile_thinking(profile: _ThinkingProfile, protocol: str | None) -> None:
    """Profile的思考设置与Provider思考协议一致：有协议才能思考，协议支持才能设强度或上限。"""
    if profile.thinking not in (None, "off") and protocol is None:
        raise ValueError("Profile思考模式要求Provider声明已求证的思考协议")
    effort, budget = profile.thinking_effort, profile.thinking_budget_tokens
    if effort is None and budget is None:
        return
    if protocol not in _EFFORT_WIRE_VALUES or profile.thinking == "off":
        raise ValueError("思考强度与思考Token上限要求思考协议支持，且没有关闭思考")
    if effort is not None and budget is not None:
        raise ValueError("思考强度与思考Token上限只能设置一个")
    if budget is not None and budget >= profile.max_output_tokens:
        raise ValueError("思考Token上限必须低于单次输出上限")
