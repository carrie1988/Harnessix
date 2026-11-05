"""Score local SWE-bench runs with the official evaluator and compare arms.

`evaluate RUN_DIR` runs `swebench eval` on every predictions*.jsonl of a run (selected patch and,
for candidate arms, each candidate) against the pinned full dataset rows, offline, with local
images. `report RUN_DIR...` prints resolved counts per arm, single-attempt / selected / oracle
numbers for candidate arms, and exact McNemar tests between arms on their common tasks.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path


def evaluate(run_dir: Path, dataset: Path, swebench: str, workers: int) -> None:
    arm = json.loads((run_dir / "arm.json").read_text())
    revision = arm["harnessix_revision"][:12]
    for predictions in sorted(run_dir.glob("predictions*.jsonl")):
        rows = [json.loads(line) for line in predictions.read_text().splitlines() if line]
        if not rows:
            continue
        report_dir = run_dir / "eval" / predictions.stem
        report_dir.mkdir(parents=True, exist_ok=True)
        command = [
            swebench,
            "eval",
            str(dataset),
            "-p",
            str(predictions),
            "--run-id",
            f"{arm['name']}-{revision}-{predictions.stem}",
            "-j",
            str(workers),
            "--report-dir",
            str(report_dir),
        ]
        for row in rows:
            command += ["-i", row["instance_id"]]
        with open(report_dir / "evaluate.log", "w") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
        print(f"{predictions.name}: {resolved_summary(report_dir)}", flush=True)


def resolved_ids(report_dir: Path) -> set[str]:
    """Resolved instance IDs from the single official report in report_dir."""

    reports = list(report_dir.glob("*.json"))
    if len(reports) != 1:
        raise SystemExit(f"expected one official report in {report_dir}, found {len(reports)}")
    return set(json.loads(reports[0].read_text())["resolved_ids"])


def predicted_ids(predictions: Path) -> set[str]:
    """Every task of the arm, including empty patches, which count as unresolved."""

    return {
        json.loads(line)["instance_id"] for line in predictions.read_text().splitlines() if line
    }


def resolved_summary(report_dir: Path) -> str:
    return f"{len(resolved_ids(report_dir))} resolved"


def mcnemar(a: set[str], b: set[str], tasks: set[str]) -> tuple[int, int, float]:
    """Discordant counts and the exact two-sided binomial p-value."""

    only_a, only_b = len((a - b) & tasks), len((b - a) & tasks)
    n = only_a + only_b
    if n == 0:
        return only_a, only_b, 1.0
    tail = sum(math.comb(n, k) for k in range(0, min(only_a, only_b) + 1)) / 2**n
    return only_a, only_b, min(1.0, 2 * tail)


def report(run_dirs: list[Path]) -> None:
    arms: dict[str, tuple[set[str], set[str]]] = {}
    print("| Arm | Resolved | Notes |\n|---|---|---|")
    for run_dir in run_dirs:
        name = json.loads((run_dir / "arm.json").read_text())["name"]
        tasks = predicted_ids(run_dir / "predictions.jsonl")
        resolved = resolved_ids(run_dir / "eval" / "predictions") & tasks
        arms[name] = (resolved, tasks)
        notes = ""
        candidates = sorted((run_dir / "eval").glob("predictions-c*"))
        if candidates:
            per = [resolved_ids(path) & tasks for path in candidates]
            oracle = set().union(*per)
            mean = sum(len(item) for item in per) / len(per)
            selector = len(resolved & oracle) / len(oracle) if oracle else 0.0
            notes = (
                f"single c0 {len(per[0])}, mean {mean:.1f}, selected {len(resolved)}, "
                f"oracle {len(oracle)}, selector accuracy {selector:.0%}"
            )
        print(
            f"| {name} | {len(resolved)}/{len(tasks)} ({len(resolved) / len(tasks):.0%}) "
            f"| {notes} |"
        )
    names = list(arms)
    if len(names) > 1:
        print("\n| A vs B | common | only A | only B | McNemar p |\n|---|---|---|---|---|")
        for index, first in enumerate(names):
            for second in names[index + 1 :]:
                common = arms[first][1] & arms[second][1]
                only_a, only_b, p = mcnemar(arms[first][0], arms[second][0], common)
                print(f"| {first} vs {second} | {len(common)} | {only_a} | {only_b} | {p:.3f} |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("evaluate")
    run.add_argument("run_dir", type=Path)
    run.add_argument("--dataset", type=Path, required=True, help="full dataset rows JSONL")
    run.add_argument("--swebench", default="swebench")
    run.add_argument("--workers", type=int, default=6)
    summary = commands.add_parser("report")
    summary.add_argument("run_dirs", type=Path, nargs="+")
    arguments = parser.parse_args()
    if arguments.command == "evaluate":
        evaluate(arguments.run_dir, arguments.dataset, arguments.swebench, arguments.workers)
    else:
        report(arguments.run_dirs)


if __name__ == "__main__":
    main()
