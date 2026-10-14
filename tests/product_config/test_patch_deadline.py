"""按账本可负担请求数的首次Patch截止：只结束首次修改前调查，不改测试、Review或执行校验。"""

import pytest

from harnessix.product_config.coding_tool_view import CodingWorkflowToolView
from tests.product_config.test_closing_phase_budget import PATCH, PROFILE, SNAPSHOT, observed_turn
from tests.product_config.test_coding_tool_view import _definitions

READ = ("read_file", "succeeded", None, SNAPSHOT)


def select(turn, deadline=4):
    return CodingWorkflowToolView(
        require_pre_edit_profile=False, patch_deadline_requests=deadline
    ).select(turn, _definitions())


def test_deadline_forces_patch_once_only_patch_test_review_fit():
    # 剩余 20000，上一请求 6100 → 3 次可负担 ≤ 4。
    selected = select(observed_turn(READ, remaining=20000, input_tokens=6000))
    assert [tool.name for tool in selected.tools] == [PATCH]
    assert selected.forced_tool == PATCH and selected.choice == "required"


def test_enough_budget_keeps_free_investigation():
    selected = select(observed_turn(READ, remaining=40000, input_tokens=6000))
    assert selected.forced_tool is None
    assert {"read_file", "grep", "glob", PATCH} == {tool.name for tool in selected.tools}


@pytest.mark.parametrize(
    "results",
    [
        (),
        (("read_file", "succeeded", None, {"path": "a.py", "digest_status": "partial"}),),
        (("read_file", "failed", "tool_not_found"),),
    ],
)
def test_no_complete_source_snapshot_never_forces_a_blind_patch(results):
    selected = select(observed_turn(*results, remaining=12000, input_tokens=6000))
    assert selected.forced_tool is None


def test_after_first_patch_original_test_and_repair_flow_is_unchanged():
    turn = observed_turn(READ, (PATCH, "succeeded", None), remaining=12000, input_tokens=6000)
    selected = select(turn)
    assert selected.forced_tool == PROFILE
    failed = observed_turn(
        READ, (PATCH, "failed", "tool_anchor_not_found"), remaining=12000, input_tokens=6000
    )
    unchanged = CodingWorkflowToolView(require_pre_edit_profile=False).select(
        failed, _definitions()
    )
    assert select(failed) == unchanged


def test_default_view_and_unknown_usage_are_unchanged():
    turn = observed_turn(READ, remaining=12000, input_tokens=6000)
    default = CodingWorkflowToolView(require_pre_edit_profile=False).select(turn, _definitions())
    assert default.forced_tool is None
    no_usage = turn.model_copy(update={"model_attempts": ()})
    assert select(no_usage).forced_tool is None


@pytest.mark.parametrize("invalid", [0, 17, True, "4", 1.5])
def test_deadline_must_be_a_small_integer(invalid):
    with pytest.raises(ValueError):
        CodingWorkflowToolView(require_pre_edit_profile=False, patch_deadline_requests=invalid)
