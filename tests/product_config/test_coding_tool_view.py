from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from harnessix.agent.models import (
    AgentFailure,
    Budget,
    Item,
    ItemStatus,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    Turn,
)
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.product_config.coding_tool_view import CodingWorkflowToolView

_PROFILE = "run_profile.fixed-check"
_PATCH = "apply_patch_batch"
_ROLLBACK = "rollback_workspace_patch"
_Result = (
    tuple[str, str, str | None]
    | tuple[str, str, str | None, object]
    | tuple[str, str, str | None, object, dict[str, object]]
)


def _descriptor(name: str, effect: EffectClass = EffectClass.READ_ONLY) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        version="1",
        description=name,
        input_schema={"type": "object"},
        effect_class=effect,
        risk_level=RiskLevel.LOW,
        requires_idempotency=effect != EffectClass.READ_ONLY,
        requires_approval=effect != EffectClass.READ_ONLY,
        supports_reconciliation=effect != EffectClass.READ_ONLY,
    )


def _definitions(*, second_profile: bool = False) -> tuple[ToolDescriptor, ...]:
    values = [
        _descriptor("list_files"),
        _descriptor("read_file"),
        _descriptor("glob"),
        _descriptor("grep"),
        _descriptor("read_artifact"),
        _descriptor("git_status"),
        _descriptor("git_diff"),
        _descriptor("git_review"),
        _descriptor(_PROFILE, EffectClass.NON_IDEMPOTENT_WRITE),
        _descriptor(_PATCH, EffectClass.NON_IDEMPOTENT_WRITE),
        _descriptor(_ROLLBACK, EffectClass.NON_IDEMPOTENT_WRITE),
    ]
    if second_profile:
        values.append(_descriptor("run_profile.other-check", EffectClass.NON_IDEMPOTENT_WRITE))
    return tuple(values)


def _turn(*results: _Result) -> Turn:
    items: list[Item] = []
    for result_spec in results:
        tool, outcome, error_code = result_spec[:3]
        call_id = uuid4()
        effect = (
            EffectClass.NON_IDEMPOTENT_WRITE
            if tool == _PATCH or tool.startswith("run_profile.")
            else EffectClass.READ_ONLY
        )
        default_arguments = {"files": [{"path": "src/example.py"}]} if tool == _PATCH else {}
        call = ToolCallContent(
            call_id=call_id,
            provider_call_id=f"provider-{call_id}",
            tool=tool,
            tool_version="1",
            effect_class=effect,
            requires_approval=effect != EffectClass.READ_ONLY,
            arguments=result_spec[4] if len(result_spec) == 5 else default_arguments,
        )
        default_output = (
            {"path": "src/example.py"} if tool == "read_file" and outcome == "succeeded" else None
        )
        result = ToolResultContent(
            call_id=call_id,
            outcome=outcome,
            output=result_spec[3] if len(result_spec) == 4 else default_output,
            error=(
                AgentFailure(code=error_code, message="failed") if error_code is not None else None
            ),
        )
        items.extend(
            (
                Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=call),
                Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=result),
            )
        )
    return Turn(
        turn_id=uuid4(),
        request_id="request",
        request_fingerprint="f" * 64,
        budget=Budget(),
        items=tuple(items),
        created_at=datetime.now(UTC),
    )


def _selection_for(
    turn: Turn,
    *,
    require_pre_edit_profile: bool = True,
    second_profile: bool = False,
):
    return CodingWorkflowToolView(require_pre_edit_profile=require_pre_edit_profile).select(
        turn,
        _definitions(second_profile=second_profile),
    )


def _with_rejected_tool(turn: Turn, *, model_step: int = 5) -> Turn:
    call_id = uuid4()
    rejection = ToolCallRejectionContent(
        call_id=call_id,
        provider_call_id=f"provider-{call_id}",
        model_step=model_step,
    )
    result = ToolResultContent(
        call_id=call_id,
        outcome="failed",
        error=AgentFailure(code="unknown_tool", message="rejected"),
    )
    return turn.model_copy(
        update={
            "items": (
                *turn.items,
                Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=rejection),
                Item(item_id=uuid4(), status=ItemStatus.COMPLETED, content=result),
            )
        }
    )


def _names(
    turn: Turn,
    *,
    require_pre_edit_profile: bool = True,
    second_profile: bool = False,
) -> set[str]:
    selected = _selection_for(
        turn,
        require_pre_edit_profile=require_pre_edit_profile,
        second_profile=second_profile,
    )
    return {item.name for item in selected.tools}


