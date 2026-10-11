from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from harnessix.agent.models import (
    AgentFailure,
    Budget,
    Item,
    ItemStatus,
    ToolCallContent,
    ToolResultContent,
    Turn,
)
from harnessix.domain.models import EffectClass, RiskLevel, ToolDescriptor
from harnessix.product_config.coding_tool_view import CodingWorkflowToolView

_PROFILE = "run_profile.fixed-check"
_PATCH = "apply_patch_batch"
_ROLLBACK = "rollback_workspace_patch"


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
        _descriptor("git_status"),
        _descriptor("git_diff"),
        _descriptor(_PROFILE, EffectClass.NON_IDEMPOTENT_WRITE),
        _descriptor(_PATCH, EffectClass.NON_IDEMPOTENT_WRITE),
        _descriptor(_ROLLBACK, EffectClass.NON_IDEMPOTENT_WRITE),
    ]
    if second_profile:
        values.append(_descriptor("run_profile.other-check", EffectClass.NON_IDEMPOTENT_WRITE))
    return tuple(values)


def _turn(*results: tuple[str, str, str | None]) -> Turn:
    items: list[Item] = []
    for tool, outcome, error_code in results:
        call_id = uuid4()
        effect = (
            EffectClass.NON_IDEMPOTENT_WRITE
            if tool == _PATCH or tool.startswith("run_profile.")
            else EffectClass.READ_ONLY
        )
        call = ToolCallContent(
            call_id=call_id,
            provider_call_id=f"provider-{call_id}",
            tool=tool,
            tool_version="1",
            effect_class=effect,
            requires_approval=effect != EffectClass.READ_ONLY,
            arguments={},
        )
        result = ToolResultContent(
            call_id=call_id,
            outcome=outcome,
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


def _names(turn: Turn, *, second_profile: bool = False) -> set[str]:
    selected = CodingWorkflowToolView().select(
        turn,
        _definitions(second_profile=second_profile),
    )
    return {item.name for item in selected}


def test_single_profile_workflow_exposes_only_current_phase_tools() -> None:
    initial = _names(_turn())
    assert _PROFILE in initial and _PATCH in initial
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
    assert _PATCH in _names(correction) and _PROFILE not in _names(correction)

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
    assert _names(status_only) == {"git_status", "git_diff"}
    closed = _turn(
        (_PROFILE, "failed", "process_nonzero_exit"),
        (_PATCH, "succeeded", None),
        (_PROFILE, "succeeded", None),
        ("git_status", "succeeded", None),
        ("git_diff", "succeeded", None),
    )
    assert _names(closed) == set()


def test_ambiguous_or_infrastructure_workflow_keeps_full_catalog() -> None:
    definitions = _definitions(second_profile=True)
    assert CodingWorkflowToolView().select(_turn(), definitions) == definitions

    unavailable = _turn((_PROFILE, "failed", "process_timeout"))
    assert CodingWorkflowToolView().select(unavailable, _definitions()) == _definitions()
