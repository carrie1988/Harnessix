from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.sandbox.capabilities import probe_container_engine, probe_host_sandbox


def test_host_sandbox_probe_only_advertises_backend_after_preflight() -> None:
    observed = []

    def failed(argv, timeout):
        observed.append((tuple(argv), timeout))
        return subprocess.CompletedProcess(argv, 1, "", "denied")

    probe = probe_host_sandbox(runner=failed)
    assert probe.guarded_available
    assert not probe.sandboxed_available
    assert probe.sandboxed_backend is None
    assert probe.backend_identity is None
    if observed:
        assert observed[0][1] == 5.0
        assert "preflight_failed" in probe.reason_code


@pytest.mark.parametrize(
    ("engine", "security", "rootless"),
    [("docker", '["name=seccomp","name=rootless"]', True), ("podman", "false", False)],
)
def test_container_probe_uses_daemon_and_security_capability_evidence(
    engine: str, security: str, rootless: bool
) -> None:
    observed = []

    def runner(argv, timeout):
        observed.append((tuple(argv), timeout))
        output = "client|server" if argv[1] == "version" else security
        return subprocess.CompletedProcess(argv, 0, output, "")

    probe = probe_container_engine(  # type: ignore[arg-type]
        Path(sys.executable), engine=engine, runner=runner
    )
    assert probe.rootless is rootless
    assert [item[0][1] for item in observed] == ["version", "info"]
    assert [item[1] for item in observed] == [15.0, 15.0]


def test_container_probe_rejects_unparseable_security_evidence() -> None:
    def runner(argv, timeout):
        output = "client|server" if argv[1] == "version" else "not-json"
        return subprocess.CompletedProcess(argv, 0, output, "")

    with pytest.raises(KernelError) as error:
        probe_container_engine(Path(sys.executable), engine="docker", runner=runner)
    assert error.value.code == "sandbox_unavailable"


def test_container_probe_timeout_fails_closed_without_retry() -> None:
    calls = 0

    def runner(argv, timeout):
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(KernelError) as error:
        probe_container_engine(Path(sys.executable), engine="docker", runner=runner)
    assert error.value.code == "sandbox_unavailable"
    assert calls == 1
