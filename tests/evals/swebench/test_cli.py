from __future__ import annotations

import json
from pathlib import Path

import pytest

from harnessix.cli import main
from harnessix.evals.swebench import cli, official
from harnessix.evals.swebench.io import (
    read_preregistration_plan,
    write_preregistration_plan,
)
from harnessix.evals.swebench.predictions import write_predictions
from harnessix.evals.swebench.public_test_profile import (
    build_swebench_case_process_profile,
)
from harnessix.evals.swebench.run_config_io import read_swebench_agent_run_config
from tests.evals.swebench.test_predictions import (
    GIT,
    build_dataset_rows,
    build_test_plan,
    build_test_predictions,
)
from tests.evals.swebench.test_run_config import build_agent_test_plan


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


def test_run_agent_requires_explicit_provider_network_before_reading_plan(
    tmp_path: Path,
    capsys,
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "coding-eval-swebench",
                "run-agent",
                "--plan",
                str(tmp_path / "missing-plan.json"),
                "--source-root",
                str(tmp_path),
                "--work-root",
                str(tmp_path / "work"),
                "--cache-root",
                str(tmp_path / "cache"),
                "--git",
                str(GIT),
                "--container-engine",
                str(tmp_path / "docker"),
            ]
        )

    output = capsys.readouterr()
    assert caught.value.code == 1
    assert json.loads(output.out) == {"reason": "failed"}
    assert not (tmp_path / "work").exists()


def test_run_agent_requires_container_engine_argument(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "coding-eval-swebench",
                "run-agent",
                "--plan",
                str(tmp_path / "plan.json"),
                "--source-root",
                str(tmp_path),
                "--work-root",
                str(tmp_path / "work"),
                "--cache-root",
                str(tmp_path / "cache"),
                "--git",
                str(GIT),
                "--allow-provider-network",
            ]
        )

    output = capsys.readouterr()
    assert caught.value.code == 2
    assert "参数无效" in output.err


def test_run_agent_resume_reuses_frozen_profiles_without_rediscovery(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_agent_test_plan()
    plan_path = tmp_path / "plan.json"
    source_root = tmp_path / "source"
    work_root = tmp_path / "work"
    cache_root = tmp_path / "cache"
    source_root.mkdir()
    engine = tmp_path / "docker"
    engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    engine.chmod(0o700)
    profiles = tuple(
        build_swebench_case_process_profile(
            case.instance_id,
            repo=case.repo,
            container_engine=engine,
            image_id=f"sha256:{index + 1:064x}",
        )
        for index, case in enumerate(sorted(plan.cases, key=lambda item: item.instance_id))
    )
    write_preregistration_plan(plan_path, plan)
    discoveries = 0
    executions = 0

    def discover(_plan, _engine):
        nonlocal discoveries
        discoveries += 1
        assert _plan == plan
        assert _engine == engine
        return profiles

    async def run_suite(_plan, config, **_kwargs):
        nonlocal executions
        executions += 1
        assert config.case_process_profiles == profiles
        return {"reason": "completed", "completed_cases": 25}

    monkeypatch.setattr(cli, "discover_swebench_case_process_profiles", discover)
    monkeypatch.setattr(cli, "run_swebench_agent_suite", run_suite)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "secret")
    arguments = [
        "coding-eval-swebench",
        "run-agent",
        "--plan",
        str(plan_path),
        "--source-root",
        str(source_root),
        "--work-root",
        str(work_root),
        "--cache-root",
        str(cache_root),
        "--git",
        str(GIT),
        "--container-engine",
        str(engine),
        "--allow-provider-network",
    ]

    main(arguments)
    first_output = json.loads(capsys.readouterr().out)
    frozen = read_swebench_agent_run_config(work_root / "run-config.json")
    assert first_output["reason"] == "completed"
    assert frozen.case_process_profiles == profiles

    def forbidden_discovery(*_args, **_kwargs):
        pytest.fail("恢复既有run-config不得重新发现或替换Profile")

    monkeypatch.setattr(cli, "discover_swebench_case_process_profiles", forbidden_discovery)
    main(arguments)
    second_output = json.loads(capsys.readouterr().out)
    assert second_output["reason"] == "completed"
    assert discoveries == 1
    assert executions == 2

    other_engine = tmp_path / "docker-other"
    other_engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    other_engine.chmod(0o700)
    drifted_arguments = list(arguments)
    drifted_arguments[drifted_arguments.index(str(engine))] = str(other_engine)
    with pytest.raises(SystemExit) as caught:
        main(drifted_arguments)

    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"reason": "failed"}
    assert executions == 2


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
