"""用户在工具规划与执行期间的介入：补充输入（Steering）与取消都不能把已知结果变成未知。

Steering依据ADR 0072第8条：当前Tool Result先完成，补充输入进入下一模型步骤。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    TextContent,
    Thread,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.processes.supervision_contracts import ProcessLease
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.processes.trusted_output import parse_trusted_process_output
from harnessix.product_config.action_composition import (
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import build_product_action_config
from harnessix.product_config.process_profile import (
    ProductProcessProfileProbeResult,
    probe_product_process_profile,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step, _approval, _proposal
from tests.product_config.test_process_action import (
    _CancellableRuntime,
    _fake_engine,
    _NoSecrets,
    _probe_runner,
    _profile,
    _TerminalRuntime,
)
from tests.product_config.test_product_patch_rollback import approve, product

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="固定Container Profile集成使用POSIX Owner能力"
)

_RUN_TESTS = [
    ResponseStarted(response_id="profile-response"),
    ToolCallCompleted(
        call_id="profile-call",
        tool="run_profile.unit-tests",
        arguments={"profile": "unit-tests", "selectors": ["tests/unit"]},
    ),
    ResponseCompleted(finish_reason="tool_calls"),
]


class _PausedRuntime(_TerminalRuntime):
    """进程开始后停在运行中，测试放行后才给出终态，用来在执行期间插入用户输入。"""

    def __init__(self, capability: Any) -> None:
        super().__init__(capability)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, *args: Any, **kwargs: Any) -> ProcessLease:
        self.started.set()
        await self.release.wait()
        return await super().run(*args, **kwargs)


@dataclass(frozen=True, slots=True)
class _CommandRun:
    """已批准、即将执行测试命令的Turn，以及检查其结果所需的对象。"""

    agent: AgentRuntime
    provider: ScriptedProvider
    process: Any
    sessions: SQLiteSessionStore
    artifacts: SQLiteArtifactStore
    workspace_scope: str
    thread: Thread
    approved: Turn

    async def output_text(self, result: ToolResultContent) -> bytes:
        assert isinstance(result.output, dict)
        page = await self.artifacts.read(
            self.thread.thread_id,
            self.workspace_scope,
            result.output["artifact"]["artifact_id"],
            limit=200,
        )
        document = parse_trusted_process_output(page.text.encode())
        return b"".join(chunk.data() for chunk in document.chunks)


@asynccontextmanager
async def _approved_command(tmp_path: Path, process_type: type) -> AsyncIterator[_CommandRun]:
    """真实产品装配下，模型请求运行单元测试且用户已批准；进程由测试替身控制。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = _profile(_fake_engine(tmp_path))
    secrets = _NoSecrets()
    async with PosixProcessSupervisor(tmp_path / "probe-owner") as supervisor:
        probe = probe_product_process_profile(
            profile, supervisor, secrets, probe_runner=_probe_runner
        )
        assert probe.verified is not None
        process = process_type(probe.verified.runtime.capability)
        verified = replace(probe.verified, runtime=cast(Any, process))

    sessions = SQLiteSessionStore(tmp_path / "sessions.db")
    artifacts = SQLiteArtifactStore(sessions)
    environment = build_fixed_product_action_environment(workspace)
    plans = SQLiteExecutionPlanStore(tmp_path / "plans.db")
    audit = SQLiteActionAuditStore(tmp_path / "audit.db")
    transactions = SQLiteWorkspaceTransactionStore(tmp_path / "delivery")
    leases = WorkspaceLeaseStore(tmp_path / "leases.db")
    router = TrustedActionRouter(
        plans=plans, audit=audit, workspace_root=environment.workspace_root
    )
    provider = ScriptedProvider([_RUN_TESTS, answer("测试完成")])
    try:
        async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
            composition = build_product_action_composition(
                build_product_action_config(
                    workspace_patch_enabled=False, process_profiles=(profile,)
                ),
                environment,
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
            assert composition.gateway is not None
            async with AgentRuntime(
                sessions,
                provider,
                scoped_tools=tools,
                artifacts=artifacts,
                trusted_actions=composition.gateway,
            ) as agent:
                thread = await agent.create_thread(str(workspace))
                waiting = await agent.run_turn(thread.thread_id, "运行单元测试", request_id="run")
                approval = next(
                    item.content
                    for item in waiting.items
                    if isinstance(item.content, TrustedActionApprovalRequestContent)
                )
                approved = await agent.reply_approval(
                    thread.thread_id,
                    waiting.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="reviewer"),
                )
                yield _CommandRun(
                    agent,
                    provider,
                    process,
                    sessions,
                    artifacts,
                    tools.workspace_scope,
                    thread,
                    approved,
                )
    finally:
        plans.close()
        audit.close()
        transactions.close()
        leases.close()


