"""工具参数修复：只修复原始控制字符与非法反斜杠转义，其余畸形仍失败关闭。"""

from __future__ import annotations

import pytest

from harnessix.models._chat_errors import ChatProtocolError, ChatProtocolReason
from harnessix.models._chat_stream import CallParts, _complete_calls
from harnessix.models._json import repaired_json_object


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
    with pytest.raises(ChatProtocolError):
        _complete_calls(_calls('{"path": "a"'), {"read_file": "read_file"}, repair_arguments=True)
