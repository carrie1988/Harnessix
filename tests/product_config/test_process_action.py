from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.product_config.action_contracts import (
    ProductProcessProfile,
    build_product_process_profile,
)
from harnessix.product_config.process_action import (
    build_product_process_definition,
    decode_run_profile,
)
from harnessix.product_config.process_profile import probe_product_process_profile
from harnessix.secrets.provider import SecretMaterial
from harnessix.trusted_actions.contracts import CodingActionInvocation
from harnessix.trusted_actions.router import ActionPlanningContext, TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore


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
