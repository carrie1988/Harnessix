"""真实产品装配/Executor/SQLite Artifact链复用原版本Secret快照；Container Owner为合同替身。"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, cast

import pytest

from harnessix.agent.errors import KernelError
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.product_config.action_composition import (
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import (
    build_product_action_config,
    build_product_process_profile,
)
from harnessix.product_config.action_runtime import _close_composition
from harnessix.product_config.contracts import SecretReference
from harnessix.product_config.process_profile import (
    ProductProcessProfileProbeResult,
    probe_product_process_profile,
)
from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions.contracts import CodingActionInvocation
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.product_config.test_process_action import (
    _fake_engine,
    _probe_runner,
    _profile,
    _TerminalRuntime,
)


@pytest.mark.skipif(os.name != "posix", reason="该产品Container Owner合同装配仅适用于POSIX")
@pytest.mark.parametrize("case", ["live", "closed"])
async def test_product_executor_uses_same_frozen_secret_after_host_rotation(tmp_path, case):
    root = tmp_path / "workspace"
    root.mkdir()
    plain = "frozen-product-scope-original-value"
    environment = {"TOKEN": plain}
    secrets = EnvironmentSecretProvider(
        (EnvironmentSecretSource("TOKEN", "7", "TOKEN"),), environment=environment
    )
    base = _profile(_fake_engine(tmp_path))
    values = base.model_dump(exclude={"profile_sha256", "spec_version"})
    values["secret_refs"] = (SecretReference(name="TOKEN", version="7"),)
    profile = build_product_process_profile(**values)
    async with PosixProcessSupervisor(tmp_path / "probe") as supervisor:
        probe = probe_product_process_profile(
            profile, supervisor, secrets, probe_runner=_probe_runner
        )
        assert probe.verified is not None

        class Runtime(_TerminalRuntime):
            async def run(self, plan, checkpoint, profile, execution, **kwargs):
                assert kwargs["secrets"].as_text() == {"TOKEN": plain}
                return await super().run(plan, checkpoint, profile, execution, **kwargs)

        runtime = Runtime(probe.verified.runtime.capability)
        verified = replace(probe.verified, runtime=cast(Any, runtime))
    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    await sessions.initialize()
    artifacts = SQLiteArtifactStore(sessions)
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    transactions = SQLiteWorkspaceTransactionStore(tmp_path / "transactions")
    leases = WorkspaceLeaseStore(tmp_path / "leases")
    fixed = build_fixed_product_action_environment(root)
    router = TrustedActionRouter(plans=plans, audit=audit, workspace_root=fixed.workspace_root)
    try:
        async with CodingToolRuntime(root, artifacts=artifacts) as tools:
            composition = build_product_action_composition(
                build_product_action_config(
                    workspace_patch_enabled=False, process_profiles=(profile,)
                ),
                fixed,
                router,
                transactions,
                leases,
                artifacts,
                artifact_workspace_scope=tools.workspace_scope,
                process_probes=(
                    ProductProcessProfileProbeResult(
                        profile=profile, reason_code="verified", verified=verified
                    ),
                ),
                secrets=secrets,
            )
            scope = composition.gateway._state.secret_scope
            assert scope is not None
            environment["TOKEN"] = "changed-during-product-session"
            binding = router.bindings()[0]
            from uuid import uuid4

            context = composition.gateway._state.context
            from tests.trusted_actions.test_agent_gateway import agent_state

            thread, turn, call = agent_state(root)
            call = call.model_copy(update={"tool": binding.tool})
            route = router.plan(
                CodingActionInvocation(
                    invocation_id=uuid4(),
                    source=binding.source,
                    source_id=binding.source_id,
                    tool=binding.tool,
                    tool_version=binding.tool_version,
                    tool_fingerprint=binding.tool_fingerprint,
                    arguments={"profile": "unit-tests", "selectors": ["tests/unit"]},
                    idempotency_key="secret-profile-once",
                ),
                context(thread, turn, call),
            )
            router.decide(
                route.plan.execution.plan_id,
                ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="test"),
            )
            if case == "closed":
                scope.close()
            outcome = await router.execute(route.plan.execution.plan_id)
            if case == "closed":
                assert outcome.kind == "failed" and outcome.error_code == "process_preflight_failed"
                assert runtime.run_calls == runtime.reconcile_calls == 0
            else:
                assert outcome.kind == "succeeded" and runtime.run_calls == 1
                scope.assert_safe(
                    outcome.output, route.plan.execution.secrets, checkpoint=lambda: None
                )
            _close_composition(composition)
            with pytest.raises(KernelError) as closed:
                scope.resolve("TOKEN")
            assert closed.value.code == "trusted_action_secret_unavailable"
    finally:
        plans.close()
        audit.close()
        transactions.close()
        leases.close()


@pytest.mark.parametrize("mode", ["normal", "startup-failure"])
async def test_product_owner_scope_cleanup_even_without_agent_runtime(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace

    from harnessix.product_config import action_runtime
    from harnessix.secrets.publication import SecretPublicationScope
    from harnessix.trusted_actions.agent_gateway import RouterBackedAgentActionGateway
    from harnessix.trusted_actions.contracts import ActionExecutionOutcome
    from tests.trusted_actions.test_agent_gateway import (
        FakeExecutor,
        build_gateway,
        descriptor,
        runtime_context,
    )
    from tests.trusted_actions.test_secret_publication import BINDING, source

    root = tmp_path / "workspace"
    root.mkdir()
    scope = SecretPublicationScope((BINDING,), source())
    _, action_router, plans, audit = build_gateway(
        root, FakeExecutor(ActionExecutionOutcome(kind="succeeded"))
    )
    gate = RouterBackedAgentActionGateway(
        action_router,
        (descriptor(),),
        lambda *_: runtime_context(root),
        secret_scope=scope,
        owns_secret_scope=True,
    )
    # 组合替身只隔离能力构造；Owner、Gateway、Scope、SQLite和资源退出均运行真实实现。
    monkeypatch.setattr(
        action_runtime,
        "build_product_action_composition",
        lambda *_args, **_kwargs: SimpleNamespace(gateway=gate),
    )
    if mode == "startup-failure":

        async def fail_scan(**_kwargs):
            raise KernelError("test_startup_failure", "启动扫描故障")

        monkeypatch.setattr(action_runtime, "scan_product_action_recovery", fail_scan)
    session = SQLiteSessionStore(tmp_path / "sessions.db")
    artifacts = SQLiteArtifactStore(session)
    try:
        context = action_runtime.open_default_product_action_runtime(
            tmp_path / "owner",
            root,
            artifacts,
            source(),
            build_product_action_config(workspace_patch_enabled=False),
            artifact_workspace_scope="fixture-scope",
        )
        if mode == "startup-failure":
            with pytest.raises(KernelError, match="启动扫描故障"):
                async with context:
                    pytest.fail("失败启动不可发布Owner")
        else:
            async with context as owner:
                assert owner.gateway is gate and not scope._closed
        with pytest.raises(KernelError) as caught:
            scope.resolve("registry")
        assert caught.value.code == "trusted_action_secret_unavailable" and scope._closed
    finally:
        gate.close()
        plans.close()
        audit.close()
