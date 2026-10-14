from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

import harnessix.evals.swebench.execution as execution
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    AgentFailure,
    Budget,
    Item,
    ItemStatus,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    Turn,
    TurnStatus,
    Usage,
)
from harnessix.agent.tool_rejections import rejection_result
from harnessix.agent.usage import ModelAttempt, UsageObservation
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL
from harnessix.domain.models import EffectClass
from harnessix.evals.swebench.contracts import (
    SweBenchModelConfig,
    SweBenchModelParameter,
    SweBenchTaskBudget,
    build_swebench_preregistration_plan,
)
from harnessix.evals.swebench.execution_contracts import (
    build_swebench_suite_execution_state,
    swebench_case_execution_state_digest,
)
from harnessix.evals.swebench.execution_io import (
    read_case_execution_state,
    read_suite_execution_state,
    write_suite_execution_state,
)
from harnessix.evals.swebench.predictions import read_predictions
from harnessix.evals.swebench.public_test_profile import (
    build_swebench_case_process_profile,
)
from harnessix.evals.swebench.run_config import (
    SweBenchRecyclingAgentRunConfig,
    build_swebench_agent_run_config,
)
from harnessix.models.pricing import amount_units, format_amount
from harnessix.product_config.process_profile import process_tool_name

NOW = datetime(2026, 10, 12, 2, tzinfo=UTC)
PUBLIC_TEST_TOOL = process_tool_name("public-tests")
PATCH = (
    "diff --git a/example.py b/example.py\n"
    "--- a/example.py\n"
    "+++ b/example.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)
ToolResult = tuple[str, str, str | None] | tuple[str, str, str | None, object]
SUCCESSFUL_EVIDENCE: tuple[ToolResult, ...] = (
    (WORKSPACE_PATCH_TOOL, "succeeded", None),
    (PUBLIC_TEST_TOOL, "succeeded", None),
    ("git_status", "succeeded", None),
    ("git_diff", "succeeded", None),
)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution, "utc_now", lambda: NOW)
    # 单元测试不依赖宿主剩余空间；真实空间门禁另有独立正负控。
    monkeypatch.setattr(execution, "require_evaluation_disk_space", lambda *_: None)


def _plan():
    rows = tuple(
        {
            "instance_id": f"owner{index % 5}__repo{index % 5}-{index}",
            "repo": f"owner{index % 5}/repo{index % 5}",
            "base_commit": f"{index + 1:040x}",
            "problem_statement": f"Fix issue {index}",
            "version": "1.0",
        }
        for index in range(30)
    )
    return build_swebench_preregistration_plan(
        rows,
        dataset_name="SWE-bench/SWE-bench_Verified",
        dataset_revision="1" * 40,
        dataset_artifact_sha256="2" * 64,
        split="test",
        harnessix_revision="3" * 40,
        swebench_revision="4" * 40,
        swebench_package_version="5.0.2",
        task_repo_revision="5" * 40,
        model_name="harnessix-qwen3.8-max",
        model_config=SweBenchModelConfig(
            provider="openai_chat",
            parameters=(SweBenchModelParameter(name="thinking", value="disabled"),),
        ),
        task_budget=SweBenchTaskBudget(
            max_steps=16,
            max_tokens=50_000,
            timeout_seconds=900,
            max_output_chars=65_536,
            max_tool_calls_per_step=8,
        ),
        salt="harnessix-swebench-execution-test-v1",
    )


def _config(tmp_path: Path, plan):
    source = tmp_path / "source"
    source.mkdir()
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
    return build_swebench_agent_run_config(
        plan,
        source_root=source,
        work_root=tmp_path / "work",
        cache_root=tmp_path / "cache",
        git_executable=Path("/usr/bin/git"),
        case_process_profiles=profiles,
        fee_stop_amount="80",
        observed_at=NOW,
    )


