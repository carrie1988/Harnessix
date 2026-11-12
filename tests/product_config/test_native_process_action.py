"""外部边界原生命令经产品Action链路端到端执行（ADR 0121）。

ScriptedProvider只模拟模型提出命令；审批、规划、执行、输出发布都走产品链路，
进程经真实POSIX Process Owner执行。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from harnessix.agent.models import (
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.processes.supervisor import PosixProcessSupervisor, WindowsProcessSupervisor
from harnessix.processes.trusted_output import parse_trusted_process_output
from harnessix.product_config.action_composition import (
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import (
    ProductNativeProcessProfile,
    build_product_action_config,
)
from harnessix.product_config.native_process import (
    EXTERNAL_BOUNDARY_ENVIRONMENT,
    VerifiedNativeProcessProfile,
    build_native_process_definition,
    native_tool_name,
    probe_native_process_profile,
)
from harnessix.product_config.process_command import RunCommandInput
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.agent.helpers import answer
from tests.product_config.test_native_process_profile import native_profile

pytestmark = pytest.mark.skipif(os.name != "posix", reason="外部边界只支持POSIX Process Owner")
_ASSERTED = {EXTERNAL_BOUNDARY_ENVIRONMENT: "1"}


@dataclass
class _NativeCase:
    agent: AgentRuntime
    router: TrustedActionRouter
    artifacts: SQLiteArtifactStore
    workspace: Path
    workspace_scope: str
    thread_id: UUID
    turn_id: UUID
    approval: TrustedActionApprovalRequestContent
    owner: VerifiedNativeProcessProfile
    workspace_root: Callable[[str], Path]
    settled: Turn | None = None

    async def approve_and_resume(self) -> Turn:
        if self.approval.decision is not None:
            # 宿主已以系统身份自动批准（ADR 0113），轮次已执行完毕。
            assert self.settled is not None
            return self.settled
        await self.agent.reply_approval(
            self.thread_id,
            self.turn_id,
            self.approval.approval_id,
            fingerprint=self.approval.request_fingerprint,
            decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="native-reviewer"),
        )
        return await self.agent.resume_turn(self.thread_id, self.turn_id)

    async def stdout(self, result: ToolResultContent) -> bytes:
        assert isinstance(result.output, dict)
        reference = cast(dict[str, Any], result.output["artifact"])
        offset, pages = 0, []
        while True:
            page = await self.artifacts.read(
                self.thread_id,
                self.workspace_scope,
                reference["artifact_id"],
                offset=offset,
                limit=200,
            )
            pages.append(page.text.encode())
            if page.next_offset is None:
                break
            offset = page.next_offset
        document = parse_trusted_process_output(b"".join(pages))
        return b"".join(chunk.data() for chunk in document.chunks if chunk.stream == "stdout")


def _result(turn: Turn) -> ToolResultContent:
    return next(item.content for item in turn.items if isinstance(item.content, ToolResultContent))


@asynccontextmanager
async def _native_case(
    root: Path,
    cmd: str,
    profile: ProductNativeProcessProfile | None = None,
    **probe_options: Any,
) -> AsyncIterator[_NativeCase]:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "source.txt").write_text("base\n")
    profile = profile or native_profile()
    owner = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    async with owner(root / "owner") as supervisor:
        options = {"environ": _ASSERTED} | probe_options
        if callable(options.get("site")):
            options["site"] = options["site"](workspace)
        probe = probe_native_process_profile(profile, supervisor, **options)
        assert probe.verified is not None
        with ExitStack() as cleanup:
            sessions = SQLiteSessionStore(root / "sessions.db")
            artifacts = SQLiteArtifactStore(sessions)
            environment = build_fixed_product_action_environment(workspace)
            plans = SQLiteExecutionPlanStore(root / "plans.db")
            audit = SQLiteActionAuditStore(root / "audit.db")
            transactions = SQLiteWorkspaceTransactionStore(root / "delivery")
            leases = WorkspaceLeaseStore(root / "leases.db")
            for store in (plans, audit, transactions, leases):
                cleanup.callback(store.close)
            router = TrustedActionRouter(
                plans=plans, audit=audit, workspace_root=environment.workspace_root
            )
            provider = ScriptedProvider(
                [
                    [
                        ResponseStarted(response_id="native-response"),
                        ToolCallCompleted(
                            call_id="native-call",
                            tool=native_tool_name(profile),
                            arguments={"profile": profile.profile_id, "cmd": cmd}
                            | (
                                {"reason": "测试：需要在沙箱外执行"}
                                if profile.backend == "host"
                                else {}
                            ),
                        ),
                        ResponseCompleted(finish_reason="tool_calls"),
                    ],
                    answer("命令执行完成"),
                ]
            )
            async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
                composition = build_product_action_composition(
                    build_product_action_config(
                        workspace_patch_enabled=False, native_process_profiles=(profile,)
                    ),
                    environment,
                    router,
                    transactions,
                    leases,
                    artifacts,
                    artifact_workspace_scope=tools.workspace_scope,
                    native_probes=(probe,),
                )
                assert composition.gateway is not None
                async with AgentRuntime(
                    sessions,
                    provider,
                    scoped_tools=tools,
                    artifacts=artifacts,
                    trusted_actions=composition.gateway,
                    approval_responder=composition.approval_responder,
                ) as agent:
                    thread = await agent.create_thread(str(workspace))
                    waiting = await agent.run_turn(thread.thread_id, "运行命令", request_id="n")
                    approval = next(
                        item.content
                        for item in waiting.items
                        if isinstance(item.content, TrustedActionApprovalRequestContent)
                    )
                    assert approval.presentation == "process"
                    if approval.decision is None:
                        assert waiting.status is TurnStatus.WAITING_APPROVAL
                    yield _NativeCase(
                        agent,
                        router,
                        artifacts,
                        workspace,
                        tools.workspace_scope,
                        thread.thread_id,
                        waiting.turn_id,
                        approval,
                        probe.verified,
                        environment.workspace_root,
                        None if approval.decision is None else waiting,
                    )


async def test_command_writes_workspace_and_changes_persist(tmp_path: Path) -> None:
    cmd = "printf 'changed\\n' > source.txt; printf 'new\\n' > created.txt; echo done"
    async with _native_case(tmp_path, cmd) as case:
        assert not (case.workspace / "created.txt").exists()
        completed = await case.approve_and_resume()
        assert completed.status is TurnStatus.COMPLETED
        result = _result(completed)
        assert result.outcome == "succeeded"
        assert await case.stdout(result) == b"done\n"
        assert (case.workspace / "source.txt").read_text(encoding="utf-8") == "changed\n"
        assert (case.workspace / "created.txt").read_text(encoding="utf-8") == "new\n"
        plan = case.router.status(case.approval.plan_id).plan
        sandbox = plan.execution.sandbox
        assert (sandbox.level, sandbox.backend, sandbox.network) == ("host_guarded", "host", "full")
        assert plan.execution.capabilities.provider == "external-boundary"
        assert {(item.kind, item.access) for item in plan.resources} == {
            ("process", "execute"),
            ("workspace", "write"),
            ("network", "connect"),
        }
        # 再次恢复同一Turn不重放命令。
        assert await case.agent.resume_turn(case.thread_id, case.turn_id) == completed


async def test_reconcile_reads_ledger_without_replaying_command(tmp_path: Path) -> None:
    async with _native_case(tmp_path, "echo run >> count.txt; echo counted") as case:
        assert _result(await case.approve_and_resume()).outcome == "succeeded"
        route = case.router.status(case.approval.plan_id)
        # 重启后新建的执行器只凭Process账本对账。
        _, restarted, _ = build_native_process_definition(
            case.owner, case.router, case.workspace_root
        )
        arguments = RunCommandInput.model_validate(route.plan.invocation.arguments)
        outcome = await restarted.reconcile(route.plan, arguments)
        assert (outcome.kind, outcome.error_code) == ("succeeded", None)
        assert (case.workspace / "count.txt").read_text(encoding="utf-8") == "run\n"


async def test_command_gets_only_profile_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESSIX_TEST_MODEL_KEY", "must-not-leak")
    monkeypatch.setenv(EXTERNAL_BOUNDARY_ENVIRONMENT, "1")
    async with _native_case(tmp_path, "env | sort > env.txt") as case:
        completed = await case.approve_and_resume()
        assert _result(completed).outcome == "succeeded"
        names = {
            line.split("=", 1)[0]
            for line in (case.workspace / "env.txt").read_text(encoding="utf-8").splitlines()
        }
        assert "HARNESSIX_TEST_MODEL_KEY" not in names
        assert EXTERNAL_BOUNDARY_ENVIRONMENT not in names
        assert {"LANG", "LC_ALL", "PATH"} <= names


async def test_nonzero_exit_is_reported_like_container_commands(tmp_path: Path) -> None:
    async with _native_case(tmp_path, "echo failing >&2; exit 7") as case:
        result = _result(await case.approve_and_resume())
        assert result.outcome == "failed"
        assert result.error is not None and result.error.code == "process_nonzero_exit"


async def test_profile_timeout_stops_command(tmp_path: Path) -> None:
    profile = native_profile(timeout_seconds=1)
    async with _native_case(tmp_path, "sleep 30; touch late.txt", profile) as case:
        result = _result(await case.approve_and_resume())
        assert result.outcome == "failed"
        assert result.error is not None and result.error.code == "process_timeout"
        assert not (case.workspace / "late.txt").exists()


async def test_unasserted_boundary_omits_command_tool(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = native_profile()
    async with PosixProcessSupervisor(tmp_path / "owner") as supervisor:
        probe = probe_native_process_profile(profile, supervisor, environ={})
        with ExitStack() as cleanup:
            sessions = SQLiteSessionStore(tmp_path / "sessions.db")
            plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
            audit = SQLiteActionAuditStore(tmp_path / "audit.db")
            transactions = SQLiteWorkspaceTransactionStore(tmp_path / "delivery")
            leases = WorkspaceLeaseStore(tmp_path / "leases.db")
            for store in (plans, audit, transactions, leases):
                cleanup.callback(store.close)
            environment = build_fixed_product_action_environment(workspace)
            composition = build_product_action_composition(
                build_product_action_config(
                    workspace_patch_enabled=False, native_process_profiles=(profile,)
                ),
                environment,
                TrustedActionRouter(
                    plans=plans, audit=audit, workspace_root=environment.workspace_root
                ),
                transactions,
                leases,
                SQLiteArtifactStore(sessions),
                artifact_workspace_scope="scope",
                native_probes=(probe,),
            )
    assert composition.gateway is None
    assert composition.catalog.definitions() == ()
    (native,) = (
        item
        for item in composition.report.capabilities
        if item.capability_id == "exec_command.task-shell"
    )
    assert (native.status, native.reason_code) == ("omitted", "external_boundary_unasserted")
