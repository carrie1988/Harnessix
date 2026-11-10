"""doctor文本：可启动时失败项只是警告（如沙箱不可用时改为逐条批准），不可启动时才标失败。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harnessix.product_ui.cli import _render_preflight


def _report(*, ready: bool) -> SimpleNamespace:
    check = SimpleNamespace(
        status="failed",
        category="action",
        check_id="product_native_sandbox",
        code="seatbelt_nested_unavailable",
        remediation_id=None,
    )
    return SimpleNamespace(
        ready=ready, mode="doctor", platform="posix", report_sha256="0" * 64, checks=(check,)
    )


@pytest.mark.parametrize(("ready", "label"), [(True, "[警告]"), (False, "[失败]")])
def test_failed_checks_are_warnings_when_the_product_can_start(ready: bool, label: str) -> None:
    text = _render_preflight(_report(ready=ready))  # type: ignore[arg-type]
    assert f"{label} action/product_native_sandbox: seatbelt_nested_unavailable" in text