def _definitions_without_profile() -> tuple[ToolDescriptor, ...]:
    return tuple(item for item in _definitions() if item.name != _PROFILE)


def test_single_profile_workflow_exposes_only_current_phase_tools() -> None:
    assert CodingWorkflowToolView().require_pre_edit_profile is True
    initial = _names(_turn())
    assert _PROFILE in initial and _PATCH not in initial
    assert not {"git_status", "git_diff"} & initial
    assert _ROLLBACK not in initial

    baseline_failed = _turn((_PROFILE, "failed", "process_nonzero_exit"))
    editing = _names(baseline_failed)
    assert _PATCH in editing and "read_file" in editing
    assert _PROFILE not in editing and not {"git_status", "git_diff"} & editing

    patched = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
    )
    assert _names(patched) == {_PROFILE}

    correction = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
        (_PROFILE, "failed", "process_nonzero_exit"),
    )
    assert _names(correction) == {"read_file", "grep", "glob", "read_artifact", _PROFILE}
    correction_read = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
        (_PROFILE, "failed", "process_nonzero_exit"),
        ("read_file", "succeeded", None),
    )
    assert _names(correction_read) == {
        _PATCH,
        "read_file",
        "grep",
        "glob",
        "read_artifact",
        _PROFILE,
    }

    validated = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
        (_PROFILE, "succeeded", None),
    )
    assert _names(validated) == {"git_review"}
    closed = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
        (_PROFILE, "succeeded", None),
        ("git_review", "succeeded", None),
    )
    assert _names(closed) == set()


@pytest.mark.parametrize("read_count", (4, 6, 12))
def test_public_validation_does_not_force_a_patch_before_source_is_understood(
    read_count: int,
) -> None:
    turn = _turn(*(("read_file", "succeeded", None) for _ in range(read_count)))
    selected = _selection_for(turn, require_pre_edit_profile=False)
    assert {item.name for item in selected.tools} == {"read_file", "grep", "glob", _PATCH}
    assert selected.choice == "auto" and selected.forced_tool is None

    no_profile = CodingWorkflowToolView(require_pre_edit_profile=False).select(
        turn, _definitions_without_profile()
    )
    assert {item.name for item in no_profile.tools} == {_PATCH}
    assert no_profile.forced_tool == _PATCH


def test_post_patch_only_workflow_allows_investigation_and_forces_retest_and_review() -> None:
    initial = _selection_for(_turn(), require_pre_edit_profile=False)
    assert {item.name for item in initial.tools} == {"read_file", "grep", "glob", _PATCH}
    assert _PROFILE not in {item.name for item in initial.tools}
    assert initial.choice == "auto" and initial.forced_tool is None

    before_limit = _selection_for(
        _turn(
            ("grep", "succeeded", None),
            ("read_file", "succeeded", None),
            ("glob", "succeeded", None),
        ),
        require_pre_edit_profile=False,
    )
    assert _PATCH in {item.name for item in before_limit.tools}
    assert _PROFILE not in {item.name for item in before_limit.tools}
    assert before_limit.forced_tool is None

    at_limit = _selection_for(
        _turn(
            ("grep", "succeeded", None),
            ("read_file", "succeeded", None),
            ("glob", "succeeded", None),
            ("read_file", "succeeded", None),
        ),
        require_pre_edit_profile=False,
    )
    assert {item.name for item in at_limit.tools} == {"read_file", "grep", "glob", _PATCH}
    assert at_limit.choice == "auto" and at_limit.forced_tool is None

    rejected_at_limit = _selection_for(
        _with_rejected_tool(
            _turn(
                ("grep", "succeeded", None),
                ("read_file", "succeeded", None),
                ("glob", "succeeded", None),
                ("read_file", "succeeded", None),
            )
        ),
        require_pre_edit_profile=False,
    )
    assert {item.name for item in rejected_at_limit.tools} == {"read_file", "grep", "glob", _PATCH}
    assert rejected_at_limit.choice == "auto"
    assert rejected_at_limit.forced_tool is None

    patched = _selection_for(
        _turn((_PATCH, "succeeded", None)),
        require_pre_edit_profile=False,
    )
    assert {item.name for item in patched.tools} == {_PROFILE}
    assert patched.choice == "required" and patched.forced_tool == _PROFILE

    failed_validation = _turn(
        (_PATCH, "succeeded", None),
        (_PROFILE, "failed", "process_nonzero_exit"),
    )
    diagnosis = _selection_for(failed_validation, require_pre_edit_profile=False)
    assert {item.name for item in diagnosis.tools} == {
        "read_file",
        "grep",
        "glob",
        "read_artifact",
        _PROFILE,
    }
    assert _PATCH not in {item.name for item in diagnosis.tools}

    correction = _selection_for(
        _turn(
            (_PATCH, "succeeded", None),
            (_PROFILE, "failed", "process_nonzero_exit"),
            ("read_file", "succeeded", None),
        ),
        require_pre_edit_profile=False,
    )
    assert {item.name for item in correction.tools} == {
        "read_file",
        "grep",
        "glob",
        "read_artifact",
        _PATCH,
        _PROFILE,
    }
    assert _PROFILE in {item.name for item in correction.tools}

    retest = _selection_for(
        _turn(
            (_PATCH, "succeeded", None),
            (_PROFILE, "failed", "process_nonzero_exit"),
            ("read_file", "succeeded", None),
            (_PATCH, "succeeded", None),
        ),
        require_pre_edit_profile=False,
    )
    assert {item.name for item in retest.tools} == {_PROFILE}
    assert retest.choice == "required" and retest.forced_tool == _PROFILE

    validated = _selection_for(
        _turn(
            (_PATCH, "succeeded", None),
            (_PROFILE, "failed", "process_nonzero_exit"),
            ("read_file", "succeeded", None),
            (_PATCH, "succeeded", None),
            (_PROFILE, "succeeded", None),
        ),
        require_pre_edit_profile=False,
    )
    assert {item.name for item in validated.tools} == {"git_review"}
    assert validated.choice == "required" and validated.forced_tool == "git_review"

    reviewed = _selection_for(
        _turn(
            (_PATCH, "succeeded", None),
            (_PROFILE, "succeeded", None),
            ("git_review", "succeeded", None),
        ),
        require_pre_edit_profile=False,
    )
    assert reviewed.tools == ()
    assert reviewed.choice == "auto" and reviewed.forced_tool is None


