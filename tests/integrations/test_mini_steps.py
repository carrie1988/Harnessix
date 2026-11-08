"""mini-swe-agent轨迹转逐步记录：只用真实时间戳，模型调用带逐次用量，命令输出挂在调用之下。"""

from __future__ import annotations

from datetime import UTC, datetime

from integrations.harbor.mini_steps import _execution_start, trajectory_steps

START = datetime.fromtimestamp(1_800_000_000, UTC)


def tool_call_reply(seconds: int, text: str, *commands: tuple[str, str]) -> dict:
    """mini-swe-agent 2.4.6的工具调用回复：正文可为空，命令在extra.actions，附tool_call_id。"""
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": [{"id": call_id, "type": "function"} for call_id, _ in commands],
        "extra": {
            "timestamp": 1_800_000_000 + seconds,
            "actions": [
                {"command": command, "tool_call_id": call_id} for call_id, command in commands
            ],
        },
    }


def tool_output(seconds: int, call_id: str | None, output: str) -> dict:
    body: dict = {
        "role": "tool",
        "content": output,
        "extra": {"timestamp": 1_800_000_000 + seconds},
    }
    if call_id is not None:
        body["tool_call_id"] = call_id
    return body


def message(role: str, seconds: int | None, **extra: object) -> dict:
    body: dict = {"role": role, "content": f"{role}-{seconds}"}
    if seconds is not None:
        body["extra"] = {"timestamp": 1_800_000_000 + seconds, **extra}
    return body


def test_generations_and_tools_use_real_timestamps_and_per_call_usage() -> None:
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 9,
        "prompt_tokens_details": {"cached_tokens": 100},
    }
    trajectory = {
        "messages": [
            message("system", 0),
            message("user", 1),
            message("assistant", 4, response={"model": "qwen3.8-flash-next", "usage": usage}),
            message("user", 6),
            message("assistant", None),
            message("user", 9),
        ]
    }
    steps = trajectory_steps(trajectory)
    assert [(s.step_id, s.kind, s.parent_step_id) for s in steps] == [
        ("message-2", "generation", None),
        ("message-3", "tool", "message-2"),
    ]
    generation = steps[0]
    assert (generation.completed_at - generation.started_at).total_seconds() == 3
    assert generation.model == "qwen3.8-flash-next"
    assert (generation.usage.input_tokens, generation.usage.cached_input_tokens) == (120, 100)
    assert generation.usage.output_tokens == 9


def test_steps_carry_new_input_reply_with_commands_and_command_per_output() -> None:
    """系统提示与任务不带时间：首次调用从智能体执行开始起算，输入为系统提示与任务。"""
    trajectory = {
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Please solve this issue: COUNT(DISTINCT"},
            tool_call_reply(4, "", ("call-a", "grep -rn DISTINCT"), ("call-b", "ls")),
            tool_output(5, "call-b", "aggregates.py"),
            tool_output(6, "call-a", "42: COUNT(DISTINCT"),
            tool_call_reply(9, "Found it; patching.", ("call-c", "sed -i s/x/y/ f.py")),
            tool_output(10, None, "patched"),
        ]
    }
    steps = trajectory_steps(trajectory, started_at=START)
    first, second = (s for s in steps if s.kind == "generation")
    assert first.started_at == START
    assert first.input_text == (
        "[system]\nYou are a helpful assistant.\n\n[user]\nPlease solve this issue: COUNT(DISTINCT"
    )
    assert first.output_text == "[tool_call bash]\ngrep -rn DISTINCT\n\n[tool_call bash]\nls"
    tools = [(s.parent_step_id, s.input_text, s.output_text) for s in steps if s.kind == "tool"]
    assert tools == [
        (first.step_id, "ls", "aggregates.py"),
        (first.step_id, "grep -rn DISTINCT", "42: COUNT(DISTINCT"),
        (second.step_id, "sed -i s/x/y/ f.py", "patched"),
    ]
    assert second.input_text == (
        "[tool_result bash]\naggregates.py\n\n[tool_result bash]\n42: COUNT(DISTINCT"
    )
    assert second.output_text == (
        "[assistant]\nFound it; patching.\n\n[tool_call bash]\nsed -i s/x/y/ f.py"
    )
    # 没有执行开始时间时不估算：首次调用及其命令输出不成步骤，其后的调用照常。
    untimed = trajectory_steps(trajectory)
    assert [s.step_id for s in untimed if s.kind == "generation"] == [second.step_id]


def test_execution_start_requires_an_aware_harbor_timestamp() -> None:
    assert _execution_start({"agent_execution": {"started_at": "2026-11-06T15:12:15.401369Z"}}) == (
        datetime(2026, 11, 6, 15, 12, 15, 401369, tzinfo=UTC)
    )
    assert _execution_start({"agent_execution": {"started_at": "2026-11-06T15:12:15"}}) is None
    assert _execution_start({"agent_execution": None}) is None
    assert _execution_start({}) is None
