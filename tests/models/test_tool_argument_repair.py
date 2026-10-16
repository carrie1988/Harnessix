"""工具参数修复：只修复原始控制字符与非法反斜杠转义，其余畸形仍失败关闭。"""

from __future__ import annotations

import pytest

from harnessix.models._chat_errors import ChatProtocolError, ChatProtocolReason
from harnessix.models._chat_stream import CallParts, _complete_calls
from harnessix.models._json import repaired_json_object
from harnessix.models.contracts import ToolCallRejected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"pattern": "a\\sb"}', {"pattern": "a\\sb"}),
        ('{"pattern": "\\\\d+"}', {"pattern": "\\d+"}),
        ('{"content": "line1\nline2\tx"}', {"content": "line1\nline2\tx"}),
        ('{"a": "ok\\n"}', {"a": "ok\n"}),
    ],
)
def test_unique_intent_preserving_repairs(text, expected):
    assert repaired_json_object(text) == expected


@pytest.mark.parametrize(
    "text",
    ['{"a": "x"', '{"a": 1}}', '{"a": 1, "a": 2}', '{"a": NaN}', '["not", "an", "object"]'],
)
def test_other_malformations_are_not_guessed(text):
    assert repaired_json_object(text) is None


def _calls(arguments: str) -> dict[int, CallParts]:
    call = CallParts()
    call.call_id, call.name, call.type, call.arguments = "c1", "read_file", "function", arguments
    return {0: call}


def test_stream_repairs_only_when_enabled():
    broken = '{"path": "src\\x.py"}'
    with pytest.raises(ChatProtocolError) as error:
        _complete_calls(_calls(broken), {"read_file": "read_file"})
    assert error.value.reason is ChatProtocolReason.TOOL_ARGUMENTS_INVALID
    (completed,) = _complete_calls(
        _calls(broken), {"read_file": "read_file"}, repair_arguments=True
    )
    assert completed.arguments == {"path": "src\\x.py"}
    # 修复后仍非JSON对象：只拒绝本次调用，不再使整个响应失败。
    for unrepairable in ('{"path": "a"', '["a"]'):
        (rejected,) = _complete_calls(
            _calls(unrepairable), {"read_file": "read_file"}, repair_arguments=True
        )
        assert isinstance(rejected, ToolCallRejected)
        assert rejected.reason == "invalid_arguments_json"
    with pytest.raises(ChatProtocolError) as not_object:
        _complete_calls(_calls('["a"]'), {"read_file": "read_file"})
    assert not_object.value.reason is ChatProtocolReason.TOOL_ARGUMENTS_NOT_OBJECT


def test_rejection_results_are_paired_by_reason():
    from uuid import uuid4

    from harnessix.agent.errors import KernelError
    from harnessix.agent.tool_rejections import rejection_result, require_rejection_result

    call_id = uuid4()
    json_result = rejection_result(call_id, "invalid_arguments_json")
    assert json_result.error.code == "tool_arguments_invalid_json"
    require_rejection_result(json_result)
    require_rejection_result(json_result, "invalid_arguments_json")
    require_rejection_result(rejection_result(call_id))
    with pytest.raises(KernelError):
        require_rejection_result(json_result, "unregistered_tool")