def _turn(
    index: int,
    results: tuple[ToolResult, ...] = SUCCESSFUL_EVIDENCE,
    *,
    status: TurnStatus = TurnStatus.COMPLETED,
    turn_error: str | None = None,
) -> Turn:
    items: list[Item] = []
    for offset, result_spec in enumerate(results):
        tool, outcome, error_code = result_spec[:3]
        call_id = UUID(int=10_000 + index * 100 + offset)
        effect_class = (
            EffectClass.NON_IDEMPOTENT_WRITE
            if tool in {WORKSPACE_PATCH_TOOL, PUBLIC_TEST_TOOL}
            else EffectClass.READ_ONLY
        )
        items.extend(
            (
                Item(
                    item_id=UUID(int=20_000 + index * 200 + offset * 2),
                    status=ItemStatus.COMPLETED,
                    content=ToolCallContent(
                        call_id=call_id,
                        provider_call_id=f"provider-{index}-{offset}",
                        tool=tool,
                        tool_version="1",
                        effect_class=effect_class,
                        arguments={},
                        requires_approval=effect_class is not EffectClass.READ_ONLY,
                    ),
                ),
                Item(
                    item_id=UUID(int=20_001 + index * 200 + offset * 2),
                    status=ItemStatus.COMPLETED,
                    content=ToolResultContent(
                        call_id=call_id,
                        outcome=outcome,
                        output=result_spec[3] if len(result_spec) == 4 else None,
                        error=(
                            AgentFailure(code=error_code, message="failed")
                            if error_code is not None
                            else None
                        ),
                    ),
                ),
            )
        )
    return Turn(
        turn_id=UUID(int=1000 + index),
        request_id=f"request-{index}",
        request_fingerprint="a" * 64,
        status=status,
        error=AgentFailure(code=turn_error, message="failed") if turn_error else None,
        budget=Budget(),
        usage=Usage(input_tokens=100 + index, output_tokens=10),
        items=tuple(items),
        created_at=NOW + timedelta(minutes=index),
        completed_at=NOW + timedelta(minutes=index, seconds=30),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_fails", [False, True])
@pytest.mark.parametrize("recycle", [False, True])
async def test_suite_freezes_all_25_cases_and_reopens_same_turns(
    tmp_path: Path,
    first_fails: bool,
    recycle: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    config = _config(tmp_path, plan)
    if recycle:
        config = SweBenchRecyclingAgentRunConfig.model_validate(
            {**config.model_dump(), "spec_version": "harnessix.swebench-agent-run-config/v9"}
        )
    turns = {case.instance_id: _turn(index) for index, case in enumerate(plan.cases)}
    turns[plan.cases[0].instance_id] = _turn(
        0,
        (
            (WORKSPACE_PATCH_TOOL, "succeeded", None),
            (PUBLIC_TEST_TOOL, "failed", "process_nonzero_exit"),
            ("read_file", "succeeded", None),
            (WORKSPACE_PATCH_TOOL, "succeeded", None),
            (PUBLIC_TEST_TOOL, "succeeded", None),
            ("git_status", "succeeded", None),
            ("git_diff", "succeeded", None),
        ),
    )
    if first_fails:
        turns[plan.cases[0].instance_id] = _turn(
            0, (), status=TurnStatus.FAILED, turn_error="budget_exceeded"
        )
    calls: list[tuple[str, str]] = []
    materialized = []
    sealed = {}
    collected = []

    monkeypatch.setattr(execution, "require_harnessix_execution_revision", lambda *_: None)

    def materialize(case, **_kwargs):
        # 入口必须逐项创建；v9上一项封存后才允许创建下一项。
        if recycle:
            assert len(materialized) == len(sealed)
        materialized.append(case.instance_id)
        workspace = Path(config.work_root) / "workspaces" / case.instance_id
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        return workspace

    def seal(_plan, _config, index, state, patch, **_kwargs):
        assert state.thread_id is not None and state.turn_id is not None
        assert state.patch_sha256 == hashlib.sha256(patch.encode()).hexdigest()
        sealed[index] = patch

    def collect(workspace, *_args):
        collected.append(workspace)
        return PATCH

    async def open_turn(
        _plan,
        _config,
        case,
        _case_root,
        _workspace,
        process_profile,
        _api_key,
        _cancel,
    ):
        calls.append((case.instance_id, process_profile.image))
        index = plan.cases.index(case)
        return UUID(int=500 + index), turns[case.instance_id]

    monkeypatch.setattr(execution, "materialize_swebench_workspace", materialize)
    monkeypatch.setattr(execution, "_open_case_turn", open_turn)
    monkeypatch.setattr(
        execution,
        "_turn_cost",
        lambda *_: (1, True),
    )
    monkeypatch.setattr(execution, "collect_workspace_patch", collect)
    # 本组只验证Suite编排；签名、文件回收及原生认证由独立真实Session测试覆盖。
    monkeypatch.setattr(execution, "seal_and_retire_workspace", seal)
    monkeypatch.setattr(execution, "read_retired_patch", lambda _p, _c, i, **_: sealed.get(i))

    first = await execution.run_swebench_agent_suite(
        plan,
        config,
        api_key="secret",
        allow_repository_network=True,
        allow_provider_network=True,
    )
    second = await execution.run_swebench_agent_suite(
        plan,
        config,
        api_key="secret",
        allow_repository_network=False,
        allow_provider_network=True,
    )

    predictions = read_predictions(Path(config.work_root) / "predictions.jsonl", plan)
    suite = read_suite_execution_state(Path(config.work_root) / "suite-state.json", plan)
    assert first["attempted_cases"] == second["attempted_cases"] == 25
    assert first["completed_cases"] == second["completed_cases"] == 25 - first_fails
    assert first["quality_failed_cases"] == second["quality_failed_cases"] == int(first_fails)
    assert first["unaccounted_cost_cases"] == second["unaccounted_cost_cases"] == 0
    assert first["reserved_cost_amount"] == second["reserved_cost_amount"] == "0"
    assert len(predictions) == 25 and all(item.model_patch == PATCH for item in predictions)
    assert suite.status == "completed"
    expected_routes = [
        (case.instance_id, config.process_profile_for(case.instance_id).image)
        for case in plan.cases
    ]
    assert calls == expected_routes * 2
    assert len(materialized) == len(collected) == (25 if recycle else 50)
    assert len(sealed) == (25 if recycle else 0)


@pytest.mark.parametrize(
    ("results", "patch", "expected_code"),
    (
        pytest.param(
            (
                (PUBLIC_TEST_TOOL, "succeeded", None),
                ("git_status", "succeeded", None),
                ("git_diff", "succeeded", None),
            ),
            "",
            "swebench_patch_missing",
            id="no-patch-blocker",
        ),
        pytest.param(
            ((WORKSPACE_PATCH_TOOL, "succeeded", None),),
            PATCH,
            "swebench_public_tests_missing",
            id="public-tests-missing",
        ),
        pytest.param(
            (
                (WORKSPACE_PATCH_TOOL, "succeeded", None),
                (PUBLIC_TEST_TOOL, "failed", "process_nonzero_exit"),
            ),
            PATCH,
            "swebench_public_tests_failed",
            id="code-failure-without-repair-retest",
        ),
        *(
            pytest.param(
                (
                    (WORKSPACE_PATCH_TOOL, "succeeded", None),
                    (PUBLIC_TEST_TOOL, outcome, error_code),
                ),
                PATCH,
                "swebench_public_tests_unavailable",
                id=case_id,
            )
            for outcome, error_code, case_id in (
                ("failed", "tool_invalid_arguments", "invalid-arguments"),
                ("failed", "process_launch_failed", "launch-failed"),
                ("failed", "process_timeout", "timeout"),
                ("failed", "process_output_unavailable", "output-unavailable"),
                ("unknown", "process_state_unknown", "unknown-state"),
            )
        ),
        pytest.param(
            (
                (WORKSPACE_PATCH_TOOL, "succeeded", None),
                (PUBLIC_TEST_TOOL, "failed", "tool_invalid_arguments"),
                (WORKSPACE_PATCH_TOOL, "succeeded", None),
                (PUBLIC_TEST_TOOL, "succeeded", None),
                ("git_status", "succeeded", None),
                ("git_diff", "succeeded", None),
            ),
            PATCH,
            "swebench_public_tests_unavailable",
            id="invalid-arguments-cannot-recover-to-completed",
        ),
        pytest.param(
            (
                (WORKSPACE_PATCH_TOOL, "succeeded", None),
                (PUBLIC_TEST_TOOL, "succeeded", None),
                ("git_status", "succeeded", None),
            ),
            PATCH,
            "swebench_git_review_incomplete",
            id="git-diff-missing",
        ),
        pytest.param(
            (
                (WORKSPACE_PATCH_TOOL, "succeeded", None),
                ("git_status", "succeeded", None),
                ("git_diff", "succeeded", None),
                (PUBLIC_TEST_TOOL, "succeeded", None),
            ),
            PATCH,
            "swebench_git_review_incomplete",
            id="git-review-before-tests",
        ),
        pytest.param(
            (*SUCCESSFUL_EVIDENCE, ("git_diff", "failed", "git_observation_failed")),
            PATCH,
            "swebench_git_review_incomplete",
            id="latest-git-review-failed",
        ),
        pytest.param(
            (
                (WORKSPACE_PATCH_TOOL, "succeeded", None),
                (PUBLIC_TEST_TOOL, "succeeded", None),
                ("git_status", "succeeded", None),
                ("git_diff", "succeeded", None),
                (WORKSPACE_PATCH_TOOL, "succeeded", None),
            ),
            PATCH,
            "swebench_public_tests_missing",
            id="unverified-final-patch",
        ),
        pytest.param(
            (
                (WORKSPACE_PATCH_TOOL, "succeeded", None),
                (WORKSPACE_PATCH_TOOL, "failed", "patch_ambiguous_context"),
            ),
            PATCH,
            "swebench_patch_failed",
            id="failed-final-patch",
        ),
        pytest.param(
            SUCCESSFUL_EVIDENCE,
            "",
            "swebench_patch_missing",
            id="empty-workspace-patch",
        ),
    ),
)
@pytest.mark.asyncio
async def test_suite_preserves_failure_and_only_continues_quality_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    results: tuple[ToolResult, ...],
    patch: str,
    expected_code: str,
) -> None:
    plan = _plan()
    config = _config(tmp_path, plan)
    turn = _turn(0, results)

    monkeypatch.setattr(execution, "require_harnessix_execution_revision", lambda *_: None)

    def materialize(case, **_kwargs):
        workspace = Path(config.work_root) / "workspaces" / case.instance_id
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        return workspace

    async def open_turn(*_args, **_kwargs):
        return UUID(int=500), turn

    monkeypatch.setattr(execution, "materialize_swebench_workspace", materialize)
    monkeypatch.setattr(execution, "_open_case_turn", open_turn)
    monkeypatch.setattr(execution, "_turn_cost", lambda *_: (0, True))
    monkeypatch.setattr(execution, "collect_workspace_patch", lambda *_: patch)

    fatal = expected_code == "swebench_public_tests_unavailable"
    caught_code = None
    try:
        result = await execution.run_swebench_agent_suite(
            plan,
            config,
            api_key="secret",
            allow_repository_network=False,
            allow_provider_network=True,
        )

    except KernelError as error:
        caught_code = error.code

    state = read_case_execution_state(
        Path(config.work_root) / "cases" / "case-000" / "case-state.json",
        plan,
    )
    suite = read_suite_execution_state(Path(config.work_root) / "suite-state.json", plan)
    assert state.status == "failed"
    if fatal:
        assert caught_code == expected_code
        assert suite.status == "failed"
        assert not (Path(config.work_root) / "predictions.jsonl").exists()
    else:
        assert caught_code is None
        assert suite.status == "completed"
        assert result["attempted_cases"] == result["quality_failed_cases"] == 25
        assert result["completed_cases"] == 0
        assert len(read_predictions(Path(config.work_root) / "predictions.jsonl", plan)) == 25
    assert state.turn_status.value == "completed"
    assert state.error_code == expected_code


def _git_review_output() -> dict[str, object]:
    return {
        "spec_version": "harnessix.git-review/v1",
        "observation_mode": "sequential_non_atomic",
        "status": {"entries": [], "total_entries": 0, "truncated": False, "revision": "a" * 64},
        "diff": {
            "target": "worktree",
            "text": PATCH,
            "utf8_bytes": len(PATCH.encode()),
            "observed_bytes": len(PATCH.encode()),
            "observed_sha256": "b" * 64,
            "truncated": False,
        },
    }


def test_evidence_accepts_typed_aggregate_after_final_public_test() -> None:
    turn = _turn(
        0, (*SUCCESSFUL_EVIDENCE[:2], ("git_review", "succeeded", None, _git_review_output()))
    )
    assert (
        execution._case_evidence_failure(turn, PATCH, PUBLIC_TEST_TOOL, cost_complete=True) is None
    )


@pytest.mark.parametrize("output", [None, {}, {"status": {}}, {"diff": {}}])
def test_evidence_rejects_incomplete_aggregate_even_after_legacy_review(output: object) -> None:
    turn = _turn(0, (*SUCCESSFUL_EVIDENCE, ("git_review", "succeeded", None, output)))
    assert (
        execution._case_evidence_failure(turn, PATCH, PUBLIC_TEST_TOOL, cost_complete=True)[0]
        == "swebench_git_review_incomplete"
    )


@pytest.mark.parametrize("target,truncated", [("staged", False), ("worktree", True)])
def test_evidence_rejects_wrong_diff_or_truncated_aggregate(target: str, truncated: bool) -> None:
    output = _git_review_output()
    output["diff"]["target"] = target
    output["diff"]["truncated"] = truncated
    if truncated:
        output["diff"]["observed_bytes"] += 1
    turn = _turn(0, (*SUCCESSFUL_EVIDENCE[:2], ("git_review", "succeeded", None, output)))
    assert (
        execution._case_evidence_failure(turn, PATCH, PUBLIC_TEST_TOOL, cost_complete=True)[0]
        == "swebench_git_review_incomplete"
    )


def test_evidence_requires_aggregate_after_test_and_rejects_latest_failure() -> None:
    review = ("git_review", "succeeded", None, _git_review_output())
    for results in (
        (SUCCESSFUL_EVIDENCE[0], review, SUCCESSFUL_EVIDENCE[1]),
        (*SUCCESSFUL_EVIDENCE[:2], review, ("git_review", "failed", "git_observation_failed")),
    ):
        assert (
            execution._case_evidence_failure(
                _turn(0, results), PATCH, PUBLIC_TEST_TOOL, cost_complete=True
            )[0]
            == "swebench_git_review_incomplete"
        )


@pytest.mark.parametrize("tool", [WORKSPACE_PATCH_TOOL, "git_review"])
@pytest.mark.parametrize("outcome", ["unknown", "cancelled"])
def test_unknown_or_cancelled_tool_cannot_become_continuable_quality_failure(
    tool: str, outcome: str
) -> None:
    turn = _turn(
        0,
        (*SUCCESSFUL_EVIDENCE, (tool, outcome, "tool_state_unknown")),
        status=TurnStatus.FAILED,
        turn_error="budget_exceeded",
    )
    assert (
        execution._case_evidence_failure(turn, PATCH, PUBLIC_TEST_TOOL, cost_complete=True)[0]
        == "swebench_tool_state_unavailable"
    )


def _accounted_turn(index, config, *, turn_error=None, last_attempt_changes=None):
    turn = _turn(
        index,
        status=TurnStatus.FAILED if turn_error else TurnStatus.COMPLETED,
        turn_error=turn_error,
    )
    attempts = []
    for step in range(1, 9):
        values = {
            "attempt_id": UUID(int=30_000 + index * 100 + step),
            "step": step,
            "index": 1,
            "provider": "openai_chat",
            "requested_model": config.price.model,
            "actual_model": config.price.model,
            "response_id": f"response-{index}-{step}",
            "usage": UsageObservation(completeness="complete", input_tokens=100, output_tokens=10),
            "status": "completed",
            "started_at": turn.created_at,
            "finished_at": turn.completed_at,
        }
        if step == 8:
            values.update(last_attempt_changes or {})
        attempts.append(ModelAttempt.model_validate(values))
    return turn.model_copy(
        update={
            "model_steps": 8,
            "model_attempts": tuple(attempts),
            "usage": Usage(
                input_tokens=sum(attempt.usage.input_tokens or 0 for attempt in attempts),
                output_tokens=sum(attempt.usage.output_tokens or 0 for attempt in attempts),
            ),
        }
    )


def _offline_suite(monkeypatch, plan, config, turns):
    calls = []
    monkeypatch.setattr(execution, "require_harnessix_execution_revision", lambda *_: None)

    def materialize(case, **_kwargs):
        workspace = Path(config.work_root) / "workspaces" / case.instance_id
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        return workspace

    async def open_turn(_plan, _config, case, *_args):
        index = plan.cases.index(case)
        calls.append(case.instance_id)
        return UUID(int=500 + index), turns[index]

    monkeypatch.setattr(execution, "materialize_swebench_workspace", materialize)
    monkeypatch.setattr(execution, "_open_case_turn", open_turn)
    monkeypatch.setattr(execution, "collect_workspace_patch", lambda *_: PATCH)
    return calls


async def _run_offline_suite(plan, config):
    return await execution.run_swebench_agent_suite(
        plan,
        config,
        api_key="offline-fixture",
        allow_repository_network=False,
        allow_provider_network=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("last_status", ["completed", "failed"])
@pytest.mark.parametrize(
    "turn_error", ["invalid_provider_output", "provider_invalid_provider_output"]
)
async def test_known_invalid_response_continues_without_retry_and_preserves_resume_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, last_status: str, turn_error: str
) -> None:
    plan = _plan()
    config = _config(tmp_path, plan)
    turns = {index: _accounted_turn(index, config) for index in range(25)}
    turns[2] = _accounted_turn(
        2,
        config,
        turn_error=turn_error,
        last_attempt_changes=(
            {
                "status": "failed",
                "error": AgentFailure(code=turn_error, message="fixture"),
            }
            if last_status == "failed"
            else {}
        ),
    )
    original = turns[2].model_dump_json()
    calls = _offline_suite(monkeypatch, plan, config, turns)
    assert execution._turn_cost(turns[2], config)[1]

    first = await _run_offline_suite(plan, config)
    root = Path(config.work_root)
    case_path = root / "cases" / "case-002" / "case-state.json"
    frozen_case = case_path.read_bytes()
    frozen_suite = (root / "suite-state.json").read_bytes()
    state = read_case_execution_state(case_path, plan)
    assert state.status == "failed" and state.turn_status.value == "failed"
    assert state.error_code == "swebench_model_response_invalid"
    assert state.state_sha256 == swebench_case_execution_state_digest(state)
    assert state.patch_sha256 == hashlib.sha256(PATCH.encode()).hexdigest()

    second = await _run_offline_suite(plan, config)
    assert first == second
    assert first["attempted_cases"] == 25
    assert first["completed_cases"] == 24 and first["quality_failed_cases"] == 1
    assert first["reserved_cost_amount"] == "0" and first["unaccounted_cost_cases"] == 0
    assert calls == [case.instance_id for case in plan.cases] * 2
    assert turns[2].model_dump_json() == original
    assert case_path.read_bytes() == frozen_case
    assert (root / "suite-state.json").read_bytes() == frozen_suite
    assert len(read_predictions(root / "predictions.jsonl", plan)) == 25
    assert (
        plan.task_budget.max_tokens,
        plan.task_budget.max_steps,
        plan.task_budget.timeout_seconds,
    ) == (50_000, 16, 900)


@pytest.mark.parametrize(
    "scenario", ["partial", "unknown", "uncovered_step", "wrong_model", "legacy"]
)
@pytest.mark.parametrize(
    "turn_error", ["invalid_provider_output", "provider_invalid_provider_output", "budget_exceeded"]
)
@pytest.mark.asyncio
async def test_unresolved_cost_stops_immediately_keeps_full_reserve_and_cannot_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, turn_error: str
) -> None:
    plan = _plan()
    config = _config(tmp_path, plan)
    changes = {}
    if scenario in {"partial", "unknown"}:
        changes = {
            "status": "failed",
            "error": AgentFailure(code="provider_failed", message="fixture"),
            "usage": (
                UsageObservation(completeness="partial", input_tokens=100)
                if scenario == "partial"
                else UsageObservation()
            ),
        }
    elif scenario == "wrong_model":
        changes = {"actual_model": "unpriced-model"}
    turn = _accounted_turn(0, config, turn_error=turn_error, last_attempt_changes=changes)
    if scenario == "uncovered_step":
        turn = turn.model_copy(update={"model_steps": 9})
    elif scenario == "legacy":
        turn = turn.model_copy(update={"model_attempts": ()})
    original = turn.model_dump_json()
    known, complete = execution._turn_cost(turn, config)
    assert not complete
    if scenario != "legacy":
        assert known > 0
    reserve = execution._unknown_request_reserve(config)
    assert known + reserve < amount_units(config.fee_stop_amount)
    assert format_amount(reserve) == "3.147456"
    calls = _offline_suite(monkeypatch, plan, config, {0: turn})

    with pytest.raises(KernelError) as stopped:
        await _run_offline_suite(plan, config)
    assert stopped.value.code == "swebench_cost_incomplete"
    assert f"{config.price.currency} {format_amount(reserve)}" in str(stopped.value)
    root = Path(config.work_root)
    suite = read_suite_execution_state(root / "suite-state.json", plan)
    assert suite.status == "failed" and suite.attempted_case_ids == ()
    assert suite.case_states[0].error_code == "swebench_cost_incomplete"
    assert suite.case_states[0].turn_status.value == "failed"
    assert not (root / "predictions.jsonl").exists()
    with pytest.raises(KernelError) as resumed:
        await _run_offline_suite(plan, config)
    assert resumed.value.code == "swebench_execution_stopped"
    assert calls == [plan.cases[0].instance_id]
    assert turn.model_dump_json() == original


