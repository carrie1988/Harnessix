"""把Eval公开测试Profile确定性绑定为受信Process执行计划。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import BaseModel, JsonValue, ValidationError

from harnessix.agent.errors import KernelError
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.execution.contracts import (
    ExecutionCapabilityEvidenceV2,
    SandboxBindingV2,
    canonical_digest,
)
from harnessix.execution.planner import bind_environment, build_capability_evidence_v2
from harnessix.processes.supervision_contracts import ProcessSpec
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.processes.test_contracts import RunTestsInput, TestProfile, TestProfiles
from harnessix.trusted_actions.contracts import ActionRoutePlan, CanonicalActionResource
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    canonical_action_resource,
)
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import capture_workspace_snapshot

EVAL_RUN_TESTS_TOOL = "run_tests"
EVAL_RUN_TESTS_VERSION = "harnessix.eval-run-tests/v1"
_PROCESS_OUTPUT_BYTES = 128 * 1024
_ENVIRONMENT: Mapping[str, str] = {
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}


def _run_tests_schema(profile: str) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {"profile": {"type": "string", "const": profile}},
        "required": ["profile"],
        "additionalProperties": False,
    }


def _decode_run_tests(profile: str, arguments: dict[str, JsonValue]) -> RunTestsInput:
    try:
        checked = RunTestsInput.model_validate_json(
            json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        )
    except (ValidationError, ValueError, TypeError):
        raise ValueError("Eval测试Profile参数无效") from None
    if checked.profile != profile:
        raise ValueError("Eval测试Profile未注册")
    return checked


@dataclass(frozen=True, slots=True)
class FixedEvalActionEnvironment:
    """一次Eval运行冻结的Workspace、宿主能力与公开执行环境。"""

    root: Path
    workspace_id: str
    capabilities: ExecutionCapabilityEvidenceV2
    sandbox: SandboxBindingV2
    environment: Mapping[str, str]

    def workspace_root(self, workspace_id: str) -> Path:
        if workspace_id != self.workspace_id:
            raise KernelError("eval_action_workspace_mismatch", "Eval Action不属于固定Workspace")
        return self.root

    def context(self, thread_workspace: str) -> ActionPlanningContext:
        try:
            selected = Path(thread_workspace).resolve(strict=True)
        except (OSError, RuntimeError):
            raise KernelError(
                "eval_action_workspace_mismatch", "Eval Thread Workspace不可用"
            ) from None
        if selected != self.root:
            raise KernelError("eval_action_workspace_mismatch", "Eval Thread不属于固定Workspace")
        return ActionPlanningContext(
            workspace_root=self.root,
            sandbox=self.sandbox,
            capabilities=self.capabilities,
            environment=self.environment,
        )


@dataclass(frozen=True, slots=True)
class VerifiedEvalTestProfile:
    """由固定launcher、Profile和Process Owner能力共同证明的测试执行边界。"""

    profile: TestProfile
    launcher: Path
    supervisor: PosixProcessSupervisor
    environment: FixedEvalActionEnvironment
    executor_evidence_sha256: str


def _launcher_evidence(launcher: Path) -> dict[str, JsonValue]:
    try:
        selected = launcher.resolve(strict=True)
        info = launcher.lstat()
        body = launcher.read_bytes()
    except (OSError, RuntimeError):
        raise KernelError("eval_python_binding_invalid", "Eval Python launcher不可证明") from None
    if (
        selected != launcher
        or not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or not os.access(launcher, os.X_OK)
        or not body
    ):
        raise KernelError("eval_python_binding_invalid", "Eval Python launcher不可证明")
    return {
        "path": str(launcher),
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _build_owner(
    workspace: Path,
    launcher: Path,
    profile: TestProfile,
    supervisor: PosixProcessSupervisor,
) -> VerifiedEvalTestProfile:
    try:
        root = workspace.resolve(strict=True)
        checked_profiles = TestProfiles(profiles=(profile,))
    except (OSError, RuntimeError, ValidationError, ValueError, TypeError):
        raise KernelError("eval_test_profile_invalid", "Eval测试Profile或Workspace无效") from None
    checked = checked_profiles.profiles[0]
    if checked.program != "python" or supervisor.capability.platform != "posix":
        raise KernelError("eval_test_profile_invalid", "Eval测试Profile超出固定宿主能力")
    launcher_fact = _launcher_evidence(launcher)
    capabilities = build_capability_evidence_v2(
        platform="posix",
        provider="eval-native-process-owner",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("full",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest=supervisor.capability.digest,
    )
    profile_digest = canonical_digest(
        {
            "spec_version": "harnessix.eval-host-profile/v1",
            "profile": checked.model_dump(mode="json", warnings="error"),
            "launcher": launcher_fact,
            "owner_capability_sha256": supervisor.capability.digest,
            "environment": dict(_ENVIRONMENT),
            "network": "full",
        }
    )
    sandbox = SandboxBindingV2(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="full",
        capability_digest=capabilities.evidence_digest,
        profile_digest=profile_digest,
    )
    snapshot = capture_workspace_snapshot(root, platform="posix")
    environment = FixedEvalActionEnvironment(
        root=root,
        workspace_id=snapshot.workspace_id,
        capabilities=capabilities,
        sandbox=sandbox,
        environment=dict(_ENVIRONMENT),
    )
    evidence = canonical_digest(
        {
            "spec_version": "harnessix.eval-run-tests-executor-evidence/v1",
            "profile_sha256": profile_digest,
            "capability_sha256": capabilities.evidence_digest,
            "sandbox_sha256": canonical_digest(sandbox.model_dump(mode="json")),
        }
    )
    return VerifiedEvalTestProfile(
        profile=checked,
        launcher=launcher,
        supervisor=supervisor,
        environment=environment,
        executor_evidence_sha256=evidence,
    )


def _descriptor(owner: VerifiedEvalTestProfile) -> ToolDescriptor:
    profile = owner.profile
    return ToolDescriptor(
        name=EVAL_RUN_TESTS_TOOL,
        version=f"{EVAL_RUN_TESTS_VERSION}:{owner.executor_evidence_sha256[:24]}",
        description=(
            f"运行宿主冻结的历史评测测试Profile“{profile.name}”"
            f"（{profile.description}）；模型不能提交命令、参数或环境。"
        ),
        input_schema=_run_tests_schema(profile.name),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        requires_idempotency=True,
        requires_approval=True,
        supports_reconciliation=True,
        supports_parallel_calls=False,
    )


def _checked_arguments(value: BaseModel, owner: VerifiedEvalTestProfile) -> RunTestsInput:
    try:
        dumped = cast(dict[str, JsonValue], value.model_dump(mode="json"))
        return _decode_run_tests(owner.profile.name, dumped)
    except (ValidationError, ValueError, TypeError):
        raise KernelError(
            "eval_test_profile_arguments_invalid", "Eval测试Profile参数不一致"
        ) from None


def _resolved_action(
    owner: VerifiedEvalTestProfile,
    arguments: RunTestsInput,
    context: ActionPlanningContext,
) -> ResolvedAction:
    environment = owner.environment
    if (
        context.workspace_root != environment.root
        or context.cwd != "."
        or context.capabilities != environment.capabilities
        or context.sandbox != environment.sandbox
        or dict(context.environment) != dict(environment.environment)
        or context.secrets
        or context.external_roots is not None
    ):
        raise KernelError("eval_test_profile_context_mismatch", "Eval测试Profile规划上下文不匹配")
    return ResolvedAction(
        resources=(
            canonical_action_resource(
                kind="process",
                access="execute",
                identifier={"profile": arguments.profile},
                attributes={
                    "executor_evidence_sha256": owner.executor_evidence_sha256,
                    "owner_capability_sha256": owner.supervisor.capability.digest,
                },
            ),
            canonical_action_resource(
                kind="workspace",
                access="write",
                identifier={"location": "workspace", "path": "."},
                attributes={"mount": "host_guarded", "network": "full"},
            ),
            canonical_action_resource(
                kind="network",
                access="connect",
                identifier={"policy": "full"},
                attributes={"enforcement": "host_environment"},
            ),
        ),
        workspace_resources=(WorkspaceResourceRequest(path=".", access="write"),),
    )


def _sorted_resources(action: ResolvedAction) -> tuple[CanonicalActionResource, ...]:
    return tuple(
        sorted(
            action.resources,
            key=lambda item: (
                item.kind,
                item.access,
                item.identifier_sha256,
                item.attributes_sha256,
            ),
        )
    )


def _validate_route(
    owner: VerifiedEvalTestProfile,
    route: ActionRoutePlan,
    arguments: RunTestsInput,
) -> None:
    environment = owner.environment
    resolved = _resolved_action(
        owner,
        arguments,
        environment.context(
            str(environment.workspace_root(route.execution.workspace.workspace_id))
        ),
    )
    descriptor = _descriptor(owner)
    if (
        route.binding.tool != descriptor.name
        or route.binding.executor_id != "eval.run-tests"
        or route.invocation.arguments != arguments.model_dump(mode="json")
        or route.resources != _sorted_resources(resolved)
        or route.execution.capabilities != environment.capabilities
        or route.execution.sandbox != environment.sandbox
        or route.execution.environment
        != bind_environment(environment.environment, platform="posix")
        or route.execution.secrets
    ):
        raise KernelError("eval_test_profile_plan_mismatch", "Eval测试Profile与Action Route不匹配")


def _process_spec(owner: VerifiedEvalTestProfile, route: ActionRoutePlan) -> ProcessSpec:
    return build_process_spec(
        invocation="argv",
        argv=(str(owner.launcher), *owner.profile.arguments),
        terminal="pipe",
        stdin="closed",
        lifecycle="foreground",
        timeout_seconds=owner.profile.timeout_seconds,
        output_bytes=_PROCESS_OUTPUT_BYTES,
        input_bytes=0,
        process_id=route.execution.plan_id,
    )
