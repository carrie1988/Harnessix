from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench.official import (
    build_official_evaluation_command,
    evaluation_run_id,
    import_official_result,
)
from harnessix.evals.swebench.predictions import (
    SweBenchPrediction,
    predictions_sha256,
    write_predictions,
)
from tests.evals.swebench.test_predictions import (
    GIT,
    build_test_plan,
    build_test_predictions,
)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _source_repository(path: Path) -> tuple[Path, str]:
    path.mkdir()
    subprocess.run((str(GIT), "init", "--quiet"), cwd=path, check=True)
    subprocess.run(
        (str(GIT), "config", "user.name", "Harnessix Tests"), cwd=path, check=True
    )
    subprocess.run(
        (str(GIT), "config", "user.email", "tests@harnessix.invalid"),
        cwd=path,
        check=True,
    )
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run((str(GIT), "add", "README.md"), cwd=path, check=True)
    subprocess.run(
        (str(GIT), "commit", "--quiet", "-m", "fixture"), cwd=path, check=True
    )
    revision = subprocess.run(
        (str(GIT), "rev-parse", "HEAD"),
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return path, revision


def _official_environment(tmp_path: Path):
    source, source_revision = _source_repository(tmp_path / "swebench-source")
    task_repo, task_revision = _source_repository(tmp_path / "task-repo")
    executable = _executable(source / ".venv" / "bin" / "swebench")
    plan = build_test_plan(
        swebench_revision=source_revision,
        task_repo_revision=task_revision,
    )
    return plan, executable, source, task_repo


def _official_result_payload(
    plan,
    *,
    empty_patch_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    submitted = tuple(sorted(case.instance_id for case in plan.cases))
    completed = tuple(item for item in submitted if item not in empty_patch_ids)
    resolved = completed[:8]
    unresolved = completed[8:]
    return {
        "total_instances": 25,
        "submitted_instances": 25,
        "completed_instances": len(completed),
        "resolved_instances": 8,
        "unresolved_instances": len(unresolved),
        "infra_failure_instances": 1,
        "ambiguous_failure_instances": 1,
        "empty_patch_instances": len(empty_patch_ids),
        "error_instances": 0,
        "completed_ids": completed,
        "incomplete_ids": (),
        "empty_patch_ids": tuple(sorted(empty_patch_ids)),
        "submitted_ids": submitted,
        "resolved_ids": resolved,
        "unresolved_ids": unresolved,
        "infra_failure_ids": unresolved[:1],
        "ambiguous_failure_ids": unresolved[1:2],
        "failure_reasons": {unresolved[0]: "container setup failed"},
        "error_ids": (),
        "schema_version": 2,
        "unstopped_instances": 0,
        "unstopped_containers": (),
        "unremoved_images": (),
    }


def _write_official_result(
    work_root: Path,
    plan,
    predictions: tuple[SweBenchPrediction, ...],
    payload: dict[str, object],
    *,
    write_completed_artifacts: bool = False,
) -> bytes:
    directory = work_root / "logs" / "evaluation" / evaluation_run_id(plan, predictions)
    directory.mkdir(parents=True)
    body = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    (directory / "results.json").write_bytes(body)
    if write_completed_artifacts:
        prediction_by_id = {item.instance_id: item for item in predictions}
        model_directory = plan.model_name.replace("/", "__")
        for instance_id in payload["completed_ids"]:
            instance_root = directory / model_directory / instance_id
            instance_root.mkdir(parents=True)
            artifacts = {
                "eval.sh": b"#!/bin/sh\n",
                "patch.diff": prediction_by_id[instance_id].model_patch.encode("utf-8"),
                "report.json": b"{}\n",
                "run_instance.log": b"completed\n",
                "test_output.txt": b"tests completed\n",
            }
            for name, content in artifacts.items():
                (instance_root / name).write_bytes(content)
    return body


def test_run_id_changes_with_plan_digest_and_patch_digest() -> None:
    plan = build_test_plan()
    predictions = build_test_predictions(plan)
    changed_plan = build_test_plan(dataset_revision="4" * 40)
    changed_plan_predictions = build_test_predictions(changed_plan)
    changed_patch = list(predictions)
    changed_patch[0] = changed_patch[0].model_copy(
        update={
            "model_patch": changed_patch[0].model_patch.replace(
                "+after-default", "+after-changed"
            )
        }
    )

    original = evaluation_run_id(plan, predictions)
    plan_changed = evaluation_run_id(changed_plan, changed_plan_predictions)
    patch_changed = evaluation_run_id(plan, tuple(changed_patch))

    assert len({original, plan_changed, patch_changed}) == 3
    assert plan.plan_sha256[:12] in original
    assert predictions_sha256(predictions)[:12] in original


def test_official_argv_is_shell_free_tuple_for_exactly_25_instances_without_gold(
    tmp_path: Path,
) -> None:
    plan, executable, source, task_repo = _official_environment(tmp_path)
    predictions = build_test_predictions(plan)
    predictions_path = tmp_path / "predictions with spaces.jsonl"
    write_predictions(predictions_path, plan, predictions)

    command = build_official_evaluation_command(
        executable=executable,
        swebench_source=source,
        git_executable=GIT,
        plan=plan,
        predictions_path=predictions_path,
        predictions=predictions,
        workers=4,
        timeout_seconds=1_800,
        task_repo=task_repo,
    )
    instances = tuple(
        command[index + 1]
        for index, argument in enumerate(command)
        if argument == "--instance"
    )

    assert type(command) is tuple
    assert all(type(argument) is str for argument in command)
    assert command[0] == str(executable.resolve())
    assert str(predictions_path.resolve()) in command
    assert str(task_repo.resolve()) in command
    assert instances == tuple(case.instance_id for case in plan.cases)
    assert len(instances) == 25
    assert "--gold" not in command


@pytest.mark.parametrize(
    ("workers", "timeout_seconds"),
    [(0, 1_800), (33, 1_800), (4, 59), (4, 7_201)],
)
def test_official_command_rejects_invalid_worker_or_timeout_bounds(
    tmp_path: Path,
    workers: int,
    timeout_seconds: int,
) -> None:
    plan, executable, source, task_repo = _official_environment(tmp_path)
    predictions = build_test_predictions(plan)
    predictions_path = tmp_path / "predictions.jsonl"
    write_predictions(predictions_path, plan, predictions)

    with pytest.raises(KernelError) as caught:
        build_official_evaluation_command(
            executable=executable,
            swebench_source=source,
            git_executable=GIT,
            plan=plan,
            predictions_path=predictions_path,
            predictions=predictions,
            workers=workers,
            timeout_seconds=timeout_seconds,
            task_repo=task_repo,
        )

    assert caught.value.code == "swebench_evaluator_config_invalid"


@pytest.mark.parametrize("drift", ["evaluator", "task_repo", "executable"])
def test_official_command_rejects_pinned_environment_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    plan, executable, source, task_repo = _official_environment(tmp_path)
    if drift == "evaluator":
        plan = build_test_plan(
            swebench_revision="a" * 40,
            task_repo_revision=plan.task_repo_revision,
        )
    elif drift == "task_repo":
        plan = build_test_plan(
            swebench_revision=plan.swebench_revision,
            task_repo_revision="a" * 40,
        )
    else:
        executable = _executable(tmp_path / "outside" / "swebench")
    predictions = build_test_predictions(plan)
    predictions_path = tmp_path / "predictions.jsonl"
    write_predictions(predictions_path, plan, predictions)

    with pytest.raises(KernelError) as caught:
        build_official_evaluation_command(
            executable=executable,
            swebench_source=source,
            git_executable=GIT,
            plan=plan,
            predictions_path=predictions_path,
            predictions=predictions,
            workers=4,
            timeout_seconds=1_800,
            task_repo=task_repo,
        )

    assert caught.value.code in {
        "swebench_executable_source_mismatch",
        "swebench_revision_mismatch",
    }


def test_import_official_result_validates_counts_and_binds_summary(
    tmp_path: Path,
) -> None:
    plan = build_test_plan()
    predictions = build_test_predictions(plan, empty_at=0)
    empty_id = predictions[0].instance_id
    payload = _official_result_payload(plan, empty_patch_ids=(empty_id,))
    body = _write_official_result(
        tmp_path,
        plan,
        predictions,
        payload,
        write_completed_artifacts=True,
    )

    result, summary = import_official_result(
        work_root=tmp_path,
        plan=plan,
        predictions=predictions,
    )

    assert result.submitted_instances == 25
    assert result.completed_instances == len(result.completed_ids) == 24
    assert result.resolved_instances == len(result.resolved_ids) == 8
    assert result.unresolved_instances == len(result.unresolved_ids) == 16
    assert summary.run_id == evaluation_run_id(plan, predictions)
    assert summary.plan_sha256 == plan.plan_sha256
    assert summary.predictions_sha256 == predictions_sha256(predictions)
    assert summary.official_results_sha256 == hashlib.sha256(body).hexdigest()
    assert summary.submitted_cases == 25
    assert summary.completed_cases == 24
    assert summary.resolved_cases == 8
    assert summary.unresolved_cases == 16
    assert summary.infrastructure_failures == 1
    assert summary.ambiguous_failures == 1
    assert result.empty_patch_ids == (empty_id,)
    assert summary.empty_patch_cases == 1
    assert summary.artifact_files == 24 * 5
    assert len(summary.artifacts_sha256) == 64


def test_import_official_result_rejects_count_to_id_mismatch(tmp_path: Path) -> None:
    plan = build_test_plan()
    predictions = build_test_predictions(plan)
    payload = _official_result_payload(plan)
    payload["completed_instances"] = 24
    _write_official_result(tmp_path, plan, predictions, payload)

    with pytest.raises(KernelError) as caught:
        import_official_result(
            work_root=tmp_path,
            plan=plan,
            predictions=predictions,
        )

    assert caught.value.code == "swebench_results_invalid"


@pytest.mark.parametrize("total_instances", [24, 26])
def test_import_official_result_rejects_total_outside_registered_25_case_scope(
    tmp_path: Path,
    total_instances: int,
) -> None:
    plan = build_test_plan()
    predictions = build_test_predictions(plan)
    payload = _official_result_payload(plan)
    payload["total_instances"] = total_instances
    _write_official_result(tmp_path, plan, predictions, payload)

    with pytest.raises(KernelError) as caught:
        import_official_result(
            work_root=tmp_path,
            plan=plan,
            predictions=predictions,
        )

    assert caught.value.code == "swebench_results_scope_mismatch"


def test_import_official_result_rejects_submitted_scope_drift(tmp_path: Path) -> None:
    plan = build_test_plan()
    predictions = build_test_predictions(plan)
    payload = _official_result_payload(plan)
    submitted = list(payload["submitted_ids"])
    completed = list(payload["completed_ids"])
    unresolved = list(payload["unresolved_ids"])
    submitted[-1] = "outside__scope-999"
    completed[-1] = "outside__scope-999"
    unresolved[-1] = "outside__scope-999"
    payload["submitted_ids"] = tuple(sorted(submitted))
    payload["completed_ids"] = tuple(sorted(completed))
    payload["unresolved_ids"] = tuple(sorted(unresolved))
    _write_official_result(tmp_path, plan, predictions, payload)

    with pytest.raises(KernelError) as caught:
        import_official_result(
            work_root=tmp_path,
            plan=plan,
            predictions=predictions,
        )

    assert caught.value.code == "swebench_results_scope_mismatch"