@pytest.mark.parametrize("tool", [WORKSPACE_PATCH_TOOL, PUBLIC_TEST_TOOL, "git_review"])
@pytest.mark.parametrize("outcome", ["unknown", "cancelled"])
@pytest.mark.parametrize("cost_complete", [True, False])
def test_unsettled_tool_outcome_takes_priority_over_invalid_response_and_cost(
    tool: str, outcome: str, cost_complete: bool
) -> None:
    turn = _turn(
        0,
        (*SUCCESSFUL_EVIDENCE, (tool, outcome, "tool_state_unknown")),
        status=TurnStatus.FAILED,
        turn_error="invalid_provider_output",
    )
    failure = execution._case_evidence_failure(
        turn, PATCH, PUBLIC_TEST_TOOL, cost_complete=cost_complete
    )
    assert failure[0] == (
        "swebench_public_tests_unavailable"
        if tool == PUBLIC_TEST_TOOL
        else "swebench_tool_state_unavailable"
    )


@pytest.mark.parametrize(
    "defect",
    ["unpaired_call", "orphan_result", "duplicate_result", "started_call", "cancelled_result"],
)
@pytest.mark.parametrize("turn_error", [None, "invalid_provider_output", "budget_exceeded"])
def test_unpaired_or_unfinished_tool_facts_cannot_be_success_or_quality(
    defect: str, turn_error: str | None
) -> None:
    turn = _turn(
        0,
        status=TurnStatus.FAILED if turn_error else TurnStatus.COMPLETED,
        turn_error=turn_error,
    )
    items = list(turn.items)
    if defect == "unpaired_call":
        items.pop()
    elif defect == "orphan_result":
        items.pop(-2)
    elif defect == "duplicate_result":
        items.append(items[-1])
    elif defect == "started_call":
        items[-2] = items[-2].model_copy(update={"status": ItemStatus.STARTED})
    else:
        items[-1] = items[-1].model_copy(update={"status": ItemStatus.CANCELLED})
    turn = turn.model_copy(update={"items": tuple(items)})
    assert (
        execution._case_evidence_failure(turn, PATCH, PUBLIC_TEST_TOOL, cost_complete=True)[0]
        == "swebench_tool_state_unavailable"
    )


