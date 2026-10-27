"""复现门禁：首次改源码前须有复现草稿测试以代码级失败结束；有界解锁且关闭时行为逐字不变。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from harnessix.agent.models import ToolCallContent
from harnessix.domain.models import EffectClass
from harnessix.product_config.coding_reproduction_gate import (
    LOCKED_NOTICE,
    REPRODUCTION_PASSED_NOTICE,
    REPRODUCTION_REQUIRED,
    RUN_REPRODUCTION_NOTICE,
    VERBATIM_SELF_CHECK,
    selects_scratch,
)
from harnessix.product_config.coding_tool_view import CodingWorkflowToolView
from tests.product_config.test_coding_tool_view import (
    _DIRTY_REVIEW,
    _PATCH,
    _PROFILE,
    _definitions,
    _reviewed,
    _turn,
)

SCRATCH = "tests/forms/test_harnessix_scratch.py"
SOURCE = "django/forms/models.py"


def _view(enabled: bool = True, **changes) -> CodingWorkflowToolView:
    options = {"scratch_reproduction": True, "explain_tool_gates": True, **changes}
    return CodingWorkflowToolView(
        require_pre_edit_profile=False, require_failing_reproduction=enabled, **options
    )


def _patch_args(*paths: str) -> dict:
    return {"files": [{"operation": "create", "path": path} for path in paths]}


def _call(*paths: str) -> ToolCallContent:
    call_id = uuid4()
    return ToolCallContent(
        call_id=call_id,
        provider_call_id=f"provider-{call_id}",
        tool=_PATCH,
        tool_version="1",
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        requires_approval=True,
        arguments=_patch_args(*paths),
    )


def _scratch_patch():
    return (_PATCH, "succeeded", None, None, _patch_args(SCRATCH))


def _scratch_run(outcome: str, code: str | None = None):
    return (_PROFILE, outcome, code, None, {"profile": "fixed-check", "selectors": [SCRATCH]})


def _rejection():
    return (_PATCH, "failed", REPRODUCTION_REQUIRED, None, _patch_args(SOURCE))


def test_source_edit_is_rejected_until_a_reproduction_fails() -> None:
    view = _view()
    turn = _turn(("read_file", "succeeded", None))
    failure = view.admit_call(turn, _call(SOURCE))
    assert failure is not None and failure.code == REPRODUCTION_REQUIRED
    assert view.admit_call(turn, _call(SCRATCH)) is None
    mixed = view.admit_call(turn, _call(SCRATCH, SOURCE))
    assert mixed is not None and mixed.code == REPRODUCTION_REQUIRED


def test_failing_reproduction_unlocks_source_edits() -> None:
    turn = _turn(_scratch_patch(), _scratch_run("failed", "process_nonzero_exit"))
    assert _view().admit_call(turn, _call(SOURCE)) is None


@pytest.mark.parametrize(
    "history",
    [
        (_scratch_patch(), _scratch_run("succeeded"), _scratch_patch(), _scratch_run("succeeded")),
        (_rejection(), _rejection(), _rejection()),
    ],
    ids=["two-passing-reproductions", "three-rejections"],
)
def test_gate_unlocks_after_bounded_attempts(history) -> None:
    assert _view().admit_call(_turn(*history), _call(SOURCE)) is None


def test_existing_source_change_is_never_relocked() -> None:
    turn = _turn((_PATCH, "succeeded", None, None, _patch_args(SOURCE)))
    assert _view().admit_call(turn, _call(SOURCE)) is None


def test_disabled_gate_admits_everything_and_keeps_selection_identical() -> None:
    turn = _turn(_scratch_patch(), _scratch_run("succeeded"))
    disabled = _view(enabled=False)
    assert disabled.admit_call(_turn(), _call(SOURCE)) is None
    plain = CodingWorkflowToolView(
        require_pre_edit_profile=False, scratch_reproduction=True, explain_tool_gates=True
    )
    assert disabled.select(turn, _definitions()) == plain.select(turn, _definitions())


def test_locked_investigation_explains_the_unlock_condition() -> None:
    selection = _view().select(_turn(("read_file", "succeeded", None)), _definitions())
    assert selection.notice == LOCKED_NOTICE
    assert _PATCH in {tool.name for tool in selection.tools}


def test_scratch_patch_forces_a_reproduction_run() -> None:
    selection = _view().select(_turn(_scratch_patch()), _definitions())
    assert selection.forced_tool == _PROFILE and selection.notice == RUN_REPRODUCTION_NOTICE


def test_passing_reproduction_reopens_editing_instead_of_review() -> None:
    turn = _turn(_scratch_patch(), _scratch_run("succeeded"))
    selection = _view().select(turn, _definitions())
    names = {tool.name for tool in selection.tools}
    assert _PATCH in names and not names & {"git_review", "git_status", "git_diff"}
    assert selection.notice == REPRODUCTION_PASSED_NOTICE
    plain = _view(enabled=False).select(turn, _definitions())
    assert "git_review" in {tool.name for tool in plain.tools}


def test_rejected_source_edit_does_not_force_a_reread() -> None:
    selection = _view().select(
        _turn(("read_file", "succeeded", None), _rejection()), _definitions()
    )
    assert selection.forced_tool is None and selection.notice == LOCKED_NOTICE


def test_self_check_asks_for_the_issue_example_verbatim() -> None:
    view = _view(self_verification=True)
    selection = view.select(_reviewed(_DIRTY_REVIEW), _definitions())
    assert selection.notice is not None and selection.notice.endswith(VERBATIM_SELF_CHECK)


@pytest.mark.parametrize(
    "selector,expected",
    [
        ("tests/forms/test_harnessix_scratch.py", True),
        ("forms_tests.test_harnessix_scratch", True),
        ("sympy/core/tests/test_harnessix_scratch.py::test_x", True),
        ("pkg/harnessix_scratch/test_x.py", True),
        ("tests/forms/tests.py", False),
        ("forms_tests.tests.FormTests", False),
    ],
)
def test_scratch_selectors_cover_paths_and_native_labels(selector, expected) -> None:
    assert selects_scratch(selector) is expected


@pytest.mark.parametrize(
    "changes",
    [{"scratch_reproduction": False}, {"explain_tool_gates": False}],
)
def test_gate_requires_scratch_reproduction_and_gate_explanations(changes) -> None:
    with pytest.raises(ValueError):
        _view(**changes)
