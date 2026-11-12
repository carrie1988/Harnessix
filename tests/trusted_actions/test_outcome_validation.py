"""原始Outcome的正式封套、序列化前防回调及独立JSON副本合同。"""

from __future__ import annotations

from copy import deepcopy
from time import monotonic
from uuid import uuid4

import pytest

from harnessix.domain.models import EffectClass
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from harnessix.trusted_actions.outcome_validation import (
    OutcomeValidationError,
    validate_executor_outcome,
)
from harnessix.trusted_actions.public_errors import (
    execute_exception_outcome,
    reconcile_exception_code,
)


def validate(value):
    return validate_executor_outcome(value, deadline=monotonic() + 5)


@pytest.mark.parametrize("external", [None, uuid4()])
@pytest.mark.parametrize("output", [None, {"中文": '\n"\\'}, [True, 0, -1, 1.5], (1 << 128) - 1])
def test_valid_native_envelope_preserves_fields_and_independent_output(external, output):
    raw = ActionExecutionOutcome(
        kind="succeeded",
        output=deepcopy(output),
        artifact_sha256="b" * 64,
        external_action_id=external,
    )
    result = validate(raw)
    assert result == raw and result is not raw
    if isinstance(raw.output, (dict, list)):
        before = deepcopy(raw.output)
        assert result.output is not raw.output
        raw.output.clear()
        assert result.output == before


@pytest.mark.parametrize(
    "name,value",
    [
        ("kind", True),
        ("kind", None),
        ("kind", "invalid-kind"),
        ("kind", "x" * 1000000),
        ("spec_version", None),
        ("spec_version", "invalid"),
        ("spec_version", "x" * 1000000),
        ("external_action_id", str(uuid4())),
        ("external_action_id", object()),
        ("artifact_sha256", "short"),
        ("artifact_sha256", "x" * 1000000),
        ("artifact_sha256", True),
        ("error_code", "x" * 1000000),
        ("error_code", 12),
        ("error_code", "invalid-error"),
    ],
)
def test_constructed_or_copied_header_does_not_bypass_contract(name, value):
    raw = ActionExecutionOutcome.model_construct(kind="failed", error_code="executor_error")
    raw = raw.model_copy(update={name: value})
    with pytest.raises(OutcomeValidationError) as caught:
        validate(raw)
    assert caught.value.reason == "invalid"


def test_extra_or_missing_raw_fields_fail_closed():
    for raw in [
        ActionExecutionOutcome.model_construct(),
        ActionExecutionOutcome(kind="succeeded").model_copy(update={"extra": "data"}),
    ]:
        with pytest.raises(OutcomeValidationError) as caught:
            validate(raw)
        assert caught.value.reason == "invalid"


def test_no_executor_model_serializer_is_called(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("不应调用执行器返回实例的通用序列化器")

    monkeypatch.setattr(ActionExecutionOutcome, "model_dump_json", forbidden)
    assert validate(ActionExecutionOutcome(kind="succeeded", output={"ok": True})).output == {
        "ok": True
    }


@pytest.mark.parametrize(
    "output", [float("nan"), "\ud800", {"bad": object()}, (1, 2), {1: "invalid-key"}]
)
def test_invalid_json_values_fail_before_dto_encoding(output):
    with pytest.raises(OutcomeValidationError) as caught:
        validate(ActionExecutionOutcome.model_construct(kind="succeeded", output=output))
    assert caught.value.reason == "invalid"


def test_processing_deadline_is_checked_before_any_payload_work():
    with pytest.raises(OutcomeValidationError) as caught:
        validate_executor_outcome(object(), deadline=monotonic() - 1)
    assert caught.value.reason == "timeout"


@pytest.mark.parametrize("reason", ["invalid", "limit", "timeout", "not_registered", object()])
def test_only_finite_rejection_reason_is_public(reason):
    error = OutcomeValidationError("invalid")
    error.reason = reason
    fixed = (
        reason if type(reason) is str and reason in {"invalid", "limit", "timeout"} else "invalid"
    )
    assert execute_exception_outcome(error, EffectClass.READ_ONLY) == (
        "failed",
        f"executor_output_{fixed}",
    )
    assert execute_exception_outcome(error, EffectClass.NON_IDEMPOTENT_WRITE) == (
        "unknown",
        f"write_output_{fixed}_unknown",
    )
    assert reconcile_exception_code(error) == f"reconciliation_output_{fixed}"


def test_missing_rejection_reason_cannot_escape_public_classification():
    error = OutcomeValidationError("invalid")
    del error.reason
    assert execute_exception_outcome(error, EffectClass.NON_IDEMPOTENT_WRITE) == (
        "unknown",
        "write_output_invalid_unknown",
    )
    assert reconcile_exception_code(error) == "reconciliation_output_invalid"