def test_post_patch_only_failed_patch_never_exposes_profile() -> None:
    failed = _selection_for(
        _turn(
            (
                _PATCH,
                "failed",
                "patch_ambiguous_context",
                None,
                {"files": [{"operation": "edit", "path": "src/example.py"}]},
            )
        ),
        require_pre_edit_profile=False,
    )
    assert {item.name for item in failed.tools} == {"read_file"}
    assert failed.forced_tool == "read_file"

    reread = _selection_for(
        _turn(
            (
                _PATCH,
                "failed",
                "patch_ambiguous_context",
                None,
                {"files": [{"operation": "edit", "path": "src/example.py"}]},
            ),
            ("read_file", "succeeded", None),
        ),
        require_pre_edit_profile=False,
    )
    assert {item.name for item in reread.tools} == {_PATCH}
    assert reread.forced_tool == _PATCH


def test_no_profile_workflow_allows_bounded_edit_then_forces_git_review() -> None:
    view = CodingWorkflowToolView()
    definitions = _definitions_without_profile()

    initial = view.select(_turn(), definitions)
    assert _PATCH in {item.name for item in initial.tools}
    assert not {"git_status", "git_diff"} & {item.name for item in initial.tools}
    assert initial.choice == "required"

    failed_inspection = view.select(
        _turn(
            ("grep", "succeeded", None),
            ("read_file", "succeeded", None),
            ("glob", "failed", "result_limit"),
            ("read_file", "succeeded", None),
        ),
        definitions,
    )
    assert {item.name for item in failed_inspection.tools} == {
        "read_file",
        "grep",
        "glob",
        _PATCH,
    }
    assert failed_inspection.choice == "auto"
    assert failed_inspection.forced_tool is None

    bounded = view.select(
        _turn(
            ("grep", "succeeded", None),
            ("read_file", "succeeded", None),
            ("glob", "failed", "result_limit"),
            ("read_file", "succeeded", None),
            ("grep", "succeeded", None),
        ),
        definitions,
    )
    assert {item.name for item in bounded.tools} == {_PATCH}
    assert bounded.choice == "required"
    assert bounded.forced_tool == _PATCH

    bounded_turn = _turn(
        ("grep", "succeeded", None),
        ("read_file", "succeeded", None),
        ("glob", "failed", "result_limit"),
        ("read_file", "succeeded", None),
        ("grep", "succeeded", None),
    )
    rejected_bounded = view.select(_with_rejected_tool(bounded_turn), definitions)
    assert {item.name for item in rejected_bounded.tools} == {_PATCH}
    assert rejected_bounded.choice == "required"
    assert rejected_bounded.forced_tool == _PATCH

    patched = view.select(_turn((_PATCH, "succeeded", None)), definitions)
    assert {item.name for item in patched.tools} == {"git_review"}
    assert patched.choice == "required" and patched.forced_tool == "git_review"

    reviewed = view.select(
        _turn((_PATCH, "succeeded", None), ("git_review", "succeeded", None)),
        definitions,
    )
    assert {item.name for item in reviewed.tools} == {"read_file"}

    correction_or_finish = view.select(
        _turn(
            (_PATCH, "succeeded", None),
            ("git_review", "succeeded", None),
        ),
        definitions,
    )
    assert {item.name for item in correction_or_finish.tools} == {"read_file"}
    assert correction_or_finish.choice == "auto"

    correction_ready = view.select(
        _turn(
            (_PATCH, "succeeded", None),
            ("git_review", "succeeded", None),
            ("read_file", "succeeded", None),
        ),
        definitions,
    )
    assert {item.name for item in correction_ready.tools} == {"read_file", _PATCH}
    assert correction_ready.choice == "auto"

    failed_patch = view.select(
        _turn(
            (
                _PATCH,
                "failed",
                "patch_ambiguous_context",
                None,
                {"files": [{"operation": "edit", "path": "src/example.py"}]},
            )
        ),
        definitions,
    )
    assert {item.name for item in failed_patch.tools} == {"read_file"}
    assert failed_patch.choice == "required"
    assert failed_patch.forced_tool == "read_file"

    invalid_patch = view.select(
        _turn(
            (
                _PATCH,
                "failed",
                "tool_invalid_arguments",
                None,
                {"files": [{"operation": "edit", "path": "src/example.py"}]},
            )
        ),
        definitions,
    )
    assert {item.name for item in invalid_patch.tools} == {_PATCH}
    assert invalid_patch.choice == "required" and invalid_patch.forced_tool == _PATCH

    failed_create = view.select(
        _turn(
            (
                _PATCH,
                "failed",
                "delivery_path_conflict",
                None,
                {"files": [{"operation": "create", "path": "src/new.py"}]},
            )
        ),
        definitions,
    )
    assert {item.name for item in failed_create.tools} == {_PATCH}
    assert failed_create.choice == "required" and failed_create.forced_tool == _PATCH


