"""配对比较的四个口径：得分、封顶的平均耗时、都在预算内完成的题的耗时、每步耗时（2026-11-20）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from integrations.harbor.compare_runs import compare, load_run, main, verdict


def _run(root: Path, name: str, trials: dict[str, tuple[str, float, str, int]]) -> Path:
    """trials: 题 -> (得分文本, 智能体分钟, Turn终态, 模型步数)。"""
    run = root / name
    lines = []
    for task, (score, minutes, status, steps) in trials.items():
        lines.append(f"{task} {score}")
        trial = run / "jobs" / "harnessix" / f"{task}__abc"
        (trial / "agent").mkdir(parents=True)
        start = "2026-11-20T00:00:00Z"
        hours, rest = divmod(minutes, 60)
        finish = f"2026-11-20T{int(hours):02d}:{int(rest):02d}:00Z"
        result = {
            "task_name": f"nl2repo/{task}",
            "agent_execution": {"started_at": start, "finished_at": finish},
        }
        (trial / "result.json").write_text(json.dumps(result))
        summary = {"turn_status": status, "model_steps": steps}
        (trial / "agent" / "harnessix-summary.json").write_text(json.dumps(summary))
    (run / "harnessix-rewards.txt").write_text("\n".join(lines) + "\n")
    return run


@pytest.fixture
def runs(tmp_path: Path):
    # 候选：a在预算内更快完成；b用满120分钟但多做了步数；c没有得分（判分超时记0）。
    candidate = _run(
        tmp_path,
        "candidate",
        {
            "a": ("1.0", 30, "completed", 30),
            "b": ("0.5", 120, "failed", 120),
            "c": ("none", 60, "completed", 20),
        },
    )
    reference = _run(
        tmp_path,
        "reference",
        {
            "a": ("1.0", 60, "completed", 30),
            "b": ("0.5", 120, "failed", 60),
            "c": ("0.2", 60, "failed", 20),
        },
    )
    return {"C": load_run(candidate), "R": load_run(reference)}


def test_capped_time_hides_what_the_uncapped_metrics_show(runs) -> None:
    results = compare(runs, ["C"], ["R"])

    assert results["score"].difference == pytest.approx(-0.2 / 3)
    assert results["minutes"].difference == pytest.approx(-10.0)
    # 只有a在两边都正常完成：快30分钟。
    completed = results["minutes_completed_in_every_run"]
    assert completed.tasks == 1 and completed.difference == pytest.approx(-30.0)
    # 每步秒数：a 60对120，b 60对120（多做的步数显出来），c 180对180；平均快40秒。
    per_step = results["seconds_per_model_step"]
    assert per_step.tasks == 3 and per_step.difference == pytest.approx(-40.0)


def test_verdict_follows_the_owner_non_inferiority_rule() -> None:
    assert verdict(-1.0, 0.5) == "pass"
    assert verdict(0.5, 0.5) == "pass"
    assert verdict(0.8, 0.5) == "between"
    assert verdict(1.1, 0.5) == "regression"


def test_command_line_prints_and_writes_json(tmp_path: Path, capsys) -> None:
    candidate = _run(tmp_path, "c", {"a": ("1.0", 30, "completed", 10)})
    reference = _run(tmp_path, "r", {"a": ("0.0", 40, "completed", 10)})
    output = tmp_path / "report.json"
    main(
        [
            "--run",
            f"C={candidate}",
            "--run",
            f"R={reference}:harnessix",
            "--compare",
            "C:R",
            "--json",
            str(output),
        ]
    )

    assert "seconds_per_model_step: n=1" in capsys.readouterr().out
    report = json.loads(output.read_text())
    assert report["C:R"]["score"]["difference"] == pytest.approx(1.0)


def test_a_run_without_rewards_compares_time_only(tmp_path: Path) -> None:
    """SWE-bench运行没有奖励文件（以官方判分为准）：耗时口径照常，得分不比较。"""
    candidate = _run(tmp_path, "c", {"a": ("1.0", 30, "completed", 10)})
    reference = _run(tmp_path, "r", {"a": ("1.0", 40, "completed", 10)})
    (candidate / "harnessix-rewards.txt").unlink()
    results = compare({"C": load_run(candidate), "R": load_run(reference)}, ["C"], ["R"])

    assert results["score"] is None
    assert results["minutes"].difference == pytest.approx(-10.0)
