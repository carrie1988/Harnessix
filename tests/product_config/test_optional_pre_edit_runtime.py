"""真实Kernel/SQLite证明阶段和混合组拒绝；假工具不作为编码质量证据。"""

import pytest

from harnessix.agent.models import ToolCallContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_config.coding_tool_view import CodingWorkflowToolView
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import answer
from tests.product_config.test_coding_tool_view_runtime import _SuccessfulCodingTools, _tool_step

PROFILE = "run_profile.public-tests"


@pytest.mark.parametrize("pre_test", [False, True])
async def test_optional_prediction_does_not_replace_required_post_edit_test(tmp_path, pre_test):
    tools = _SuccessfulCodingTools()
    names = ([PROFILE] if pre_test else []) + ["read_file", "apply_patch", PROFILE, "git_review"]
    provider = ScriptedProvider(
        [
            *(
                _tool_step(
                    i, name, {"path": "src/file.py"} if name in {"read_file", "apply_patch"} else {}
                )
                for i, name in enumerate(names)
            ),
            answer(),
        ]
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store,
        provider,
        tools,
        model_tool_view=CodingWorkflowToolView(
            require_pre_edit_profile=False, allow_optional_pre_edit_profile=True
        ),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "修复并验证", request_id="optional")
    assert turn.status is TurnStatus.COMPLETED and tools.calls == names
    patch_index = names.index("apply_patch")
    assert provider.requests[patch_index + 1].forced_tool == PROFILE
    assert provider.requests[patch_index + 2].forced_tool == "git_review"
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)


@pytest.mark.parametrize("order", [(PROFILE, "apply_patch"), ("apply_patch", PROFILE)])
async def test_optional_view_cannot_publish_any_call_from_mixed_test_patch_response(
    tmp_path, order
):
    tools = _SuccessfulCodingTools()
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="mixed"),
                *(
                    ToolCallCompleted(call_id=f"call-{i}", tool=tool, arguments={})
                    for i, tool in enumerate(order)
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ]
        ]
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store,
        provider,
        tools,
        model_tool_view=CodingWorkflowToolView(
            require_pre_edit_profile=False, allow_optional_pre_edit_profile=True
        ),
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "拒绝盲目批量修改", request_id="mixed")
    assert turn.status is TurnStatus.FAILED and turn.error.code == "invalid_provider_output"
    assert not tools.calls and not any(isinstance(i.content, ToolCallContent) for i in turn.items)
    assert replay(await store.events(thread.thread_id)) == await store.get_thread(thread.thread_id)
