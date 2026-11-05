from __future__ import annotations

import csv
import sys
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.planner import build_execution_plan_v2
from harnessix.processes import supervisor as supervisor_module
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.sandbox.container import ContainerCommandBuilder
from harnessix.sandbox.contracts import ContainerSandboxProfile
from harnessix.sandbox.planner import (
    build_container_command,
    build_container_execution,
    build_container_sandbox_profile,
)
from harnessix.sandbox.process_runtime import ContainerProcessRuntime
from harnessix.workspace.contracts import ResourceAccess, WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot, verify_workspace_snapshot
from tests.sandbox.test_process_runtime import _execution_plan, _resource_inspect

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="使用POSIX目录身份和Owner能力")
LOCATION = "deps_" + "a" * 32
ACCESS: tuple[ResourceAccess, ...] = ("read", "write", "execute")
ExternalRoots = Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]]


@pytest.fixture
async def owner(tmp_path: Path) -> AsyncIterator[PosixProcessSupervisor]:
    async with PosixProcessSupervisor(tmp_path / "state") as supervisor:
        yield supervisor


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "main.py").write_text("print('source')\n", encoding="utf-8")
    return root


@pytest.fixture
def dependencies(tmp_path: Path) -> Path:
    root = tmp_path / "dependencies"
    root.mkdir()
    return root


def _plan(
    workspace: Path,
    owner: PosixProcessSupervisor,
    roots: ExternalRoots,
    *,
    enabled: bool = True,
    resources: tuple[WorkspaceResourceRequest, ...] | None = None,
    cwd: str = ".",
):
    (probe, old_profile), old_execution, old_plan = _execution_plan(workspace, owner)
    profile = build_container_sandbox_profile(
        image=old_profile.image,
        workspace_mode="read_only",
        network=old_profile.network,
        task_dependencies=enabled,
    )
    command = build_container_command(old_execution.command.argv, profile_digest=profile.digest)
    execution = build_container_execution(
        command, old_execution.process, owner_capability_digest=owner.capability.digest
    )
    snapshot = capture_workspace_snapshot(
        workspace,
        cwd=cwd,
        resources=(
            tuple(
                WorkspaceResourceRequest(location=location, path=".", access=access)
                for location, (_, access_modes) in roots.items()
                for access in access_modes
            )
            if resources is None
            else resources
        ),
        external_roots=roots,
    )
    plan = build_execution_plan_v2(
        old_plan.intent.model_copy(update={"arguments": execution.model_dump(mode="json")}),
        snapshot,
        environment={"LANG": "C"},
        secrets=(),
        sandbox=old_plan.sandbox.model_copy(update={"profile_digest": profile.digest}),
        policy=old_plan.policy,
        capabilities=old_plan.capabilities,
    )
    builder = ContainerCommandBuilder(Path(sys.executable), probe, inspect_runner=_resource_inspect)
    return builder, profile, execution, plan


def _prepare(builder, profile, execution, plan, workspace: Path, roots: ExternalRoots | None):
    return builder.prepare(
        plan,
        None,
        profile,
        workspace=workspace,
        command=execution,
        environment={"LANG": "C"},
        external_roots=roots,
    )


def test_dependency_mount_keeps_source_and_rootfs_read_only(
    workspace: Path, dependencies: Path, owner: PosixProcessSupervisor
) -> None:
    roots = {LOCATION: (dependencies / ".", ACCESS)}
    builder, profile, execution, plan = _plan(workspace, owner, roots)
    launch = _prepare(builder, profile, execution, plan, workspace, roots)
    mounts = [
        next(csv.reader([launch.argv[index + 1]]))
        for index, argument in enumerate(launch.argv)
        if argument == "--mount"
    ]
    assert mounts == [
        ["type=bind", f"src={workspace}", "dst=/workspace", "readonly"],
        ["type=bind", f"src={dependencies.resolve()}", "dst=/harnessix/deps"],
    ]
    assert "--read-only" in launch.argv
    assert not any(
        "docker.sock" in argument or "wheelhouse" in argument for argument in launch.argv
    )
    assert profile.model_dump(mode="json")["task_dependencies"] is True


