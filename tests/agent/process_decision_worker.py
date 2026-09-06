"""跨进程竞争同一Process Action审批的夹具。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from harnessix.agent.approvals import approval_for
from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ProcessApprovalRequestContent
from harnessix.agent.patching import inspection_scope
from harnessix.agent.reducer import get_turn, pending_calls
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, Principal
from harnessix.domain.registry import ToolRegistry
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.agent_runtime import ProcessAgentBridge
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.storage import SQLiteEffectJournal


async def main() -> None:
    root, session_path, effects_path, barrier = map(Path, sys.argv[1:5])
    outcome = ApprovalOutcome(sys.argv[5])
    actor = sys.argv[6]
    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    service = ActionService(
        journal=SQLiteEffectJournal(effects_path),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
        auto_execute=False,
    )
    try:
        await service.initialize()
        bridge = ProcessAgentBridge(
            service,
            Principal(
                tenant_id="tenant-a",
                subject_id="agent-a",
                framework="harnessix-agent",
            ),
        )
        store = SQLiteSessionStore(session_path)
        await store.initialize()
        thread_id = (await store.thread_ids())[0]
        thread = await store.get_thread(thread_id)
        assert thread.active_turn_id is not None
        turn = get_turn(thread, thread.active_turn_id)
        call = pending_calls(turn)[0]
        item = approval_for(turn, call)
        assert item is not None and isinstance(item.content, ProcessApprovalRequestContent)
        for _ in range(500):
            if barrier.exists():
                break
            await asyncio.sleep(0.01)
        else:
            raise TimeoutError("审批竞态屏障未释放")
        try:
            projected = await bridge.decide(
                call,
                inspection_scope(thread, turn, call),
                item.content,
                ApprovalDecision(outcome=outcome, actor=actor),
                CancelToken(),
            )
        except KernelError as error:
            print(json.dumps({"ok": False, "code": error.code}))
        else:
            assert projected.decision is not None
            print(
                json.dumps(
                    {
                        "ok": True,
                        "outcome": projected.decision.outcome.value,
                        "actor": projected.decision.actor,
                    }
                )
            )
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
