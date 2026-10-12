from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.evals.swebench import (
    SweBenchModelConfig,
    SweBenchModelParameter,
    SweBenchPreRegistrationPlan,
    SweBenchTaskBudget,
    build_swebench_preregistration_plan,
)
from harnessix.evals.swebench.predictions import (
    SweBenchPrediction,
    collect_workspace_patch,
    predictions_sha256,
    read_predictions,
    validate_predictions,
    write_predictions,
)

GIT = Path("/usr/bin/git")


def build_dataset_rows(count: int = 25) -> list[dict[str, object]]:
    return [
        {
            "instance_id": f"owner{index % 5}__repo{index % 5}-{index}",
            "repo": f"owner{index % 5}/repo{index % 5}",
            "base_commit": f"{index + 1:040x}",
            "problem_statement": f"Fix public issue {index}",
            "version": "1.0",
            "patch": f"gold patch {index}",
            "test_patch": f"gold test patch {index}",
            "FAIL_TO_PASS": [f"test_failed_{index}"],
            "PASS_TO_PASS": [f"test_passed_{index}"],
        }
        for index in range(count)
    ]


def build_test_plan(
    *,
    dataset_revision: str = "1" * 40,
    model_name: str = "model-v1",
    swebench_revision: str = "3" * 40,
    task_repo_revision: str = "5" * 40,
) -> SweBenchPreRegistrationPlan:
    return build_swebench_preregistration_plan(
        build_dataset_rows(),
        dataset_name="SWE-bench/SWE-bench_Verified",
        dataset_revision=dataset_revision,
        dataset_artifact_sha256="4" * 64,
        split="test",
        harnessix_revision="2" * 40,
        swebench_revision=swebench_revision,
        swebench_package_version="5.0.0",
        task_repo_revision=task_repo_revision,
        model_name=model_name,
        model_config=SweBenchModelConfig(
            provider="test-provider",
            parameters=(SweBenchModelParameter(name="temperature", value=0.0),),
        ),
        task_budget=SweBenchTaskBudget(
            max_steps=32,
            max_tokens=50_000,
            timeout_seconds=1_800.0,
            max_output_chars=65_536,
            max_tool_calls_per_step=16,
        ),
        salt="strict-swebench-test-salt-v1",
    )


def _patch(index: int, suffix: str = "default") -> str:
    return (
        f"diff --git a/file-{index}.txt b/file-{index}.txt\n"
        f"--- a/file-{index}.txt\n"
        f"+++ b/file-{index}.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        f"+after-{suffix}\n"
    )


def build_test_predictions(
    plan: SweBenchPreRegistrationPlan,
    *,
    empty_at: int | None = None,
) -> tuple[SweBenchPrediction, ...]:
    return tuple(
        SweBenchPrediction(
            instance_id=case.instance_id,
            model_name_or_path=plan.model_name,
            model_patch="" if index == empty_at else _patch(index),
        )
        for index, case in enumerate(plan.cases)
    )


def _run_git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        (str(GIT), *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _committed_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _run_git(repository, "init", "--quiet")
    _run_git(repository, "config", "user.name", "Harnessix Tests")
    _run_git(repository, "config", "user.email", "tests@harnessix.invalid")
    (repository / "tracked.txt").write_text("tracked before\n", encoding="utf-8")
    (repository / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    _run_git(repository, "add", "--all")
    _run_git(repository, "commit", "--quiet", "-m", "base")
    return repository, _run_git(repository, "rev-parse", "HEAD")


def test_predictions_require_exact_25_case_order_and_model_binding() -> None:
    plan = build_test_plan()
    predictions = build_test_predictions(plan)

    assert len(predictions) == 25
    assert plan.dataset_artifact_sha256 == "4" * 64
    assert plan.swebench_package_version == "5.0.0"
    assert plan.task_repo_revision == "5" * 40
    assert tuple(item.instance_id for item in predictions) == tuple(
        case.instance_id for case in plan.cases
    )
    assert {item.model_name_or_path for item in predictions} == {plan.model_name}
    assert validate_predictions(plan, predictions) == predictions


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "outside", "reordered"])
def test_predictions_reject_incomplete_duplicate_outside_or_reordered_scope(
    mutation: str,
) -> None:
    plan = build_test_plan()
    changed = list(build_test_predictions(plan))
    if mutation == "missing":
        changed.pop()
    elif mutation == "duplicate":
        changed[-1] = changed[0]
    elif mutation == "outside":
        changed[-1] = changed[-1].model_copy(
            update={"instance_id": "outside__scope-999"}
        )
    else:
        changed[0], changed[1] = changed[1], changed[0]

    with pytest.raises(KernelError) as caught:
        validate_predictions(plan, tuple(changed))

    assert caught.value.code == "swebench_predictions_scope_mismatch"


