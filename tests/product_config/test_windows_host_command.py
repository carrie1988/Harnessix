"""Windows沙箱外PowerShell命令（2026-11-10用户决定W2）：合同、零配置、证明与真实执行。

合同、零配置与证明逻辑在各平台验证；真实执行只在Windows上运行（Windows CI的product_config全集）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.processes.supervision_planner import build_process_capability
from harnessix.product_config import zero_config
from harnessix.product_config.action_contracts import build_product_native_process_profile
from harnessix.product_config.native_process import attest_native_process_profile
from harnessix.product_config.native_shells import (
    POWERSHELL_ARGUMENTS,
    fixed_shell,
    windows_powershell,
)

PWSH = r"C:\Program Files\PowerShell\7\pwsh.exe"


def _powershell_profile(program: str = PWSH, backend: str = "host", **overrides: object):
    fields: dict[str, object] = {
        "profile_id": "host-shell",
        "version": "1",
        "description": "PowerShell",
        "backend": backend,
        "program": program,
        "arguments": POWERSHELL_ARGUMENTS,
    }
    return build_product_native_process_profile(**(fields | overrides))  # type: ignore[arg-type]


def _windows_capability():
    return build_process_capability(
        platform="windows", supports_pty=False, implementation_digest="1" * 64
    )


@pytest.mark.parametrize(
    "program",
    [PWSH, r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", r"D:\tools\PWSH.EXE"],
)
def test_powershell_profiles_are_accepted_outside_the_sandbox(program: str) -> None:
    assert _powershell_profile(program).program == program


@pytest.mark.parametrize(
    "changes",
    [
        {"backend": "bubblewrap"},
        {"backend": "seatbelt"},
        {"arguments": ("-Command",)},
        {"arguments": (*POWERSHELL_ARGUMENTS, "-ExecutionPolicy", "Bypass")},
        {"program": r"C:\Windows\System32\cmd.exe"},
        {"program": "pwsh.exe"},
        {"program": r"C:\tools\..\pwsh"},
    ],
)
def test_other_windows_shapes_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _powershell_profile(**changes)  # type: ignore[arg-type]


def test_posix_rules_are_unchanged() -> None:
    assert fixed_shell("/bin/zsh", ("-c",), "seatbelt")
    assert fixed_shell("/usr/bin/bash", ("-lc",), "container")
    assert not fixed_shell("/usr/bin/pwsh.exe", POWERSHELL_ARGUMENTS, "host")
    assert not fixed_shell("/bin/bash", POWERSHELL_ARGUMENTS, "host")


def test_zero_config_offers_only_an_approved_powershell_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(zero_config, "windows_powershell", lambda _environ: PWSH)
    config = zero_config.zero_config_action_config({}, platform="nt")
    (profile,) = config.native_process_profiles
    assert (profile.backend, profile.program, profile.arguments) == (
        "host",
        PWSH,
        POWERSHELL_ARGUMENTS,
    )
    assert "PowerShell" in profile.description and "每条都需批准" in profile.description

    monkeypatch.setattr(zero_config, "windows_powershell", lambda _environ: None)
    assert zero_config.zero_config_action_config({}, platform="nt").native_process_profiles == ()


def test_windows_owner_attests_only_host_commands() -> None:
    environ = {
        "PATH": r"C:\Windows\System32",
        "SYSTEMROOT": r"C:\Windows",
        "USERPROFILE": r"C:\Users\dev",
        "APPDATA": r"C:\Users\dev\AppData\Roaming",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "MODEL_API_KEY": "secret",
        "HARNESSIX_PRODUCT_CONFIG": r"C:\x",
    }
    reason, attested = attest_native_process_profile(
        _powershell_profile(),
        _windows_capability(),
        environ=environ,
        secret_names=("MODEL_API_KEY",),
    )
    assert reason == "verified" and attested is not None
    assert attested.capabilities.platform == "windows"
    assert attested.sandbox.level == "host_guarded" and attested.native_sandbox is None
    for name in ("SYSTEMROOT", "USERPROFILE", "APPDATA", "HTTPS_PROXY"):
        assert attested.environment[name] == environ[name]
    assert "MODEL_API_KEY" not in attested.environment
    assert "HARNESSIX_PRODUCT_CONFIG" not in attested.environment

    sandboxed = build_product_native_process_profile(
        profile_id="shell",
        version="1",
        description="sandbox",
        backend="bubblewrap",
        program="/bin/bash",
        arguments=("-c",),
    )
    reason, attested = attest_native_process_profile(
        sandboxed, _windows_capability(), environ=environ
    )
    assert (reason, attested) == ("native_backend_platform_mismatch", None)


windows_only = pytest.mark.skipif(os.name != "nt", reason="真实PowerShell执行只在Windows上验证")


@windows_only
async def test_powershell_command_runs_after_approval_and_writes_the_workspace(
    tmp_path: Path,
) -> None:
    from tests.product_config.test_native_process_action import _native_case, _result

    program = windows_powershell(os.environ)
    assert program is not None
    cmd = "Set-Content -Path out.txt -Value windows -NoNewline; Write-Output done"
    async with _native_case(
        tmp_path, cmd, _powershell_profile(program), environ=dict(os.environ)
    ) as case:
        assert case.approval.decision is None
        assert not (case.workspace / "out.txt").exists()
        result = _result(await case.approve_and_resume())
        assert result.outcome == "succeeded", result
        assert (case.workspace / "out.txt").read_text(encoding="utf-8") == "windows"


@windows_only
async def test_powershell_nonzero_exit_is_a_failure(tmp_path: Path) -> None:
    from tests.product_config.test_native_process_action import _native_case, _result

    program = windows_powershell(os.environ)
    assert program is not None
    async with _native_case(
        tmp_path, "cmd /c exit 3", _powershell_profile(program), environ=dict(os.environ)
    ) as case:
        result = _result(await case.approve_and_resume())
        assert result.outcome == "failed"
        assert result.error is not None and result.error.code == "process_nonzero_exit"


def test_profiles_written_before_the_program_files_prefix_are_still_accepted() -> None:
    legacy = POWERSHELL_ARGUMENTS[:-1] + ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;",)
    program = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert legacy != POWERSHELL_ARGUMENTS
    assert fixed_shell(program, legacy, "host")
    assert fixed_shell(program, POWERSHELL_ARGUMENTS, "host")


@windows_only
async def test_powershell_commands_can_read_program_files_x86(tmp_path: Path) -> None:
    # 变量名带括号，不能随执行计划的环境继承；由固定前缀向系统查询后补上（用户决定A，2026-11-11）。
    from tests.product_config.test_native_process_action import _native_case, _result

    program = windows_powershell(os.environ)
    assert program is not None
    cmd = "Set-Content -Path out.txt -Value ${env:ProgramFiles(x86)} -NoNewline"
    async with _native_case(
        tmp_path, cmd, _powershell_profile(program), environ=dict(os.environ)
    ) as case:
        result = _result(await case.approve_and_resume())
        assert result.outcome == "succeeded", result
        written = (case.workspace / "out.txt").read_text(encoding="utf-8")
        assert written == os.environ["ProgramFiles(x86)"]
