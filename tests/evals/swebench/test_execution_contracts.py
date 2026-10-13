from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import TurnStatusV18
from harnessix.evals.swebench.contracts import (
    SweBenchModelConfig,
    SweBenchModelParameter,
    SweBenchPreRegistrationPlan,
    SweBenchTaskBudget,
    build_swebench_preregistration_plan,
)
from harnessix.evals.swebench.execution_contracts import (
    SweBenchCaseExecutionState,
    SweBenchExecutionStatus,
    SweBenchSuiteExecutionState,
    build_swebench_case_execution_state,
    build_swebench_suite_execution_state,
    is_swebench_quality_failure,
    swebench_case_execution_state_digest,
    swebench_suite_execution_state_digest,
)
from harnessix.evals.swebench.execution_io import (
    read_case_execution_state,
    read_suite_execution_state,
    write_case_execution_state,
    write_suite_execution_state,
)

NOW = datetime(2026, 10, 12, 1, 2, 3, tzinfo=UTC)
QUALITY_FAILURE_CODES = (
    "budget_exceeded",
    "swebench_patch_missing",
    "swebench_patch_failed",
    "swebench_public_tests_missing",
    "swebench_public_tests_failed",
    "swebench_git_review_incomplete",
)


def _plan(*, model_name: str = "example/model-v1") -> SweBenchPreRegistrationPlan:
    rows = [
        {
            "instance_id": f"owner{index % 5}__repo{index % 5}-{index}",
            "repo": f"owner{index % 5}/repo{index % 5}",
            "base_commit": f"{index + 1:040x}",
            "problem_statement": f"Fix public issue {index}",
            "version": "1.0",
        }
        for index in range(30)
    ]
    return build_swebench_preregistration_plan(
        rows,
        dataset_name="SWE-bench/SWE-bench_Verified",
        dataset_revision="1" * 40,
        dataset_artifact_sha256="2" * 64,
        split="test",
        harnessix_revision="3" * 40,
        swebench_revision="4" * 40,
        swebench_package_version="5.0.0",
        task_repo_revision="5" * 40,
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
        salt="strict-execution-state-salt-v1",
    )


def _run_id(index: int) -> UUID:
    return UUID(int=index + 1)


def _ready_states(plan: SweBenchPreRegistrationPlan) -> list[SweBenchCaseExecutionState]:
    return [
        build_swebench_case_execution_state(
            plan,
            case.instance_id,
            _run_id(index),
            started_at=NOW,
        )
        for index, case in enumerate(plan.cases)
    ]


def _completed_state(
    plan: SweBenchPreRegistrationPlan,
    index: int,
    *,
    input_tokens: int,
    output_tokens: int,
) -> SweBenchCaseExecutionState:
    return build_swebench_case_execution_state(
        plan,
        plan.cases[index].instance_id,
        _run_id(index),
        status="completed",
        thread_id=UUID(int=100 + index),
        turn_id=UUID(int=200 + index),
        turn_status=TurnStatusV18.COMPLETED,
        patch_sha256=f"{index + 7:064x}",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        started_at=NOW,
        updated_at=NOW + timedelta(minutes=index + 1),
    )


def _case_with_changes(
    state: SweBenchCaseExecutionState, **changes: object
) -> SweBenchCaseExecutionState:
    candidate = state.model_copy(update=changes)
    return SweBenchCaseExecutionState.model_validate(
        {
            **candidate.model_dump(mode="python"),
            "state_sha256": swebench_case_execution_state_digest(candidate),
        },
        strict=True,
    )


def _failed_state(
    plan: SweBenchPreRegistrationPlan,
    index: int,
    *,
    error_code: str = "swebench_public_tests_failed",
    turn_status: TurnStatusV18 = TurnStatusV18.FAILED,
) -> SweBenchCaseExecutionState:
    return _case_with_changes(
        _completed_state(plan, index, input_tokens=100, output_tokens=10),
        status="failed",
        turn_status=turn_status,
        error_code=error_code,
    )


