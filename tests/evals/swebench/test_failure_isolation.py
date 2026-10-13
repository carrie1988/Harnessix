"""故障不重试、不改分数；只对可证明且可足额预留的独立Case推进。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import AgentFailure, TurnStatus, TurnStatusV18
from harnessix.agent.usage import UsageObservation
from harnessix.evals.swebench import execution
from harnessix.evals.swebench.execution_contracts import (
    build_swebench_suite_execution_state,
    is_swebench_quality_failure,
    swebench_suite_execution_state_digest,
)
from harnessix.evals.swebench.execution_io import (
    read_case_execution_state,
    read_suite_execution_state,
)
from harnessix.evals.swebench.predictions import read_predictions
from harnessix.evals.swebench.run_config import (
    SweBenchBudgetWindowAgentRunConfig,
    SweBenchIsolatedAgentRunConfig,
)
from harnessix.evals.swebench.run_config_io import (
    read_swebench_agent_run_config,
    write_swebench_agent_run_config,
)
from harnessix.models.pricing import format_amount
from tests.evals.swebench.test_execution import (
    PATCH,
    PUBLIC_TEST_TOOL,
    SUCCESSFUL_EVIDENCE,
    _accounted_turn,
    _config,
    _offline_suite,
    _plan,
    _run_offline_suite,
    _turn,
)
from tests.evals.swebench.test_execution_contracts import (
    _case_with_changes,
    _failed_state,
    _ready_states,
)


@pytest.fixture(autouse=True)
def fixed_host_boundary(monkeypatch):
    from tests.evals.swebench.test_execution import NOW

    monkeypatch.setattr(execution, "utc_now", lambda: NOW)
    monkeypatch.setattr(execution, "require_evaluation_disk_space", lambda *_: None)


def _isolated(config, amount="43.088836"):
    payload = config.model_dump(mode="python")
    payload.update(spec_version="harnessix.swebench-agent-run-config/v3", fee_stop_amount=amount)
    return SweBenchIsolatedAgentRunConfig.model_validate(payload)


def _transport(index, config, completeness="unknown"):
    return _accounted_turn(
        index,
        config,
        turn_error="provider_transport",
        last_attempt_changes={
            "status": "failed",
            "error": AgentFailure(code="provider_transport", message="fixture"),
            "actual_model": None if completeness != "complete" else config.price.model,
            "response_id": None if completeness != "complete" else f"response-{index}-8",
            "usage": UsageObservation(completeness="partial", input_tokens=50)
            if completeness == "partial"
            else (
                UsageObservation(completeness="complete", input_tokens=100, output_tokens=10)
                if completeness == "complete"
                else UsageObservation()
            ),
        },
    )


def _frozen_states(root):
    return {p: p.read_bytes() for p in root.glob("**/*state.json")}


def test_explicit_v3_admission_and_io_preserve_v2_without_upgrade(tmp_path):
    plan = _plan()
    old = _config(tmp_path, plan)
    encoded = old.model_dump_json()
    config = _isolated(old, "4.947456")
    config.require_plan(plan)
    assert config.worst_case_amount(plan) == "123.6864"
    assert old.worst_case_amount(plan) == "48.147456"
    assert config.fingerprint != old.fingerprint
    with pytest.raises(ValueError):
        _isolated(old, "4.947455").require_plan(plan)
    with pytest.raises(ValueError):
        old.model_copy(update={"fee_stop_amount": "43.088836"}).require_plan(plan)
    for name, value in (("old", old), ("new", config)):
        path = tmp_path / f"{name}.json"
        write_swebench_agent_run_config(path, value)
        actual = read_swebench_agent_run_config(path)
        assert actual == value and actual.fingerprint == value.fingerprint
        assert actual.isolate_provider_failures == (name == "new")
    assert old.model_dump_json() == encoded


@pytest.mark.parametrize("version", [2, 3, 4])
async def test_frozen_window_policy_reaches_case_entrypoint(tmp_path, monkeypatch, version):
    plan = _plan()
    config = _config(tmp_path, plan)
    if version == 3:
        config = _isolated(config)
    elif version == 4:
        payload = _isolated(config).model_dump(mode="python")
        payload.update(spec_version="harnessix.swebench-agent-run-config/v4")
        config = SweBenchBudgetWindowAgentRunConfig.model_validate(payload)
    case = plan.cases[0]
    root = tmp_path / "case"
    workspace = tmp_path / "workspace"
    profile = config.process_profile_for(case.instance_id)
    monkeypatch.setattr(
        execution, "read_case_execution_state", lambda *_: SimpleNamespace(status="ready")
    )
    calls = []

    async def run_case(**arguments):
        calls.append(arguments)
        return _turn(0).turn_id, _turn(0)

    monkeypatch.setattr(execution, "run_swebench_agent_case", run_case)
    from harnessix.agent.cancellation import CancelToken

    await execution._open_case_turn(
        plan, config, case, root, workspace, profile, "offline", CancelToken()
    )
    assert len(calls) == 1
    assert calls[0]["budget_pressure_compaction"] is (version == 4)
    assert calls[0]["execution_fingerprint"] == config.fingerprint
    assert calls[0]["process_profile"] == profile
    assert calls[0]["expected_terminal_turn"] is None


@pytest.mark.parametrize("completeness", ["unknown", "partial", "complete"])
@pytest.mark.asyncio
async def test_multiple_isolated_failures_keep_each_reserve_and_full_failed_predictions(
    tmp_path, monkeypatch, completeness
):
    plan = _plan()
    config = _isolated(_config(tmp_path, plan))
    turns = {i: _accounted_turn(i, config) for i in range(25)}
    for index in (0, 8, 19):
        turns[index] = _transport(index, config, completeness)
    native_before = {i: t.model_dump_json() for i, t in turns.items()}
    calls = _offline_suite(monkeypatch, plan, config, turns)
    first = await _run_offline_suite(plan, config)
    root = Path(config.work_root)
    frozen = _frozen_states(root)
    second = await _run_offline_suite(plan, config)
    assert first == second
    assert first["attempted_cases"] == 25 and first["completed_cases"] == 22
    assert first["quality_failed_cases"] == 0 and first["provider_failed_cases"] == 3
    assert first["unaccounted_cost_cases"] == (0 if completeness == "complete" else 3)
    assert first["reserved_cost_amount"] == ("0" if completeness == "complete" else "9.442368")
    assert first["known_cost_amount"] == format_amount(
        sum(execution._turn_cost(t, config)[0] for t in turns.values())
    )
    assert calls == [c.instance_id for c in plan.cases] * 2
    assert {i: t.model_dump_json() for i, t in turns.items()} == native_before
    assert all(p.read_bytes() == data for p, data in frozen.items())
    predictions = read_predictions(root / "predictions.jsonl", plan)
    assert len(predictions) == 25 and all(p.model_patch == PATCH for p in predictions)
    for index in (0, 8, 19):
        state = read_case_execution_state(
            root / "cases" / f"case-{index:03d}" / "case-state.json", plan
        )
        assert state.status == "failed" and state.turn_status is TurnStatusV18.FAILED
        assert state.error_code == "swebench_provider_transport_isolated"
        assert not is_swebench_quality_failure(state)


@pytest.mark.asyncio
async def test_insufficient_next_case_funds_stop_before_next_request(tmp_path, monkeypatch):
    plan = _plan()
    config = _isolated(_config(tmp_path, plan), "4.947456")
    calls = _offline_suite(monkeypatch, plan, config, {0: _transport(0, config)})
    with pytest.raises(KernelError) as caught:
        await _run_offline_suite(plan, config)
    assert caught.value.code == "swebench_fee_limit_reached"
    assert calls == [plan.cases[0].instance_id]
    root = Path(config.work_root)
    suite = read_suite_execution_state(root / "suite-state.json", plan)
    assert suite.attempted_case_ids == (plan.cases[0].instance_id,)
    assert suite.completed_case_ids == () and suite.case_states[1].status == "ready"
    assert not (root / "predictions.jsonl").exists()


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_model",
        "wrong_provider",
        "no_actual_model",
        "no_response_id",
        "earlier_unknown",
        "missing_step",
        "multiple_attempts",
        "running",
        "outside_price",
        "rate_limit",
        "cancelled",
        "missing_attempts",
    ],
)
def test_unproved_or_unpriced_failure_is_not_isolatable(tmp_path, defect):
    config = _isolated(_config(tmp_path, _plan()))
    turn = _transport(0, config)
    attempts = list(turn.model_attempts)
    if defect == "wrong_model":
        attempts[-1] = attempts[-1].model_copy(
            update={"requested_model": "other", "actual_model": "other"}
        )
    elif defect == "wrong_provider":
        attempts[-1] = attempts[-1].model_copy(update={"provider": "other"})
    elif defect in {"no_actual_model", "no_response_id", "outside_price"}:
        key, value = {
            "no_actual_model": ("actual_model", None),
            "no_response_id": ("response_id", None),
            "outside_price": ("started_at", config.price.valid_from - timedelta(seconds=1)),
        }[defect]
        attempts[0] = attempts[0].model_copy(update={key: value})
    elif defect == "earlier_unknown":
        attempts[0] = attempts[-1].model_copy(
            update={"step": 1, "attempt_id": attempts[0].attempt_id}
        )
    elif defect == "missing_step":
        attempts.pop(0)
    elif defect == "multiple_attempts":
        attempts[-1] = attempts[-1].model_copy(update={"index": 2})
    elif defect == "running":
        attempts[-1] = attempts[-1].model_copy(update={"status": "running"})
    elif defect == "rate_limit":
        turn = turn.model_copy(
            update={"error": AgentFailure(code="provider_rate_limit", message="fixture")}
        )
    elif defect == "cancelled":
        turn = turn.model_copy(update={"status": TurnStatus.CANCELLED})
    elif defect == "missing_attempts":
        attempts = []
    turn = turn.model_copy(update={"model_attempts": tuple(attempts)})
    assert not execution._can_isolate_transport_failure(turn, config)


@pytest.mark.parametrize("tool", ["workspace_patch", PUBLIC_TEST_TOOL, "git_review"])
@pytest.mark.parametrize("outcome", ["unknown", "cancelled"])
def test_tool_unknown_takes_priority_over_approved_transport_isolation(tool, outcome):
    turn = _turn(
        0,
        (*SUCCESSFUL_EVIDENCE, (tool, outcome, "unknown")),
        status=TurnStatus.FAILED,
        turn_error="provider_transport",
    )
    failure = execution._case_evidence_failure(
        turn, PATCH, PUBLIC_TEST_TOOL, cost_complete=False, transport_failure_isolatable=True
    )
    assert failure[0] in {"swebench_tool_state_unavailable", "swebench_public_tests_unavailable"}


def test_v3_zero_usage_failure_advances_but_v2_stops_and_upgrade_is_rejected(tmp_path):
    plan = _plan()
    states = _ready_states(plan)
    states[0] = _case_with_changes(
        _failed_state(plan, 0, error_code="swebench_provider_transport_isolated"),
        input_tokens=0,
        output_tokens=0,
    )
    old = build_swebench_suite_execution_state(plan, states, continue_quality_failures=True)
    new = build_swebench_suite_execution_state(
        plan, states, continue_quality_failures=True, continue_provider_failures=True
    )
    assert old.status == "failed" and old.attempted_case_ids == ()
    assert new.status == "running" and new.attempted_case_ids == (plan.cases[0].instance_id,)
    assert new.completed_case_ids == () and new.input_tokens == new.output_tokens == 0
    assert new.state_sha256 == swebench_suite_execution_state_digest(new)
    (tmp_path / "suite-state.json").write_text(old.model_dump_json())
    with pytest.raises(KernelError) as caught:
        execution._reconcile_suite_state(tmp_path, plan, states, isolate_provider_failures=True)
    assert caught.value.code == "swebench_suite_execution_state_invalid"


def test_first_request_timeout_isolates_with_zero_usage_but_unknown_compaction_stops(tmp_path):
    from harnessix.agent.models import Usage

    config = _isolated(_config(tmp_path, _plan()))
    turn = _transport(0, config)
    last = turn.model_attempts[-1].model_copy(update={"step": 1})
    turn = turn.model_copy(
        update={"model_steps": 1, "model_attempts": (last,), "usage": Usage(), "items": ()}
    )
    assert execution._turn_cost(turn, config) == (0, False)
    assert execution._can_isolate_transport_failure(turn, config)
    uncertain = turn.model_copy(
        update={"compactions": (SimpleNamespace(attempt=None, unaccounted_request_possible=True),)}
    )
    assert not execution._can_isolate_transport_failure(uncertain, config)
