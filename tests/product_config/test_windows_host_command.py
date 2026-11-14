"""Windows沙箱外PowerShell命令（2026-11-10用户决定W2）：合同、零配置、证明与真实执行。

合同、零配置与证明逻辑在各平台验证；真实执行只在Windows上运行（Windows CI的product_config全集）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.processes.supervision_planner import build_process_capability
from harnessix.product_config import zero_config
from harnessix.product_config.action_contracts import build_product_native_process_profile
from harnessix.product_config.native_process import attest_native_process_profile
from harnessix.product_config.native_shells import (
    NATIVE_EXIT_NOTE,
    POWERSHELL_ARGUMENTS,
    POWERSHELL_EXIT_TRAILER,
    fixed_shell,
    shell_command_text,
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
        # Beta Issue #12：此前PowerShell -Command把原生程序的退出码都报成1。
        assert isinstance(result.output, dict) and result.output["returncode"] == 3


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


@pytest.mark.parametrize(
    "program", [PWSH, r"C:\Windows\System32\WindowsPowerShell\v1.0\POWERSHELL.EXE"]
)
def test_powershell_commands_carry_the_native_exit_code_trailer(program: str) -> None:
    """Beta Issue #12：命令之后另起一行补上带出原生退出码的一段；其他Shell原样。"""
    profile = _powershell_profile(program)
    assert (
        shell_command_text(profile, "python -m tool") == "python -m tool" + POWERSHELL_EXIT_TRAILER
    )
    assert POWERSHELL_EXIT_TRAILER.startswith("\n")
    sandboxed = build_product_native_process_profile(
        profile_id="shell",
        version="1",
        description="sh",
        backend="bubblewrap",
        program="/bin/bash",
        arguments=("-c",),
    )
    assert shell_command_text(sandboxed, "python -m tool") == "python -m tool"


def _real_powershell() -> str | None:
    """真实PowerShell：Windows上的系统PowerShell，或任意平台PATH上的pwsh；也可用环境变量指定。"""
    return (
        os.environ.get("HARNESSIX_TEST_PWSH")
        or (windows_powershell(os.environ) if os.name == "nt" else None)
        or shutil.which("pwsh")
    )


@pytest.mark.skipif(_real_powershell() is None, reason="本机没有PowerShell")
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("{python} -c 'raise SystemExit(0)'", 0),
        ("{python} -c 'raise SystemExit(1)'", 1),
        ("{python} -c 'raise SystemExit(2)'", 2),
        ("{python} -c 'raise SystemExit(42)'", 42),
        ("{python} -c 'raise SystemExit(2)' # 末尾的注释", 2),
        ("Write-Output a\n{python} -c 'raise SystemExit(7)'", 7),
        ("{python} -c 'raise SystemExit(3)'; Write-Output ok", 0),
        ("Get-Item definitely-missing-item", 1),
        ("definitely-not-a-command-xyz", 1),
        ("exit 5", 5),
        ("{python} -c 'raise SystemExit(2)'; exit $LASTEXITCODE", 2),
    ],
)
def test_a_real_powershell_reports_the_native_exit_code(command: str, expected: int) -> None:
    program = _real_powershell()
    assert program is not None
    profile = _powershell_profile(program if os.name == "nt" else PWSH)
    text = shell_command_text(profile, command.format(python=f"& '{sys.executable}'"))
    completed = subprocess.run(
        [program, *POWERSHELL_ARGUMENTS, text], capture_output=True, timeout=120, check=False
    )
    assert completed.returncode == expected, completed.stderr.decode("utf-8", "replace")


@pytest.mark.skipif(_real_powershell() is None, reason="本机没有PowerShell")
@pytest.mark.parametrize(
    ("command", "note"),
    [
        ("{python} -c 'raise SystemExit(42)'; Write-Host done", "42"),
        ("{python} -c 'raise SystemExit(42)'; exit $LASTEXITCODE", None),
        ("{python} -c 'raise SystemExit(3)'\n{python} -c 'raise SystemExit(0)'", None),
        ("Write-Output plain", None),
    ],
)
def test_a_real_powershell_notes_the_failed_native_program_it_did_not_exit_with(
    command: str, note: str | None
) -> None:
    """Beta Issue #27：测试失败后又Write-Host，整条命令退出0；stderr末尾注明原生退出码。"""
    program = _real_powershell()
    assert program is not None
    profile = _powershell_profile(program if os.name == "nt" else PWSH)
    text = shell_command_text(profile, command.format(python=f"& '{sys.executable}'"))
    completed = subprocess.run(
        [program, *POWERSHELL_ARGUMENTS, text], capture_output=True, timeout=120, check=False
    )
    stderr = completed.stderr.decode("utf-8", "replace")
    if note is None:
        assert NATIVE_EXIT_NOTE not in stderr
    else:
        assert completed.returncode == 0
        assert stderr.strip().endswith(NATIVE_EXIT_NOTE + note)