def test_ambiguous_workflow_keeps_full_catalog() -> None:
    definitions = _definitions(second_profile=True)
    assert CodingWorkflowToolView().select(_turn(), definitions).tools == definitions


def test_infrastructure_failure_closes_ambiguous_catalog() -> None:
    selected = _selection_for(
        _turn((_PROFILE, "failed", "process_timeout")),
        second_profile=True,
    )

    assert selected.tools == ()
    assert selected.choice == "auto" and selected.forced_tool is None


@pytest.mark.parametrize("require_pre_edit_profile", (True, False))
@pytest.mark.parametrize(
    ("outcome", "error_code"),
    (
        ("failed", "process_timeout"),
        ("failed", "process_launch_failed"),
        ("failed", "tool_invalid_arguments"),
        ("unknown", "process_state_unknown"),
        ("failed", "process_output_unavailable"),
        ("cancelled", None),
        ("failed", "unexpected_profile_failure"),
    ),
)
def test_profile_infrastructure_failure_closes_catalog(
    require_pre_edit_profile: bool,
    outcome: str,
    error_code: str | None,
) -> None:
    results: tuple[_Result, ...]
    if require_pre_edit_profile:
        results = ((_PROFILE, outcome, error_code),)
    else:
        results = ((_PATCH, "succeeded", None), (_PROFILE, outcome, error_code))

    selected = _selection_for(
        _turn(*results),
        require_pre_edit_profile=require_pre_edit_profile,
    )

    assert selected.tools == ()
    assert selected.choice == "auto" and selected.forced_tool is None


@pytest.mark.parametrize(
    ("outcome", "error_code"),
    (("unknown", "process_state_unknown"), ("failed", "process_output_unavailable")),
)
def test_post_patch_only_infrastructure_patch_failure_closes_catalog(
    outcome: str,
    error_code: str,
) -> None:
    selected = _selection_for(
        _turn((_PATCH, outcome, error_code)),
        require_pre_edit_profile=False,
    )

    assert selected.tools == ()
    assert selected.choice == "auto" and selected.forced_tool is None


