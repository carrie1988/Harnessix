"""Harbor Job导入：完整分母、状态判定、身份绑定、零重试，以及奖励判分器读取冻结制品。"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from evalkit import harbor as evalkit_harbor
from evalkit.contracts import EvalRunReport, Usage
from evalkit.errors import EvalkitError
from evalkit.grading import build_report
from evalkit.harbor import AgentResult, HarborRewardGrader, import_harbor_job

from tests.evalkit.builders import HARBOR_REWARD, Trial, change_result, harbor_job, harbor_plan

DONE = datetime(2026, 11, 5, 11, 0, tzinfo=UTC)


class ProviderParser:
    """把智能体自报的失败映射为通用状态；provider_turn作为必须唯一的系统引用。"""

    agent_name = "fixture-agent"
    unique_refs = ("turn_id",)

    def parse(self, trial_dir: Path, result: Mapping[str, Any]) -> AgentResult:
        native = result["agent_result"]["metadata"]
        refs = {"turn_id": native["turn_id"]} if "turn_id" in native else {}
        usage = Usage(completeness="partial", input_tokens=native.get("tokens", 0))
        artifacts = (
            {"patch": trial_dir / "patch.diff"} if (trial_dir / "patch.diff").exists() else {}
        )
        if native.get("failed") == "provider":
            return AgentResult("system_error", "provider_failed", usage, artifacts, refs)
        return AgentResult(usage=usage, artifact_files=artifacts, system_refs=refs)


def report_for(job: Path, plan, parsers=None) -> EvalRunReport:
    trials = import_harbor_job(job, plan, "A", parsers or {}, job.parent)
    return build_report(plan, trials, (HarborRewardGrader(HARBOR_REWARD),), job.parent, DONE)


def test_complete_denominator_statuses_and_reward_grading(tmp_path: Path) -> None:
    specs = [
        Trial(reward=1.0),
        Trial(reward=0.0),
        Trial(exception="NonZeroAgentExitCodeError"),
        Trial(exception="AgentTimeoutError", reward=0.0),
        Trial(exception="EnvironmentStartTimeoutError", reward=None),
        Trial(reward=None),
        None,
    ]
    job = harbor_job(tmp_path / "job", specs)
    plan = harbor_plan(len(specs))
    first = report_for(job, plan)
    assert first == report_for(job, plan)
    assert [(t.status, t.reason) for t in first.trials] == [
        ("completed", "completed"),
        ("completed", "completed"),
        ("system_error", "agent_failed"),
        ("timeout", "deadline_exceeded"),
        ("infra_error", "harbor_infrastructure_error"),
        ("completed", "completed"),
        ("missing", "missing_result"),
    ]
    assert first.summary[0].model_dump() == {
        "arm_id": "A",
        "scheduled": 7,
        "passed": 1,
        "failed": 3,
        "invalid": 3,
    }
    assert first.trials[0].score("harbor-reward", "reward_passed").value is True
    assert first.trials[5].scores == ()
    assert first.trials[6].executor_refs.keys() == {"harbor_job_id", "input_lock_sha256"}
    assert all(t.cost.completeness == "unknown" for t in first.trials)
    assert "PRIVATE-EXCEPTION" not in first.model_dump_json()


def test_parser_refines_status_usage_artifacts_and_unique_refs(tmp_path: Path) -> None:
    specs = [
        Trial(metadata=lambda _: {"turn_id": "t1", "tokens": 30}),
        Trial(
            exception="NonZeroAgentExitCodeError",
            metadata=lambda _: {"turn_id": "t2", "failed": "provider"},
        ),
    ]
    job = harbor_job(tmp_path / "job", specs)
    (job / "trial-0-0/patch.diff").write_text("diff --git a/x b/x\n")
    trials = import_harbor_job(
        job, harbor_plan(2), "A", {"fixture-agent": ProviderParser()}, tmp_path
    )
    assert (trials[1].status, trials[1].reason) == ("system_error", "provider_failed")
    assert trials[0].usage.input_tokens == 30
    assert [a.name for a in trials[0].artifacts] == ["harbor_result", "patch"]
    assert trials[0].artifacts[1].path == "job/trial-0-0/patch.diff"
    assert trials[0].system_refs == {"turn_id": "t1"}

    change_result(job, lambda d: d["agent_result"]["metadata"].update(turn_id="t1"), "trial-1-0")
    with pytest.raises(EvalkitError) as error:
        import_harbor_job(job, harbor_plan(2), "A", {"fixture-agent": ProviderParser()}, tmp_path)
    assert error.value.code == "harbor_source_invalid"


def test_an_oversized_agent_artifact_only_affects_its_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超限制品不中止整个Job的导入：该试次不登记它并在执行引用中注明，其余试次照常。"""
    monkeypatch.setattr(evalkit_harbor, "_MAX_AGENT_ARTIFACT_BYTES", 64)
    job = harbor_job(tmp_path / "job", [Trial(reward=0.0), Trial(reward=1.0)])
    (job / "trial-0-0/patch.diff").write_text("diff --git a/x b/x\n" + "+" * 64)
    (job / "trial-1-0/patch.diff").write_text("diff --git a/y b/y\n")
    trials = import_harbor_job(
        job, harbor_plan(2), "A", {"fixture-agent": ProviderParser()}, tmp_path
    )
    assert [a.name for a in trials[0].artifacts] == ["harbor_result"]
    assert trials[0].executor_refs[evalkit_harbor.OVERSIZED_ARTIFACTS_REF] == "patch"
    assert [a.name for a in trials[1].artifacts] == ["harbor_result", "patch"]
    assert evalkit_harbor.OVERSIZED_ARTIFACTS_REF not in trials[1].executor_refs


