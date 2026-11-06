"""mini-swe-agent轨迹转逐步记录：只用真实时间戳，模型调用带逐次用量，命令输出挂在调用之下。"""

from __future__ import annotations

from integrations.harbor.mini_steps import trajectory_steps


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
