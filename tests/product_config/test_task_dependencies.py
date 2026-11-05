"""任务依赖环境仅接受宿主派生目录，持久化与隔离标记不能跨任务复用。"""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.product_config import task_dependencies
from harnessix.product_config.action_contracts import (
    ProductProcessProfile,
    build_product_process_profile,
)
from harnessix.product_config.contracts import SecretReference
from harnessix.product_config.process_action import (
    build_product_process_definition,
    process_profile_descriptor,
)
from harnessix.product_config.process_profile import probe_product_process_profile
from harnessix.product_config.task_dependencies import DependencyRoots, TaskDependencyEnvironments
from harnessix.trusted_actions.contracts import ActionRoutePlan, CodingActionInvocation
from harnessix.trusted_actions.router import ActionPlanningContext, TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from tests.product_config.test_command_profile import _command_profile
from tests.product_config.test_process_action import (
    _fake_engine,
    _NoSecrets,
    _probe_runner,
    _TerminalRuntime,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="依赖目录当前要求POSIX本机权限证明")


def _profile(*, version: str = "v1", persistent: bool = True) -> ProductProcessProfile:
    return build_product_process_profile(
        profile_id="command",
        version=version,
        description="离线依赖修复",
        container_engine="/bin/docker",
        image="sha256:" + "a" * 64,
        program="/bin/sh",
        arguments=("-c",),
        selector_policy="shell_command",
        persistent_dependencies=persistent,
    )


def _route(mapping: DependencyRoots) -> ActionRoutePlan:
    roots = tuple(
        SimpleNamespace(
            location=location,
            access=access,
            path_digest=canonical_digest(str(path)),
        )
        for location, (path, access) in mapping.items()
    )
    return cast(
        ActionRoutePlan,
        SimpleNamespace(execution=SimpleNamespace(workspace=SimpleNamespace(external_roots=roots))),
    )


@pytest.fixture
def registry(tmp_path: Path) -> TaskDependencyEnvironments:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return TaskDependencyEnvironments(state, workspace)


def _assert_invalid(call) -> None:
    with pytest.raises(KernelError) as failure:
        call()
    assert failure.value.code == "task_dependencies_invalid"


def test_dependency_configuration_is_opt_in_and_only_for_command_profiles() -> None:
    ordinary = _profile(persistent=False)
    assert "persistent_dependencies" not in ordinary.model_dump(mode="json")
    assert "/harnessix/deps" not in process_profile_descriptor(ordinary).description
    enabled = _profile()
    assert enabled.profile_sha256 != ordinary.profile_sha256
    assert "/harnessix/deps" in process_profile_descriptor(enabled).description
    with pytest.raises(ValidationError):
        build_product_process_profile(
            **ordinary.model_dump(exclude={"spec_version", "profile_sha256", "selector_policy"}),
            selector_policy="none",
            persistent_dependencies=True,
        )
    with pytest.raises(ValidationError):
        build_product_process_profile(
            **enabled.model_dump(exclude={"spec_version", "profile_sha256", "secret_refs"}),
            secret_refs=(SecretReference(name="TOKEN", version="v1"),),
        )


def test_stable_environment_keeps_packages_and_scopes_thread_and_profile(
    registry: TaskDependencyEnvironments, tmp_path: Path
) -> None:
    thread_id = uuid4()
    profile = _profile()
    mapping = registry.for_thread(thread_id, profile)
    leaf, access = mapping[f"deps_{thread_id.hex}"]
    assert (
        leaf
        == tmp_path / "state/task-dependencies" / thread_id.hex / profile.profile_sha256 / "deps"
    )
    assert access == ("read", "write", "execute")
    (leaf / "installed-package").write_text("persistent", encoding="utf-8")
    assert registry.for_thread(thread_id, profile) == mapping
    assert (leaf / "installed-package").read_text(encoding="utf-8") == "persistent"
    assert registry.for_route(_route(mapping), profile) == mapping
    assert next(iter(registry.for_thread(uuid4(), profile).values()))[0] != leaf
    assert next(iter(registry.for_thread(thread_id, _profile(version="v2")).values()))[0] != leaf
    for parent in (leaf.parent, leaf.parent.parent, leaf.parent.parent.parent):
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o777
    registry.validate(mapping, profile)


def test_for_route_never_creates_missing_environment(
    registry: TaskDependencyEnvironments, tmp_path: Path
) -> None:
    thread_id = uuid4()
    profile = _profile()
    leaf = tmp_path / "state/task-dependencies" / thread_id.hex / profile.profile_sha256 / "deps"
    route = _route({f"deps_{thread_id.hex}": (leaf, ("read", "write", "execute"))})
    _assert_invalid(lambda: registry.for_route(route, profile))
    assert not (tmp_path / "state/task-dependencies").exists()


