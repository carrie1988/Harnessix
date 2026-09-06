from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.models import (
    Budget,
    Item,
    ItemStatus,
    PatchEffect,
    TextContent,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
    Usage,
)
from harnessix.domain.models import ContractModel, EffectClass
from harnessix.evals.contracts import (
    CodingEvalEnvironment,
    CodingEvalReport,
    CodingEvalTask,
    EvalFinalAnswer,
    EvalGitEvidence,
    EvalRepository,
    EvalTestObservation,
)
from harnessix.evals.grader import grade_coding_eval

SHA = "a" * 64
SOURCE_REVISION = "b" * 40
BASELINE_REVISION = "c" * 40
CHANGED_PATH = "src/harnessix/models/_chat_stream.py"


def task(**updates: object) -> CodingEvalTask:
    values: dict[str, object] = {
        "task_id": "harnessix-empty-tool-id",
        "task_version": 1,
        "repository": EvalRepository(
            name="Harnessix",
            origin="https://github.com/carrie1988/Harnessix",
            source_revision=SOURCE_REVISION,
            baseline_tree_sha256=SHA,
        ),
        "prompt": "修复工具调用增量中的空 ID 兼容缺陷，并按约定输出 JSON。",
        "allowed_changed_paths": (CHANGED_PATH,),
        "required_test_profiles": ("unit",),
        "baseline_checks": ("empty-id",),
        "behavior_checks": ("empty-id",),
        "regression_checks": ("identity-drift",),
        "max_changed_files": 1,
        "budget": Budget(max_steps=12, max_tokens=1000),
    }
    values.update(updates)
    return CodingEvalTask.model_validate(values)


def item(content: object) -> Item:
    return Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=content)


def call(tool: str, effect: EffectClass = EffectClass.READ_ONLY) -> ToolCallContent:
    return ToolCallContent(
        call_id=uuid4(),
        provider_call_id=f"provider-{uuid4()}",
        tool=tool,
        tool_version="1",
        effect_class=effect,
        arguments={},
        requires_approval=effect is not EffectClass.READ_ONLY,
    )


def tool_pair(
    tool: str,
    *,
    output: object = None,
    effect: EffectClass = EffectClass.READ_ONLY,
    patch: PatchEffect | None = None,
) -> tuple[Item, Item]:
    tool_call = call(tool, effect)
    return (
        item(tool_call),
        item(
            ToolResultContent(
                call_id=tool_call.call_id,
                outcome="succeeded",
                output=output,
                patch=patch,
            )
        ),
    )


def completed_turn(*, changed_path: str = CHANGED_PATH, ordered: bool = True) -> Turn:
    failed = tool_pair(
        "run_tests",
        output={"profile": "unit", "passed": False},
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
    )
    patch = tool_pair(
        "apply_patch",
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        patch=PatchEffect(
            workspace_id=uuid4(),
            plan_id=uuid4(),
            request_id=SHA,
            approval_fingerprint=SHA,
            state="applied",
            origin="execution",
        ),
    )
    passed = tool_pair(
        "run_tests",
        output={"profile": "unit", "passed": True},
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
    )
    status = tool_pair("git_status", output={"total_entries": 1})
    diff = tool_pair("git_diff", output={"observed_sha256": SHA})
    sequence = (*failed, *patch, *passed, *status, *diff)
    if not ordered:
        sequence = (*passed, *patch, *failed, *status, *diff)
    answer = (
        '{"summary":"已修复空 ID 增量兼容并保留身份漂移校验",'
        f'"changed_paths":["{changed_path}"],'
        '"tests":[{"profile":"unit","passed":true}]}'
    )
    now = datetime.now(UTC)
    return Turn(
        turn_id=uuid4(),
        request_id="eval-run",
        request_fingerprint=SHA,
        status=TurnStatus.COMPLETED,
        budget=task().budget,
        items=(
            item(TextContent(kind="user_message", text="修复缺陷")),
            *sequence,
            item(TextContent(kind="assistant_message", text=answer)),
        ),
        model_steps=6,
        usage=Usage(input_tokens=100, output_tokens=40),
        created_at=now,
        completed_at=now,
    )


def observations() -> tuple[tuple[EvalTestObservation, ...], tuple[EvalTestObservation, ...]]:
    baseline = (
        EvalTestObservation(
            check_id="empty-id",
            phase="baseline",
            passed=False,
            returncode=1,
            output_sha256=SHA,
            elapsed_seconds=0.1,
        ),
    )
    final = (
        EvalTestObservation(
            check_id="empty-id",
            phase="final",
            passed=True,
            returncode=0,
            output_sha256=SHA,
            elapsed_seconds=0.1,
        ),
        EvalTestObservation(
            check_id="identity-drift",
            phase="final",
            passed=True,
            returncode=0,
            output_sha256=SHA,
            elapsed_seconds=0.1,
        ),
    )
    return baseline, final


def git_evidence(*, changed_paths: tuple[str, ...] = (CHANGED_PATH,)) -> EvalGitEvidence:
    return EvalGitEvidence(
        baseline_revision=BASELINE_REVISION,
        baseline_tree_sha256=SHA,
        head_revision=BASELINE_REVISION,
        changed_paths=changed_paths,
        staged_paths=(),
        untracked_paths=(),
        unsupported_change_paths=(),
        status_sha256=SHA,
        diff_sha256=SHA,
        diff_observed_bytes=128,
    )


