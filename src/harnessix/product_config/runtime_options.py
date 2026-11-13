"""产品配置里的宿主运行选项：未设置时沿用产品默认值，且不写入配置文件。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field


def _unset(value: object) -> bool:
    return value is None


# 读工具（读文件/搜索）单次时限；未设置沿用5秒。大仓库或与推理、构建共用机器时可放宽。
WorkspaceReadTimeout = Annotated[int | None, Field(ge=1, le=60, strict=True, exclude_if=_unset)]

# 一次模型请求因网络中断、限流、服务端错误失败后最多自动重试的次数（ADR 0144）。
# 设置后它同时决定一次请求最多尝试几次（N+1，不超过Profile允许的上限6次）；未设置时尝试次数沿用
# Profile的max_attempts，付费交互模式下“用量未知”的失败最多连续重试5次。0表示不自动重试。
ServiceFailureRetries = Annotated[int | None, Field(ge=0, le=20, strict=True, exclude_if=_unset)]
# 一次模型请求最多的尝试次数，与ModelProfile.max_attempts的上限相同。
MAX_REQUEST_ATTEMPTS = 6


def request_attempts(service_failure_retries: int | None, profile_attempts: int) -> int:
    """一次模型请求实际的最多尝试次数：配置了重试次数时为N+1（最多6次），否则沿用Profile。

    服务端明确拒绝（503、429）或连接没有建立的失败确定没有计费，不受“用量未知”的限制，
    此前只由Profile的max_attempts决定，配置的重试次数对它们不起作用（Beta Issue #2）。
    """
    if service_failure_retries is None:
        return profile_attempts
    return min(service_failure_retries + 1, MAX_REQUEST_ATTEMPTS)