@pytest.mark.parametrize("level", ["state", "task-dependencies", "thread", "profile", "leaf"])
def test_existing_permissions_are_rejected_without_repair(
    registry: TaskDependencyEnvironments, tmp_path: Path, level: str
) -> None:
    profile = _profile()
    thread_id = uuid4()
    mapping = registry.for_thread(thread_id, profile)
    leaf = next(iter(mapping.values()))[0]
    selected = {
        "state": tmp_path / "state",
        "task-dependencies": leaf.parents[2],
        "thread": leaf.parents[1],
        "profile": leaf.parent,
        "leaf": leaf,
    }[level]
    selected.chmod(0o755)
    _assert_invalid(lambda: registry.for_thread(thread_id, profile))
    assert stat.S_IMODE(selected.stat().st_mode) == 0o755


@pytest.mark.parametrize("level", ["task-dependencies", "thread", "profile", "leaf"])
def test_symlink_components_are_rejected(
    registry: TaskDependencyEnvironments, tmp_path: Path, level: str
) -> None:
    profile = _profile()
    thread_id = uuid4()
    mapping = registry.for_thread(thread_id, profile)
    leaf = next(iter(mapping.values()))[0]
    selected = {
        "task-dependencies": leaf.parents[2],
        "thread": leaf.parents[1],
        "profile": leaf.parent,
        "leaf": leaf,
    }[level]
    moved = tmp_path / "moved"
    selected.rename(moved)
    selected.symlink_to(moved, target_is_directory=True)
    _assert_invalid(lambda: registry.for_thread(thread_id, profile))
    _assert_invalid(lambda: registry.for_route(_route(mapping), profile))


