"""百炼已求证的空 ID 增量占位；不放宽其他身份或协议校验。"""

import pytest

from harnessix.models.contracts import ResponseCompleted, ResponseFailed, ToolCallCompleted
from tests.models.test_openai_chat import collect
from tests.models.test_openai_chat import credentials as credentials
from tests.models.wire import call, chunk, frame, tool_frames


def empty_id_frames(arguments):
    parts = tool_frames("{}" if arguments is None else "{")
    parts.insert(
        1,
        frame(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "",
                            "type": "function",
                            "function": {"arguments": arguments},
                        }
                    ]
                }
            )
        ),
    )
    return parts


@pytest.mark.parametrize("arguments", [None, "}"])
async def test_empty_incremental_id_keeps_first_identity_and_releases_one_call(arguments):
    events, wire = await collect(empty_id_frames(arguments))
    calls = [e for e in events if isinstance(e, ToolCallCompleted)]
    assert len(calls) == 1 and calls[0].call_id == call()["id"] and calls[0].arguments == {}
    assert isinstance(events[-1], ResponseCompleted) and events[-1].usage.total_tokens == 12
    assert wire.closed


@pytest.mark.parametrize("violation", ["no_identity", "id_drift", "name_drift", "invalid_type"])
async def test_empty_id_does_not_hide_other_protocol_violations(violation):
    parts = empty_id_frames(None)
    if violation == "no_identity":
        first = call()
        first["id"] = ""
        parts[0] = frame(chunk({"tool_calls": [first]}))
    else:
        update = {"index": 0}
        update.update(
            {
                "id_drift": {"id": "another-id"},
                "name_drift": {"function": {"name": ""}},
                "invalid_type": {"type": ""},
            }[violation]
        )
        parts.insert(2, frame(chunk({"tool_calls": [update]})))
    events, wire = await collect(parts)
    assert events[-1] == ResponseFailed(code="invalid_provider_output")
    assert not any(isinstance(e, ToolCallCompleted | ResponseCompleted) for e in events)
    assert wire.closed