def test_recognized_workflow_requires_tools_and_forces_post_patch_profile() -> None:
    initial = CodingWorkflowToolView().select(_turn(), _definitions())
    assert initial.choice == "required" and initial.forced_tool is None

    patched = CodingWorkflowToolView().select(
        _turn(
            (_PROFILE, "failed", "process_nonzero_exit"),
            (_PATCH, "succeeded", None),
        ),
        _definitions(),
    )
    assert {tool.name for tool in patched.tools} == {_PROFILE}
    assert patched.choice == "required" and patched.forced_tool == _PROFILE


def test_complete_profile_diagnostic_does_not_advertise_artifact_read() -> None:
    complete_diagnostic = {
        "diagnostic_preview": {
            "stdout": {"text": "", "truncated": False},
            "stderr": {"text": "assert failed", "truncated": False},
        }
    }
    correction = CodingWorkflowToolView().select(
        _turn(
            (_PROFILE, "failed", "process_nonzero_exit"),
            (_PATCH, "succeeded", None),
            (_PROFILE, "failed", "process_nonzero_exit", complete_diagnostic),
        ),
        _definitions(),
    )
    assert {tool.name for tool in correction.tools} == {"read_file", "grep", "glob", _PROFILE}
    assert correction.choice == "required" and correction.forced_tool is None


def test_failed_validation_after_delete_can_propose_corrective_patch() -> None:
    correction = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (
            _PATCH,
            "succeeded",
            None,
            None,
            {"files": [{"operation": "delete", "path": "src/example.py"}]},
        ),
        (_PROFILE, "failed", "process_nonzero_exit"),
    )

    assert _PATCH in _names(correction)


def test_git_review_is_hidden_until_post_patch_test_succeeds() -> None:
    for turn in (_turn(), _turn((_PROFILE, "failed", "process_nonzero_exit"))):
        assert "git_review" not in _names(turn)


def test_failed_aggregate_without_profile_stops_without_missing_review_crash() -> None:
    definitions = tuple(item for item in _definitions() if not item.name.startswith("run_profile."))
    turn = _turn((_PATCH, "succeeded", None), ("git_review", "failed", "git_observation_failed"))
    assert CodingWorkflowToolView().select(turn, definitions).tools == ()


def test_legacy_two_git_tools_remain_available_when_aggregate_is_absent() -> None:
    definitions = tuple(item for item in _definitions() if item.name != "git_review")
    turn = _turn((_PATCH, "succeeded", None), (_PROFILE, "succeeded", None))
    selection = CodingWorkflowToolView().select(turn, definitions)
    assert {item.name for item in selection.tools} == {"git_status", "git_diff"}


def _reopen_select(turn: Turn, enabled: bool):
    return CodingWorkflowToolView(
        require_pre_edit_profile=False, reopen_after_clean_review=enabled
    ).select(turn, _definitions())


_CLEAN_REVIEW = {
    "status": {"total_entries": 0, "entries": []},
    "diff": {"text": "", "utf8_bytes": 0},
}
_DIRTY_REVIEW = {
    "status": {"total_entries": 1, "entries": [{"path": "src/example.py"}]},
    "diff": {"text": "-a\n+b\n", "utf8_bytes": 6},
}


def _reviewed(review_output):
    return _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
        (_PROFILE, "succeeded", None),
        ("git_review", "succeeded", None, review_output),
    )


def test_clean_review_reopens_investigation_and_patch_when_enabled() -> None:
    reopened = _reopen_select(_reviewed(_CLEAN_REVIEW), True)
    names = {item.name for item in reopened.tools}
    assert _PATCH in names and "read_file" in names
    assert not names & {"git_review", "git_status", "git_diff", _PROFILE}
    assert reopened.forced_tool is None


@pytest.mark.parametrize(
    ("review_output", "enabled"),
    [(_DIRTY_REVIEW, True), (_CLEAN_REVIEW, False), (None, True)],
)
def test_real_changes_or_default_view_still_close_after_review(review_output, enabled) -> None:
    closed = _reopen_select(_reviewed(review_output), enabled)
    assert closed.tools == ()


def test_a_new_patch_after_reopening_returns_to_the_forced_test_gate() -> None:
    turn = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
        (_PROFILE, "succeeded", None),
        ("git_review", "succeeded", None, _CLEAN_REVIEW),
        ("read_file", "succeeded", None),
        (_PATCH, "succeeded", None),
    )
    selected = _reopen_select(turn, True)
    assert selected.forced_tool == _PROFILE