def _running_state(plan: SweBenchPreRegistrationPlan, index: int) -> SweBenchCaseExecutionState:
    return _case_with_changes(
        _completed_state(plan, index, input_tokens=25, output_tokens=5),
        status="running",
        turn_status=TurnStatusV18.CALLING_MODEL,
    )


def test_case_state_binds_plan_runtime_result_and_digest() -> None:
    plan = _plan()
    state = _completed_state(plan, 0, input_tokens=123, output_tokens=45)

    assert state.plan_sha256 == plan.plan_sha256
    assert state.instance_id == plan.cases[0].instance_id
    assert state.base_commit == state.workspace_head == plan.cases[0].base_commit
    assert state.input_tokens == 123 and state.output_tokens == 45
    assert state.state_sha256 == swebench_case_execution_state_digest(state)

    payload = state.model_dump(mode="python")
    payload["output_tokens"] = 46
    with pytest.raises(ValidationError, match="摘要"):
        SweBenchCaseExecutionState.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "running"},
        {"status": "failed", "error_code": None},
        {"workspace_head": "f" * 40},
        {"turn_id": UUID(int=999), "thread_id": None},
        {"turn_status": None},
        {"patch_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
    ],
)
def test_case_state_rejects_invalid_lifecycle_even_with_recomputed_digest(
    changes: dict[str, object],
) -> None:
    plan = _plan()
    original = _completed_state(plan, 0, input_tokens=1, output_tokens=2)
    candidate = original.model_copy(update={**changes, "state_sha256": "0" * 64})
    payload = candidate.model_dump(mode="python")
    payload["state_sha256"] = swebench_case_execution_state_digest(candidate)

    with pytest.raises(ValidationError):
        SweBenchCaseExecutionState.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    ("status", "turn_status", "error_code"),
    [
        ("failed", TurnStatusV18.FAILED, "provider_failed"),
        ("cancelled", TurnStatusV18.CANCELLED, None),
    ],
)
def test_failed_and_cancelled_case_states_drive_matching_suite_terminal_status(
    status: SweBenchExecutionStatus,
    turn_status: TurnStatusV18,
    error_code: str | None,
) -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = build_swebench_case_execution_state(
        plan,
        plan.cases[0].instance_id,
        _run_id(0),
        status=status,
        thread_id=UUID(int=100),
        turn_id=UUID(int=200),
        turn_status=turn_status,
        error_code=error_code,
        started_at=NOW,
        updated_at=NOW + timedelta(minutes=1),
    )

    assert build_swebench_suite_execution_state(plan, states).status == status


def test_suite_completed_state_requires_the_entire_registered_prefix() -> None:
    plan = _plan()
    states = [
        _completed_state(plan, index, input_tokens=index + 1, output_tokens=1)
        for index in range(len(plan.cases))
    ]

    suite = build_swebench_suite_execution_state(plan, states)

    assert suite.status == "completed"
    assert suite.completed_case_ids == tuple(case.instance_id for case in plan.cases)
    assert suite.current_case_id is None
    assert suite.input_tokens == sum(range(1, len(plan.cases) + 1))


def test_failed_agent_turn_cannot_be_recorded_as_completed_case() -> None:
    plan = _plan()
    with pytest.raises(ValidationError, match="成功 Turn"):
        build_swebench_case_execution_state(
            plan,
            plan.cases[0].instance_id,
            _run_id(0),
            status="completed",
            thread_id=UUID(int=100),
            turn_id=UUID(int=200),
            turn_status=TurnStatusV18.FAILED,
            error_code="provider_failed",
            started_at=NOW,
            updated_at=NOW + timedelta(minutes=1),
        )


def test_completed_turn_with_open_evidence_can_be_recorded_as_failed_case() -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = build_swebench_case_execution_state(
        plan,
        plan.cases[0].instance_id,
        _run_id(0),
        status="failed",
        thread_id=UUID(int=100),
        turn_id=UUID(int=200),
        turn_status=TurnStatusV18.COMPLETED,
        error_code="swebench_git_review_incomplete",
        started_at=NOW,
        updated_at=NOW + timedelta(minutes=1),
    )

    suite = build_swebench_suite_execution_state(plan, states)

    assert suite.status == "failed"
    assert suite.completed_case_ids == ()