def test_profile_opt_in_preserves_legacy_digest_and_rejects_custom_target(
    workspace: Path, owner: PosixProcessSupervisor
) -> None:
    (_, legacy), _, _ = _execution_plan(workspace, owner)
    payload = legacy.model_dump(mode="json", exclude={"digest"})
    assert "task_dependencies" not in payload
    assert legacy.digest == canonical_digest(payload)
    assert ContainerSandboxProfile.model_validate_json(legacy.model_dump_json()) == legacy
    with pytest.raises(ValidationError):
        ContainerSandboxProfile.model_validate(
            {**legacy.model_dump(), "dependency_target": "/model-selected"}
        )
    with pytest.raises(KernelError, check=lambda error: error.code == "sandbox_profile_invalid"):
        build_container_sandbox_profile(
            image=legacy.image,
            workspace_mode="read_write",
            network=legacy.network,
            task_dependencies=True,
        )


@pytest.mark.parametrize("location", [LOCATION, "arbitrary_root", "deps_" + "g" * 32])
def test_disabled_or_unknown_roots_are_rejected(
    workspace: Path, dependencies: Path, owner: PosixProcessSupervisor, location: str
) -> None:
    roots = {location: (dependencies, ACCESS)}
    builder, profile, execution, plan = _plan(workspace, owner, roots, enabled=location != LOCATION)
    with pytest.raises(
        KernelError, check=lambda error: error.code == "sandbox_capability_mismatch"
    ):
        _prepare(builder, profile, execution, plan, workspace, roots)


@pytest.mark.parametrize(
    "change", ["missing", "extra", "unknown", "access", "declaration", "declared_extra"]
)
def test_actual_root_mapping_must_match_the_single_snapshot_root(
    workspace: Path,
    dependencies: Path,
    owner: PosixProcessSupervisor,
    change: str,
) -> None:
    roots = {LOCATION: (dependencies, ACCESS)}
    declared = {} if change == "declaration" else roots
    if change == "declared_extra":
        declared = {**roots, "deps_" + "b" * 32: (dependencies, ACCESS)}
    builder, profile, execution, plan = _plan(workspace, owner, declared)
    actual = dict(roots)
    if change == "missing":
        actual.clear()
    elif change == "extra":
        actual["deps_" + "b" * 32] = (dependencies, ACCESS)
    elif change == "unknown":
        actual = {"deps_" + "b" * 32: (dependencies, ACCESS)}
    elif change == "access":
        actual[LOCATION] = (dependencies, ("read", "write"))
    with pytest.raises(
        KernelError, check=lambda error: error.code == "sandbox_capability_mismatch"
    ):
        _prepare(builder, profile, execution, plan, workspace, actual)


@pytest.mark.parametrize("missing", ACCESS)
def test_each_dependency_directory_permission_is_required(
    workspace: Path,
    dependencies: Path,
    owner: PosixProcessSupervisor,
    missing: ResourceAccess,
) -> None:
    roots = {LOCATION: (dependencies, ACCESS)}
    resources = tuple(
        WorkspaceResourceRequest(location=LOCATION, path=".", access=access)
        for access in ACCESS
        if access != missing
    )
    builder, profile, execution, plan = _plan(workspace, owner, roots, resources=resources)
    with pytest.raises(
        KernelError, check=lambda error: error.code == "sandbox_capability_mismatch"
    ):
        _prepare(builder, profile, execution, plan, workspace, roots)


def test_external_root_declaration_cannot_omit_execute(
    workspace: Path, dependencies: Path, owner: PosixProcessSupervisor
) -> None:
    roots = {LOCATION: (dependencies, ("read", "write"))}
    builder, profile, execution, plan = _plan(workspace, owner, roots)
    with pytest.raises(
        KernelError, check=lambda error: error.code == "sandbox_capability_mismatch"
    ):
        _prepare(builder, profile, execution, plan, workspace, roots)


