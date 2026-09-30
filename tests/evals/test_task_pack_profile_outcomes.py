from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import (
    ItemStatus,
    PatchBatchEffect,
    PatchEffect,
    ProcessActionEffect,
    ToolResultContent,
    TrustedActionEffect,
)
from harnessix.artifacts.contracts import ArtifactRef
from harnessix.domain.models import ActionStatus
from harnessix.evals.task_pack_trial import _profile_observations
from harnessix.product_config.process_action import decode_run_profile
from harnessix.tools.workspace import digest
from tests.evals.test_grader import completed_turn, item
from tests.evals.test_task_pack_execution import _profile_approval

_PROFILE = "agents-dump-compatible-refactor-check"
_SHA = "a" * 64


def _pair(*, arguments=None, outcome="succeeded", output=None, error=None):
    turn, approval, case = _profile_approval(
        {"profile": _PROFILE} if arguments is None else arguments
    )
    call = turn.items[0]
    result = item(
        ToolResultContent(
            call_id=call.content.call_id,
            outcome=outcome,
            output=output,
            error=error,
        )
    )
    return (call, result), approval, case


def _rejected(arguments=None):
    return _pair(
        arguments={} if arguments is None else arguments,
        outcome="failed",
        error=AgentFailure(code="tool_invalid_arguments", message="Profile参数无效"),
    )


def _output(returncode=0, *, stdout="fixture"):
    return {
        "profile": _PROFILE,
        "state": "exited",
        "stop_reason": "exited",
        "returncode": returncode,
        "stdout": stdout,
        "stderr": "",
        "complete": True,
    }


def _exited(returncode=0, *, stdout="fixture"):
    return _pair(
        outcome="succeeded" if returncode == 0 else "failed",
        output=_output(returncode, stdout=stdout),
    )


def _project(items, case):
    return _profile_observations(completed_turn().model_copy(update={"items": items}), case)


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"profile": "other-profile"},
        {"profile": _PROFILE, "selectors": ["tests/test_a.py"]},
        {"profile": _PROFILE, "selectors": None},
        {"profile": _PROFILE, "selectors": ""},
        {"profile": _PROFILE, "program": "/bin/sh"},
        {"profile": _PROFILE, "environment": {}},
    ),
)
def test_formally_rejected_no_effect_results_are_empty_evidence(arguments) -> None:
    rejected, _, case = _rejected(arguments)
    with pytest.raises(ValueError):
        decode_run_profile(case.profile_id, "none", arguments)

    assert _project((), case) == ((), ())
    second, _, _ = _rejected(arguments)
    assert _project((*rejected, *second), case) == ((), ())


@pytest.mark.parametrize("middle_exit", (False, True), ids=("two-exits", "three-exits"))
def test_rejections_do_not_replace_first_and_last_actual_exits(middle_exit) -> None:
    before, _, case = _rejected()
    baseline, _, _ = _exited(1, stdout="actual-baseline")
    between, _, _ = _rejected()
    middle = _exited(2, stdout="actual-middle")[0] if middle_exit else ()
    final, _, _ = _exited(0, stdout="actual-final")
    after, _, _ = _rejected()

    observed_baseline, observed_final = _project(
        (*before, *baseline, *between, *middle, *final, *after), case
    )

    assert len(observed_baseline) == len(observed_final) == 1
    first, last = observed_baseline[0], observed_final[0]
    assert (first.check_id, first.phase, first.passed, first.returncode) == (
        _PROFILE,
        "baseline",
        False,
        1,
    )
    assert (last.check_id, last.phase, last.passed, last.returncode) == (_PROFILE, "final", True, 0)
    assert first.output_sha256 == digest(baseline[1].content.output)
    assert last.output_sha256 == digest(final[1].content.output)


@pytest.mark.parametrize("returncode", (0, 1))
def test_only_one_actual_exit_is_baseline_even_among_rejections(returncode) -> None:
    before, _, case = _rejected()
    actual, _, _ = _exited(returncode)
    after, _, _ = _rejected()

    baseline, final = _project((*before, *actual, *after), case)

    assert len(baseline) == 1 and final == ()
    assert baseline[0].phase == "baseline"
    assert baseline[0].passed is (returncode == 0)
    assert baseline[0].returncode == returncode
    assert baseline[0].output_sha256 == digest(actual[1].content.output)


@pytest.mark.parametrize(
    "arguments", ({"profile": _PROFILE}, {"profile": _PROFILE, "selectors": []})
)
def test_invalid_arguments_label_does_not_exempt_formally_valid_calls(arguments) -> None:
    rejected, _, case = _rejected(arguments)
    assert decode_run_profile(case.profile_id, "none", arguments).selectors == ()

    with pytest.raises(KernelError) as failure:
        _project(rejected, case)
    assert failure.value.code == "eval_baseline_invalid"


@pytest.mark.parametrize(
    "updates",
    (
        {"outcome": "succeeded"},
        {"outcome": "unknown"},
        {"outcome": "cancelled"},
        {"error": None},
        {"error": AgentFailure(code="tool_failed", message="非参数拒绝")},
        {"output": {}},
        {"output": ""},
    ),
)
def test_rejection_exemption_requires_exact_outcome_error_and_absent_output(updates) -> None:
    rejected, _, case = _rejected()
    call, result = rejected
    changed = result.model_copy(update={"content": result.content.model_copy(update=updates)})

    with pytest.raises(KernelError) as failure:
        _project((call, changed), case)
    assert failure.value.code == "eval_baseline_invalid"


