"""并发重放：从mini轨迹还原逐步的模型调用，各档并发以不同标记隔开前缀缓存。"""

from __future__ import annotations

from integrations.harbor.replay_concurrency import (
    CallResult,
    session_calls,
    summarize,
    with_nonce,
)


def assistant(text: str, completion: int | None) -> dict:
    usage = {"completion_tokens": completion} if completion is not None else None
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "bash"}}],
        "function_call": None,
        "extra": {"response": {"usage": usage}},
    }


TRAJECTORY = {
    "messages": [
        {"role": "system", "content": "sys", "extra": {}},
        {"role": "user", "content": "task"},
        assistant("a1", 50),
        {"role": "tool", "content": "out1", "tool_call_id": "t1", "extra": {"raw": "x"}},
        assistant("a2", None),
        {"role": "user", "content": "format error"},
        assistant("a3", 7),
    ]
}


def test_each_call_replays_the_messages_before_its_reply_without_extra_fields() -> None:
    calls = session_calls(TRAJECTORY, max_steps=10)
    assert [c.completion_tokens for c in calls] == [50, 7]  # 没有用量记录的回复不重放
    assert [m["role"] for m in calls[0].messages] == ["system", "user"]
    assert len(calls[1].messages) == 6
    assert all("extra" not in m and "function_call" not in m for m in calls[1].messages)
    assert calls[1].messages[2]["tool_calls"][0]["id"] == "t1"
    assert session_calls(TRAJECTORY, max_steps=1) == calls[:1]


def test_the_level_marker_prefixes_only_the_first_message() -> None:
    messages = session_calls(TRAJECTORY, max_steps=1)[0].messages
    marked = with_nonce(messages, "n1")
    assert marked[0]["content"] == "[replay n1]\nsys"
    assert marked[1] == messages[1] and messages[0]["content"] == "sys"


def test_summary_excludes_failed_calls_from_latency() -> None:
    def result(total: float, error: str | None = None) -> CallResult:
        return CallResult(4, 0, 0, "e", 1000, 10, None, total / 2, total, error)

    summary = summarize(4, [result(2.0), result(4.0), result(0.0, "URLError")], wall_s=60)
    assert summary["calls"] == 3 and summary["errors"] == 1
    assert summary["total_p50_s"] == 4.0 and summary["total_mean_s"] == 3.0
    assert summary["output_tokens_per_s"] == round(20 / 60, 1)
