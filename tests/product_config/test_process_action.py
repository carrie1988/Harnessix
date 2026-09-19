from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, utc_now
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.processes.supervision_contracts import ProcessLease, ProcessOutputObservation
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.processes.trusted_output import parse_trusted_process_output
from harnessix.product_config.action_composition import (
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import (
    ProductProcessProfile,
    build_product_action_config,
    build_product_process_profile,
)
from harnessix.product_config.process_action import (
    build_product_process_definition,
    decode_run_profile,
)
from harnessix.product_config.process_profile import (
    ProductProcessProfileProbeResult,
    probe_product_process_profile,
)
from harnessix.secrets.provider import SecretMaterial
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions.contracts import CodingActionInvocation
from harnessix.trusted_actions.router import ActionPlanningContext, TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.agent.helpers import answer


class _NoSecrets:
    def resolve(self, name: str) -> SecretMaterial:
        raise AssertionError(f"无Secret Profile不应解析{name}")


class _PreflightFailureRuntime:
    def __init__(self, capability: object) -> None:
        self.capability = capability
        self.calls = 0

    async def run(self, *_: object, **__: object) -> object:
        self.calls += 1
        raise KernelError("sandbox_binding_changed", "测试注入的确定性前置失败")

    async def reconcile(self, _: object) -> object:
        raise KernelError("process_lease_not_found", "测试进程尚未启动")

    def status(self, _: object) -> object:
        raise KernelError("process_lease_not_found", "测试进程尚未启动")

    async def output(self, *_: object) -> bytes:
        raise AssertionError("前置失败不应读取输出")


class _TerminalRuntime:
    """以真实Lease合同模拟Container Owner终态，不绕过Executor和Artifact链。"""

    def __init__(
        self,
        capability: Any,
        stdout: bytes = b"tests passed\n",
        *,
        returncode: int = 0,
        stop_reason: str = "exited",
    ) -> None:
        self.capability = capability
        self.stdout = stdout
        self.returncode = returncode
        self.stop_reason = stop_reason
        self.lease: ProcessLease | None = None
        self.run_calls = 0
        self.reconcile_calls = 0

    async def run(
        self,
        plan: Any,
        _checkpoint: object,
        _profile: object,
        execution: Any,
        **_kwargs: Any,
    ) -> ProcessLease:
        self.run_calls += 1
        now = utc_now()
        stdout = _observation(self.stdout)
        stderr = _observation(b"")
        self.lease = ProcessLease(
            process_id=plan.plan_id,
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            process_spec_digest=execution.process.digest,
            capability_digest=self.capability.digest,
            launch_binding_digest="9" * 64,
            lifecycle="foreground",
            state="exited",
            sequence=4,
            owner_token="7" * 64,
            owner_identity="8" * 64,
            pid=4321,
            deadline=now + timedelta(minutes=1),
            started_at=now,
            finished_at=now,
            returncode=self.returncode,
            stop_reason=self.stop_reason,
            stdout=stdout,
            stderr=stderr,
        )
        return self.lease

    async def reconcile(self, _: object) -> ProcessLease:
        self.reconcile_calls += 1
        if self.lease is None:
            raise KernelError("process_lease_not_found", "测试Process尚未执行")
        return self.lease

    def status(self, _: object) -> ProcessLease:
        if self.lease is None:
            raise KernelError("process_lease_not_found", "测试Process尚未执行")
        return self.lease

    async def output(self, _: object, stream: str) -> bytes:
        return self.stdout if stream == "stdout" else b""


class _CancellableRuntime(_TerminalRuntime):
    def __init__(self, capability: Any) -> None:
        super().__init__(capability, stdout=b"cancelled\n", returncode=-15)
        self.started = asyncio.Event()

    async def run(
        self,
        plan: Any,
        _checkpoint: object,
        _profile: object,
        execution: Any,
        **_kwargs: Any,
    ) -> ProcessLease:
        self.run_calls += 1
        now = utc_now()
        self.lease = ProcessLease(
            process_id=plan.plan_id,
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            process_spec_digest=execution.process.digest,
            capability_digest=self.capability.digest,
            launch_binding_digest="9" * 64,
            lifecycle="foreground",
            state="running",
            sequence=2,
            owner_token="7" * 64,
            owner_identity="8" * 64,
            pid=4321,
            deadline=now + timedelta(minutes=1),
            started_at=now,
        )
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.lease = self.lease.model_copy(
                update={
                    "state": "exited",
                    "sequence": 4,
                    "finished_at": utc_now(),
                    "returncode": self.returncode,
                    "stop_reason": "cancelled",
                    "stdout": _observation(self.stdout),
                    "stderr": _observation(b""),
                }
            )
            self.lease = ProcessLease.model_validate_json(self.lease.model_dump_json())
            raise


def _observation(body: bytes) -> ProcessOutputObservation:
    return ProcessOutputObservation(
        observed_bytes=len(body),
        persisted_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        persisted_sha256=hashlib.sha256(body).hexdigest(),
        truncated=False,
        eof=True,
    )


def _profile(engine: Path) -> ProductProcessProfile:
    return build_product_process_profile(
        profile_id="unit-tests",
        version="2026.09.1",
        description="在固定容器中运行单元测试",
        container_engine=str(engine),
        image="registry.example/harnessix/tests@sha256:" + "a" * 64,
        program="/usr/bin/python",
        arguments=("-m", "pytest"),
        selector_policy="bounded_test_selector",
    )


def _probe_runner(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    assert timeout == 15.0
    if argv[1] == "version":
        output = "28.3.2|28.3.2\n"
    elif argv[1] == "info":
        output = '["name=seccomp"]\n'
    else:
        assert argv[1:3] == ("image", "inspect")
        image = argv[-1]
        output = json.dumps([image]) + "\n"
    return subprocess.CompletedProcess(argv, 0, output, "")


def _fake_engine(root: Path) -> Path:
    path = root / ("docker.exe" if os.name == "nt" else "docker")
    path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_run_profile_rejects_shell_and_path_escape_selectors() -> None:
    for selector in ("../tests", "tests/unit;touch pwned", "-k", "/tmp/test.py"):
        with pytest.raises(ValueError):
            decode_run_profile(
                "unit-tests",
                "bounded_test_selector",
                {"profile": "unit-tests", "selectors": [selector]},
            )


@pytest.mark.skipif(os.name != "posix", reason="该候选链使用POSIX Process Owner能力")
async def test_verified_process_profile_builds_exact_route_and_fails_before_effect(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = _fake_engine(tmp_path)
    profile = _profile(engine)
    secrets = _NoSecrets()

    async with PosixProcessSupervisor(tmp_path / "process-state") as supervisor:
        probe = probe_product_process_profile(
            profile,
            supervisor,
            secrets,
            probe_runner=_probe_runner,
        )
        assert probe.reason_code == "verified" and probe.verified is not None
        runtime = _PreflightFailureRuntime(probe.verified.runtime.capability)
        owner = replace(probe.verified, runtime=cast(Any, runtime))
        plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
        audit = SQLiteActionAuditStore(tmp_path / "audit.db")
        router = TrustedActionRouter(
            plans=plans,
            audit=audit,
            workspace_root=lambda _: workspace,
        )
        definition, _, descriptor = build_product_process_definition(
            owner,
            router,
            lambda _: workspace,
            secrets,
        )
        router.register(definition)
        invocation = CodingActionInvocation(
            invocation_id=uuid4(),
            source="builtin",
            source_id="harnessix.product",
            tool=descriptor.name,
            tool_version=descriptor.version,
            tool_fingerprint=definition.binding.tool_fingerprint,
            arguments={"profile": "unit-tests", "selectors": ["tests/unit"]},
            idempotency_key="unit-tests-once",
        )
        context = ActionPlanningContext(
            workspace_root=workspace,
            sandbox=owner.sandbox,
            capabilities=owner.capabilities,
            environment=owner.environment,
            secrets=owner.secret_bindings,
        )
        route = router.plan(invocation, context)
        assert route.state == "pending_approval"
        assert {item.kind for item in route.plan.resources} == {"process", "workspace"}
        router.decide(
            route.plan.execution.plan_id,
            ApprovalDecision(
                outcome=ApprovalOutcome.APPROVED,
                actor="unit-test",
                reason="验证确定性前置失败",
            ),
        )
        outcome = await router.execute(route.plan.execution.plan_id)
        assert outcome.kind == "failed"
        assert outcome.error_code == "process_preflight_failed"
        assert runtime.calls == 1
        assert router.status(route.plan.execution.plan_id).state == "failed"
        plans.close()
        audit.close()


@pytest.mark.skipif(os.name != "posix", reason="固定Container Profile测试使用POSIX Owner能力")
@pytest.mark.parametrize(
    ("returncode", "stop_reason", "expected_error"),
    [
        (2, "exited", "process_nonzero_exit"),
        (-15, "timeout", "process_timeout"),
        (-15, "output_limit", "process_output_limit"),
    ],
)
async def test_process_terminal_failures_are_stable_and_never_replayed(
    tmp_path: Path,
    returncode: int,
    stop_reason: str,
    expected_error: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = _profile(_fake_engine(tmp_path))
    secrets = _NoSecrets()
    async with PosixProcessSupervisor(tmp_path / "process-state") as supervisor:
        probe = probe_product_process_profile(
            profile,
            supervisor,
            secrets,
            probe_runner=_probe_runner,
        )
        assert probe.verified is not None
        runtime = _TerminalRuntime(
            probe.verified.runtime.capability,
            returncode=returncode,
            stop_reason=stop_reason,
        )
        owner = replace(probe.verified, runtime=cast(Any, runtime))
        plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
        audit = SQLiteActionAuditStore(tmp_path / "audit.db")
        router = TrustedActionRouter(
            plans=plans,
            audit=audit,
            workspace_root=lambda _: workspace,
        )
        definition, _, descriptor = build_product_process_definition(
            owner,
            router,
            lambda _: workspace,
            secrets,
        )
        router.register(definition)
        route = router.plan(
            CodingActionInvocation(
                invocation_id=uuid4(),
                source="builtin",
                source_id="harnessix.product",
                tool=descriptor.name,
                tool_version=descriptor.version,
                tool_fingerprint=definition.binding.tool_fingerprint,
                arguments={"profile": "unit-tests", "selectors": []},
                idempotency_key="terminal-failure-once",
            ),
            ActionPlanningContext(
                workspace_root=workspace,
                sandbox=owner.sandbox,
                capabilities=owner.capabilities,
                environment=owner.environment,
                secrets=owner.secret_bindings,
            ),
        )
        router.decide(
            route.plan.execution.plan_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="unit-test"),
        )
        outcome = await router.execute(route.plan.execution.plan_id)
        assert outcome.kind == "failed" and outcome.error_code == expected_error
        assert runtime.run_calls == 1 and runtime.reconcile_calls == 0
        assert router.status(route.plan.execution.plan_id).state == "failed"
        plans.close()
        audit.close()


@pytest.mark.skipif(os.name != "posix", reason="固定Container Profile测试使用POSIX Owner能力")
async def test_cancelled_process_becomes_unknown_then_reconciles_without_replay(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = _profile(_fake_engine(tmp_path))
    secrets = _NoSecrets()
    async with PosixProcessSupervisor(tmp_path / "process-state") as supervisor:
        probe = probe_product_process_profile(
            profile,
            supervisor,
            secrets,
            probe_runner=_probe_runner,
        )
        assert probe.verified is not None
        runtime = _CancellableRuntime(probe.verified.runtime.capability)
        owner = replace(probe.verified, runtime=cast(Any, runtime))
        plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
        audit = SQLiteActionAuditStore(tmp_path / "audit.db")
        router = TrustedActionRouter(
            plans=plans,
            audit=audit,
            workspace_root=lambda _: workspace,
        )
        definition, _, descriptor = build_product_process_definition(
            owner,
            router,
            lambda _: workspace,
            secrets,
        )
        router.register(definition)
        route = router.plan(
            CodingActionInvocation(
                invocation_id=uuid4(),
                source="builtin",
                source_id="harnessix.product",
                tool=descriptor.name,
                tool_version=descriptor.version,
                tool_fingerprint=definition.binding.tool_fingerprint,
                arguments={"profile": "unit-tests", "selectors": []},
                idempotency_key="cancelled-process-once",
            ),
            ActionPlanningContext(
                workspace_root=workspace,
                sandbox=owner.sandbox,
                capabilities=owner.capabilities,
                environment=owner.environment,
                secrets=owner.secret_bindings,
            ),
        )
        plan_id = route.plan.execution.plan_id
        router.decide(
            plan_id,
            ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="unit-test"),
        )
        execution = asyncio.create_task(router.execute(plan_id))
        await asyncio.wait_for(runtime.started.wait(), timeout=1)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert router.status(plan_id).state == "unknown"

        outcome = await router.reconcile(plan_id)

        assert outcome.kind == "failed" and outcome.error_code == "process_cancelled"
        assert router.status(plan_id).state == "failed"
        assert runtime.run_calls == 1 and runtime.reconcile_calls == 1
        plans.close()
        audit.close()


@pytest.mark.skipif(os.name != "posix", reason="该候选链使用POSIX Process Owner能力")
async def test_process_profile_probe_omits_unattested_image(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path)
    profile = _profile(engine)

    def missing_image(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        completed = _probe_runner(argv, timeout)
        if argv[1] == "image":
            return subprocess.CompletedProcess(argv, 0, "[]\n", "")
        return completed

    async with PosixProcessSupervisor(tmp_path / "process-state") as supervisor:
        probe = probe_product_process_profile(
            profile,
            supervisor,
            _NoSecrets(),
            probe_runner=missing_image,
        )
    assert probe.verified is None
    assert probe.reason_code == "container_image_unavailable"


def test_product_composition_omits_unverified_profile_without_host_fallback(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = _profile(_fake_engine(tmp_path))
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    artifacts = SQLiteArtifactStore(sessions)
    environment = build_fixed_product_action_environment(workspace)
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    transactions = SQLiteWorkspaceTransactionStore(tmp_path / "delivery")
    leases = WorkspaceLeaseStore(tmp_path / "leases.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=environment.workspace_root,
    )

    composition = build_product_action_composition(
        build_product_action_config(
            workspace_patch_enabled=False,
            process_profiles=(profile,),
        ),
        environment,
        router,
        transactions,
        leases,
        artifacts,
        artifact_workspace_scope="0" * 64,
        process_probes=(
            ProductProcessProfileProbeResult(
                profile=profile,
                reason_code="container_image_unavailable",
            ),
        ),
    )

    process = next(
        item
        for item in composition.report.capabilities
        if item.capability_id == "run_profile.unit-tests"
    )
    assert process.status == "omitted"
    assert process.reason_code == "container_image_unavailable"
    assert composition.gateway is None and router.bindings() == ()
    plans.close()
    audit.close()
    transactions.close()
    leases.close()


@pytest.mark.skipif(os.name != "posix", reason="固定Container Profile集成使用POSIX Owner能力")
async def test_product_composition_executes_approved_profile_and_publishes_output_once(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = _profile(_fake_engine(tmp_path))
    secrets = _NoSecrets()
    async with PosixProcessSupervisor(tmp_path / "probe-owner") as supervisor:
        probe = probe_product_process_profile(
            profile,
            supervisor,
            secrets,
            probe_runner=_probe_runner,
        )
        assert probe.verified is not None
        runtime = _TerminalRuntime(probe.verified.runtime.capability)
        verified = replace(probe.verified, runtime=cast(Any, runtime))

    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    confirmation_lost = False

    def lose_first_confirmation(point: str) -> None:
        nonlocal confirmation_lost
        if point == "action_output.after_commit" and not confirmation_lost:
            confirmation_lost = True
            raise OSError("模拟Action输出提交确认丢失")

    artifacts = SQLiteArtifactStore(sessions, fault=lose_first_confirmation)
    environment = build_fixed_product_action_environment(workspace)
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    transactions = SQLiteWorkspaceTransactionStore(tmp_path / "delivery")
    leases = WorkspaceLeaseStore(tmp_path / "leases.db")
    router = TrustedActionRouter(
        plans=plans,
        audit=audit,
        workspace_root=environment.workspace_root,
    )
    action = [
        ResponseStarted(response_id="profile-response"),
        ToolCallCompleted(
            call_id="profile-call",
            tool="run_profile.unit-tests",
            arguments={"profile": "unit-tests", "selectors": ["tests/unit"]},
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]
    provider = ScriptedProvider([action, answer("测试完成")])

    async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
        composition = build_product_action_composition(
            build_product_action_config(
                workspace_patch_enabled=False,
                process_profiles=(profile,),
            ),
            environment,
            router,
            transactions,
            leases,
            artifacts,
            artifact_workspace_scope=tools.workspace_scope,
            process_probes=(
                ProductProcessProfileProbeResult(
                    profile=profile,
                    reason_code="verified",
                    verified=verified,
                ),
            ),
            secrets=secrets,
        )
        assert composition.gateway is not None
        assert [item.name for item in composition.catalog.definitions()] == [
            "run_profile.unit-tests"
        ]
        async with AgentRuntime(
            sessions,
            provider,
            scoped_tools=tools,
            artifacts=artifacts,
            trusted_actions=composition.gateway,
        ) as agent:
            thread = await agent.create_thread(str(workspace))
            waiting = await agent.run_turn(
                thread.thread_id,
                "运行单元测试",
                request_id="run-profile",
            )
            approval = next(
                item.content
                for item in waiting.items
                if isinstance(item.content, TrustedActionApprovalRequestContent)
            )
            assert waiting.status is TurnStatus.WAITING_APPROVAL
            assert approval.presentation == "process" and approval.diff_artifact is None
            await agent.reply_approval(
                thread.thread_id,
                waiting.turn_id,
                approval.approval_id,
                fingerprint=approval.request_fingerprint,
                decision=ApprovalDecision(
                    outcome=ApprovalOutcome.APPROVED,
                    actor="process-reviewer",
                ),
            )
            completed = await agent.resume_turn(thread.thread_id, waiting.turn_id)
            result = next(
                item.content
                for item in completed.items
                if isinstance(item.content, ToolResultContent)
            )
            assert completed.status is TurnStatus.COMPLETED
            assert result.outcome == "succeeded"
            assert isinstance(result.output, dict)
            artifact = result.output["artifact"]
            page = await artifacts.read(
                thread.thread_id,
                tools.workspace_scope,
                artifact["artifact_id"],
                limit=200,
            )
            assert "tests passed" not in result.output
            document = parse_trusted_process_output(page.text.encode())
            assert b"".join(item.data() for item in document.chunks) == b"tests passed\n"

    assert confirmation_lost
    assert runtime.run_calls == 1 and runtime.reconcile_calls == 0
    plans.close()
    audit.close()
    transactions.close()
    leases.close()