def test_contracts_are_strict_frozen_and_forbid_extra_fields() -> None:
    plan = _plan()
    state = _ready_states(plan)[0]
    payload = state.model_dump(mode="python")

    with pytest.raises(ValidationError):
        SweBenchCaseExecutionState.model_validate({**payload, "input_tokens": "0"}, strict=True)
    with pytest.raises(ValidationError):
        SweBenchCaseExecutionState.model_validate({**payload, "unexpected": True})
    with pytest.raises(ValidationError):
        state.status = "running"  # type: ignore[misc]


def test_suite_state_keeps_registered_order_prefix_current_case_and_token_totals() -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _completed_state(plan, 0, input_tokens=100, output_tokens=10)
    states[1] = build_swebench_case_execution_state(
        plan,
        plan.cases[1].instance_id,
        _run_id(1),
        status="running",
        thread_id=UUID(int=101),
        turn_id=UUID(int=201),
        turn_status=TurnStatusV18.CALLING_MODEL,
        input_tokens=25,
        output_tokens=5,
        started_at=NOW,
        updated_at=NOW + timedelta(minutes=2),
    )

    suite = build_swebench_suite_execution_state(plan, states)

    assert tuple(state.instance_id for state in suite.case_states) == tuple(
        case.instance_id for case in plan.cases
    )
    assert suite.status == "running"
    assert suite.completed_case_ids == (plan.cases[0].instance_id,)
    assert suite.current_case_id == plan.cases[1].instance_id
    assert (suite.input_tokens, suite.output_tokens) == (125, 15)
    assert suite.model_name == plan.model_name
    assert suite.state_sha256 == swebench_suite_execution_state_digest(suite)

    reordered = [states[1], states[0], *states[2:]]
    with pytest.raises(ValueError, match="预注册顺序"):
        build_swebench_suite_execution_state(plan, reordered)


