"""字符串化容器参数还原：只按Schema还原同类型JSON容器，其余原样交给严格校验。"""

from __future__ import annotations

import json

import pytest

from harnessix.agent.argument_normalization import normalize_stringified_containers
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolCallContent, TurnStatus
from harnessix.agent.runtime import AgentRuntime
from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput
from harnessix.delivery.workspace_patch_compact import compact_workspace_patch_schema
from harnessix.domain.models import ToolDescriptor
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from tests.agent.helpers import RecordingTools, answer

FILES = [{"operation": "delete", "path": "a.py", "expected_sha256": "0" * 64}]


@pytest.mark.parametrize(
    "schema", [compact_workspace_patch_schema(), WorkspacePatchInput.model_json_schema()]
)
@pytest.mark.parametrize("text", [json.dumps(FILES), "\n" + json.dumps(FILES, indent=2) + "\n"])
def test_stringified_array_is_restored_for_compact_and_ref_schemas(schema, text):
    restored = normalize_stringified_containers({"files": text}, schema)
    assert restored == {"files": FILES}
    WorkspacePatchInput.model_validate_json(json.dumps(restored))


def test_nested_stringified_items_follow_item_schema():
    nested = [
        {**FILES[0], "operation": "edit", "edits": json.dumps([{"old_text": "a", "new_text": "b"}])}
    ]
    restored = normalize_stringified_containers(
        {"files": json.dumps(nested)}, compact_workspace_patch_schema()
    )
    assert restored["files"][0]["edits"] == [{"old_text": "a", "new_text": "b"}]


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        json.dumps({"operation": "delete"}),  # 对象不是声明的数组
        "[1, 2",  # 截断
        json.dumps(FILES) + " trailing",
    ],
)
def test_wrong_type_or_invalid_text_is_left_for_strict_validation(value):
    assert normalize_stringified_containers({"files": value}, compact_workspace_patch_schema()) == {
        "files": value
    }


def test_string_typed_fields_and_unknown_fields_are_never_decoded():
    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "data": {"type": "array"}},
    }
    value = {"text": "[1]", "data": "[1]", "extra": "[1]"}
    assert normalize_stringified_containers(value, schema) == {
        "text": "[1]",
        "data": [1],
        "extra": "[1]",
    }


class ArrayTools(RecordingTools):
    def definitions(self):
        (base,) = super().definitions()
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array"}},
            "required": ["items"],
        }
        return (ToolDescriptor(**{**base.model_dump(), "input_schema": schema}),)


def stringified_step():
    return [
        ResponseStarted(response_id="r"),
        ToolCallCompleted(call_id="c", tool="test.read", arguments={"items": "[1, 2]"}),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


@pytest.mark.parametrize(("enabled", "expected"), [(True, [1, 2]), (False, "[1, 2]")])
async def test_runtime_persists_and_executes_the_normalized_arguments(tmp_path, enabled, expected):
    tools = ArrayTools()
    async with AgentRuntime(
        SQLiteSessionStore(tmp_path / "s.db"),
        ScriptedProvider([stringified_step(), answer()]),
        tools,
        normalize_stringified_arguments=enabled,
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        turn = await runtime.run_turn(thread.thread_id, "任务", request_id="n")
    assert turn.status is TurnStatus.COMPLETED
    (call,) = [i.content for i in turn.items if isinstance(i.content, ToolCallContent)]
    assert call.arguments == {"items": expected}
    assert tools.calls[0].arguments == {"items": expected}


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_switch_requires_literal_boolean(tmp_path, value):
    with pytest.raises(KernelError):
        AgentRuntime(
            SQLiteSessionStore(tmp_path / "s.db"),
            ScriptedProvider([]),
            RecordingTools(),
            normalize_stringified_arguments=value,
        )


def test_stringified_array_missing_only_its_final_closers_is_restored():
    edit = {
        "operation": "edit",
        "path": "a.py",
        "expected_sha256": "0" * 64,
        "edits": [{"old_text": "x = [1]", "new_text": "x = [1, 2]  # keep ] in text"}],
    }
    complete = json.dumps([edit])
    assert complete.endswith("}]}]")
    truncated = "\n" + complete[: -len("}]")]  # 模型关闭了edits却遗漏文件对象与外层数组
    restored = normalize_stringified_containers(
        {"files": truncated}, compact_workspace_patch_schema()
    )
    assert restored == {"files": [edit]}
    WorkspacePatchInput.model_validate_json(json.dumps(restored))


@pytest.mark.parametrize(
    "text",
    [
        '[{"operation": "edit", "path": "a.py", "edits": [{"old_text": "unterminated',
        '[{"operation": "delete"}]]',
        '[{"operation": "delete"} {"operation": "delete"}',
    ],
)
def test_unrepairable_text_is_left_for_strict_validation(text):
    schema = compact_workspace_patch_schema()
    assert normalize_stringified_containers({"files": text}, schema) == {"files": text}
