"""产品预检内部支撑：平台归一化、耗时记录与脱敏工作区标识。"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable

from harnessix.agent.errors import KernelError
from harnessix.product_config.product_contracts import (
    PreflightCategory,
    PreflightPlatform,
    PreflightRequirement,
    PreflightStatus,
    ProductPreflightCheck,
)

MonotonicClock = Callable[[], float]


class PreflightRecorder:
    """把独立检查结果收敛为稳定、脱敏且按标识可排序的事实。"""

    def __init__(self, clock: MonotonicClock) -> None:
        self._clock = clock
        self._checks: dict[str, ProductPreflightCheck] = {}

    def started(self) -> float:
        try:
            return self._clock()
        except Exception:
            return 0.0

    def record(
        self,
        check_id: str,
        category: PreflightCategory,
        requirement: PreflightRequirement,
        status: PreflightStatus,
        code: str,
        remediation_id: str | None,
        started: float,
    ) -> None:
        self._checks[check_id] = ProductPreflightCheck(
            check_id=check_id,
            category=category,
            requirement=requirement,
            status=status,
            code=code,
            remediation_id=remediation_id,
            duration_ms=self._duration_ms(started),
        )

    def ordered(self) -> tuple[ProductPreflightCheck, ...]:
        return tuple(self._checks[key] for key in sorted(self._checks))

    def _duration_ms(self, started: float) -> int:
        try:
            elapsed = max(0.0, self._clock() - started)
        except Exception:
            return 0
        return min(300_000, int(elapsed * 1000))


def workspace_fingerprint(path: os.PathLike[str]) -> str:
    """返回不可逆的规范工作区标识，避免报告暴露宿主路径。"""

    normalized = os.path.normcase(os.path.abspath(path))
    return hashlib.sha256(
        b"harnessix-product-preflight-workspace/v1\0"
        + normalized.encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def native_platform(value: str | None) -> PreflightPlatform:
    """把宿主平台名收敛为产品合同允许的两种值。"""

    selected = value or os.name
    if selected == "posix":
        return "posix"
    if selected in {"nt", "windows"}:
        return "windows"
    raise KernelError("product_tools_platform_unsupported", "产品运行平台不受支持")