def test_case_and_suite_state_round_trip_as_atomic_0600_json(tmp_path: Path) -> None:
    plan = _plan()
    states = _ready_states(plan)
    suite = build_swebench_suite_execution_state(plan, states)
    case_path = tmp_path / "case-state.json"
    suite_path = tmp_path / "suite-state.json"

    write_case_execution_state(case_path, plan, states[0])
    write_suite_execution_state(suite_path, plan, suite)
    os.chmod(case_path, 0o644)
    write_case_execution_state(case_path, plan, states[0])

    assert stat_mode(case_path) == stat_mode(suite_path) == 0o600
    assert read_case_execution_state(case_path, plan) == states[0]
    assert read_suite_execution_state(suite_path, plan) == suite
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_state_read_rejects_tampering_permissions_and_recomputed_order_drift(
    tmp_path: Path,
) -> None:
    plan = _plan()
    states = _ready_states(plan)
    suite = build_swebench_suite_execution_state(plan, states)
    case_path = tmp_path / "case-state.json"
    suite_path = tmp_path / "suite-state.json"
    write_case_execution_state(case_path, plan, states[0])
    write_suite_execution_state(suite_path, plan, suite)

    case_payload = json.loads(case_path.read_text(encoding="utf-8"))
    case_payload["input_tokens"] = 1
    case_path.write_text(json.dumps(case_payload), encoding="utf-8")
    os.chmod(case_path, 0o600)
    with pytest.raises(KernelError) as tampered:
        read_case_execution_state(case_path, plan)
    assert tampered.value.code == "swebench_case_execution_state_invalid"

    write_case_execution_state(case_path, plan, states[0])
    os.chmod(case_path, 0o644)
    with pytest.raises(KernelError) as public:
        read_case_execution_state(case_path, plan)
    assert public.value.code == "swebench_case_execution_state_invalid"

    reordered = suite.model_copy(
        update={
            "case_states": (suite.case_states[1], suite.case_states[0], *suite.case_states[2:]),
            "state_sha256": "0" * 64,
        }
    )
    reordered = reordered.model_copy(
        update={"state_sha256": swebench_suite_execution_state_digest(reordered)}
    )
    suite_path.write_text(reordered.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.chmod(suite_path, 0o600)
    with pytest.raises(KernelError) as order_drift:
        read_suite_execution_state(suite_path, plan)
    assert order_drift.value.code == "swebench_suite_execution_state_invalid"


def test_suite_io_rejects_a_different_plan_even_when_case_order_matches(tmp_path: Path) -> None:
    plan = _plan()
    suite = build_swebench_suite_execution_state(plan, _ready_states(plan))
    path = tmp_path / "suite-state.json"
    write_suite_execution_state(path, plan, suite)

    with pytest.raises(KernelError) as drift:
        read_suite_execution_state(path, _plan(model_name="example/model-v2"))

    assert drift.value.code == "swebench_suite_execution_state_invalid"


def test_suite_builder_defaults_to_v1_without_changing_legacy_digest(tmp_path: Path) -> None:
    plan = _plan()
    states = _ready_states(plan)
    suite = build_swebench_suite_execution_state(plan, states)
    explicit_v1 = build_swebench_suite_execution_state(
        plan, states, continue_quality_failures=False
    )

    assert suite == explicit_v1
    assert suite.spec_version == "harnessix.swebench-suite-execution-state/v1"
    # Frozen from the pre-v2 implementation, including the exact serialized field set.
    assert suite.state_sha256 == "3ec8a97f421559048905470681fb80dcb23f57f243442be71b3088524b7a6944"
    assert tuple(suite.model_dump()) == (
        "spec_version",
        "plan_sha256",
        "model_name",
        "status",
        "case_states",
        "completed_case_ids",
        "current_case_id",
        "input_tokens",
        "output_tokens",
        "started_at",
        "updated_at",
        "state_sha256",
    )
    assert suite.attempted_case_ids == ()
    path = tmp_path / "legacy-suite.json"
    write_suite_execution_state(path, plan, suite)
    assert read_suite_execution_state(path, plan) == suite


@pytest.mark.parametrize("error_code", QUALITY_FAILURE_CODES)
@pytest.mark.parametrize("turn_status", [TurnStatusV18.COMPLETED, TurnStatusV18.FAILED])
def test_quality_failure_allowlist_requires_failed_case_and_preserves_original_turn(
    error_code: str, turn_status: TurnStatusV18
) -> None:
    plan = _plan()
    states = _ready_states(plan)
    failed = _failed_state(plan, 0, error_code=error_code, turn_status=turn_status)
    states[0] = failed

    assert is_swebench_quality_failure(failed)
    assert not is_swebench_quality_failure(states[1])
    assert not is_swebench_quality_failure(_running_state(plan, 1))
    assert not is_swebench_quality_failure(
        _completed_state(plan, 1, input_tokens=100, output_tokens=10)
    )
    suite = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)
    assert suite.status == "running"
    assert suite.attempted_case_ids == (failed.instance_id,)
    assert suite.completed_case_ids == ()
    assert suite.current_case_id is None
    assert suite.case_states[0] == failed
    assert suite.case_states[0].status == "failed"
    assert suite.case_states[0].turn_status == turn_status
    assert suite.case_states[0].error_code == error_code