def test_attempts_bind_to_plan_slots_and_job_must_cover_the_plan(tmp_path: Path) -> None:
    job = harbor_job(tmp_path / "job", [Trial(), Trial(reward=0.0)], attempts=2)
    trials = import_harbor_job(job, harbor_plan(2, attempts=2), "A", {}, tmp_path)
    assert [(t.slot, t.item_id, t.attempt) for t in trials] == [
        (0, "case-0", 0),
        (1, "case-0", 1),
        (2, "case-1", 0),
        (3, "case-1", 1),
    ]
    for plan in (
        harbor_plan(2),
        harbor_plan(3, attempts=2),
        harbor_plan(2, attempts=2, system="x"),
    ):
        with pytest.raises(EvalkitError) as error:
            import_harbor_job(job, plan, "A", {}, tmp_path)
        assert error.value.code == "harbor_source_invalid"


@pytest.mark.parametrize(
    "field", ["job_id", "task", "reward_bool", "agent_shape", "agent_config", "trial_time"]
)
def test_source_identity_and_malformed_shapes_fail_closed(tmp_path: Path, field: str) -> None:
    job = harbor_job(tmp_path / "job", [Trial()])

    def mutate(data: dict[str, Any]) -> None:
        if field == "job_id":
            data["config"]["job_id"] = "00000000-0000-4000-8000-000000000000"
        elif field == "task":
            data["task_name"] = "other-task"
        elif field == "reward_bool":
            data["verifier_result"]["rewards"]["reward"] = True
        elif field == "agent_config":
            data["config"]["agent"]["kwargs"] = {"model": "different"}
        elif field == "trial_time":
            data["started_at"] = "2026-11-05T09:59:00Z"
        else:
            data["agent_result"] = "PRIVATE-MALFORMED-BODY"

    change_result(job, mutate, "trial-0-0")
    with pytest.raises(EvalkitError) as error:
        import_harbor_job(job, harbor_plan(1), "A", {}, tmp_path)
    assert error.value.code == "harbor_source_invalid"
    assert "PRIVATE-" not in str(error.value)


@pytest.mark.parametrize("foreign_lock", [False, True])
def test_extra_or_duplicate_result_is_not_silently_excluded(
    tmp_path: Path, foreign_lock: bool
) -> None:
    job = harbor_job(tmp_path / "job", [Trial()])
    duplicate = job / "unexpected-trial"
    shutil.copytree(job / "trial-0-0", duplicate)
    if foreign_lock:
        path = duplicate / "lock.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["task"]["digest"] = "sha256:" + "a" * 64
        path.write_text(json.dumps(value))
    with pytest.raises(EvalkitError) as error:
        import_harbor_job(job, harbor_plan(1), "A", {}, tmp_path)
    assert error.value.code == "harbor_source_invalid"


def test_naive_time_requires_explicit_source_timezone(tmp_path: Path) -> None:
    job = harbor_job(tmp_path / "job", [Trial()])
    path = job / "result.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["finished_at"] = "2026-11-05T10:30:00"
    path.write_text(json.dumps(value))
    with pytest.raises(EvalkitError) as error:
        import_harbor_job(job, harbor_plan(1), "A", {}, tmp_path)
    assert error.value.code == "harbor_timezone_required"
    trials = import_harbor_job(job, harbor_plan(1), "A", {}, tmp_path, source_timezone="UTC")
    assert trials[0].status == "completed"


