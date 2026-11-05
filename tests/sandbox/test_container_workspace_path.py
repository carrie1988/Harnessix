from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.planner import build_execution_plan_v2
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.sandbox.container import ContainerCommandBuilder
from harnessix.sandbox.contracts import ContainerSandboxProfile, WorkspaceMountMode
from harnessix.sandbox.planner import (
    build_container_command,
    build_container_execution,
    build_container_sandbox_profile,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot
from tests.sandbox.test_process_runtime import _execution_plan, _resource_inspect

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="容器工作区路径只支持POSIX")


def _launch_argv(
    workspace: Path,
    owner: PosixProcessSupervisor,
    path: str,
    cwd: str = ".",
    *,
    mode: WorkspaceMountMode = "read_only",
    environment: tuple[tuple[str, str], ...] = (),
    snapshot_root: Path | None = None,
):
    (probe, legacy), legacy_execution, legacy_plan = _execution_plan(workspace, owner)
    profile = build_container_sandbox_profile(
        image=legacy.image,
        workspace_mode=mode,
        network=legacy.network,
        workspace_path=path,
        environment=environment,
    )
    command = build_container_command(legacy_execution.command.argv, profile_digest=profile.digest)
    execution = build_container_execution(
        command, legacy_execution.process, owner_capability_digest=owner.capability.digest
    )
    snapshot = capture_workspace_snapshot(
        workspace,
        cwd=cwd,
        resources=(WorkspaceResourceRequest(location="workspace", path=".", access="read"),),
    )
    plan = build_execution_plan_v2(
        legacy_plan.intent.model_copy(update={"arguments": execution.model_dump(mode="json")}),
        snapshot,
        environment={"LANG": "C"},
        secrets=(),
        sandbox=legacy_plan.sandbox.model_copy(update={"profile_digest": profile.digest}),
        policy=legacy_plan.policy,
        capabilities=legacy_plan.capabilities,
    )
    builder = ContainerCommandBuilder(
        Path(sys.executable), probe, inspect_runner=_resource_inspect, snapshot_root=snapshot_root
    )
    launch = builder.prepare(
        plan, None, profile, workspace=workspace, command=execution, environment={"LANG": "C"}
    )
    return launch.argv, builder, execution


@pytest.fixture
async def owner(tmp_path: Path):
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        yield supervisor


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "main.py").write_text("print('source')\n", encoding="utf-8")
    return root


@pytest.mark.parametrize(("cwd", "workdir"), [(".", "/testbed"), ("pkg", "/testbed/pkg")])
def test_configured_path_is_the_read_only_mount_target_and_working_directory(
    workspace: Path, owner: PosixProcessSupervisor, cwd: str, workdir: str
) -> None:
    argv, _, _ = _launch_argv(workspace, owner, "/testbed", cwd)
    mounts = [next(csv.reader([argv[i + 1]])) for i, arg in enumerate(argv) if arg == "--mount"]
    assert mounts == [["type=bind", f"src={workspace}", "dst=/testbed", "readonly"]]
    assert argv[argv.index("--workdir") + 1] == workdir
    assert "noexec" in argv[argv.index("--tmpfs") + 1]
    assert "--read-only" in argv


def test_default_path_keeps_legacy_digest_and_mount(
    workspace: Path, owner: PosixProcessSupervisor
) -> None:
    (_, legacy), _, _ = _execution_plan(workspace, owner)
    payload = legacy.model_dump(mode="json", exclude={"digest"})
    assert "workspace_path" not in payload
    assert legacy.digest == canonical_digest(payload)
    assert legacy.workspace_path == "/workspace"
    argv, _, _ = _launch_argv(workspace, owner, "/workspace")
    assert "dst=/workspace" in argv[argv.index("--mount") + 1]
    custom = build_container_sandbox_profile(
        image=legacy.image,
        workspace_mode="read_only",
        network=legacy.network,
        workspace_path="/testbed",
    )
    assert custom.digest != legacy.digest
    assert ContainerSandboxProfile.model_validate_json(custom.model_dump_json()) == custom