_SOURCE_PATCH = {"files": [{"path": "src/example.py", "operation": "edit"}]}


def _gate_view(explain: bool) -> CodingWorkflowToolView:
    return CodingWorkflowToolView(require_pre_edit_profile=False, explain_tool_gates=explain)


@pytest.mark.parametrize("explain", [True, False])
def test_hidden_patch_after_failed_test_is_explained_only_when_enabled(explain: bool) -> None:
    """v54：测试失败后Patch隐藏待重读，模型只收到“工具未注册”，连续10次被拒。"""
    turn = _turn(
        ("read_file", "succeeded", None),
        (_PATCH, "succeeded", None, None, {"files": [{"path": "pkg/core.py"}]}),
        (_PROFILE, "failed", "process_nonzero_exit"),
    )
    selection = _gate_view(explain).select(turn, _definitions())
    assert _PATCH not in {tool.name for tool in selection.tools}
    if explain:
        assert selection.notice is not None and "pkg/core.py" in selection.notice
        assert _PATCH in selection.notice
    else:
        assert selection.notice is None


def test_gates_explanation_never_changes_the_callable_selection() -> None:
    turns = (
        _turn(("read_file", "succeeded", None)),
        _turn(
            ("read_file", "succeeded", None),
            (_PATCH, "succeeded", None, None, {"files": [{"path": "pkg/core.py"}]}),
            (_PROFILE, "failed", "process_nonzero_exit"),
        ),
        _turn(
            ("read_file", "succeeded", None),
            (_PATCH, "failed", "workspace_patch_precondition_failed", None, _SOURCE_PATCH),
        ),
    )
    for turn in turns:
        plain = _gate_view(False).select(turn, _definitions())
        explained = _gate_view(True).select(turn, _definitions())
        assert replace(explained, notice=None) == plain


def test_failed_patch_reread_and_closed_catalog_are_explained() -> None:
    failed = _turn(
        ("read_file", "succeeded", None),
        (_PATCH, "failed", "workspace_patch_precondition_failed", None, _SOURCE_PATCH),
    )
    selection = _gate_view(True).select(failed, _definitions())
    assert selection.forced_tool == "read_file"
    assert selection.notice is not None and "src/example.py" in selection.notice
    closed = _turn((_PROFILE, "cancelled", None))
    selection = _gate_view(True).select(closed, _definitions())
    assert selection.tools == () and selection.notice is not None


@pytest.mark.parametrize("enabled", [True, False])
def test_public_test_timeout_enters_diagnosis_only_when_enabled(enabled: bool) -> None:
    """d5：修复后测试挂起超时被当作基础设施故障，目录关闭并停止整轮。"""
    turn = _turn(
        ("read_file", "succeeded", None),
        (_PATCH, "succeeded", None, None, _SOURCE_PATCH),
        (_PROFILE, "failed", "process_timeout"),
    )
    view = CodingWorkflowToolView(
        require_pre_edit_profile=False, test_timeouts_are_failures=enabled
    )
    names = {tool.name for tool in view.select(turn, _definitions()).tools}
    if enabled:
        assert {"read_file", _PROFILE} <= names
    else:
        assert names == set()


@pytest.mark.parametrize("enabled", [True, False])
def test_failed_patch_keeps_reads_instead_of_forcing_patch_only(enabled: bool) -> None:
    """v55 sphinx-11445：参数错误后只剩强制Patch，模型无法重读取得SHA，连续被拒至步数耗尽。"""
    turn = _turn(
        ("read_file", "succeeded", None),
        (_PATCH, "failed", "tool_invalid_arguments", None, _SOURCE_PATCH),
    )
    view = CodingWorkflowToolView(
        require_pre_edit_profile=False, keep_reads_after_failed_patch=enabled
    )
    selection = view.select(turn, _definitions())
    names = {tool.name for tool in selection.tools}
    if enabled:
        assert selection.forced_tool is None and selection.choice == "required"
        assert names == {_PATCH, "read_file"}
    else:
        assert selection.forced_tool == _PATCH and names == {_PATCH}


def test_patch_deadline_before_any_patch_is_not_relaxed() -> None:
    turn = _turn(("read_file", "succeeded", None))
    view = CodingWorkflowToolView(
        require_pre_edit_profile=False, keep_reads_after_failed_patch=True
    )
    plain = CodingWorkflowToolView(require_pre_edit_profile=False)
    assert view.select(turn, _definitions()) == plain.select(turn, _definitions())


