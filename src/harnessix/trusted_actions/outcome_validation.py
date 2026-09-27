"""执行器返回合同的序列化前校验；尚未验证的效果声明不得直接完成Audit。"""

from __future__ import annotations

import json
from time import monotonic
from typing import Literal
from uuid import UUID

from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.trusted_actions.contracts import ActionExecutionOutcome
from harnessix.trusted_actions.output_budget import DEFAULT_OUTPUT_BUDGET, bounded_projection

OutcomeRejectionReason = Literal["invalid", "limit", "timeout"]


class OutcomeValidationError(Exception):
    """只携带内核有限分类，不保留原值、路径、验证详情或异常消息。"""

    def __init__(self, reason: OutcomeRejectionReason) -> None:
        super().__init__("Action返回合同未通过校验")
        self.reason = reason


def outcome_checkpoint(deadline: float) -> None:
    """编码、归一和摘要之后仍核对期限，防止同步工作绕过asyncio超时。"""

    if monotonic() >= deadline:
        raise OutcomeValidationError("timeout")


def _outcome_payload(raw: object) -> dict[str, object]:
    """只读精确合同实例的原生字段，不调用执行器提供的serializer或值转换。"""

    if type(raw) is not ActionExecutionOutcome:
        raise OutcomeValidationError("invalid")
    values = vars(raw)
    fields = {
        "spec_version",
        "kind",
        "output",
        "artifact_sha256",
        "external_action_id",
        "error_code",
    }
    if (
        type(values) is not dict
        or len(values) != len(fields)
        or any(type(key) is not str for key in values)
        or set(values) != fields
    ):
        raise OutcomeValidationError("invalid")
    for name, maximum in (
        ("spec_version", 128),
        ("kind", 32),
        ("artifact_sha256", 64),
        ("error_code", 128),
    ):
        value = values[name]
        if value is None and name in {"artifact_sha256", "error_code"}:
            continue
        if type(value) is not str or len(value) > maximum:
            raise OutcomeValidationError("invalid")
    external = values["external_action_id"]
    if external is not None and type(external) is not UUID:
        raise OutcomeValidationError("invalid")
    return {**values, "external_action_id": str(external) if external is not None else None}


def validate_executor_outcome(raw: object, *, deadline: float) -> ActionExecutionOutcome:
    """整个Outcome封套先过有界JSON预检，再进入正式DTO；不信任model_copy/construct。"""

    outcome_checkpoint(deadline)
    payload = _outcome_payload(raw)
    try:
        bounded = bounded_projection(
            payload, budget=DEFAULT_OUTPUT_BUDGET, cancel=CancelToken(), deadline=deadline
        )
    except KernelError as error:
        reason: OutcomeRejectionReason = (
            "limit"
            if error.code == "trusted_action_output_limit"
            else "timeout"
            if error.code == "trusted_action_output_timeout"
            else "invalid"
        )
        raise OutcomeValidationError(reason) from None
    try:
        # UUID采用严格JSON模式；字符串和可选字段不作任意类型转换。
        outcome = ActionExecutionOutcome.model_validate_json(
            json.dumps(bounded, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        )
    except (ValidationError, ValueError, TypeError):
        raise OutcomeValidationError("invalid") from None
    outcome_checkpoint(deadline)
    return outcome
