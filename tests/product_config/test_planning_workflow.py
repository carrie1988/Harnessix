"""可选模型规划不替代正式Profile、Review和基础设施失败边界。"""

import json
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import Item, ItemStatus, PlanContent, PlanStep
from harnessix.agent.planning import planning_tool_descriptor
from harnessix.product_config.agent_context import build_product_agent_context
from harnessix.product_config.coding_tool_view import CodingWorkflowToolView
from tests.product_config.test_agent_context import _request
from tests.product_config.test_coding_tool_view import _definitions, _turn


def selection(turn, *, enabled=True, pre_edit=False):
    return CodingWorkflowToolView(
        enable_planning=enabled, require_pre_edit_profile=pre_edit
    ).select(turn, (*_definitions(), planning_tool_descriptor()))


async def test_context_opt_in_adds_one_independent_fragment_default_remains_exact(tmp_path):
    default = await build_product_agent_context(tmp_path).context.prepare(
        _request(tmp_path), CancelToken()
    )
    disabled = await build_product_agent_context(tmp_path, enable_planning=False).context.prepare(
        _request(tmp_path), CancelToken()
    )
    enabled = await build_product_agent_context(tmp_path, enable_planning=True).context.prepare(
        _request(tmp_path), CancelToken()
    )
    assert default.instructions == disabled.instructions
    plain = json.loads(default.instructions)["fragments"]
    extra = json.loads(enabled.instructions)["fragments"]
    planning = [
        fragment for fragment in extra if fragment["source"] == "harnessix.coding-planning/v1"
    ]
    assert len(planning) == 1 and planning[0]["trust"] == "runtime"
    assert [fragment for fragment in extra if fragment not in planning] == plain
    assert "self-report" in planning[0]["content"]


@pytest.mark.parametrize("invalid", [1, None, "true"])
def test_context_and_catalog_reject_implicit_enablement(tmp_path, invalid):
    with pytest.raises(ValueError):
        build_product_agent_context(tmp_path, enable_planning=invalid)
    with pytest.raises(ValueError):
        CodingWorkflowToolView(enable_planning=invalid)


def test_investigation_opt_in_preserves_existing_choice_and_catalog():
    turn = _turn()
    baseline = selection(turn, enabled=False)
    enabled = selection(turn)
    assert "update_plan" not in {tool.name for tool in baseline.tools}
    assert enabled.tools == (*baseline.tools, planning_tool_descriptor())
    assert (enabled.choice, enabled.forced_tool) == (baseline.choice, baseline.forced_tool)


def test_pre_edit_baseline_remains_last_without_competing_plan_control():
    initial = selection(_turn(), pre_edit=True)
    assert "run_profile.fixed-check" in {tool.name for tool in initial.tools}
    assert "update_plan" not in {tool.name for tool in initial.tools}
    after_baseline = selection(_turn(("run_profile.fixed-check", "succeeded", None)), pre_edit=True)
    assert "update_plan" in {tool.name for tool in after_baseline.tools}


@pytest.mark.parametrize(
    "results,expected",
    [
        ((("apply_patch_batch", "succeeded", None),), "run_profile.fixed-check"),
        (
            (
                ("apply_patch_batch", "succeeded", None),
                ("run_profile.fixed-check", "succeeded", None),
            ),
            "git_review",
        ),
        ((("apply_patch_batch", "failed", "tool_invalid_arguments"),), "apply_patch_batch"),
        ((("run_profile.fixed-check", "failed", "process_timeout"),), None),
        ((("apply_patch_batch", "unknown", None),), None),
    ],
)
def test_self_reported_completion_cannot_bypass_forced_phase_or_fail_closed(results, expected):
    turn = _turn(*results)
    completed = Item(
        item_id=uuid4(),
        status=ItemStatus.COMPLETED,
        content=PlanContent(
            steps=(PlanStep(step_id="all", description="模型声称完成", status="completed"),)
        ),
    )
    turn = turn.model_copy(update={"items": (*turn.items, completed)})
    enabled = selection(turn)
    assert enabled.forced_tool == expected
    assert "update_plan" not in {tool.name for tool in enabled.tools}
    assert enabled == selection(turn, enabled=False)


def test_code_test_failure_keeps_diagnosis_and_original_repair_requirements():
    turn = _turn(
        ("apply_patch_batch", "succeeded", None),
        ("run_profile.fixed-check", "failed", "process_nonzero_exit"),
    )
    enabled, baseline = selection(turn), selection(turn, enabled=False)
    assert enabled.choice == baseline.choice and enabled.forced_tool is None
    assert {tool.name for tool in enabled.tools} == {
        *[tool.name for tool in baseline.tools],
        "update_plan",
    }
    assert "apply_patch_batch" not in {tool.name for tool in enabled.tools}
