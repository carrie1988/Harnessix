"""mini-swe-agent轨迹 → evalkit逐步记录：模型调用为generation（附逐次用量），命令输出为tool。

只使用轨迹中真实存在的时间戳；缺少时间的消息不生成步骤，不估算。正文写入步骤，
是否导出由运行计划的内容级别决定（默认只导出元数据）。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalkit.contracts import ArmTrials, Usage
from evalkit.files import read_model, write_model
from evalkit.harbor import RESULT_ARTIFACT
from evalkit.steps import Step, StepTrace

_TRAJECTORY = "agent/mini-swe-agent.trajectory.json"


def _timestamp(message: dict[str, Any]) -> datetime | None:
    extra = message.get("extra") if isinstance(message.get("extra"), dict) else {}
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


def trajectory_steps(trajectory: dict[str, Any]) -> tuple[Step, ...]:
    """相邻消息的时间构成步骤窗口：模型调用从上一条消息到本条回复，命令从回复到输出。"""
    messages = [m for m in trajectory.get("messages") or [] if isinstance(m, dict)]
    steps: list[Step] = []
    previous: datetime | None = None
    last_generation: Step | None = None
    for index, message in enumerate(messages):
        moment = _timestamp(message)
        role = message.get("role")
        if role == "assistant" and moment is None:
            last_generation = None  # 无法定位的调用，其后的输出不归到更早的调用
        if moment is not None and previous is not None and moment >= previous:
            if role == "assistant":
                usage, model = _usage(message)
                last_generation = Step(
                    step_id=f"message-{index}",
                    kind="generation",
                    name="model",
                    started_at=previous,
                    completed_at=moment,
                    status="ok",
                    model=model,
                    usage=usage,
                    output_text=_text(message),
                )
                steps.append(last_generation)
            elif role in ("tool", "user") and last_generation is not None:
                steps.append(
                    Step(
                        step_id=f"message-{index}",
                        parent_step_id=last_generation.step_id,
                        kind="tool",
                        name="bash",
                        started_at=previous,
                        completed_at=moment,
                        status="ok",
                        output_text=_text(message),
                    )
                )
        if moment is not None:
            previous = moment
    return tuple(steps)


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
        steps = trajectory_steps(json.loads(path.read_text(encoding="utf-8")))
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