def _effect_markers():
    identity = uuid4()
    return {
        "action_id": identity,
        "patch": PatchEffect(
            workspace_id=identity,
            plan_id=uuid4(),
            request_id=_SHA,
            approval_fingerprint=_SHA,
            state="applied",
            origin="execution",
        ),
        "patch_batch": PatchBatchEffect(
            workspace_id=identity,
            batch_id=uuid4(),
            request_id=_SHA,
            approval_fingerprint=_SHA,
            origin="execution",
        ),
        "process": ProcessActionEffect(
            plan_fingerprint=_SHA,
            action_id=identity,
            action_fingerprint=_SHA,
            status=ActionStatus.FAILED,
            result_fingerprint=_SHA,
            origin="execution",
        ),
        "trusted_action": TrustedActionEffect(
            plan_id=identity,
            plan_fingerprint=_SHA,
            state="failed",
            origin="execution",
        ),
        "diff_artifact": ArtifactRef(
            artifact_id=identity,
            sha256=_SHA,
            size_bytes=1,
            records=1,
            complete=True,
            expires_at=datetime(2100, 1, 1, tzinfo=UTC),
        ),
    }


@pytest.mark.parametrize("field", tuple(_effect_markers()))
def test_any_effect_marker_prevents_no_effect_rejection_exemption(field) -> None:
    rejected, _, case = _rejected()
    call, result = rejected
    # 纯投影测试单独扰动每个字段，防止依赖其他效果字段间接通过保护。
    content = result.content.model_copy(update={field: _effect_markers()[field]})

    with pytest.raises(KernelError) as failure:
        _project((call, result.model_copy(update={"content": content})), case)
    assert failure.value.code == "eval_baseline_invalid"


@pytest.mark.parametrize("status", tuple(ItemStatus))
def test_same_call_approval_in_any_item_state_prevents_rejection_exemption(status) -> None:
    rejected, approval, case = _rejected()
    request = item(approval).model_copy(update={"status": status})

    with pytest.raises(KernelError) as failure:
        _project((*rejected, request), case)
    assert failure.value.code == "eval_baseline_invalid"


def test_unrelated_call_approval_does_not_taint_no_effect_rejection() -> None:
    rejected, approval, case = _rejected()
    unrelated = item(approval.model_copy(update={"call_id": uuid4()}))

    assert _project((*rejected, unrelated), case) == ((), ())


@pytest.mark.parametrize("position", (0, 1, 2), ids=("baseline", "middle", "final"))
@pytest.mark.parametrize(
    "updates",
    (
        {"outcome": "unknown"},
        {"outcome": "cancelled"},
        {"output": None},
        {"output": []},
        {"output": {}},
        {"output": _output() | {"profile": "other-profile"}},
        {"output": _output() | {"state": "running"}},
        {"output": _output() | {"state": "unknown"}},
        {"output": _output() | {"state": "cancelled"}},
        {"output": _output() | {"stop_reason": "cancelled"}},
        {"output": _output() | {"stop_reason": None}},
        {"output": _output() | {"returncode": None}},
        {"output": _output() | {"returncode": True}},
        {"output": _output() | {"returncode": "0"}},
    ),
    ids=(
        "unknown-disguised-exit",
        "cancelled-disguised-exit",
        "missing-output",
        "non-object-output",
        "missing-terminal",
        "wrong-profile",
        "running",
        "unknown-terminal",
        "cancelled-terminal",
        "cancelled-stop",
        "missing-stop",
        "missing-returncode",
        "boolean-returncode",
        "string-returncode",
    ),
)
def test_any_untrusted_same_profile_result_fails_closed_including_middle(position, updates) -> None:
    pairs = [_exited(1)[0], _exited(0)[0], _exited(0)[0]]
    call, result = pairs[position]
    pairs[position] = (
        call,
        result.model_copy(update={"content": result.content.model_copy(update=updates)}),
    )
    _, _, case = _exited()

    with pytest.raises(KernelError) as failure:
        _project(tuple(entry for pair in pairs for entry in pair), case)
    assert failure.value.code == "eval_baseline_invalid"


@pytest.mark.parametrize("status", (ItemStatus.STARTED, ItemStatus.FAILED, ItemStatus.CANCELLED))
def test_only_completed_same_profile_call_result_pairs_enter_observations(status) -> None:
    rejected, _, case = _rejected()
    call, result = rejected
    unrelated_call = call.model_copy(
        update={"content": call.content.model_copy(update={"tool": "read_file"})}
    )
    orphan = result.model_copy(
        update={"content": result.content.model_copy(update={"call_id": uuid4()})}
    )

    assert _project((call.model_copy(update={"status": status}), result), case) == ((), ())
    assert _project((call, result.model_copy(update={"status": status})), case) == ((), ())
    assert _project((unrelated_call, result, orphan), case) == ((), ())