def test_predictions_reject_model_binding_drift() -> None:
    plan = build_test_plan()
    changed = list(build_test_predictions(plan))
    changed[7] = changed[7].model_copy(update={"model_name_or_path": "other-model"})

    with pytest.raises(KernelError) as caught:
        validate_predictions(plan, tuple(changed))

    assert caught.value.code == "swebench_prediction_model_mismatch"


def test_empty_patch_is_written_and_round_trips(tmp_path: Path) -> None:
    plan = build_test_plan()
    predictions = build_test_predictions(plan, empty_at=0)
    path = tmp_path / "predictions.jsonl"

    write_predictions(path, plan, predictions)
    loaded = read_predictions(path, plan)

    assert loaded[0].model_patch == ""
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])[
        "model_patch"
    ] == ""


def test_predictions_jsonl_round_trip_preserves_order_and_digest(tmp_path: Path) -> None:
    plan = build_test_plan()
    predictions = build_test_predictions(plan)
    path = tmp_path / "predictions.jsonl"

    digest = write_predictions(path, plan, predictions)
    loaded = read_predictions(path, plan)
    lines = path.read_text(encoding="utf-8").splitlines()

    assert loaded == predictions
    assert len(lines) == 25
    assert [json.loads(line)["instance_id"] for line in lines] == [
        case.instance_id for case in plan.cases
    ]
    assert digest == predictions_sha256(predictions)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_collect_workspace_patch_captures_modified_added_and_deleted_files_without_changing_index(
    tmp_path: Path,
) -> None:
    repository, base_commit = _committed_repository(tmp_path)
    (repository / "tracked.txt").write_text("tracked after\n", encoding="utf-8")
    (repository / "added.txt").write_text("new file\n", encoding="utf-8")
    (repository / "deleted.txt").unlink()
    real_index = repository / ".git" / "index"
    index_before = real_index.read_bytes()

    patch = collect_workspace_patch(repository, GIT, base_commit)

    assert "diff --git a/tracked.txt b/tracked.txt" in patch
    assert "-tracked before" in patch and "+tracked after" in patch
    assert "diff --git a/added.txt b/added.txt" in patch
    assert "new file mode" in patch and "+new file" in patch
    assert "diff --git a/deleted.txt b/deleted.txt" in patch
    assert "deleted file mode" in patch and "-delete me" in patch
    assert real_index.read_bytes() == index_before
    assert _run_git(repository, "diff", "--cached", "--name-only") == ""


def test_collect_workspace_patch_returns_empty_patch_for_clean_workspace(
    tmp_path: Path,
) -> None:
    repository, base_commit = _committed_repository(tmp_path)
    real_index = repository / ".git" / "index"
    index_before = real_index.read_bytes()

    patch = collect_workspace_patch(repository, GIT, base_commit)

    assert patch == ""
    assert real_index.read_bytes() == index_before


def test_collect_workspace_patch_rejects_head_drift(tmp_path: Path) -> None:
    repository, base_commit = _committed_repository(tmp_path)
    (repository / "later.txt").write_text("later\n", encoding="utf-8")
    _run_git(repository, "add", "later.txt")
    _run_git(repository, "commit", "--quiet", "-m", "later")

    with pytest.raises(KernelError) as caught:
        collect_workspace_patch(repository, GIT, base_commit)

    assert caught.value.code == "swebench_workspace_revision_mismatch"