@pytest.mark.parametrize("closed", [True, False])
def test_catalog_rejection_is_only_accepted_as_a_closed_known_no_effect_pair(closed: bool) -> None:
    call_id = UUID(int=60_000)
    rejected = Item(
        item_id=UUID(int=60_001),
        status=ItemStatus.COMPLETED,
        content=ToolCallRejectionContent(
            call_id=call_id, provider_call_id="rejected", model_step=1
        ),
    )
    result = Item(
        item_id=UUID(int=60_002),
        status=ItemStatus.COMPLETED,
        content=rejection_result(call_id),
    )
    turn = _turn(0)
    turn = turn.model_copy(
        update={"items": (*turn.items, rejected, *((result,) if closed else ()))}
    )
    failure = execution._case_evidence_failure(turn, PATCH, PUBLIC_TEST_TOOL, cost_complete=True)
    assert failure is None if closed else failure[0] == "swebench_tool_state_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["prefix", "patch", "cost", "none"])
async def test_mapped_quality_failure_resume_rejects_prefix_or_runtime_evidence_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    plan = _plan()
    config = _config(tmp_path, plan)
    turn = _accounted_turn(0, config, turn_error="invalid_provider_output")
    turns = {index: _accounted_turn(index, config) for index in range(25)}
    turns[0] = turn
    calls = _offline_suite(monkeypatch, plan, config, turns)
    open_turn = execution._open_case_turn

    async def stop_before_second_case(*args):
        if args[2] != plan.cases[0]:
            raise KernelError("fixture_stop", "offline interruption")
        return await open_turn(*args)

    monkeypatch.setattr(execution, "_open_case_turn", stop_before_second_case)
    with pytest.raises(KernelError, match="offline interruption"):
        await _run_offline_suite(plan, config)
    root = Path(config.work_root)
    suite = read_suite_execution_state(root / "suite-state.json", plan)
    assert suite.attempted_case_ids == (plan.cases[0].instance_id,)
    assert suite.completed_case_ids == ()
    case_before = (root / "cases" / "case-000" / "case-state.json").read_bytes()
    if tamper == "prefix":
        states = list(suite.case_states)
        states[0] = execution.build_swebench_case_execution_state(
            plan,
            states[0].instance_id,
            states[0].run_id,
            status="failed",
            thread_id=states[0].thread_id,
            turn_id=states[0].turn_id,
            turn_status=states[0].turn_status,
            patch_sha256=states[0].patch_sha256,
            input_tokens=states[0].input_tokens + 1,
            output_tokens=states[0].output_tokens,
            error_code=states[0].error_code,
            started_at=states[0].started_at,
            updated_at=states[0].updated_at,
        )
        with pytest.raises(KernelError) as drift:
            execution._reconcile_suite_state(root, plan, states)
        assert drift.value.code == "swebench_suite_execution_state_invalid"
    elif tamper != "none":
        if tamper == "patch":
            monkeypatch.setattr(execution, "collect_workspace_patch", lambda *_: PATCH + "drift")
        else:
            monkeypatch.setattr(execution, "_turn_cost", lambda *_: (0, False))
        with pytest.raises(KernelError) as drift:
            await _run_offline_suite(plan, config)
        assert drift.value.code == "swebench_case_evidence_mismatch"
    else:
        monkeypatch.setattr(execution, "_open_case_turn", open_turn)
        result = await _run_offline_suite(plan, config)
        assert result["completed_cases"] == 24 and result["quality_failed_cases"] == 1
        assert calls == [plan.cases[0].instance_id, *[case.instance_id for case in plan.cases]]
    if tamper != "none":
        assert len(calls) == (1 if tamper == "prefix" else 2)
    assert (root / "cases" / "case-000" / "case-state.json").read_bytes() == case_before