@pytest.mark.parametrize(
    ("success_indexes", "success_count"),
    [(tuple(range(0, 25, 2)), 13), ((), 0)],
    ids=["13_successes_12_failures", "25_quality_failures"],
)
def test_v2_completes_all_25_attempts_without_counting_failures_as_success(
    tmp_path: Path, success_indexes: tuple[int, ...], success_count: int
) -> None:
    plan = _plan()
    assert len(plan.cases) == 25
    states = [
        _completed_state(plan, index, input_tokens=index + 1, output_tokens=1)
        if index in success_indexes
        else _failed_state(
            plan,
            index,
            error_code=QUALITY_FAILURE_CODES[(index // 2) % len(QUALITY_FAILURE_CODES)],
            turn_status=TurnStatusV18.COMPLETED if index % 4 == 1 else TurnStatusV18.FAILED,
        )
        for index in range(25)
    ]

    suite = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)

    assert suite.spec_version == "harnessix.swebench-suite-execution-state/v2"
    assert suite.status == "completed"
    assert suite.attempted_case_ids == tuple(case.instance_id for case in plan.cases)
    assert suite.completed_case_ids == tuple(
        plan.cases[index].instance_id for index in success_indexes
    )
    assert len(suite.completed_case_ids) == success_count
    failure_count = 25 - success_count
    assert sum(state.status == "failed" for state in suite.case_states) == failure_count
    assert suite.current_case_id is None
    assert (suite.input_tokens, suite.output_tokens) == (
        sum(index + 1 for index in success_indexes) + failure_count * 100,
        success_count + failure_count * 10,
    )
    assert suite.case_states == tuple(states)
    assert suite.state_sha256 == swebench_suite_execution_state_digest(suite)
    assert "attempted_case_ids" not in suite.model_dump()

    path = tmp_path / "mixed-suite.json"
    write_suite_execution_state(path, plan, suite)
    restored = read_suite_execution_state(path, plan)
    assert restored == suite
    assert restored.attempted_case_ids == suite.attempted_case_ids
    assert stat_mode(path) == 0o600


def test_v2_can_resume_after_quality_failure_without_rewriting_failed_evidence(
    tmp_path: Path,
) -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _completed_state(plan, 0, input_tokens=100, output_tokens=10)
    states[1] = _failed_state(plan, 1, error_code="swebench_git_review_incomplete")
    between_cases = build_swebench_suite_execution_state(
        plan, states, continue_quality_failures=True
    )
    assert between_cases.status == "running"
    assert between_cases.attempted_case_ids == tuple(case.instance_id for case in plan.cases[:2])
    assert between_cases.completed_case_ids == (plan.cases[0].instance_id,)
    assert between_cases.current_case_id is None

    path = tmp_path / "suite.json"
    write_suite_execution_state(path, plan, between_cases)
    restored = read_suite_execution_state(path, plan)
    states = list(restored.case_states)
    states[2] = _running_state(plan, 2)
    running = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)
    assert running.status == "running"
    assert running.current_case_id == plan.cases[2].instance_id
    assert running.attempted_case_ids == between_cases.attempted_case_ids

    states[2] = _completed_state(plan, 2, input_tokens=125, output_tokens=15)
    continued = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)
    assert continued.status == "running"
    assert continued.attempted_case_ids == tuple(case.instance_id for case in plan.cases[:3])
    assert continued.completed_case_ids == (plan.cases[0].instance_id, plan.cases[2].instance_id)
    assert continued.current_case_id is None
    assert continued.case_states[1] == restored.case_states[1]
    assert (continued.input_tokens, continued.output_tokens) == (325, 35)


@pytest.mark.parametrize(
    "error_code",
    [
        "timeout",
        "provider_timeout",
        "public_input_timeout",
        "cancelled",
        "provider_cancelled",
        "unknown_tool",
        "provider_failed",
        "swebench_public_tests_unavailable",
        "swebench_cost_invalid",
        "swebench_fee_limit_reached",
        "swebench_price_expired",
        "swebench_case_evidence_mismatch",
        "protocol_invalid",
        "unknown_error",
        "budget_exceeded_unknown",
    ],
)
def test_v2_fatal_error_stops_after_mixed_attempts_and_cannot_continue(error_code: str) -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _failed_state(plan, 0)
    states[1] = _completed_state(plan, 1, input_tokens=100, output_tokens=10)
    states[2] = _failed_state(plan, 2, error_code=error_code)
    assert not is_swebench_quality_failure(states[2])

    suite = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)
    assert suite.status == "failed"
    assert suite.attempted_case_ids == tuple(case.instance_id for case in plan.cases[:2])
    assert suite.completed_case_ids == (plan.cases[1].instance_id,)
    assert suite.current_case_id == plan.cases[2].instance_id
    assert suite.case_states[2].error_code == error_code

    states[3] = _completed_state(plan, 3, input_tokens=1, output_tokens=1)
    with pytest.raises(ValidationError, match="前缀"):
        build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)


