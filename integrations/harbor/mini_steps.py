"""mini-swe-agent轨迹 → evalkit逐步记录：模型调用为generation（附逐次用量），命令输出为tool。

只使用轨迹中真实存在的时间戳；缺少时间的消息不生成步骤，不估算。正文（模型调用新增的输入、
回复与命令，命令及其输出）按evalkit.steps.step_text的共用格式写入步骤，
是否导出由运行计划的内容级别决定（默认只导出元数据）。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalkit.contracts import ArmTrials, Usage
from evalkit.files import read_artifact, read_model, write_model
from evalkit.harbor import RESULT_ARTIFACT
from evalkit.steps import Step, StepTrace, step_text

_TRAJECTORY = "agent/mini-swe-agent.trajectory.json"
_MAX_RESULT_BYTES = 8 * 1024 * 1024


def _timestamp(message: dict[str, Any]) -> datetime | None:
    raw = message.get("extra")
    extra = raw if isinstance(raw, dict) else {}
    for value in (
        message.get("created_at"),
        message.get("timestamp"),
        message.get("completed_at"),
        extra.get("timestamp"),
    ):
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                return parsed
    return None


def _usage(message: dict[str, Any]) -> tuple[Usage, str | None]:
    response = ((message.get("extra") or {}).get("response")) or {}
    usage = response.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    observed = Usage(
        completeness="complete" if usage else "unknown",
        model_calls=1,
        input_tokens=usage.get("prompt_tokens"),
        cached_input_tokens=prompt_details.get("cached_tokens"),
        output_tokens=usage.get("completion_tokens"),
        reasoning_tokens=completion_details.get("reasoning_tokens"),
    )
    model = response.get("model")
    return observed, model if isinstance(model, str) and model else None


def _text(message: dict[str, Any]) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return None


def _commands(message: dict[str, Any]) -> list[tuple[str | None, str]]:
    """模型回复中mini解析出并执行的命令（extra.actions），按顺序附tool_call_id。"""
    raw = message.get("extra")
    extra = raw if isinstance(raw, dict) else {}
    actions = extra.get("actions")
    commands = []
    for action in actions if isinstance(actions, list) else []:
        if isinstance(action, dict) and isinstance(action.get("command"), str):
            call_id = action.get("tool_call_id")
            commands.append((call_id if isinstance(call_id, str) else None, action["command"]))
    return commands


def trajectory_steps(
    trajectory: dict[str, Any], *, started_at: datetime | None = None
) -> tuple[Step, ...]:
    """相邻消息的时间构成步骤窗口：模型调用从上一条消息到本条回复，命令从回复到输出。

    mini不给系统提示与任务消息打时间，首次调用从started_at（Harbor记录的智能体执行开始，
    对应Harnessix的Turn开始）起算；没有该时间时首次调用不生成步骤。
    模型调用的输入是上次回复之后新增的消息（首次为系统提示与任务），输出是回复正文与命令；
    命令输出按tool_call_id对应命令，只有一条命令时直接对应。
    """
    messages = [m for m in trajectory.get("messages") or [] if isinstance(m, dict)]
    steps: list[Step] = []
    previous: datetime | None = started_at
    last_generation: Step | None = None
    received: list[tuple[str, str]] = []
    commands: dict[str | None, str] = {}
    for index, message in enumerate(messages):
        moment = _timestamp(message)
        role = message.get("role")
        text = _text(message)
        window = (
            (previous, moment)
            if moment is not None and previous is not None and moment >= previous
            else None
        )
        if role == "assistant":
            issued = _commands(message)
            last_generation = None  # 无法定位的调用，其后的输出不归到更早的调用
            if window is not None:
                usage, model = _usage(message)
                last_generation = Step(
                    step_id=f"message-{index}",
                    kind="generation",
                    name="model",
                    started_at=window[0],
                    completed_at=window[1],
                    status="ok",
                    model=model,
                    usage=usage,
                    input_text=step_text(received),
                    output_text=step_text(
                        [("assistant", text or "")] + [("tool_call bash", c) for _, c in issued]
                    ),
                )
                steps.append(last_generation)
            received = []
            commands = dict(issued)
            if len(issued) == 1:
                commands.setdefault(None, issued[0][1])
        else:
            label = "tool_result bash" if role == "tool" else str(role or "message")
            received.append((label, text or ""))
            if window is not None and role in ("tool", "user") and last_generation is not None:
                call_id = message.get("tool_call_id")
                steps.append(
                    Step(
                        step_id=f"message-{index}",
                        parent_step_id=last_generation.step_id,
                        kind="tool",
                        name="bash",
                        started_at=window[0],
                        completed_at=window[1],
                        status="ok",
                        input_text=commands.get(call_id)
                        if call_id in commands
                        else commands.get(None),
                        output_text=text,
                    )
                )
        if moment is not None:
            previous = moment
    return tuple(steps)


def _execution_start(result: dict[str, Any]) -> datetime | None:
    """Harbor结果中智能体执行开始的时刻；只接受带时区的时间，不猜测时区。"""
    execution = result.get("agent_execution")
    value = execution.get("started_at") if isinstance(execution, dict) else None
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def export_steps(run_dir: Path, trials_path: Path, output: Path) -> int:
    group = read_model(trials_path, ArmTrials, 64 * 1024 * 1024, require_private=True)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    written = 0
    for trial in group.trials:
        result = next((a for a in trial.artifacts if a.name == RESULT_ARTIFACT), None)
        if result is None:
            continue
        path = run_dir / Path(result.path).parent / _TRAJECTORY
        if not path.is_file():
            continue
        started_at = _execution_start(json.loads(read_artifact(result, run_dir, _MAX_RESULT_BYTES)))
        steps = trajectory_steps(
            json.loads(path.read_text(encoding="utf-8")), started_at=started_at
        )
        if not steps:
            continue
        trace = StepTrace(plan_digest=group.plan_digest, slot=trial.slot, steps=steps)
        write_model(output / f"{group.arm_id}-slot{trial.slot}.json", trace, 64 * 1024 * 1024)
        written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--trials", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    count = export_steps(args.run_dir, args.trials, args.output_dir)
    print(json.dumps({"reason": "exported", "step_traces": count}))


if __name__ == "__main__":
    main()
