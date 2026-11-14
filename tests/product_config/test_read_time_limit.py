"""工作区读取时限：配置优先，未配置时Windows默认20秒（Beta Issue #24，负责人2026-11-14选方案A）。"""

from __future__ import annotations

import pytest

from harnessix.product_config.contracts import ProductConfigV2
from harnessix.product_config.read_time_limit import (
    WINDOWS_READ_TIMEOUT_SECONDS,
    workspace_read_timeout,
)
from harnessix.tools.contracts import READ_TIMEOUT_SECONDS


@pytest.mark.parametrize(("windows", "expected"), [(True, 20), (False, READ_TIMEOUT_SECONDS)])
def test_unset_limit_follows_the_platform(
    config: ProductConfigV2, windows: bool, expected: int
) -> None:
    assert config.workspace_read_timeout_seconds is None
    assert workspace_read_timeout(config, windows=windows) == expected
    assert WINDOWS_READ_TIMEOUT_SECONDS == 20 and READ_TIMEOUT_SECONDS == 5


@pytest.mark.parametrize("windows", [True, False])
def test_a_configured_limit_wins_on_every_platform(config: ProductConfigV2, windows: bool) -> None:
    configured = config.model_copy(update={"workspace_read_timeout_seconds": 9})
    assert workspace_read_timeout(configured, windows=windows) == 9