def test_foreign_owner_is_rejected(
    registry: TaskDependencyEnvironments, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    mapping = registry.for_thread(uuid4(), profile)
    owner = os.getuid()
    monkeypatch.setattr(task_dependencies.os, "getuid", lambda: owner + 1)
    _assert_invalid(lambda: registry.validate(mapping, profile))


@pytest.mark.parametrize("reverse", [False, True])
def test_state_and_source_roots_cannot_overlap(tmp_path: Path, reverse: bool) -> None:
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    child = parent / "child"
    child.mkdir(mode=0o700)
    child.chmod(0o700)
    state, workspace = (parent, child) if reverse else (child, parent)
    _assert_invalid(lambda: TaskDependencyEnvironments(state, workspace))


def test_registry_roots_must_be_absolute_and_not_symlinks(tmp_path: Path) -> None:
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(state, target_is_directory=True)
    _assert_invalid(lambda: TaskDependencyEnvironments(alias, workspace))
    _assert_invalid(lambda: TaskDependencyEnvironments(Path("relative"), workspace))


def test_mapping_cannot_choose_roots_access_or_profile(
    registry: TaskDependencyEnvironments, tmp_path: Path
) -> None:
    profile = _profile()
    thread_id = UUID("12345678-1234-5678-1234-567812345678")
    mapping = registry.for_thread(thread_id, profile)
    location, (leaf, access) = next(iter(mapping.items()))
    invalid = (
        {},
        {"chosen": (leaf, access)},
        {location.upper(): (leaf, access)},
        {location: (tmp_path, access)},
        {location: (leaf, ("read",))},
        {location: (leaf, list(access))},
        {**mapping, "other": (leaf, access)},
    )
    for value in invalid:
        _assert_invalid(lambda value=value: registry.validate(value, profile))
    other = _profile(version="v2")
    registry.for_thread(thread_id, other)
    _assert_invalid(lambda: registry.validate(mapping, other))
    _assert_invalid(lambda: registry.for_route(_route(mapping), other))
    _assert_invalid(lambda: registry.for_thread(thread_id, _profile(persistent=False)))
    route = _route(mapping)
    route.execution.workspace.external_roots[0].access = ("read",)
    _assert_invalid(lambda: registry.for_route(route, profile))


def test_quarantine_persists_blocks_reuse_and_allows_existing_reconciliation(
    registry: TaskDependencyEnvironments, tmp_path: Path
) -> None:
    profile = _profile()
    thread_id = uuid4()
    mapping = registry.for_thread(thread_id, profile)
    route = _route(mapping)
    registry.quarantine(route, profile)
    registry.quarantine(route, profile)
    leaf = next(iter(mapping.values()))[0]
    marker = leaf.parent / ".quarantined"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    restarted = TaskDependencyEnvironments(tmp_path / "state", tmp_path / "workspace")
    for call in (
        lambda: restarted.for_thread(thread_id, profile),
        lambda: restarted.for_route(route, profile),
        lambda: restarted.validate(mapping, profile),
    ):
        with pytest.raises(KernelError) as failure:
            call()
        assert failure.value.code == "task_dependencies_quarantined"
    assert restarted.for_route(route, profile, allow_quarantined=True) == mapping
    assert marker.exists()
    assert restarted.for_thread(uuid4(), profile) != mapping


def test_invalid_quarantine_marker_is_not_repaired(
    registry: TaskDependencyEnvironments, tmp_path: Path
) -> None:
    profile = _profile()
    mapping = registry.for_thread(uuid4(), profile)
    leaf = next(iter(mapping.values()))[0]
    marker = leaf.parent / ".quarantined"
    target = tmp_path / "target"
    target.write_text("untouched", encoding="utf-8")
    marker.symlink_to(target)
    _assert_invalid(lambda: registry.quarantine(_route(mapping), profile))
    assert marker.is_symlink()
    assert target.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("failure", [None, "nonzero", "unknown", "cancelled", "cleanup_failed"])
async def test_dependency_actions_bind_persistent_roots_and_quarantine_uncertain_effects(
    tmp_path: Path, failure: str | None
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    registry = TaskDependencyEnvironments(state, workspace)
    profile = _command_profile(_fake_engine(tmp_path), persistent_dependencies=True)
    secrets = _NoSecrets()
    uncertain = failure in {"unknown", "cancelled", "cleanup_failed"}

    class DependencyRuntime(_TerminalRuntime):
        mappings: list[DependencyRoots] = []

        async def run(self, *args: Any, **kwargs: Any):
            mapping = kwargs["external_roots"]
            self.mappings.append(mapping)
            leaf = next(iter(mapping.values()))[0]
            marker = leaf / "package-state"
            if len(self.mappings) == 1:
                marker.write_text("available", encoding="utf-8")
            else:
                assert marker.read_text(encoding="utf-8") == "available"
            assert args[3].process.argv == ("/bin/sh", "-c", args[0].intent.arguments["cmd"])
            if failure == "unknown":
                raise KernelError("process_effect_unknown", "注入未决执行")
            lease = await super().run(*args, **kwargs)
            if failure == "cleanup_failed":
                raise KernelError("process_cleanup_failed", "注入未决清理")
            return lease

    async with PosixProcessSupervisor(tmp_path / "probe-owner") as supervisor:
        probe = probe_product_process_profile(
            profile, supervisor, secrets, probe_runner=_probe_runner
        )
        assert probe.verified is not None
        assert probe.verified.sandbox_profile.task_dependencies is True
        assert probe.verified.environment["HARNESSIX_DEPENDENCIES"] == "/harnessix/deps"
        runtime = DependencyRuntime(
            probe.verified.runtime.capability,
            returncode=-15 if failure == "cancelled" else 2 if failure == "nonzero" else 0,
            stop_reason="cancelled" if failure == "cancelled" else "exited",
        )
        owner = replace(probe.verified, runtime=cast(Any, runtime))

    with ExitStack() as cleanup:
        plans = cleanup.enter_context(SQLiteExecutionPlanStore(tmp_path / "plans.db"))
        audit = cleanup.enter_context(SQLiteActionAuditStore(tmp_path / "audit.db"))
        router = TrustedActionRouter(plans=plans, audit=audit, workspace_root=lambda _: workspace)
        with pytest.raises(KernelError, match="Registry"):
            build_product_process_definition(owner, router, lambda _: workspace, secrets)
        definition, executor, descriptor = build_product_process_definition(
            owner,
            router,
            lambda _: workspace,
            secrets,
            dependency_environments=registry,
        )
        router.register(definition)
        thread_id = uuid4()
        mapping = registry.for_thread(thread_id, profile)
        routes = []
        for number in range(1 if uncertain else 2):
            route = router.plan(
                CodingActionInvocation(
                    invocation_id=uuid4(),
                    source="builtin",
                    source_id="harnessix.product",
                    tool=descriptor.name,
                    tool_version=descriptor.version,
                    tool_fingerprint=definition.binding.tool_fingerprint,
                    arguments={"profile": profile.profile_id, "cmd": f"dependency step {number}"},
                    idempotency_key=f"dependency-step-{number}",
                ),
                ActionPlanningContext(
                    workspace_root=workspace,
                    sandbox=owner.sandbox,
                    capabilities=owner.capabilities,
                    environment=owner.environment,
                    secrets=owner.secret_bindings,
                    external_roots=registry.for_thread(thread_id, profile),
                ),
            )
            routes.append(route)
            assert {
                item.access
                for item in route.plan.execution.workspace.resources
                if item.location.startswith("deps_")
            } == {"read", "write", "execute"}
            assert {item.access for item in route.plan.resources if item.kind == "external"} == {
                "read",
                "write",
            }
            router.decide(
                route.plan.execution.plan_id,
                ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="dependency-test"),
            )
            outcome = await router.execute(route.plan.execution.plan_id)
            assert outcome.kind == (
                "unknown"
                if failure == "unknown"
                else "failed"
                if failure in {"cancelled", "nonzero"}
                else "succeeded"
            )
        assert all(item == mapping for item in runtime.mappings)
        leaf = next(iter(mapping.values()))[0]
        if uncertain:
            with pytest.raises(KernelError) as blocked:
                registry.for_thread(thread_id, profile)
            assert blocked.value.code == "task_dependencies_quarantined"
            await executor.reconcile(
                routes[0].plan, definition.decode_arguments(routes[0].plan.invocation.arguments)
            )
            assert (leaf.parent / ".quarantined").is_file()
        else:
            assert len(runtime.mappings) == 2
            assert (leaf / "package-state").read_text(encoding="utf-8") == "available"
            assert not (leaf.parent / ".quarantined").exists()