def test_subdirectory_permission_cannot_replace_dependency_root_permission(
    workspace: Path, dependencies: Path, owner: PosixProcessSupervisor
) -> None:
    (dependencies / "nested").mkdir()
    roots = {LOCATION: (dependencies, ACCESS)}
    resources = tuple(
        WorkspaceResourceRequest(
            location=LOCATION, path="nested" if access == "write" else ".", access=access
        )
        for access in ACCESS
    )
    builder, profile, execution, plan = _plan(workspace, owner, roots, resources=resources)
    with pytest.raises(
        KernelError, check=lambda error: error.code == "sandbox_capability_mismatch"
    ):
        _prepare(builder, profile, execution, plan, workspace, roots)


@pytest.mark.parametrize("change", ["replace", "symlink"])
def test_dependency_root_replacement_or_symlink_is_rejected(
    workspace: Path,
    dependencies: Path,
    owner: PosixProcessSupervisor,
    change: str,
) -> None:
    roots = {LOCATION: (dependencies, ACCESS)}
    builder, profile, execution, plan = _plan(workspace, owner, roots)
    original = dependencies.with_name("original")
    dependencies.rename(original)
    if change == "replace":
        dependencies.mkdir()
    else:
        dependencies.symlink_to(original, target_is_directory=True)
    with pytest.raises(KernelError) as error:
        _prepare(builder, profile, execution, plan, workspace, roots)
    assert error.value.code == (
        "execution_plan_stale" if change == "replace" else "sandbox_workspace_invalid"
    )


@pytest.mark.parametrize("overlap", ["equal", "child", "parent"])
def test_dependency_root_cannot_overlap_source_in_either_direction(
    workspace: Path, owner: PosixProcessSupervisor, overlap: str
) -> None:
    dependency = workspace
    if overlap == "child":
        dependency = workspace / "deps"
        dependency.mkdir()
    elif overlap == "parent":
        dependency = workspace.parent
    roots = {LOCATION: (dependency, ACCESS)}
    builder, profile, execution, plan = _plan(workspace, owner, roots)
    with pytest.raises(KernelError, check=lambda error: error.code == "sandbox_workspace_invalid"):
        _prepare(builder, profile, execution, plan, workspace, roots)


def test_workspace_root_read_permission_is_still_required(
    workspace: Path, dependencies: Path, owner: PosixProcessSupervisor
) -> None:
    (workspace / "nested").mkdir()
    roots = {LOCATION: (dependencies, ACCESS)}
    builder, profile, execution, plan = _plan(workspace, owner, roots, cwd="nested")
    with pytest.raises(
        KernelError, check=lambda error: error.code == "sandbox_capability_mismatch"
    ):
        _prepare(builder, profile, execution, plan, workspace, roots)


async def test_runtime_passes_external_roots_to_supervisor_second_snapshot_check(
    workspace: Path,
    dependencies: Path,
    owner: PosixProcessSupervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = {LOCATION: (dependencies, ACCESS)}
    builder, profile, execution, plan = _plan(workspace, owner, roots)
    checked = []

    def verify(expected, root, *, external_roots=None):
        checked.append(external_roots)
        verify_workspace_snapshot(expected, root, external_roots=external_roots)
        raise KernelError("test_launch_blocked", "验证后停止，不启动进程")

    monkeypatch.setattr(builder, "ensure_container_absent", lambda _: None)
    monkeypatch.setattr(builder, "cleanup_container", lambda _: "absent")
    monkeypatch.setattr(supervisor_module, "verify_workspace_snapshot", verify)
    with pytest.raises(KernelError, check=lambda error: error.code == "test_launch_blocked"):
        await ContainerProcessRuntime(builder, owner).start(
            plan,
            None,
            profile,
            execution,
            workspace=workspace,
            environment={"LANG": "C"},
            external_roots=roots,
        )
    assert checked == [roots]
    with pytest.raises(KernelError, check=lambda error: error.code == "process_lease_not_found"):
        owner.status(execution.process.process_id)
