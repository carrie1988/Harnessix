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
    build_swebench_case_execution_state,
    build_swebench_suite_execution_state,
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


def test_failed_agent_turn_can_freeze_an_empty_prediction_and_continue() -> None:
    plan = _plan()
    state = build_swebench_case_execution_state(
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

    states = _ready_states(plan)
    states[0] = state
    suite = build_swebench_suite_execution_state(plan, states)

    assert suite.status == "running"
    assert suite.completed_case_ids == (plan.cases[0].instance_id,)


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


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