def _verify_view(enabled: bool) -> CodingWorkflowToolView:
    return CodingWorkflowToolView(
        require_pre_edit_profile=False, explain_tool_gates=True, self_verification=enabled
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_first_closure_with_changes_reopens_one_self_check_round(enabled: bool) -> None:
    selection = _verify_view(enabled).select(_reviewed(_DIRTY_REVIEW), _definitions())
    names = {tool.name for tool in selection.tools}
    if not enabled:
        assert names == set()
        return
    assert {"read_file", "grep", _PATCH, _PROFILE} <= names
    assert not {"git_review", "git_status", "git_diff", "read_artifact"} & names
    assert selection.choice == "auto" and selection.forced_tool is None
    assert selection.notice is not None and "Self-check" in selection.notice


def test_second_review_after_the_self_check_closes() -> None:
    turn = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
        (_PROFILE, "succeeded", None),
        ("git_review", "succeeded", None, _DIRTY_REVIEW),
        ("read_file", "succeeded", None),
        (_PATCH, "succeeded", None),
        (_PROFILE, "succeeded", None),
        ("git_review", "succeeded", None, _DIRTY_REVIEW),
    )
    assert _verify_view(True).select(turn, _definitions()).tools == ()


def test_clean_review_does_not_trigger_a_self_check() -> None:
    selection = _verify_view(True).select(_reviewed(_CLEAN_REVIEW), _definitions())
    assert selection.notice is None or "Self-check" not in selection.notice


def test_self_check_requires_gate_explanations() -> None:
    with pytest.raises(ValueError):
        CodingWorkflowToolView(require_pre_edit_profile=False, self_verification=True)


def _repair_view(enabled: bool, *, verify: bool = False) -> CodingWorkflowToolView:
    return CodingWorkflowToolView(
        require_pre_edit_profile=False,
        explain_tool_gates=True,
        self_verification=verify,
        bounded_repair_cycles=enabled,
    )


def _repair_cycles(failures: int, *tail: _Result) -> Turn:
    cycles: list[_Result] = []
    for _ in range(failures):
        cycles.extend(
            (
                (_PATCH, "succeeded", None),
                (_PROFILE, "failed", "process_nonzero_exit"),
                ("read_file", "succeeded", None),
            )
        )
    return _turn(*cycles, *tail)


@pytest.mark.parametrize("failures", [1, 3])
def test_short_repair_loops_keep_review_closed(failures: int) -> None:
    turn = _repair_cycles(failures)
    selection = _repair_view(True).select(turn, _definitions())
    assert selection == _repair_view(False).select(turn, _definitions())
    assert "git_review" not in {tool.name for tool in selection.tools}


def test_repeated_failed_validations_open_review_with_an_explanation() -> None:
    turn = _repair_cycles(4)
    selection = _repair_view(True).select(turn, _definitions())
    names = {tool.name for tool in selection.tools}
    assert {"git_review", _PATCH, _PROFILE, "read_file"} <= names
    assert selection.forced_tool is None
    assert selection.notice is not None and "4 times in a row" in selection.notice
    plain = _repair_view(False).select(turn, _definitions())
    assert "git_review" not in {tool.name for tool in plain.tools}


def test_review_after_the_repair_limit_closes_the_catalog() -> None:
    turn = _repair_cycles(4, ("git_review", "succeeded", None, _DIRTY_REVIEW))
    assert _repair_view(True).select(turn, _definitions()).tools == ()


def test_review_after_the_repair_limit_still_allows_one_self_check() -> None:
    turn = _repair_cycles(4, ("git_review", "succeeded", None, _DIRTY_REVIEW))
    selection = _repair_view(True, verify=True).select(turn, _definitions())
    assert selection.notice is not None and "Self-check" in selection.notice


def test_a_passing_validation_resets_the_failure_streak() -> None:
    turn = _repair_cycles(
        3,
        (_PATCH, "succeeded", None),
        (_PROFILE, "succeeded", None),
        ("git_review", "succeeded", None, _DIRTY_REVIEW),
        ("read_file", "succeeded", None),
    )
    cycles = _repair_cycles(1)
    combined = turn.model_copy(update={"items": turn.items + cycles.items})
    selection = _repair_view(True, verify=False).select(combined, _definitions())
    assert "git_review" not in {tool.name for tool in selection.tools}


