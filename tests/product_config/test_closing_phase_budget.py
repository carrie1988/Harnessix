"""调查收尾偏好不构成修改授权；失败重读与修改后验证门禁不变。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from harnessix.agent.models import Budget, Usage
from harnessix.agent.usage import ModelAttempt, UsageObservation
from harnessix.product_config.coding_tool_view import CodingWorkflowToolView
from tests.product_config.test_coding_tool_view import _definitions, _turn

PATCH = "apply_patch_batch"
PROFILE = "run_profile.fixed-check"
SNAPSHOT = {"path": "source.py", "digest_status": "complete", "content_sha256": "a" * 64}


def observed_turn(*results, remaining=20000, input_tokens=6000, calls=5):
    now = datetime.now(UTC)
    attempts = tuple(
        ModelAttempt(
            attempt_id=uuid4(),
            step=i + 1,
            index=1,
            provider="fixture",
            requested_model="fixture-model",
            actual_model="fixture-model",
            response_id=f"response-{i}",
            usage=UsageObservation(
                completeness="complete", input_tokens=input_tokens, output_tokens=100
            ),
            status="completed",
            started_at=now,
            finished_at=now,
        )
        for i in range(calls)
    )
    return _turn(*results).model_copy(
        update=dict(
            budget=Budget(
                max_tokens=50000, max_steps=16, timeout_seconds=900, max_output_chars=65536
            ),
            model_steps=calls,
            model_attempts=attempts,
            usage=Usage(input_tokens=50000 - remaining),
        )
    )


def select(turn, enabled=True, **flags):
    return CodingWorkflowToolView(
        require_pre_edit_profile=False, reserve_closing_phase=enabled, **flags
    ).select(turn, _definitions())


def test_low_remaining_prefers_patch_or_honest_end_without_forcing_write():
    turn = observed_turn(("read_file", "succeeded", None, SNAPSHOT))
    selected = select(turn)
    assert [t.name for t in selected.tools] == [PATCH]
    assert selected.choice == "auto" and selected.forced_tool is None
    assert selected.forced_tool is None  # 未授权强迫写入，也未更改预算。
    assert {"read_file", "grep", "glob", PATCH} == {
        t.name for t in select(turn, enabled=False).tools
    }


def test_adequate_remaining_keeps_original_free_investigation():
    selected = select(
        observed_turn(
            ("read_file", "succeeded", None, SNAPSHOT), remaining=40000, input_tokens=1000, calls=1
        )
    )
    assert {"read_file", "grep", "glob", PATCH} == {t.name for t in selected.tools}


@pytest.mark.parametrize("results", [(), (("read_file", "failed", "tool_not_found"),)])
def test_no_successful_source_observation_does_not_prefer_patch(results):
    selected = select(observed_turn(*results))
    assert "read_file" in {t.name for t in selected.tools}


def test_unknown_usage_and_model_drift_do_not_infer_closing_window():
    source = observed_turn(("read_file", "succeeded", None, SNAPSHOT))
    for changes in [{"usage": UsageObservation()}, {"actual_model": "different-model"}]:
        altered = source.model_copy(
            update={"model_attempts": (source.model_attempts[-1].model_copy(update=changes),)}
        )
        assert "read_file" in {t.name for t in select(altered).tools}


def test_failed_patch_still_requires_its_original_reread():
    source = observed_turn(
        ("read_file", "succeeded", None, SNAPSHOT),
        (
            PATCH,
            "failed",
            "workspace_patch_precondition_failed",
            None,
            {"files": [{"path": "source.py", "operation": "edit"}]},
        ),
    )
    chosen = select(source)
    assert [t.name for t in chosen.tools] == ["read_file"] and chosen.forced_tool == "read_file"


def test_successful_patch_still_forces_fresh_test_then_review():
    chosen = select(
        observed_turn(("read_file", "succeeded", None, SNAPSHOT), (PATCH, "succeeded", None))
    )
    assert [t.name for t in chosen.tools] == [PROFILE] and chosen.forced_tool == PROFILE
    chosen = select(observed_turn((PATCH, "succeeded", None), (PROFILE, "succeeded", None)))
    assert [t.name for t in chosen.tools] == ["git_review"] and chosen.forced_tool == "git_review"


def test_optional_pre_edit_strategy_is_not_silently_changed():
    selected = select(
        observed_turn(("read_file", "succeeded", None, SNAPSHOT)),
        allow_optional_pre_edit_profile=True,
    )
    assert {"read_file", "grep", "glob", PATCH, PROFILE} == {t.name for t in selected.tools}


@pytest.mark.parametrize("invalid", [None, 1, "true"])
def test_closing_policy_requires_explicit_bool(invalid):
    with pytest.raises(ValueError):
        CodingWorkflowToolView(reserve_closing_phase=invalid)


def test_successful_search_without_complete_edit_sha_keeps_reads_available():
    turn = observed_turn(("grep", "succeeded", None, {"matches": [{"revision": "b" * 64}]}))
    assert "read_file" in {tool.name for tool in select(turn).tools}


@pytest.mark.parametrize("remaining,input_tokens,calls", [(45000, 1000, 11), (17500, 1000, 5)])
async def test_summary_settlement_does_not_change_frozen_main_catalog(
    remaining, input_tokens, calls
):
    from harnessix.agent.turn_budget_compaction import require_request_slots
    from harnessix.context.compaction_contracts import CompactionSummary
    from harnessix.context.compaction_ledger_contracts import CompactionRecord
    from tests.context.test_turn_budget_compaction import (
        pressure_plan,
        pressure_thread,
        settled_attempt,
    )

    before = observed_turn(
        ("read_file", "succeeded", None, SNAPSHOT),
        remaining=remaining,
        input_tokens=input_tokens,
        calls=calls,
    )
    prepared = await pressure_plan(pressure_thread())
    attempt = settled_attempt(step=calls + 1, outcome="completed").model_copy(
        update={
            "usage": UsageObservation(completeness="complete", input_tokens=1000, output_tokens=100)
        }
    )
    plan = prepared.plan.model_copy(update={"model_step": calls + 1})
    summary = CompactionRecord(
        plan=plan,
        status="summarized",
        input_tokens_before=0,
        output_tokens_before=0,
        attempt=attempt,
        summary=CompactionSummary(compaction_id=plan.compaction_id, text="Known facts"),
        candidate_history_sha256="b" * 64,
        candidate_history_tokens=1,
        created_event_sequence=plan.source_event_sequence + 1,
        finished_event_sequence=plan.source_event_sequence + 2,
        created_at=attempt.started_at,
        finished_at=attempt.finished_at,
    )
    after = before.model_copy(
        update={
            "compactions": (summary,),
            "usage": Usage(input_tokens=before.usage.total_tokens + 1100),
        }
    )
    assert select(before) == select(after)
    assert require_request_slots(after) == require_request_slots(before) - 1
    assert after.usage.total_tokens == before.usage.total_tokens + 1100
    # 后续主请求结算后才更新偏好；此前摘要仍计入真实费用、Token和总请求槽。
    next_attempt = before.model_attempts[-1].model_copy(
        update={"step": calls + 1, "attempt_id": uuid4()}
    )
    settled = after.model_copy(
        update={"model_steps": calls + 1, "model_attempts": (*before.model_attempts, next_attempt)}
    )
    assert [tool.name for tool in select(settled).tools] == [PATCH]
