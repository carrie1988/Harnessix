"""持久进程Action：显式注册、人工批准、真实执行和Effect Journal事件。"""

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.domain.models import (
    ActionContext,
    ActionRequest,
    ApprovalDecision,
    ApprovalOutcome,
    Principal,
)
from harnessix.domain.registry import ToolRegistry
from harnessix.policy import DefaultPolicyEngine
from harnessix.processes.action_executor import process_action_tool
from harnessix.processes.contracts import ProcessResult
from harnessix.processes.runtime import HostProcessRuntime
from harnessix.runtime import ActionService
from harnessix.storage import SQLiteEffectJournal


async def exercise(root: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        process_action_tool(lambda: HostProcessRuntime(root, {"python": sys.executable}))
    )
    service = ActionService(
        journal=SQLiteEffectJournal(root / "effects.db"),
        registry=registry,
        policy_engine=DefaultPolicyEngine(),
    )
    await service.initialize()
    request = ActionRequest(
        tool="host.process",
        arguments={
            "program": "python",
            "arguments": ["-I", "-c", "print('approved process action')"],
            "timeout_seconds": 5.0,
        },
        principal=Principal(tenant_id="example", subject_id="developer", framework="host"),
        context=ActionContext(session_id="example", run_id="process-action"),
        idempotency_key="example:process-action",
    )
    pending = await service.submit(request)
    assert pending.status.value == "pending_approval"
    completed = await service.decide_approval(
        request.action_id,
        ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="example-reviewer"),
    )
    assert completed.result is not None and completed.result.receipt is not None
    result = ProcessResult.model_validate(completed.result.output)
    assert result.returncode == 0 and result.stdout.text().strip() == "approved process action"
    events = await service.events(request.action_id)
    assert events[-1].to_status.value == "succeeded"
    await service.close()
    print("持久意图、审批、进程结果、Effect Receipt和事件链验收通过。")
    print("此入口不是模型Shell；硬退出后的RUNNING只恢复UNKNOWN，不重放命令。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-process-action-") as directory:
        asyncio.run(exercise(Path(directory)))


if __name__ == "__main__":
    main()
