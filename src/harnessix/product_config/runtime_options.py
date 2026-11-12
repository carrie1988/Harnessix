"""产品配置里的宿主运行选项：未设置时沿用产品默认值，且不写入配置文件。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field


def _unset(value: object) -> bool:
    return value is None


# 读工具（读文件/搜索）单次时限；未设置沿用5秒。大仓库或与推理、构建共用机器时可放宽。
WorkspaceReadTimeout = Annotated[int | None, Field(ge=1, le=60, strict=True, exclude_if=_unset)]

# 付费交互模式下，网络中断、限流、服务端错误之后最多连续自动重试的次数（ADR 0144）。
# 未设置沿用5次；0表示不自动重试，立即停下交给用户。
ServiceFailureRetries = Annotated[int | None, Field(ge=0, le=20, strict=True, exclude_if=_unset)]
