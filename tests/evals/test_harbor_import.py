"""冻结输入锁、外部结果和原生报告之间的身份、分母与未知值合同。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix import cli
from harnessix.agent.errors import KernelError
from harnessix.evals.harbor_contracts import HarborEvalReport
from harnessix.evals.harbor_import import safe_import_harbor_job
from harnessix.evals.langfuse_harbor_projection import build_langfuse_harbor_export
from harnessix.evals.report import read_harbor_eval_report


def job_source(
    root: Path,
    modes: tuple[str, ...] = ("pass", "task", "deadline", "approval", "provider", "missing"),
) -> Path:
    root.mkdir()
    job_id = str(uuid4())
    plans = []
    for index, mode in enumerate(modes):
        plan = {
            "schema_version": 2,
            "task": {"name": f"case-{index}", "digest": "sha256:" + str(index + 1) * 64},
            "agent": {
                "import_path": "integrations.harbor.harnessix_agent:HarnessixAgent",
                "kwargs": {},
            },
            "environment": {"type": "docker"},
            "verifier": {"environment_mode": "separate"},
        }
        plans.append(plan)
        if mode == "missing":
            continue
        directory = root / f"trial-{index}"
        directory.mkdir()
        trial_id = str(uuid4())
        reason = {
            "deadline": "deadline_exceeded",
            "approval": "intervention_required",
            "provider": "failed",
        }.get(mode, "completed")
        native = {
            "trial_id": trial_id,
            "thread_id": str(uuid4()),
            "turn_id": str(uuid4()),
            "protocol_reason": reason,
            "turn_status": "completed" if mode in {"pass", "task"} else "failed",
            "native_failure_category": "provider" if mode == "provider" else None,
            "observed_input_tokens": 200,
            "observed_output_tokens": 40,
        }
        value = {
            "id": trial_id,
            "task_name": plan["task"]["name"],
            "trial_name": directory.name,
            "config": {
                "job_id": job_id,
                "agent": plan["agent"],
                "environment": plan["environment"],
            },
            "agent_info": {"name": "harnessix", "version": "1.0.0rc1"},
            "agent_result": {"metadata": {"harnessix": native}, "cost_usd": None},
            "verifier_environment_mode": "separate",
            "verifier_result": {"rewards": {"reward": 1.0 if mode == "pass" else 0.0}},
            "exception_info": {
                "exception_type": "NonZeroAgentExitCodeError",
                "exception_message": "PRIVATE-EXCEPTION-MUST-NOT-EXPORT",
            }
            if mode in {"deadline", "approval", "provider"}
            else None,
            "started_at": "2026-11-05T10:01:00Z",
            "finished_at": "2026-11-05T10:02:00Z",
        }
        (directory / "lock.json").write_text(json.dumps(plan))
        (directory / "result.json").write_text(json.dumps(value))
    (root / "lock.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "created_at": "2026-11-05T10:00:00Z",
                "harbor": {"version": "0.25.0"},
                "retry": {"max_retries": 0},
                "trials": plans,
            }
        )
    )
    (root / "result.json").write_text(
        json.dumps(
            {
                "id": job_id,
                "n_total_trials": len(plans),
                "finished_at": "2026-11-05T10:03:00Z",
                "stats": {"n_retries": 0},
            }
        )
    )
    return root


def change_result(directory: Path, mutate) -> None:
    path = directory / "trial-0/result.json"
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))


def test_complete_denominator_classification_and_unknown_billing(tmp_path: Path) -> None:
    directory = job_source(tmp_path / "job")
    first = safe_import_harbor_job(directory)
    assert first.model_dump_json() == safe_import_harbor_job(directory).model_dump_json()
    assert first.summary.scheduled_trials == 6
    assert (
        first.summary.passed_trials,
        first.summary.failed_trials,
        first.summary.invalid_trials,
    ) == (1, 4, 1)
    assert [trial.classification for trial in first.trials] == [
        "passed",
        "task",
        "budget",
        "runtime",
        "provider",
        "eval_infrastructure",
    ]
    assert first.trials[-1].harbor_trial_id is None
    assert first.trials[-1].observed_input_tokens is None
    assert all(
        trial.cost_completeness == "unknown" and trial.reported_cost_usd is None
        for trial in first.trials
    )
    assert "PRIVATE-EXCEPTION" not in first.model_dump_json()
    export = build_langfuse_harbor_export(first)
    assert export.summary == first.summary
    assert export.model_dump_json() == build_langfuse_harbor_export(first).model_dump_json()
    assert sum(score["value"] for score in export.scores if score["name"] == "task_passed") == 1
    assert "generation" not in export.model_dump_json()


@pytest.mark.parametrize(
    "field", ["job_id", "trial_id", "task", "reward_bool", "agent_shape", "agent_config"]
)
def test_source_identity_and_malformed_shapes_fail_closed(tmp_path: Path, field: str) -> None:
    directory = job_source(tmp_path / "job", ("pass",))

    def mutate(data):
        if field == "job_id":
            data["config"]["job_id"] = str(uuid4())
        elif field == "trial_id":
            data["agent_result"]["metadata"]["harnessix"]["trial_id"] = str(uuid4())
        elif field == "task":
            data["task_name"] = "other-task"
        elif field == "reward_bool":
            data["verifier_result"]["rewards"]["reward"] = True
        elif field == "agent_config":
            data["config"]["agent"]["kwargs"] = {"other_model": "different"}
        else:
            data["agent_result"] = "PRIVATE-MALFORMED-BODY"

    change_result(directory, mutate)
    with pytest.raises(KernelError) as error:
        safe_import_harbor_job(directory)
    assert error.value.code == "harbor_source_invalid"
    assert "PRIVATE-" not in str(error.value)


@pytest.mark.parametrize("foreign_lock", [False, True])
def test_extra_or_duplicate_result_is_not_silently_excluded(
    tmp_path: Path, foreign_lock: bool
) -> None:
    import shutil

    directory = job_source(tmp_path / "job", ("pass",))
    duplicate = directory / "unexpected-trial"
    shutil.copytree(directory / "trial-0", duplicate)
    if foreign_lock:
        path = duplicate / "lock.json"
        value = json.loads(path.read_text())
        value["task"]["digest"] = "sha256:" + "a" * 64
        path.write_text(json.dumps(value))
    with pytest.raises(KernelError) as error:
        safe_import_harbor_job(directory)
    assert error.value.code == "harbor_source_invalid"


def test_naive_time_requires_explicit_source_timezone(tmp_path: Path) -> None:
    directory = job_source(tmp_path / "job", ("pass",))
    path = directory / "result.json"
    value = json.loads(path.read_text())
    value["finished_at"] = "2026-11-05T10:03:00"
    path.write_text(json.dumps(value))
    with pytest.raises(KernelError) as error:
        safe_import_harbor_job(directory)
    assert error.value.code == "harbor_timezone_required"
    report = safe_import_harbor_job(directory, source_timezone="UTC")
    assert report.source_timezone == "UTC"
    assert report.completed_at.utcoffset().total_seconds() == 0


@pytest.mark.parametrize("observed", [False, True])
def test_automatic_retries_are_not_collapsed_into_one_final_result(
    tmp_path: Path, observed: bool
) -> None:
    directory = job_source(tmp_path / "job", ("pass",))
    path = directory / ("result.json" if observed else "lock.json")
    value = json.loads(path.read_text())
    if observed:
        value["stats"]["n_retries"] = 1
    else:
        value["retry"]["max_retries"] = 1
    path.write_text(json.dumps(value))
    with pytest.raises(KernelError) as error:
        safe_import_harbor_job(directory)
    assert error.value.code == "harbor_retries_unsupported"


def test_report_cannot_promote_missing_result_or_tamper_summary(tmp_path: Path) -> None:
    report = safe_import_harbor_job(job_source(tmp_path / "job"))
    altered = report.model_copy(
        update={"summary": report.summary.model_copy(update={"passed_trials": 6})}
    )
    with pytest.raises(ValueError):
        HarborEvalReport.model_validate_json(altered.model_dump_json())
    with pytest.raises(ValueError):
        build_langfuse_harbor_export(altered)
    with pytest.raises(ValueError):
        HarborEvalReport.model_validate_json(
            report.model_copy(update={"pass_reward": 0.0}).model_dump_json()
        )


def test_offline_cli_writes_private_report_and_protects_sources(tmp_path: Path, capsys) -> None:
    directory = job_source(tmp_path / "job")
    before = (directory / "result.json").read_bytes()
    output = tmp_path / "native.json"
    cli.main(
        ["coding-eval-import-harbor", "--job-directory", str(directory), "--output", str(output)]
    )
    assert json.loads(capsys.readouterr().out)["reason"] == "imported"
    assert read_harbor_eval_report(output).summary.scheduled_trials == 6
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit):
        cli.main(
            [
                "coding-eval-import-harbor",
                "--job-directory",
                str(directory),
                "--output",
                str(directory / "result.json"),
            ]
        )
    assert (directory / "result.json").read_bytes() == before
