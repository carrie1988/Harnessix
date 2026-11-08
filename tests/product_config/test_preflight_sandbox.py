"""零配置的沙箱状态：Doctor检查、可读修复说明与启动提示（ADR 0113第6节第二轮决定）。"""

from __future__ import annotations

import ast
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from harnessix.product_config.contracts import ProductConfigV2
from harnessix.product_config.preflight import run_product_preflight
from harnessix.product_config.preflight_actions import (
    _REMEDIATIONS,
    NATIVE_SANDBOX_CHECK_ID,
    inspect_product_actions,
)
from harnessix.product_config.preflight_support import PreflightRecorder
from harnessix.product_config.product_contracts import ProductPreflightReport
from harnessix.product_ui.cli import _render_preflight
from harnessix.product_ui.controller import ProductControllerState
from harnessix.product_ui.main_view import _status_text
from harnessix.product_ui.preflight_text import remediation_text, sandbox_status_line
from harnessix.sandbox.native_contracts import HostSandboxProbeV2
from harnessix.sandbox.native_probe import probe_native_sandbox
from tests.product_config.conftest import write_config
from tests.product_config.test_preflight import _checks, _dependencies, _request
from tests.product_config.test_zero_config import _probe

pytestmark = pytest.mark.skipif(os.name != "posix", reason="零配置命令只在POSIX平台生成")

_ENVIRONMENT = {"PRIMARY_API_KEY": "primary-secret", "BACKUP_API_KEY": "backup-secret"}


def _missing_bubblewrap() -> HostSandboxProbeV2:
    return probe_native_sandbox(platform="linux", bubblewrap_candidates=())


def _doctor(
    tmp_path: Path,
    config: ProductConfigV2,
    probe: Callable[[], HostSandboxProbeV2],
    *,
    zero_config: bool = True,
) -> ProductPreflightReport:
    request = _request(tmp_path, write_config(tmp_path / "config.json", config))
    return run_product_preflight(
        replace(request, zero_config_actions=zero_config),
        dependency_finder=_dependencies,
        environment=_ENVIRONMENT,
        sandbox_probe=probe,
    )


def test_available_sandbox_is_a_passed_check_and_a_status_line(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    report = _doctor(tmp_path, config, _probe(tmp_path, 0))
    check = _checks(report)[NATIVE_SANDBOX_CHECK_ID]
    assert (check.requirement, check.status, check.code, check.remediation_id) == (
        "advisory",
        "passed",
        "native_sandbox_bubblewrap",
        None,
    )
    assert sandbox_status_line(report) == "沙箱：已启用（bubblewrap）"


def test_unavailable_sandbox_does_not_block_start_and_explains_the_fix(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    """沙箱不可用时仍可启动（命令逐条审批），Doctor给出可照做的修复说明。"""

    report = _doctor(tmp_path, config, _missing_bubblewrap)
    check = _checks(report)[NATIVE_SANDBOX_CHECK_ID]
    assert (check.requirement, check.status, check.code, check.remediation_id) == (
        "advisory",
        "failed",
        "bubblewrap_not_installed",
        "install_bubblewrap",
    )
    assert report.ready
    assert sandbox_status_line(report) == (
        "沙箱：不可用，命令逐条审批（运行 harnessix code doctor 查看原因）"
    )
    rendered = _render_preflight(report)
    assert (
        "修复动作：install_bubblewrap（安装bubblewrap（Debian/Ubuntu：sudo apt install" in rendered
    )


def test_no_sandbox_check_or_status_without_zero_config(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    calls: list[int] = []

    def probe() -> HostSandboxProbeV2:
        calls.append(1)
        return _missing_bubblewrap()

    report = _doctor(tmp_path, config, probe, zero_config=False)
    assert NATIVE_SANDBOX_CHECK_ID not in _checks(report)
    assert sandbox_status_line(report) is None
    assert calls == []


def test_tools_and_check_come_from_one_probe() -> None:
    calls: list[int] = []

    def probe() -> HostSandboxProbeV2:
        calls.append(1)
        return _missing_bubblewrap()

    recorder = PreflightRecorder(lambda: 0.0)
    actions = inspect_product_actions(
        None,
        None,
        platform="posix",
        environment={},
        recorder=recorder,
        zero_config=True,
        sandbox_probe=probe,
    )
    assert actions.snapshot is not None
    profiles = actions.snapshot.config.native_process_profiles
    assert [item.backend for item in profiles] == ["host"]
    assert calls == [1]


def test_status_line_shows_sandbox_only_when_known() -> None:
    assert "沙箱：已启用（macOS Seatbelt）" in _status_text(
        ProductControllerState(sandbox="沙箱：已启用（macOS Seatbelt）")
    )
    assert "沙箱" not in _status_text(ProductControllerState())


def _recorded_remediation_ids() -> set[str]:
    """预检源码里出现的所有修复标识：record的第6个参数、remediation_id关键字与映射表。"""

    found = set(_REMEDIATIONS.values()) | {"inspect_action_capability"}
    package = Path(__file__).parents[2] / "src/harnessix/product_config"
    for path in package.glob("preflight*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            values: list[ast.expr] = []
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "record":
                values += node.args[5:6]
            if isinstance(node, ast.keyword) and node.arg == "remediation_id":
                values.append(node.value)
            for value in values:
                found |= {
                    item.value
                    for item in ast.walk(value)
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
    return found


def test_every_remediation_id_has_readable_text() -> None:
    ids = _recorded_remediation_ids()
    assert {"install_bubblewrap", "run_configure", "omit_or_install_git"} <= ids
    assert sorted(item for item in ids if remediation_text(item) is None) == []
