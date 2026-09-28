from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.sandbox.container import ContainerCommandBuilder
from harnessix.sandbox.process_runtime import ContainerProcessRuntime
from tests.sandbox.test_process_runtime import _execution_plan


@pytest.mark.skipif(os.name != "posix", reason="该启动负对照使用POSIX Process Owner")
@pytest.mark.parametrize(
    "resource",
    [
        b"[false,true,true,true]",
        b"[true,false,true,true]",
        b"[true,true,false,true]",
        b"[true,true,true,false]",
    ],
)
async def test_current_resource_failure_prevents_owner_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource: bytes
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = []

    def inspect(argv, timeout):
        calls.append(tuple(argv))
        if argv[1] == "info":
            assert timeout == 15.0
            return subprocess.CompletedProcess(argv, 0, resource, b"private-diagnostic-sentinel")
        assert tuple(argv[1:3]) == ("container", "ls")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        (probe, profile), execution, plan = _execution_plan(workspace, supervisor)

        async def unexpected_start(*args, **kwargs):
            pytest.fail("资源拒绝后不得启动Owner")

        monkeypatch.setattr(supervisor, "start_prepared", unexpected_start)
        builder = ContainerCommandBuilder(Path(sys.executable), probe, inspect_runner=inspect)
        runtime = ContainerProcessRuntime(builder, supervisor)
        with pytest.raises(KernelError) as error:
            await runtime.start(
                plan, None, profile, execution, workspace=workspace, environment={"LANG": "C"}
            )
        assert error.value.code == "sandbox_resources_unavailable"
        assert "private-diagnostic-sentinel" not in str(error.value)
        assert [item[1] for item in calls] == ["container", "info"]
        assert builder.cleanup_container(execution) == "absent"
        assert calls[-1][1] == "container"
