"""固定Profile的实际检查观测；前置参数拒绝与执行证据缺失分别处理。"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ItemStatus,
    ToolCallContent,
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    Turn,
)
from harnessix.evals.contracts import EvalTestObservation
from harnessix.evals.task_pack_contracts import CodingEvalTaskPackCase
from harnessix.product_config.process_action import decode_run_profile
from harnessix.tools.workspace import digest


def _input_rejected(
    call: ToolCallContent,
    result: ToolResultContent,
    case: CodingEvalTaskPackCase,
    approved_calls: set[UUID],
) -> bool:
    """只识别正式Decoder也拒绝、无审批和无任何效果的前置参数错误。"""
    if (
        result.outcome != "failed"
        or result.error is None
        or result.error.code != "tool_invalid_arguments"
        or result.output is not None
        or call.call_id in approved_calls
        or any(
            effect is not None
            for effect in (
                result.action_id,
                result.patch,
                result.patch_batch,
                result.process,
                result.trusted_action,
                result.diff_artifact,
            )
        )
    ):
        return False
    try:
        decode_run_profile(case.profile_id, "none", call.arguments)
    except ValueError:
        return True
    return False


def _profile_observation(
    result: ToolResultContent,
    profile_id: str,
    phase: Literal["baseline", "final"],
) -> EvalTestObservation | None:
    output = result.output
    if not isinstance(output, dict) or output.get("profile") != profile_id:
        return None
    state = output.get("state")
    stop_reason = output.get("stop_reason")
    returncode = output.get("returncode")
    if (
        result.outcome not in {"succeeded", "failed"}
        or state != "exited"
        or stop_reason != "exited"
        or type(returncode) is not int
    ):
        return None
    evidence = {
        "profile": profile_id,
        "state": state,
        "stop_reason": stop_reason,
        "returncode": returncode,
        "stdout": output.get("stdout"),
        "stderr": output.get("stderr"),
        "complete": output.get("complete"),
    }
    return EvalTestObservation(
        check_id=profile_id,
        phase=phase,
        passed=returncode == 0,
        returncode=returncode,
        output_sha256=digest(evidence),
        elapsed_seconds=0,
    )


def profile_observations(
    turn: Turn,
    case: CodingEvalTaskPackCase,
) -> tuple[tuple[EvalTestObservation, ...], tuple[EvalTestObservation, ...]]:
    """按实际检查顺序取首尾；中间执行不确定也必须拒绝，不以末次成功覆盖。"""
    calls = {
        item.content.call_id: item.content
        for item in turn.items
        if item.status is ItemStatus.COMPLETED and isinstance(item.content, ToolCallContent)
    }
    approved_calls = {
        item.content.call_id
        for item in turn.items
        if isinstance(item.content, TrustedActionApprovalRequestContent)
    }
    observations: list[EvalTestObservation] = []
    profile_tool = f"run_profile.{case.profile_id}"
    for item in turn.items:
        result = item.content
        if item.status is not ItemStatus.COMPLETED or not isinstance(result, ToolResultContent):
            continue
        call = calls.get(result.call_id)
        if call is None or call.tool != profile_tool:
            continue
        if _input_rejected(call, result, case, approved_calls):
            continue
        phase: Literal["baseline", "final"] = "final" if observations else "baseline"
        observation = _profile_observation(result, case.profile_id, phase)
        if observation is None:
            raise KernelError("eval_baseline_invalid", "Task Pack固定Profile调用缺少可信终态")
        observations.append(observation)
    if not observations:
        return (), ()
    return (observations[0],), (() if len(observations) == 1 else (observations[-1],))