@pytest.mark.parametrize("error_code", QUALITY_FAILURE_CODES)
@pytest.mark.parametrize("turn_status", [TurnStatusV18.CANCELLED, TurnStatusV18.INTERRUPTED])
def test_v2_cancelled_or_interrupted_turn_stops_even_with_allowlisted_error(
    error_code: str,
    turn_status: TurnStatusV18,
) -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _failed_state(plan, 0, error_code=error_code, turn_status=turn_status)
    assert not is_swebench_quality_failure(states[0])

    suite = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)
    assert suite.status == "failed"
    assert suite.attempted_case_ids == ()
    assert suite.current_case_id == plan.cases[0].instance_id


def test_v2_cancelled_case_is_unique_active_after_attempted_prefix() -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _failed_state(plan, 0)
    states[1] = _case_with_changes(
        _completed_state(plan, 1, input_tokens=0, output_tokens=0),
        status="cancelled",
        turn_status=TurnStatusV18.CANCELLED,
    )
    assert not is_swebench_quality_failure(states[1])

    suite = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)
    assert suite.status == "cancelled"
    assert suite.attempted_case_ids == (plan.cases[0].instance_id,)
    assert suite.completed_case_ids == ()
    assert suite.current_case_id == plan.cases[1].instance_id


@pytest.mark.parametrize(
    "missing_bindings",
    [
        {"thread_id": None, "turn_id": None, "turn_status": None},
        {"turn_id": None, "turn_status": None},
        {"input_tokens": 0, "output_tokens": 0},
    ],
)
def test_v2_allowlisted_error_without_runtime_binding_is_fatal(
    missing_bindings: dict[str, object],
) -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _case_with_changes(_failed_state(plan, 0), **missing_bindings)
    assert not is_swebench_quality_failure(states[0])

    suite = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)
    assert suite.status == "failed"
    assert suite.attempted_case_ids == ()
    assert suite.completed_case_ids == ()
    assert suite.current_case_id == plan.cases[0].instance_id

    states[1] = _completed_state(plan, 1, input_tokens=1, output_tokens=1)
    with pytest.raises(ValidationError, match="前缀"):
        build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)


@pytest.mark.parametrize("missing_field", ["patch_sha256", "input_tokens", "output_tokens"])
def test_quality_failure_cannot_omit_patch_or_token_evidence(missing_field: str) -> None:
    payload = _failed_state(_plan(), 0).model_dump(mode="python")
    del payload[missing_field]

    with pytest.raises(ValidationError, match=missing_field):
        SweBenchCaseExecutionState.model_validate(payload, strict=True)


def test_v2_missing_patch_failure_can_bind_the_real_empty_patch_digest() -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _case_with_changes(
        _failed_state(plan, 0, error_code="swebench_patch_missing"),
        patch_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    assert is_swebench_quality_failure(states[0])
    suite = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)
    assert suite.status == "running"
    assert suite.attempted_case_ids == (plan.cases[0].instance_id,)
    assert suite.completed_case_ids == ()


@pytest.mark.parametrize("later_status", ["completed", "quality_failed", "running", "fatal_failed"])
def test_v2_cannot_skip_ready_case_even_after_quality_failure(later_status: str) -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _failed_state(plan, 0)
    if later_status == "completed":
        states[2] = _completed_state(plan, 2, input_tokens=100, output_tokens=10)
    elif later_status == "running":
        states[2] = _running_state(plan, 2)
    else:
        states[2] = _failed_state(
            plan,
            2,
            error_code="provider_failed"
            if later_status == "fatal_failed"
            else QUALITY_FAILURE_CODES[0],
        )

    with pytest.raises(ValidationError, match="前缀"):
        build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)


