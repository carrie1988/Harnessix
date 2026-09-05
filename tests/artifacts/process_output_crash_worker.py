"""在Process正文与Session终态联合事务的真实提交窗口退出。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from harnessix.agent.models import ProcessApprovalRequestContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.process_output import SQLiteProcessArtifactPublisher
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, Principal
from harnessix.domain.registry import ToolRegistry
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.models.scripted import ScriptedProvider
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.agent_runtime import ProcessAgentBridge
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.worker import ActionWorker
from tests.agent.helpers import answer


def _process_step(marker: Path):
    code = (
        "from pathlib import Path; import os, sys; p=Path(sys.argv[1]); "
        "p.write_text(str((int(p.read_text()) if p.exists() else 0) + 1)); "
        "os.write(1, b'crash-out-' + bytes([0, 255])); os.write(2, b'crash-err')"
    )
    return [
        ResponseStarted(response_id="process-output-crash"),
        ToolCallCompleted(
            call_id="process-output-crash",
            tool="host.process",
            arguments={
                "program": "python",
                "arguments": ["-I", "-c", code, str(marker)],
                "timeout_seconds": 5.0,
            },
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


async def main() -> None:
    root, session_path, effects_path, marker, point = sys.argv[1:]

    def fault(name: str) -> None:
        if name == "process_output." + point:
            os._exit(87)

    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(Path(root), {"python": sys.executable}))
    )
    actions = ActionService(
        journal=SQLiteEffectJournal(effects_path),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    await actions.initialize()
    bridge = ProcessAgentBridge(
        actions,
        Principal(
            tenant_id="tenant-a",
            subject_id="agent-a",
            framework="harnessix-agent",
        ),
    )
    session = SQLiteSessionStore(session_path)
    artifacts = SQLiteArtifactStore(session, fault=fault)
    async with CodingToolRuntime(Path(root), artifacts=artifacts) as tools:
        publisher = SQLiteProcessArtifactPublisher(
            artifacts, bridge, workspace_scope=tools.workspace_scope
        )
        async with AgentRuntime(
            session,
            ScriptedProvider([_process_step(Path(marker)), answer()]),
            scoped_tools=tools,
            artifacts=artifacts,
            processes=bridge,
            process_artifacts=publisher,
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            pending = await runtime.run_turn(
                thread.thread_id,
                "Process正文事务退出",
                request_id="process-output-crash",
            )
            approval = next(
                item.content
                for item in pending.items
                if isinstance(item.content, ProcessApprovalRequestContent)
            )
            await runtime.reply_approval(
                thread.thread_id,
                pending.turn_id,
                approval.approval_id,
                fingerprint=approval.request_fingerprint,
                decision=ApprovalDecision(
                    outcome=ApprovalOutcome.APPROVED,
                    actor="crash-fixture",
                ),
            )
            completed_action = await ActionWorker(
                actions,
                poll_seconds=0.01,
                heartbeat_seconds=1,
                recovery_interval_seconds=1,
            ).run_once()
            assert completed_action is not None
            await runtime.resume_turn(thread.thread_id, pending.turn_id)
    raise AssertionError("未到达Process Artifact退出点")


if __name__ == "__main__":
    asyncio.run(main())
