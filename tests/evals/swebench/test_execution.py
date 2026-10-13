from __future__ import annotations

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
    ToolResultContent,
    Turn,
    TurnStatus,
    Usage,
)
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL
from harnessix.domain.models import EffectClass
from harnessix.evals.swebench.contracts import (
    SweBenchModelConfig,
    SweBenchModelParameter,
    SweBenchTaskBudget,
    build_swebench_preregistration_plan,
)
from harnessix.evals.swebench.execution_io import (
    read_case_execution_state,
    read_suite_execution_state,
)
from harnessix.evals.swebench.predictions import read_predictions
from harnessix.evals.swebench.public_test_profile import (
    build_swebench_case_process_profile,
)
from harnessix.evals.swebench.run_config import build_swebench_agent_run_config
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
async def test_suite_freezes_all_25_cases_and_reopens_same_turns(
    tmp_path: Path,
    first_fails: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    config = _config(tmp_path, plan)
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

    monkeypatch.setattr(execution, "require_harnessix_execution_revision", lambda *_: None)

    def materialize(case, **_kwargs):
        workspace = Path(config.work_root) / "workspaces" / case.instance_id
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        return workspace

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
        lambda turn, _config: (1, turn.turn_id != UUID(int=1003)),
    )
    monkeypatch.setattr(execution, "collect_workspace_patch", lambda *_: PATCH)

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
    assert first["unaccounted_cost_cases"] == second["unaccounted_cost_cases"] == 1
    assert first["reserved_cost_amount"] == second["reserved_cost_amount"] == "3.147456"
    assert len(predictions) == 25 and all(item.model_patch == PATCH for item in predictions)
    assert suite.status == "completed"
    expected_routes = [
        (case.instance_id, config.process_profile_for(case.instance_id).image)
        for case in plan.cases
    ]
    assert calls == expected_routes * 2


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
    assert execution._case_evidence_failure(turn, PATCH, PUBLIC_TEST_TOOL) is None


@pytest.mark.parametrize("output", [None, {}, {"status": {}}, {"diff": {}}])
def test_evidence_rejects_incomplete_aggregate_even_after_legacy_review(output: object) -> None:
    turn = _turn(0, (*SUCCESSFUL_EVIDENCE, ("git_review", "succeeded", None, output)))
    assert (
        execution._case_evidence_failure(turn, PATCH, PUBLIC_TEST_TOOL)[0]
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
        execution._case_evidence_failure(turn, PATCH, PUBLIC_TEST_TOOL)[0]
        == "swebench_git_review_incomplete"
    )


def test_evidence_requires_aggregate_after_test_and_rejects_latest_failure() -> None:
    review = ("git_review", "succeeded", None, _git_review_output())
    for results in (
        (SUCCESSFUL_EVIDENCE[0], review, SUCCESSFUL_EVIDENCE[1]),
        (*SUCCESSFUL_EVIDENCE[:2], review, ("git_review", "failed", "git_observation_failed")),
    ):
        assert (
            execution._case_evidence_failure(_turn(0, results), PATCH, PUBLIC_TEST_TOOL)[0]
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
        execution._case_evidence_failure(turn, PATCH, PUBLIC_TEST_TOOL)[0]
        == "swebench_tool_state_unavailable"
    )
