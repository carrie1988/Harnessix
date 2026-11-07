"""原生进程沙箱探测v2（ADR 0111第2.7节、第5节）。"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.sandbox.native_contracts import NATIVE_GUARANTEES, HostSandboxProbeV2
from harnessix.sandbox.native_probe import probe_native_sandbox


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _runner(*, version: str = "bubblewrap 0.6.1\n", code: int = 0, stderr: str = ""):
    calls: list[tuple[str, ...]] = []

    def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, version, "")
        return subprocess.CompletedProcess(argv, code, "", stderr)

    return run, calls


def test_bubblewrap_verified_probe_lists_guarantees(tmp_path: Path) -> None:
    run, calls = _runner()
    probe = probe_native_sandbox(
        platform="linux", runner=run, bubblewrap_candidates=(_executable(tmp_path / "bwrap"),)
    )
    assert probe.available and probe.reason_code == "verified"
    assert probe.backend == "bubblewrap" and probe.backend_version == "bubblewrap 0.6.1"
    assert probe.guarantees == NATIVE_GUARANTEES["bubblewrap"]
    preflight = calls[-1]
    assert "--unshare-all" in preflight and preflight[-1] == "/bin/true"
    assert HostSandboxProbeV2.model_validate_json(probe.model_dump_json()) == probe


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        (
            "bwrap: No permissions to creating new namespace, likely because the kernel "
            "does not allow non-privileged user namespaces.",
            "bubblewrap_namespaces_denied",
        ),
        ("bwrap: setting up uid map: Permission denied", "bubblewrap_namespaces_denied"),
        ("bwrap: Can't find source path /nonexistent", "bubblewrap_preflight_failed"),
    ],
)
def test_bubblewrap_failures_are_classified(tmp_path: Path, stderr: str, reason: str) -> None:
    run, _ = _runner(code=1, stderr=stderr)
    probe = probe_native_sandbox(
        platform="linux", runner=run, bubblewrap_candidates=(_executable(tmp_path / "bwrap"),)
    )
    assert not probe.available and probe.reason_code == reason and probe.guarantees == ()


def test_bubblewrap_missing_is_reported(tmp_path: Path) -> None:
    run, calls = _runner()
    probe = probe_native_sandbox(
        platform="linux", runner=run, bubblewrap_candidates=(tmp_path / "missing",)
    )
    assert probe.reason_code == "bubblewrap_not_installed" and probe.executable is None
    assert calls == []


def test_seatbelt_nested_sandbox_is_recognised_without_running(tmp_path: Path) -> None:
    run, calls = _runner()
    probe = probe_native_sandbox(
        platform="macos",
        runner=run,
        nested=lambda: True,
        seatbelt_executable=_executable(tmp_path / "sandbox-exec"),
    )
    assert probe.reason_code == "seatbelt_nested_unavailable" and not probe.available
    assert calls == []


def test_seatbelt_preflight_success_and_failure(tmp_path: Path) -> None:
    executable = _executable(tmp_path / "sandbox-exec")
    ok, _ = _runner()
    probe = probe_native_sandbox(
        platform="macos", runner=ok, nested=lambda: False, seatbelt_executable=executable
    )
    assert probe.available and probe.guarantees == NATIVE_GUARANTEES["seatbelt"]
    failed, _ = _runner(code=71, stderr="sandbox-exec: sandbox_apply: Operation not permitted")
    probe = probe_native_sandbox(
        platform="macos", runner=failed, nested=lambda: False, seatbelt_executable=executable
    )
    assert probe.reason_code == "seatbelt_preflight_failed"
    missing = probe_native_sandbox(
        platform="macos", runner=ok, nested=lambda: False, seatbelt_executable=tmp_path / "none"
    )
    assert missing.reason_code == "seatbelt_not_installed"


def test_other_platforms_have_no_backend() -> None:
    probe = probe_native_sandbox(platform="other")
    assert probe.backend is None and probe.reason_code == "native_backend_platform_mismatch"


def test_probe_contract_rejects_inconsistent_claims(tmp_path: Path) -> None:
    run, _ = _runner()
    probe = probe_native_sandbox(
        platform="linux", runner=run, bubblewrap_candidates=(_executable(tmp_path / "bwrap"),)
    )
    payload = probe.model_dump(mode="json")
    for change in (
        {"backend": "seatbelt"},
        {"guarantees": list(NATIVE_GUARANTEES["seatbelt"])},
        {"available": False},
    ):
        with pytest.raises(ValidationError):
            HostSandboxProbeV2.model_validate(payload | change)
