from __future__ import annotations

import json
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
        if argv[1] == "version":
            output = "client|server"
        elif argv[-1] in {"{{json .SecurityOptions}}", "{{.Host.Security.Rootless}}"}:
            output = security
        else:
            output = (
                "[true,true,true,true]"
                if engine == "docker"
                else '{"version":"v2","controllers":["cpu","memory","pids"]}'
            )
        return subprocess.CompletedProcess(argv, 0, output, "")

    probe = probe_container_engine(  # type: ignore[arg-type]
        Path(sys.executable), engine=engine, runner=runner
    )
    assert probe.rootless is rootless
    assert [item[0][1] for item in observed] == ["version", "info", "info"]
    assert [item[1] for item in observed] == [15.0, 15.0, 15.0]


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


@pytest.mark.parametrize(
    ("engine", "resource"),
    [
        *[
            ("docker", json.dumps([False if index == missing else True for index in range(4)]))
            for missing in range(4)
        ],
        *[
            ("docker", value)
            for value in (
                "[]",
                "[true,true,true]",
                "[true,true,true,true,true]",
                '[true,true,true,"true"]',
                "[true,true,true,1]",
                "[true,true,true,null]",
                "true",
                "{}",
                "not-json",
                "[" + " " * 4096 + "]",
            )
        ],
        *[
            ("podman", json.dumps({"version": "v2", "controllers": controllers}))
            for controllers in (
                ["cpu", "pids"],
                ["memory", "pids"],
                ["cpu", "memory"],
                [],
                ["cpu", "memory", "pids", 1],
            )
        ],
        ("podman", '{"version":"v1","controllers":["cpu","memory","pids"]}'),
        ("podman", '{"controllers":["cpu","memory","pids"]}'),
        ("podman", '{"version":"v2","controllers":"cpu memory pids"}'),
    ],
)
def test_container_probe_refuses_missing_resource_support(engine: str, resource: str) -> None:
    calls = []

    def runner(argv, timeout):
        calls.append(tuple(argv))
        assert timeout == 15.0
        if argv[1] == "version":
            output = "28.3.2|28.3.2"
        elif argv[-1] == "{{json .SecurityOptions}}":
            output = '["name=seccomp"]'
        elif argv[-1] == "{{.Host.Security.Rootless}}":
            output = "false"
        else:
            output = resource
        return subprocess.CompletedProcess(argv, 0, output, "private-diagnostic-sentinel")

    with pytest.raises(KernelError) as error:
        probe_container_engine(Path(sys.executable), engine=engine, runner=runner)  # type: ignore[arg-type]
    assert error.value.code == "sandbox_resources_unavailable"
    assert "private-diagnostic-sentinel" not in str(error.value)
    assert len(calls) == 3


def test_container_resource_probe_timeout_does_not_retry() -> None:
    calls = []

    def runner(argv, timeout):
        calls.append(tuple(argv))
        if argv[1] == "version":
            return subprocess.CompletedProcess(argv, 0, "28.3.2|28.3.2", "")
        if argv[-1] == "{{json .SecurityOptions}}":
            return subprocess.CompletedProcess(argv, 0, '["name=seccomp"]', "")
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(KernelError) as error:
        probe_container_engine(Path(sys.executable), engine="docker", runner=runner)
    assert error.value.code == "sandbox_resources_unavailable"
    assert len(calls) == 3