def environment() -> CodingEvalEnvironment:
    return CodingEvalEnvironment(
        harnessix_revision=SOURCE_REVISION,
        provider="scripted-infrastructure",
        model="none",
        platform="test-posix",
        isolation="private-managed-copy-no-os-sandbox",
    )


def grade(
    *,
    current_task: CodingEvalTask | None = None,
    turn: Turn | None = None,
    baseline: tuple[EvalTestObservation, ...] | None = None,
    final: tuple[EvalTestObservation, ...] | None = None,
    git: EvalGitEvidence | None = None,
):
    expected_baseline, expected_final = observations()
    now = datetime.now(UTC)
    return grade_coding_eval(
        current_task or task(),
        turn or completed_turn(),
        run_id=UUID("00000000-0000-4000-8000-000000000001"),
        environment=environment(),
        started_at=now,
        completed_at=now + timedelta(seconds=1),
        baseline_observations=baseline if baseline is not None else expected_baseline,
        final_observations=final if final is not None else expected_final,
        git=git or git_evidence(),
    )


def test_task_contract_is_versioned_strict_and_fingerprinted() -> None:
    first = task()
    second = task(prompt="另一个固定任务")
    assert first.spec_version == "harnessix.coding-eval/v1"
    assert first.grader_version == "coding-eval-grader/v1"
    assert first.fingerprint != second.fingerprint
    with pytest.raises(ValidationError):
        task(allowed_changed_paths=("../escape.py",))
    with pytest.raises(ValidationError):
        task(regression_checks=("z", "a"))
    with pytest.raises(ValidationError):
        CodingEvalTask.model_validate({**first.model_dump(), "unexpected": True})
    coerced = first.model_dump(mode="json")
    coerced["budget"]["max_steps"] = "12"
    with pytest.raises(ValidationError):
        CodingEvalTask.model_validate_json(json.dumps(coerced))
    embedded_secret = first.repository.model_copy(
        update={"origin": "https://user:secret@example.invalid/repository"}
    )
    with pytest.raises(ValidationError):
        task(repository=embedded_secret.model_dump())


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("coding-eval-task", CodingEvalTask),
        ("coding-eval-final-answer", EvalFinalAnswer),
        ("coding-eval-report", CodingEvalReport),
    ],
)
def test_public_schema_is_frozen(name: str, model: type[ContractModel]) -> None:
    assert json.loads(Path(f"spec/{name}-v1.schema.json").read_text()) == model.model_json_schema()


def test_success_requires_behavior_regression_git_and_answer_evidence() -> None:
    report = grade()
    assert report.outcome == "passed" and not report.failure_categories
    assert all(check.passed for check in report.checks)
    assert report.final_answer is not None
    assert report.final_answer.changed_paths == (CHANGED_PATH,)
    assert "已修复空 ID" not in report.model_dump_json()
    assert report.metrics == report.metrics.model_copy(update={"tool_calls": 5, "changed_files": 1})


def test_report_rejects_removed_checks_or_reclassified_failures() -> None:
    payload = grade().model_dump(mode="json")
    payload["checks"] = payload["checks"][:-1]
    with pytest.raises(ValidationError):
        CodingEvalReport.model_validate(payload)
    payload = grade().model_dump(mode="json")
    payload["checks"][0]["category"] = "budget"
    with pytest.raises(ValidationError):
        CodingEvalReport.model_validate(payload)


def test_unreproducible_baseline_makes_run_invalid() -> None:
    baseline, final = observations()
    unexpected_pass = (baseline[0].model_copy(update={"passed": True, "returncode": 0}),)
    report = grade(baseline=unexpected_pass, final=final)
    assert report.outcome == "invalid"
    assert "eval_infrastructure" in report.failure_categories


def test_extra_final_check_makes_run_invalid() -> None:
    baseline, final = observations()
    extra = EvalTestObservation(
        check_id="unexpected",
        phase="final",
        passed=True,
        returncode=0,
        output_sha256=SHA,
        elapsed_seconds=0.1,
    )
    report = grade(baseline=baseline, final=(*final, extra))
    assert report.outcome == "invalid"
    assert (
        next(check for check in report.checks if check.code == "final_check_set_matched").passed
        is False
    )


@pytest.mark.parametrize(
    ("turn", "git", "category"),
    [
        (completed_turn(ordered=False), git_evidence(), "correctness"),
        (
            completed_turn(changed_path="src/forbidden.py"),
            git_evidence(changed_paths=("src/forbidden.py",)),
            "forbidden_edit",
        ),
        (completed_turn(changed_path="src/other.py"), git_evidence(), "final_answer"),
    ],
)
def test_failed_run_is_classified_without_exposing_diff(
    turn: Turn, git: EvalGitEvidence, category: str
) -> None:
    report = grade(turn=turn, git=git)
    assert report.outcome == "failed" and category in report.failure_categories
    assert "return a" not in report.model_dump_json()
