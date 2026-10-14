"""可选预测试按持久结果推进；不替代补丁后验证或放宽调用组边界。"""

import json

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.product_config.agent_context import build_product_agent_context
from harnessix.product_config.coding_tool_view import CodingWorkflowToolView
from tests.product_config.test_agent_context import _request
from tests.product_config.test_coding_tool_view import _definitions, _turn

PROFILE = "run_profile.fixed-check"
PATCH = "apply_patch_batch"
DIAGNOSTICS = {
    "diagnostic_preview": {"stdout": {"truncated": False}, "stderr": {"truncated": False}}
}


def select(turn, definitions=None):
    return CodingWorkflowToolView(
        require_pre_edit_profile=False, allow_optional_pre_edit_profile=True
    ).select(turn, _definitions() if definitions is None else definitions)


@pytest.mark.parametrize("reads", [0, 1, 4, 12])
def test_prediction_is_optional_and_investigation_does_not_force_patch(reads):
    selection = select(_turn(*(("read_file", "succeeded", None) for _ in range(reads))))
    assert {tool.name for tool in selection.tools} == {"read_file", "grep", "glob", PATCH, PROFILE}
    assert selection.choice == "auto" and selection.forced_tool is None
    assert selection.exclusive_tool_groups == ((PROFILE, PATCH),)


@pytest.mark.parametrize("outcome,code", [("succeeded", None), ("failed", "process_nonzero_exit")])
def test_pre_edit_test_outcome_neither_forces_patch_nor_opens_review(outcome, code):
    selection = select(_turn((PROFILE, outcome, code, DIAGNOSTICS)))
    assert {tool.name for tool in selection.tools} == {"read_file", "grep", "glob", PATCH, PROFILE}
    assert selection.choice == "auto" and selection.forced_tool is None
    assert selection.exclusive_tool_groups == ((PROFILE, PATCH),)


def test_truncated_prediction_diagnostics_remain_readable_without_claiming_reproduction():
    diagnostics = {
        "diagnostic_preview": {"stdout": {"truncated": True}, "stderr": {"truncated": False}}
    }
    selection = select(_turn((PROFILE, "failed", "process_nonzero_exit", diagnostics)))
    assert "read_artifact" in {tool.name for tool in selection.tools}
    assert selection.choice == "auto" and PATCH in {tool.name for tool in selection.tools}


@pytest.mark.parametrize(
    "baseline",
    [
        (),
        ((PROFILE, "succeeded", None, DIAGNOSTICS),),
        ((PROFILE, "failed", "process_nonzero_exit", DIAGNOSTICS),),
    ],
)
def test_every_successful_patch_requires_a_new_test_then_review(baseline):
    patched = _turn(*baseline, (PATCH, "succeeded", None))
    selection = select(patched)
    assert [tool.name for tool in selection.tools] == [PROFILE]
    assert selection.forced_tool == PROFILE and selection.choice == "required"
    assert not selection.exclusive_tool_groups
    reviewed = select(_turn(*baseline, (PATCH, "succeeded", None), (PROFILE, "succeeded", None)))
    assert [tool.name for tool in reviewed.tools] == ["git_review"]
    assert reviewed.forced_tool == "git_review" and not reviewed.exclusive_tool_groups


@pytest.mark.parametrize(
    "code",
    [
        "process_timeout",
        "process_launch_failed",
        "process_output_unavailable",
        "tool_invalid_arguments",
    ],
)
def test_prediction_execution_or_argument_failure_closes_catalog(code):
    assert not select(_turn((PROFILE, "failed", code))).tools


@pytest.mark.parametrize("outcome", ["cancelled", "unknown"])
def test_prediction_uncertain_effect_closes_catalog(outcome):
    assert not select(_turn((PROFILE, outcome, None))).tools


def test_failed_patch_preserves_original_reread_gate_and_does_not_offer_competing_test():
    selection = select(_turn((PATCH, "failed", "workspace_patch_precondition_failed")))
    assert [tool.name for tool in selection.tools] == ["read_file"]
    assert selection.forced_tool == "read_file" and not selection.exclusive_tool_groups


@pytest.mark.parametrize("invalid", [None, 1, "true"])
def test_strict_optional_flag_rejected_by_both_ports(tmp_path, invalid):
    with pytest.raises(ValueError):
        CodingWorkflowToolView(
            require_pre_edit_profile=False, allow_optional_pre_edit_profile=invalid
        )
    with pytest.raises(ValueError):
        build_product_agent_context(
            tmp_path, require_pre_edit_profile=False, allow_optional_pre_edit_profile=invalid
        )


def test_optional_mode_rejects_simultaneous_required_baseline(tmp_path):
    with pytest.raises(ValueError):
        CodingWorkflowToolView(allow_optional_pre_edit_profile=True)
    with pytest.raises(ValueError):
        build_product_agent_context(tmp_path, allow_optional_pre_edit_profile=True)


@pytest.mark.parametrize("missing", [PROFILE, PATCH, "git_review"])
def test_optional_mode_without_supported_capabilities_does_not_broaden_catalog(missing):
    definitions = tuple(d for d in _definitions() if d.name != missing)
    if missing == "git_review":
        definitions = tuple(d for d in definitions if d.name not in {"git_status", "git_diff"})
    assert not select(_turn(), definitions).tools
    assert not select(_turn(), _definitions(second_profile=True)).tools


def test_default_and_explicit_disabled_keep_existing_selection_exact():
    for turn in (_turn(), _turn((PROFILE, "succeeded", None)), _turn((PATCH, "succeeded", None))):
        default = CodingWorkflowToolView(require_pre_edit_profile=False).select(
            turn, _definitions()
        )
        disabled = CodingWorkflowToolView(
            require_pre_edit_profile=False, allow_optional_pre_edit_profile=False
        ).select(turn, _definitions())
        assert default == disabled and not disabled.exclusive_tool_groups


async def test_context_defaults_unchanged_and_candidate_has_own_version(tmp_path):
    async def prepare(**settings):
        return await build_product_agent_context(
            tmp_path, require_pre_edit_profile=False, **settings
        ).context.prepare(_request(tmp_path), CancelToken())

    default = await prepare()
    assert default == await prepare(allow_optional_pre_edit_profile=False)
    candidate = await prepare(allow_optional_pre_edit_profile=True)
    fragments = json.loads(candidate.instructions)["fragments"]
    coding = next(f for f in fragments if f["source"] == "harnessix.coding-instructions/v23")
    assert "available but optional" in coding["content"]
    assert "same response, in either order" in coding["content"]
    assert "cannot substitute for post-edit" in coding["content"]
    assert "stays hidden before the first patch" not in coding["content"]
    assert "harnessix.coding-instructions/v22" in default.instructions
