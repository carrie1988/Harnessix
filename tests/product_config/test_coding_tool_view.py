from __future__ import annotations

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
    assert _names(correction) == {"read_file", "read_artifact"}
    correction_read = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
        (_PROFILE, "failed", "process_nonzero_exit"),
        ("read_file", "succeeded", None),
    )
    assert _names(correction_read) == {_PATCH, "read_file", "read_artifact"}

    validated = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
        (_PROFILE, "succeeded", None),
    )
    assert _names(validated) == {"git_status", "git_diff"}
    status_only = validated.model_copy(
        update={
            "items": _turn(
                (_PROFILE, "failed", "process_nonzero_exit"),
                (_PATCH, "succeeded", None),
                (_PROFILE, "succeeded", None),
                ("git_status", "succeeded", None),
            ).items
        }
    )
    assert _names(status_only) == {"git_diff"}
    closed = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
        (_PROFILE, "succeeded", None),
        ("git_status", "succeeded", None),
        ("git_diff", "succeeded", None),
    )
    assert _names(closed) == set()


def test_post_patch_only_workflow_forces_patch_profile_repair_retest_and_review() -> None:
    initial = _selection_for(_turn(), require_pre_edit_profile=False)
    assert {item.name for item in initial.tools} == {"read_file", "grep", "glob", _PATCH}
    assert _PROFILE not in {item.name for item in initial.tools}
    assert initial.choice == "required" and initial.forced_tool is None

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
    assert {item.name for item in at_limit.tools} == {_PATCH}
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
    assert {item.name for item in rejected_at_limit.tools} == {_PATCH}
    assert rejected_at_limit.choice == "required"
    assert rejected_at_limit.forced_tool == _PATCH

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
    assert {item.name for item in diagnosis.tools} == {"read_file", "read_artifact"}
    assert _PATCH not in {item.name for item in diagnosis.tools}

    correction = _selection_for(
        _turn(
            (_PATCH, "succeeded", None),
            (_PROFILE, "failed", "process_nonzero_exit"),
            ("read_file", "succeeded", None),
        ),
        require_pre_edit_profile=False,
    )
    assert {item.name for item in correction.tools} == {"read_file", "read_artifact", _PATCH}
    assert _PROFILE not in {item.name for item in correction.tools}

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
    assert {item.name for item in validated.tools} == {"git_status", "git_diff"}
    assert validated.choice == "required" and validated.forced_tool == "git_status"

    reviewed = _selection_for(
        _turn(
            (_PATCH, "succeeded", None),
            (_PROFILE, "succeeded", None),
            ("git_status", "succeeded", None),
            ("git_diff", "succeeded", None),
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
    assert bounded.choice == "auto"
    assert bounded.forced_tool is None

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
    assert {item.name for item in patched.tools} == {"git_status", "git_diff"}
    assert patched.choice == "required" and patched.forced_tool == "git_status"

    reviewed = view.select(
        _turn((_PATCH, "succeeded", None), ("git_status", "succeeded", None)),
        definitions,
    )
    assert {item.name for item in reviewed.tools} == {"git_diff"}
    assert reviewed.choice == "required" and reviewed.forced_tool == "git_diff"

    correction_or_finish = view.select(
        _turn(
            (_PATCH, "succeeded", None),
            ("git_status", "succeeded", None),
            ("git_diff", "succeeded", None),
        ),
        definitions,
    )
    assert {item.name for item in correction_or_finish.tools} == {"read_file"}
    assert correction_or_finish.choice == "auto"

    correction_ready = view.select(
        _turn(
            (_PATCH, "succeeded", None),
            ("git_status", "succeeded", None),
            ("git_diff", "succeeded", None),
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
    assert {tool.name for tool in correction.tools} == {"read_file"}
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