@pytest.mark.parametrize("observed", [False, True])
def test_automatic_retries_are_not_collapsed_into_one_final_result(
    tmp_path: Path, observed: bool
) -> None:
    job = harbor_job(tmp_path / "job", [Trial()])
    path = job / ("result.json" if observed else "lock.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    if observed:
        value["stats"]["n_retries"] = 1
    else:
        value["retry"]["max_retries"] = 1
    path.write_text(json.dumps(value))
    with pytest.raises(EvalkitError) as error:
        import_harbor_job(job, harbor_plan(1), "A", {}, tmp_path)
    assert error.value.code == "harbor_retries_unsupported"


def test_job_outside_evidence_root_or_wrong_executor_is_rejected(tmp_path: Path) -> None:
    job = harbor_job(tmp_path / "job", [Trial()])
    with pytest.raises(EvalkitError) as error:
        import_harbor_job(job, harbor_plan(1), "A", {}, tmp_path / "elsewhere-does-not-exist")
    assert error.value.code == "harbor_source_invalid"
    (tmp_path / "other").mkdir()
    with pytest.raises(EvalkitError) as error:
        import_harbor_job(job, harbor_plan(1), "A", {}, tmp_path / "other")
    assert error.value.code == "harbor_evidence_root_invalid"
    with pytest.raises(EvalkitError) as error:
        import_harbor_job(job, harbor_plan(1), "B", {}, tmp_path)
    assert error.value.code == "harbor_arm_invalid"


def test_reward_grader_rejects_changed_frozen_result(tmp_path: Path) -> None:
    job = harbor_job(tmp_path / "job", [Trial(reward=0.0)])
    plan = harbor_plan(1)
    trials = import_harbor_job(job, plan, "A", {}, tmp_path)
    change_result(job, lambda d: d["verifier_result"]["rewards"].update(reward=1.0), "trial-0-0")
    with pytest.raises(EvalkitError) as error:
        build_report(plan, trials, (HarborRewardGrader(HARBOR_REWARD),), tmp_path, DONE)
    assert error.value.code == "artifact_digest_mismatch"


def test_cli_import_report_and_consistency_keep_sources_private(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from evalkit import cli
    from evalkit.contracts import ArmTrials, GraderSpec
    from evalkit.files import read_model, write_model

    second = GraderSpec(grader_id="harbor-reward-2", version="0.25.0", pass_score="reward_passed")
    plan = harbor_plan(2, graders=(HARBOR_REWARD, second))
    plan_path = tmp_path / "plan.json"
    write_model(plan_path, plan, 1024 * 1024)
    job = harbor_job(tmp_path / "evidence" / "job", [Trial(), None])
    before = (job / "trial-0-0/result.json").read_bytes()
    trials, report, compare = (tmp_path / name for name in ("t.json", "r.json", "c.json"))
    common = ["--plan", str(plan_path), "--evidence-root", str(tmp_path / "evidence")]
    cli.main(
        [
            "import-harbor",
            *common,
            "--arm",
            "A",
            "--job-directory",
            str(job),
            "--output",
            str(trials),
        ]
    )
    assert json.loads(capsys.readouterr().out) == {
        "reason": "imported",
        "arm_id": "A",
        "trials": 2,
        "missing": 1,
    }
    assert read_model(trials, ArmTrials, 1 << 20, require_private=True).plan_digest == plan.digest
    graders = ["--grader", "harbor-reward=evalkit.harbor:HarborRewardGrader"]
    graders += ["--grader", "harbor-reward-2=evalkit.harbor:HarborRewardGrader"]
    cli.main(["report", *common, "--trials", str(trials), *graders, "--output", str(report)])
    assert json.loads(capsys.readouterr().out)["summary"][0]["passed"] == 1
    cli.main(
        ["consistency", "--report", str(report), "--first", "harbor-reward"]
        + ["--second", "harbor-reward-2", "--output", str(compare)]
    )
    assert json.loads(capsys.readouterr().out)["disagreements"] == 0
    for arguments in (
        ["import-harbor", *common, "--arm", "A", "--job-directory", str(job)]
        + ["--output", str(job / "x.json")],
        ["report", *common, "--trials", str(trials), "--grader", "llm-judge=evalkit.harbor:X"]
        + ["--output", str(tmp_path / "r2.json")],
    ):
        with pytest.raises(SystemExit):
            cli.main(arguments)
        assert json.loads(capsys.readouterr().out)["reason"] in {
            "output_conflicts_with_source",
            "grader_not_in_plan",
        }
    assert (job / "trial-0-0/result.json").read_bytes() == before
    assert not (job / "x.json").exists()
