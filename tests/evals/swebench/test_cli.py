from __future__ import annotations

import json
from pathlib import Path

import pytest

from harnessix.cli import main
from harnessix.evals.swebench import official
from harnessix.evals.swebench.io import (
    read_preregistration_plan,
    write_preregistration_plan,
)
from harnessix.evals.swebench.predictions import write_predictions
from tests.evals.swebench.test_predictions import (
    GIT,
    build_dataset_rows,
    build_test_plan,
    build_test_predictions,
)


def test_top_level_help_discovers_swebench_cli(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--help"])

    output = capsys.readouterr()
    assert caught.value.code == 0
    assert "coding-eval-swebench" in output.out


def test_disabled_network_evaluate_fails_without_running_subprocess(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    plan = build_test_plan()
    predictions = build_test_predictions(plan)
    plan_path = tmp_path / "plan.json"
    predictions_path = tmp_path / "predictions.jsonl"
    work_root = tmp_path / "work"
    executable = tmp_path / "swebench"
    work_root.mkdir()
    executable.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    executable.chmod(0o700)
    write_preregistration_plan(plan_path, plan)
    write_predictions(predictions_path, plan, predictions)

    def forbidden_subprocess(*_args, **_kwargs):
        pytest.fail("禁网 evaluate 不得执行 subprocess")

    monkeypatch.setattr(official.subprocess, "run", forbidden_subprocess)

    with pytest.raises(SystemExit) as caught:
        main(
            [
                "coding-eval-swebench",
                "evaluate",
                "--plan",
                str(plan_path),
                "--predictions",
                str(predictions_path),
                "--swebench",
                str(executable),
                "--swebench-source",
                str(tmp_path),
                "--git",
                str(GIT),
                "--work-root",
                str(work_root),
                "--task-repo",
                str(tmp_path),
            ]
        )

    output = capsys.readouterr()
    assert caught.value.code == 1
    assert json.loads(output.out) == {"reason": "failed"}
    assert output.err == ""


def test_preregister_writes_exactly_25_public_cases_without_gold_fields(
    tmp_path: Path,
    capsys,
) -> None:
    dataset_path = tmp_path / "dataset.jsonl"
    plan_path = tmp_path / "plan.json"
    dataset_path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in build_dataset_rows(30)
        ),
        encoding="utf-8",
    )

    main(
        [
            "coding-eval-swebench",
            "preregister",
            "--dataset-jsonl",
            str(dataset_path),
            "--output",
            str(plan_path),
            "--dataset-name",
            "SWE-bench/SWE-bench_Verified",
            "--dataset-revision",
            "1" * 40,
            "--dataset-artifact-sha256",
            "4" * 64,
            "--harnessix-revision",
            "2" * 40,
            "--swebench-revision",
            "3" * 40,
            "--swebench-package-version",
            "5.0.0",
            "--task-repo-revision",
            "5" * 40,
            "--model-name",
            "model-v1",
            "--provider",
            "test-provider",
            "--model-parameter",
            "temperature=0",
            "--salt",
            "strict-swebench-cli-salt-v1",
            "--max-steps",
            "32",
            "--max-tokens",
            "50000",
            "--timeout-seconds",
            "1800",
            "--max-output-chars",
            "65536",
            "--max-tool-calls-per-step",
            "16",
        ]
    )

    output = capsys.readouterr()
    report = json.loads(output.out)
    plan = read_preregistration_plan(plan_path)
    serialized = plan_path.read_text(encoding="utf-8")
    assert output.err == ""
    assert report["reason"] == "completed"
    assert report["registered_cases"] == 25
    assert len(plan.cases) == 25
    assert plan.dataset_artifact_sha256 == "4" * 64
    assert plan.swebench_package_version == "5.0.0"
    assert plan.task_repo_revision == "5" * 40
    for forbidden in ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS"):
        assert f'"{forbidden}"' not in serialized


def test_validate_accepts_a_complete_bound_plan(tmp_path: Path, capsys) -> None:
    plan = build_test_plan()
    plan_path = tmp_path / "plan.json"
    write_preregistration_plan(plan_path, plan)

    main(["coding-eval-swebench", "validate", "--plan", str(plan_path)])

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert output.err == ""
    assert report == {
        "plan_sha256": plan.plan_sha256,
        "reason": "completed",
        "registered_cases": 25,
    }