def _tool_result(turn: Turn) -> ToolResultContent:
    return next(item.content for item in turn.items if isinstance(item.content, ToolResultContent))


def _user_messages(provider: ScriptedProvider, request: int) -> list[str]:
    return [
        item.content.text
        for item in provider.requests[request].history
        if isinstance(item.content, TextContent) and item.content.kind == "user_message"
    ]


@posix_only
async def test_steering_while_a_command_runs_keeps_the_turn_and_its_output(tmp_path: Path) -> None:
    async with _approved_command(tmp_path, _PausedRuntime) as run:
        thread_id, turn_id = run.thread.thread_id, run.approved.turn_id
        running = asyncio.create_task(run.agent.resume_turn(thread_id, turn_id))
        await run.process.started.wait()
        steered = await run.agent.steer_turn(
            thread_id, turn_id, "先补 README", request_id="steer-1"
        )
        assert steered.status is TurnStatus.EXECUTING_TOOLS
        run.process.release.set()
        completed = await running

        assert completed.error is None
        assert completed.status is TurnStatus.COMPLETED
        result = _tool_result(completed)
        assert result.outcome == "succeeded"
        assert await run.output_text(result) == b"tests passed\n"
        # 当前步骤的工具结果先结算，补充输入随后进入下一次模型调用。
        assert run.process.run_calls == 1 and len(run.provider.requests) == 2
        assert _user_messages(run.provider, 1) == ["运行单元测试", "先补 README"]
        assert replay(await run.sessions.events(thread_id)) == await run.sessions.get_thread(
            thread_id
        )


async def test_steering_while_a_patch_is_prepared_keeps_its_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_review = SQLiteArtifactStore.publish_action_review
    stale_codes: list[str] = []

    async with product(tmp_path) as (root, runtime, provider, _router, _transactions, _, _):

        async def publish_after_user_input(
            store: SQLiteArtifactStore, thread_id: Any, turn_id: Any, *args: Any, **options: Any
        ) -> Any:
            # 负控：调用方快照之后若是模型步骤自身的事件而不是Steering，仍按冲突拒绝。
            earlier = {**options, "expected_sequence": options["expected_sequence"] - 1}
            with pytest.raises(KernelError) as conflict:
                await publish_review(store, thread_id, turn_id, *args, **earlier)
            stale_codes.append(conflict.value.code)
            await runtime.steer_turn(thread_id, turn_id, "顺便更新 README", request_id="steer-1")
            return await publish_review(store, thread_id, turn_id, *args, **options)

        monkeypatch.setattr(SQLiteArtifactStore, "publish_action_review", publish_after_user_input)
        provider.steps = (_action_step(_proposal()), answer("已处理"))
        thread = await runtime.create_thread(str(root))
        waiting = await runtime.run_turn(thread.thread_id, "修改文件", request_id="patch")

        assert stale_codes == ["sequence_conflict"]
        assert waiting.error is None and waiting.status is TurnStatus.WAITING_APPROVAL
        assert _approval(waiting).diff_artifact is not None
        completed = await approve(runtime, thread.thread_id, waiting)

        assert completed.error is None and completed.status is TurnStatus.COMPLETED
        assert (root / "src/modified.py").read_bytes() == b"new\n"
        assert _user_messages(provider, 1) == ["修改文件", "顺便更新 README"]


@posix_only
async def test_cancelling_a_running_command_ends_cancelled_with_a_known_result(
    tmp_path: Path,
) -> None:
    async with _approved_command(tmp_path, _CancellableRuntime) as run:
        thread_id, turn_id = run.thread.thread_id, run.approved.turn_id
        running = asyncio.create_task(run.agent.resume_turn(thread_id, turn_id))
        await run.process.started.wait()
        await run.agent.cancel(thread_id, turn_id)
        ended = await running

        # 进程已被终止并完成对账：这是确定的“已取消”，不是结果未知。
        assert ended.status is TurnStatus.CANCELLED
        assert ended.error is not None and ended.error.code == "cancelled"
        result = _tool_result(ended)
        assert result.outcome == "failed"
        assert result.error is not None and result.error.code == "process_cancelled"
        # 取消前命令已经产生的输出仍可读取，下一轮可以据此继续。
        assert await run.output_text(result) == b"cancelled\n"
        assert run.process.run_calls == 1 and len(run.provider.requests) == 1
        assert replay(await run.sessions.events(thread_id)) == await run.sessions.get_thread(
            thread_id
        )