def test_forced_validation_after_a_patch_is_explained() -> None:
    turn = _repair_cycles(1, (_PATCH, "succeeded", None))
    selection = _repair_view(True).select(turn, _definitions())
    assert selection.forced_tool == _PROFILE
    assert selection.notice is not None and _PROFILE in selection.notice
    assert _repair_view(False).select(turn, _definitions()).notice is None


def test_repair_limit_requires_gate_explanations() -> None:
    with pytest.raises(ValueError):
        CodingWorkflowToolView(require_pre_edit_profile=False, bounded_repair_cycles=True)


def _clean_review_view(enabled: bool) -> CodingWorkflowToolView:
    return CodingWorkflowToolView(
        require_pre_edit_profile=False,
        reopen_after_clean_review=True,
        explain_tool_gates=True,
        explain_clean_review=enabled,
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_clean_review_reopen_explains_that_the_fix_was_undone(enabled: bool) -> None:
    selection = _clean_review_view(enabled).select(_reviewed(_CLEAN_REVIEW), _definitions())
    names = {tool.name for tool in selection.tools}
    assert _PATCH in names and "git_review" not in names
    if enabled:
        assert selection.notice is not None and "undid your fix" in selection.notice
        assert _PATCH in selection.notice
    else:
        assert selection.notice is None


def test_dirty_review_is_not_explained_as_undone() -> None:
    selection = _clean_review_view(True).select(_reviewed(_DIRTY_REVIEW), _definitions())
    assert selection.notice is None or "undid" not in selection.notice


def test_clean_review_explanation_requires_reopen_and_gate_explanations() -> None:
    with pytest.raises(ValueError):
        CodingWorkflowToolView(
            require_pre_edit_profile=False, explain_tool_gates=True, explain_clean_review=True
        )
    with pytest.raises(ValueError):
        CodingWorkflowToolView(
            require_pre_edit_profile=False,
            reopen_after_clean_review=True,
            explain_clean_review=True,
        )


def _hard_cap_view(enabled: bool) -> CodingWorkflowToolView:
    return CodingWorkflowToolView(
        require_pre_edit_profile=False,
        explain_tool_gates=True,
        bounded_repair_cycles=True,
        force_review_after_repairs=enabled,
    )


def test_eight_failed_validations_force_the_review() -> None:
    turn = _repair_cycles(8)
    selection = _hard_cap_view(True).select(turn, _definitions())
    assert {tool.name for tool in selection.tools} == {"git_review"}
    assert selection.forced_tool == "git_review"
    assert selection.notice is not None and "8 times in a row" in selection.notice
    plain = _hard_cap_view(False).select(turn, _definitions())
    assert _PATCH in {tool.name for tool in plain.tools}


def test_seven_failed_validations_still_allow_repairs() -> None:
    selection = _hard_cap_view(True).select(_repair_cycles(7), _definitions())
    assert {"git_review", _PATCH} <= {tool.name for tool in selection.tools}
    assert selection.forced_tool is None


def test_hard_cap_requires_bounded_repair_cycles() -> None:
    with pytest.raises(ValueError):
        CodingWorkflowToolView(
            require_pre_edit_profile=False, explain_tool_gates=True, force_review_after_repairs=True
        )


def _argument_view(enabled: bool) -> CodingWorkflowToolView:
    return CodingWorkflowToolView(
        require_pre_edit_profile=False, profile_argument_errors_recoverable=enabled
    )


def test_rejected_profile_arguments_do_not_close_the_workflow() -> None:
    turn = _turn(
        ("read_file", "succeeded", None),
        (_PATCH, "succeeded", None),
        (_PROFILE, "failed", "tool_invalid_arguments"),
    )
    recovered = _argument_view(True).select(turn, _definitions())
    assert recovered.forced_tool == _PROFILE
    assert _argument_view(False).select(turn, _definitions()).tools == ()


@pytest.mark.parametrize("enabled", [True, False])
def test_self_check_can_ask_for_sibling_implementations(enabled: bool) -> None:
    view = CodingWorkflowToolView(
        require_pre_edit_profile=False,
        explain_tool_gates=True,
        self_verification=True,
        sibling_scope_check=enabled,
    )
    selection = view.select(_reviewed(_DIRTY_REVIEW), _definitions())
    assert selection.notice is not None and "Self-check" in selection.notice
    assert ("other implementations of the same behaviour" in selection.notice) is enabled


def test_sibling_scope_check_requires_self_verification() -> None:
    with pytest.raises(ValueError):
        CodingWorkflowToolView(require_pre_edit_profile=False, sibling_scope_check=True)