@pytest.mark.parametrize(
    "path",
    [
        "testbed",
        "/",
        "/tmp",
        "/tmp/src",
        "/harnessix/deps",
        "/proc",
        "/dev/x",
        "/a/../b",
        "/a//b",
        "/a/b/",
        "/a/./b",
        "/with space",
        "/" + "a" * 300,
    ],
)
def test_unsafe_or_ambiguous_paths_are_rejected(
    workspace: Path, owner: PosixProcessSupervisor, path: str
) -> None:
    (_, legacy), _, _ = _execution_plan(workspace, owner)
    with pytest.raises(KernelError, check=lambda error: error.code == "sandbox_profile_invalid"):
        build_container_sandbox_profile(
            image=legacy.image,
            workspace_mode="read_only",
            network=legacy.network,
            workspace_path=path,
        )
    with pytest.raises(ValidationError):
        ContainerSandboxProfile.model_validate({**legacy.model_dump(), "workspace_path": path})


def _mounts(argv: tuple[str, ...]) -> list[list[str]]:
    return [next(csv.reader([argv[i + 1]])) for i, arg in enumerate(argv) if arg == "--mount"]


def test_snapshot_mode_mounts_a_disposable_writable_copy(
    workspace: Path, owner: PosixProcessSupervisor, tmp_path: Path
) -> None:
    snapshots = tmp_path / "snapshots"
    argv, builder, execution = _launch_argv(
        workspace, owner, "/testbed", mode="snapshot", snapshot_root=snapshots
    )
    copy = snapshots / str(execution.process.process_id)
    assert _mounts(argv) == [["type=bind", f"src={copy}", "dst=/testbed"]]
    assert "--read-only" in argv  # 根文件系统仍只读
    tmpfs = argv[argv.index("--tmpfs") + 1]
    assert tmpfs.startswith("/tmp:rw,nosuid,nodev,") and "noexec" not in tmpfs

    builder.create_workspace_snapshot(execution, workspace, "65532:65532")
    assert (copy / "pkg" / "main.py").read_text() == "print('source')\n"
    (copy / "pkg" / "main.py").write_text("changed\n")  # 副本可写
    assert (workspace / "pkg" / "main.py").read_text() == "print('source')\n"

    builder.remove_workspace_snapshot(execution)
    assert not copy.exists() and workspace.exists()


def test_snapshot_mode_requires_a_host_snapshot_root(
    workspace: Path, owner: PosixProcessSupervisor
) -> None:
    with pytest.raises(
        KernelError, check=lambda error: error.code == "sandbox_capability_mismatch"
    ):
        _launch_argv(workspace, owner, "/testbed", mode="snapshot")


def test_environment_presets_become_fixed_container_variables(
    workspace: Path, owner: PosixProcessSupervisor
) -> None:
    presets = (("GOFLAGS", "-mod=mod"), ("HOME", "/tmp"))
    argv, _, _ = _launch_argv(workspace, owner, "/testbed", environment=presets)
    values = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--env"]
    assert values == ["LANG", "GOFLAGS=-mod=mod", "HOME=/tmp"]

    with pytest.raises(
        KernelError, check=lambda error: error.code == "sandbox_environment_invalid"
    ):
        _launch_argv(workspace, owner, "/testbed", environment=(("LANG", "C.UTF-8"),))


@pytest.mark.parametrize(
    "environment",
    [
        (("HOME", "/tmp"), ("GOFLAGS", "x")),  # 未排序
        (("HOME", "/tmp"), ("HOME", "/root")),  # 重复
        (("HTTPS_PROXY", "http://x"),),  # 受管网络变量
        (("HARNESSIX_DEPENDENCIES", "/x"),),  # 平台变量
        (("1BAD", "x"),),
        (("HOME", "line\nbreak"),),
    ],
)
def test_invalid_environment_presets_are_rejected(
    workspace: Path, owner: PosixProcessSupervisor, environment: tuple[tuple[str, str], ...]
) -> None:
    (_, legacy), _, _ = _execution_plan(workspace, owner)
    with pytest.raises(KernelError, check=lambda error: error.code == "sandbox_profile_invalid"):
        build_container_sandbox_profile(
            image=legacy.image,
            workspace_mode="read_only",
            network=legacy.network,
            environment=environment,
        )
