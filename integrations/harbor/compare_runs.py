"""两组Harbor运行按题配对比较得分与耗时，带不被预算封顶的耗时口径（2026-11-20负责人同意）。

NL2RepoBench约一半的题用满2小时预算，这些题的耗时被封顶，提速只表现为“同样时间多做事”，平均耗时看不出来。
所以除平均耗时外再给两个口径：
- **都在预算内完成的题**：只取每个参与运行都正常完成的题
  （Harnessix摘要`turn_status`为`completed`）；
- **每个模型步骤的耗时**：智能体用时除以模型步数（Harnessix摘要`model_steps`），封顶的题同样有意义。

判定沿用负责人的不劣规则：差于对照不超过一个标准误为pass，超过两个为regression，之间要查逐题原因。
标准误来自按题的配对Bootstrap（10000次）。用法：

    python -m integrations.harbor.compare_runs \\
        --run C1=runs/nl2repo-formal-speed-x --run B1=runs/nl2repo-formal-b48-y \\
        --compare C1:B1
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

BOOTSTRAP_SAMPLES = 10_000
BOOTSTRAP_SEED = 20261120


@dataclass(frozen=True)
class Trial:
    score: float | None
    minutes: float | None
    completed: bool
    steps: int | None


@dataclass(frozen=True)
class Difference:
    tasks: int
    reference: float
    difference: float
    standard_error: float
    verdict: str


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_run(run_dir: Path, arm: str = "harnessix") -> dict[str, Trial]:
    """`<arm>-rewards.txt`给得分（none记0）；`jobs/<arm>/*/result.json`给智能体用时，Harnessix摘要给终态与步数。

    没有奖励文件的运行（SWE-bench以官方判分为准）只比较耗时口径。
    """

    scores: dict[str, float] = {}
    rewards = run_dir / f"{arm}-rewards.txt"
    for line in rewards.read_text(encoding="utf-8").splitlines() if rewards.is_file() else ():
        task, value = line.split()
        scores[task] = 0.0 if value == "none" else float(value)
    trials: dict[str, Trial] = {}
    for result_path in sorted((run_dir / "jobs" / arm).glob("*__*/result.json")):
        result = json.loads(result_path.read_text(encoding="utf-8"))
        task = result["task_name"].split("/")[-1]
        execution = result.get("agent_execution") or {}
        minutes = None
        if execution.get("started_at") and execution.get("finished_at"):
            elapsed = _timestamp(execution["finished_at"]) - _timestamp(execution["started_at"])
            minutes = elapsed.total_seconds() / 60
        summary_path = result_path.parent / "agent" / "harnessix-summary.json"
        summary = (
            json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
        )
        trials[task] = Trial(
            score=scores.get(task, 0.0) if scores else None,
            minutes=minutes,
            completed=summary.get("turn_status") == "completed",
            steps=summary.get("model_steps"),
        )
    return trials


def verdict(loss: float, standard_error: float) -> str:
    """loss为正表示比对照差（得分低或更慢）。"""

    if loss <= standard_error:
        return "pass"
    if loss > 2 * standard_error:
        return "regression"
    return "between"


def _bootstrap_se(differences: Sequence[float]) -> float:
    generator = random.Random(BOOTSTRAP_SEED)
    mean = sum(differences) / len(differences)
    means = [
        sum(generator.choice(differences) for _ in differences) / len(differences)
        for _ in range(BOOTSTRAP_SAMPLES)
    ]
    return math.sqrt(sum((value - mean) ** 2 for value in means) / len(means))


def _paired(
    runs: Mapping[str, Mapping[str, Trial]],
    plus: Sequence[str],
    minus: Sequence[str],
    value: Callable[[Trial], float | None],
    higher_is_better: bool,
) -> Difference | None:
    labels = [*plus, *minus]
    values: dict[str, dict[str, float]] = {label: {} for label in labels}
    for label in labels:
        for task, trial in runs[label].items():
            measured = value(trial)
            if measured is not None:
                values[label][task] = measured
    tasks = sorted(set.intersection(*(set(values[label]) for label in labels)))
    if not tasks:
        return None

    def mean(group: Sequence[str], task: str) -> float:
        return sum(values[label][task] for label in group) / len(group)

    differences = [mean(plus, task) - mean(minus, task) for task in tasks]
    reference = sum(mean(minus, task) for task in tasks) / len(tasks)
    difference = sum(differences) / len(differences)
    standard_error = _bootstrap_se(differences)
    loss = -difference if higher_is_better else difference
    return Difference(
        len(tasks), reference, difference, standard_error, verdict(loss, standard_error)
    )


def compare(
    runs: Mapping[str, Mapping[str, Trial]], plus: Sequence[str], minus: Sequence[str]
) -> dict[str, Difference | None]:
    """得分、平均耗时（封顶）、都在预算内完成的题的耗时、每步耗时（秒）。"""

    def completed_minutes(trial: Trial) -> float | None:
        return trial.minutes if trial.completed else None

    def seconds_per_step(trial: Trial) -> float | None:
        if trial.minutes is None or not trial.steps:
            return None
        return trial.minutes * 60 / trial.steps

    return {
        "score": _paired(runs, plus, minus, lambda trial: trial.score, True),
        "minutes": _paired(runs, plus, minus, lambda trial: trial.minutes, False),
        "minutes_completed_in_every_run": _paired(runs, plus, minus, completed_minutes, False),
        "seconds_per_model_step": _paired(runs, plus, minus, seconds_per_step, False),
    }


def _format(label: str, results: Mapping[str, Difference | None]) -> str:
    lines = [label]
    for metric, result in results.items():
        if result is None:
            lines.append(f"  {metric}: no comparable tasks")
            continue
        relative = 100 * result.difference / result.reference if result.reference else 0.0
        lines.append(
            f"  {metric}: n={result.tasks} reference={result.reference:.4g} "
            f"diff={result.difference:+.4g} ({relative:+.1f}%) SE={result.standard_error:.4g} "
            f"-> {result.verdict}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="append", required=True, help="LABEL=RUN_DIR[:ARM]")
    parser.add_argument("--compare", action="append", required=True, help="A,B:C,D")
    parser.add_argument("--json", type=Path, help="同时把结果写成JSON")
    args = parser.parse_args(argv)
    runs: dict[str, dict[str, Trial]] = {}
    for spec in args.run:
        label, _, location = spec.partition("=")
        directory, _, arm = location.partition(":")
        runs[label] = load_run(Path(directory), arm or "harnessix")
    report = {}
    for spec in args.compare:
        plus, _, minus = spec.partition(":")
        results = compare(runs, plus.split(","), minus.split(","))
        report[spec] = {
            key: None if value is None else asdict(value) for key, value in results.items()
        }
        print(_format(f"{plus} vs {minus}", results))
    if args.json is not None:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