@pytest.mark.parametrize("second_active", ["running", "failed", "cancelled"])
def test_v2_rejects_multiple_active_cases(second_active: SweBenchExecutionStatus) -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _failed_state(plan, 0)
    states[1] = _running_state(plan, 1)
    if second_active == "running":
        states[2] = _running_state(plan, 2)
    elif second_active == "failed":
        states[2] = _failed_state(plan, 2, error_code="provider_failed")
    else:
        states[2] = build_swebench_case_execution_state(
            plan, plan.cases[2].instance_id, _run_id(2), status="cancelled", started_at=NOW
        )

    with pytest.raises(ValidationError, match="当前 Case"):
        build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)


def test_v1_still_stops_at_quality_failure_and_rejects_later_success() -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _completed_state(plan, 0, input_tokens=100, output_tokens=10)
    states[1] = _failed_state(plan, 1)

    suite = build_swebench_suite_execution_state(plan, states)
    assert suite.spec_version == "harnessix.swebench-suite-execution-state/v1"
    assert suite.status == "failed"
    assert suite.attempted_case_ids == suite.completed_case_ids == (plan.cases[0].instance_id,)
    assert suite.current_case_id == plan.cases[1].instance_id

    states[2] = _completed_state(plan, 2, input_tokens=100, output_tokens=10)
    with pytest.raises(ValidationError, match="前缀"):
        build_swebench_suite_execution_state(plan, states)


@pytest.mark.parametrize(
    "changes",
    [
        {"completed_case_ids": ()},
        {"completed_case_ids": ("include_failure",)},
        {"current_case_id": None},
        {"current_case_id": "owner0__repo0-0"},
        {"input_tokens": 0},
        {"output_tokens": 0},
        {"status": "ready"},
        {"status": "completed"},
        {"status": "failed"},
    ],
)
def test_v2_rejects_forged_progress_even_with_recomputed_digest(changes: dict[str, object]) -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _failed_state(plan, 0)
    states[1] = _completed_state(plan, 1, input_tokens=100, output_tokens=10)
    states[2] = _running_state(plan, 2)
    suite = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)
    if changes.get("completed_case_ids") == ("include_failure",):
        changes = {"completed_case_ids": (states[0].instance_id, states[1].instance_id)}
    candidate = suite.model_copy(update=changes)
    payload = candidate.model_dump(mode="python")
    payload["state_sha256"] = swebench_suite_execution_state_digest(candidate)

    with pytest.raises(ValidationError):
        SweBenchSuiteExecutionState.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    ("changes", "recompute_digest"),
    [
        ({"state_sha256": "f" * 64}, False),
        ({"spec_version": "harnessix.swebench-suite-execution-state/v1"}, False),
        ({"spec_version": "harnessix.swebench-suite-execution-state/v3"}, False),
        ({"spec_version": "harnessix.swebench-suite-execution-state/v1"}, True),
        ({"spec_version": "harnessix.swebench-suite-execution-state/v3"}, True),
    ],
)
def test_suite_io_rejects_digest_or_version_tampering(
    tmp_path: Path, changes: dict[str, object], recompute_digest: bool
) -> None:
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _failed_state(plan, 0)
    states[1] = _completed_state(plan, 1, input_tokens=100, output_tokens=10)
    suite = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)
    path = tmp_path / "suite.json"
    write_suite_execution_state(path, plan, suite)
    candidate = suite.model_copy(update=changes)
    if recompute_digest:
        candidate = candidate.model_copy(
            update={"state_sha256": swebench_suite_execution_state_digest(candidate)}
        )
    path.write_text(candidate.model_dump_json(), encoding="utf-8")

    with pytest.raises(KernelError) as tampered:
        read_suite_execution_state(path, plan)
    assert tampered.value.code == "swebench_suite_execution_state_invalid"


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