@pytest.mark.asyncio
async def test_v1_resume_does_not_upgrade_or_continue_a_mapped_quality_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan()
    config = _config(tmp_path, plan)
    execution.ensure_private_directory(
        Path(config.work_root),
        error_code="swebench_execution_root_invalid",
        label="SWE-bench私有运行目录",
    )
    states = execution._load_or_initialize_case_states(Path(config.work_root), plan, NOW)
    failed = execution.build_swebench_case_execution_state(
        plan,
        states[0].instance_id,
        states[0].run_id,
        status="failed",
        thread_id=UUID(int=500),
        turn_id=UUID(int=1000),
        turn_status=execution.TurnStatusV18.FAILED,
        error_code="swebench_model_response_invalid",
        input_tokens=800,
        output_tokens=80,
        started_at=NOW,
    )
    states[0] = failed
    execution.write_case_execution_state(
        Path(config.work_root) / "cases" / "case-000" / "case-state.json", plan, failed
    )
    suite = build_swebench_suite_execution_state(plan, states)
    write_suite_execution_state(Path(config.work_root) / "suite-state.json", plan, suite)
    calls = _offline_suite(monkeypatch, plan, config, {})
    with pytest.raises(KernelError) as stopped:
        await _run_offline_suite(plan, config)
    assert stopped.value.code == "swebench_execution_stopped"
    assert calls == []
    assert read_suite_execution_state(Path(config.work_root) / "suite-state.json", plan) == suite
