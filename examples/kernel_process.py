"""离线模型调用→唯一Action审批→外部Worker→事务输出归档→恢复模型循环。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

from harnessix.agent.models import (
    ProcessApprovalRequestContent,
    ToolResultContent,
    TurnStatus,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.process_output import SQLiteProcessArtifactPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import (
    ApprovalDecision,
    ApprovalOutcome,
    Principal,
)
from harnessix.domain.registry import ToolRegistry
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.agent_runtime import ProcessAgentBridge
from harnessix.processes.output_artifact import parse_process_output_document
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.worker import ActionWorker


class ProcessFixtureProvider:
    async def stream(self, request, cancel):
        cancel.checkpoint()
        yield ResponseStarted(response_id=f"process-{request.step}")
        if request.step == 1:
            yield ToolCallCompleted(
                call_id="process-call",
                tool="host.process",
                arguments={
                    "program": "python",
                    "arguments": ["-I", "-c", "print('process output')"],
                    "timeout_seconds": 5.0,
                },
            )
            yield ResponseCompleted(finish_reason="tool_calls")
            return
        result = next(
            item.content for item in request.history if isinstance(item.content, ToolResultContent)
        )
        assert result.outcome == "succeeded"
        assert result.output["stdout"]["observed_bytes"] == len(b"process output\n")
        assert result.output["artifact"]["format"] == "jsonl/v1"
        yield TextStarted(content_id="answer")
        yield TextCompleted(content_id="answer", text="固定程序已完成。")
        yield ResponseCompleted()


async def exercise(root: Path) -> None:
    workspace = root / "workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(workspace, {"python": sys.executable}))
    )
    actions = ActionService(
        journal=SQLiteEffectJournal(root / "effects.db"),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    await actions.initialize()
    processes = ProcessAgentBridge(
        actions,
        Principal(
            tenant_id="example",
            subject_id="coding-agent",
            framework="harnessix-agent",
            roles=("developer",),
        ),
    )
    sessions = SQLiteSessionStore(root / "session.db")
    artifacts = SQLiteArtifactStore(sessions)
    try:
        async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
            process_artifacts = SQLiteProcessArtifactPublisher(
                artifacts,
                processes,
                workspace_scope=tools.workspace_scope,
            )
            async with AgentRuntime(
                sessions,
                ProcessFixtureProvider(),
                scoped_tools=tools,
                artifacts=artifacts,
                processes=processes,
                process_artifacts=process_artifacts,
            ) as runtime:
                thread = await runtime.create_thread(str(tools.workspace_root))
                pending = await runtime.run_turn(
                    thread.thread_id,
                    "运行宿主固定Python并检查结果",
                    request_id="kernel-process",
                )
                assert pending.status is TurnStatus.WAITING_APPROVAL
                approval = next(
                    item.content
                    for item in pending.items
                    if isinstance(item.content, ProcessApprovalRequestContent)
                )
                waiting = await runtime.reply_approval(
                    thread.thread_id,
                    pending.turn_id,
                    approval.approval_id,
                    fingerprint=approval.request_fingerprint,
                    decision=ApprovalDecision(
                        outcome=ApprovalOutcome.APPROVED,
                        actor="example-reviewer",
                    ),
                )
                assert waiting.status is TurnStatus.WAITING_ACTION
                completed_action = await ActionWorker(
                    actions,
                    poll_seconds=0.01,
                    heartbeat_seconds=1,
                    recovery_interval_seconds=1,
                ).run_once()
                assert completed_action is not None
                completed = await runtime.resume_turn(thread.thread_id, pending.turn_id)
                assert completed.status is TurnStatus.COMPLETED
            result = next(
                item.content
                for item in completed.items
                if isinstance(item.content, ToolResultContent) and item.content.process is not None
            )
            assert isinstance(result.output, dict)
            page = await artifacts.read(
                thread.thread_id,
                tools.workspace_scope,
                UUID(result.output["artifact"]["artifact_id"]),
                limit=200,
            )
            document = parse_process_output_document(page.text.encode())
            assert (
                b"".join(chunk.data() for chunk in document.chunks if chunk.stream == "stdout")
                == b"process output\n"
            )
        assert replay(await sessions.events(thread.thread_id)) == await sessions.get_thread(
            thread.thread_id
        )
    finally:
        await actions.close()
    print("唯一Action审批、外部Worker、事务输出归档、结果投影和Replay通过；无模型API。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-kernel-process-") as directory:
        asyncio.run(exercise(Path(directory)))


if __name__ == "__main__":
    main()
