from __future__ import annotations

from pathlib import Path

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import ToolCallContent, ToolResultContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.coding_tool_view import CodingWorkflowToolView
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer


def _descriptor(name: str, *, parallel: bool = False) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        version="1",
        description=name,
        input_schema={"type": "object"},
        effect_class=EffectClass.READ_ONLY,
        risk_level=RiskLevel.LOW,
        requires_idempotency=False,
        requires_approval=False,
        supports_reconciliation=False,
        supports_parallel_calls=parallel,
    )


class _SuccessfulCodingTools:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def definitions(self) -> tuple[ToolDescriptor, ...]:
        return tuple(
            _descriptor(name)
            for name in (
                "read_file",
                "grep",
                "glob",
                "apply_patch",
                "run_profile.public-tests",
                "git_status",
                "git_diff",
            )
        )

    async def execute(
        self,
        call: ToolCallContent,
        cancel: CancelToken,
    ) -> ToolResultContent:
        cancel.checkpoint()
        self.calls.append(call.tool)
        output = {"path": call.arguments.get("path")} if call.tool == "read_file" else {}
        return ToolResultContent(call_id=call.call_id, outcome="succeeded", output=output)


def _tool_step(index: int, tool: str, arguments: dict[str, object]) -> list[object]:
    return [
        ResponseStarted(response_id=f"response-{index}"),
        ToolCallCompleted(call_id=f"call-{index}", tool=tool, arguments=arguments),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


async def test_runtime_drives_patch_test_and_git_review_to_completion(tmp_path: Path) -> None:
    tools = _SuccessfulCodingTools()
    steps = [
        *(_tool_step(index, "read_file", {"path": f"src/file_{index}.py"}) for index in range(4)),
        _tool_step(4, "apply_patch", {"path": "src/file_0.py"}),
        _tool_step(5, "run_profile.public-tests", {"profile": "public-tests"}),
        _tool_step(6, "git_status", {}),
        _tool_step(7, "git_diff", {}),
        answer("完成"),
    ]
    provider = ScriptedProvider(steps)
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "session.db"),
        provider,
        tools,
        model_tool_view=CodingWorkflowToolView(require_pre_edit_profile=False),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "修复公开回归", request_id="workflow")

    assert turn.status is TurnStatus.COMPLETED
    assert tools.calls == [
        "read_file",
        "read_file",
        "read_file",
        "read_file",
        "apply_patch",
        "run_profile.public-tests",
        "git_status",
        "git_diff",
    ]
    assert provider.requests[4].tools[0].name == "apply_patch"
    assert provider.requests[5].forced_tool == "run_profile.public-tests"
    assert provider.requests[6].forced_tool == "git_status"
    assert provider.requests[7].forced_tool == "git_diff"
    assert provider.requests[8].tools == ()


async def test_runtime_forces_patch_after_bounded_catalog_rejection(tmp_path: Path) -> None:
    tools = _SuccessfulCodingTools()
    steps = [
        *(_tool_step(index, "read_file", {"path": f"src/file_{index}.py"}) for index in range(4)),
        _tool_step(4, "read_file", {"path": "src/hidden-after-boundary.py"}),
        _tool_step(5, "apply_patch", {"path": "src/file_0.py"}),
        _tool_step(6, "run_profile.public-tests", {"profile": "public-tests"}),
        _tool_step(7, "git_status", {}),
        _tool_step(8, "git_diff", {}),
        answer("完成"),
    ]
    provider = ScriptedProvider(steps)
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "rejected.db"),
        provider,
        tools,
        model_tool_view=CodingWorkflowToolView(require_pre_edit_profile=False),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "修复公开回归", request_id="recovery")

    assert turn.status is TurnStatus.COMPLETED
    assert tools.calls == [
        "read_file",
        "read_file",
        "read_file",
        "read_file",
        "apply_patch",
        "run_profile.public-tests",
        "git_status",
        "git_diff",
    ]
    assert provider.requests[4].forced_tool is None
    assert provider.requests[5].forced_tool == "apply_patch"
