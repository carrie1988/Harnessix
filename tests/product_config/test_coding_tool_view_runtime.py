from __future__ import annotations

from pathlib import Path

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import AgentFailure, ToolCallContent, ToolResultContent, TurnStatus
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
                "git_review",
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
        _tool_step(6, "git_review", {}),
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
        "git_review",
    ]
    assert {tool.name for tool in provider.requests[4].tools} == {
        "read_file",
        "grep",
        "glob",
        "apply_patch",
    }
    assert provider.requests[4].forced_tool is None
    assert provider.requests[5].forced_tool == "run_profile.public-tests"
    assert provider.requests[6].forced_tool == "git_review"
    assert provider.requests[7].tools == ()


async def test_failed_validation_supports_search_reselection_repair_and_durable_review(
    tmp_path: Path,
) -> None:
    class FailedValidationTools(_SuccessfulCodingTools):
        def __init__(self):
            super().__init__()
            self.validations = 0

        async def execute(self, call, cancel):
            result = await super().execute(call, cancel)
            if call.tool == "run_profile.public-tests":
                self.validations += 1
                if self.validations <= 2:
                    return result.model_copy(
                        update={
                            "outcome": "failed",
                            "error": AgentFailure(
                                code="process_nonzero_exit", message="check failed"
                            ),
                        }
                    )
            return result

    tools = FailedValidationTools()
    steps = [
        _tool_step(0, "apply_patch", {"path": "src/model.py"}),
        _tool_step(1, "run_profile.public-tests", {"profile": "public-tests"}),
        _tool_step(2, "grep", {"path": "tests", "query": "test_model"}),
        _tool_step(3, "glob", {"path": "tests", "pattern": "*model*"}),
        _tool_step(4, "run_profile.public-tests", {"profile": "public-tests"}),
        _tool_step(5, "read_file", {"path": "src/model.py"}),
        _tool_step(6, "apply_patch", {"path": "src/model.py"}),
        _tool_step(7, "run_profile.public-tests", {"profile": "public-tests"}),
        _tool_step(8, "git_review", {}),
        answer("完成"),
    ]
    provider = ScriptedProvider(steps)
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store,
        provider,
        tools,
        model_tool_view=CodingWorkflowToolView(require_pre_edit_profile=False),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "修复并重测", request_id="repair")
    assert turn.status is TurnStatus.COMPLETED
    assert tools.calls == [
        "apply_patch",
        "run_profile.public-tests",
        "grep",
        "glob",
        "run_profile.public-tests",
        "read_file",
        "apply_patch",
        "run_profile.public-tests",
        "git_review",
    ]
    for index in (2, 3, 4, 5):
        names = {tool.name for tool in provider.requests[index].tools}
        assert {"read_file", "grep", "glob", "run_profile.public-tests"} <= names
        assert "apply_patch" not in names and "git_review" not in names
    assert "apply_patch" in {tool.name for tool in provider.requests[6].tools}
    assert provider.requests[7].forced_tool == "run_profile.public-tests"
    assert provider.requests[8].forced_tool == "git_review"
    assert provider.requests[9].tools == ()
    restored = (
        await SQLiteSessionStore(tmp_path / "session.db").get_thread(thread.thread_id)
    ).turns[0]
    assert restored == turn
    assert (
        CodingWorkflowToolView(require_pre_edit_profile=False)
        .select(restored, tools.definitions())
        .tools
        == ()
    )
