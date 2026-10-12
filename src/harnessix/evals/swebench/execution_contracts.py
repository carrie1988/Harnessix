"""SWE-bench Agent 执行进度、恢复身份与完整性摘要合同。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from harnessix.agent.models import TurnStatusV18
from harnessix.domain.models import ContractModel, utc_now
from harnessix.evals.swebench.contracts import (
    SWE_BENCH_CASE_COUNT,
    SweBenchPreRegistrationPlan,
)

SweBenchExecutionStatus = Literal["ready", "running", "completed", "failed", "cancelled"]

_EMPTY_PATCH_SHA256 = hashlib.sha256(b"").hexdigest()
_GIT_REVISION = r"^[0-9a-f]{40,64}$"
_SHA256 = r"^[0-9a-f]{64}$"
_ERROR_CODE = r"^[a-z][a-z0-9_]{0,127}$"


class _SweBenchExecutionContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class SweBenchCaseExecutionState(_SweBenchExecutionContract):
    """单个预注册 Case 的可恢复 Agent 执行账本。"""

    spec_version: Literal["harnessix.swebench-case-execution-state/v1"] = (
        "harnessix.swebench-case-execution-state/v1"
    )
    plan_sha256: str = Field(pattern=_SHA256)
    instance_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+$",
    )
    base_commit: str = Field(pattern=_GIT_REVISION)
    run_id: UUID
    status: SweBenchExecutionStatus
    thread_id: UUID | None = None
    turn_id: UUID | None = None
    turn_status: TurnStatusV18 | None = None
    workspace_head: str = Field(pattern=_GIT_REVISION)
    patch_sha256: str = Field(pattern=_SHA256)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    error_code: str | None = Field(default=None, pattern=_ERROR_CODE)
    started_at: AwareDatetime
    updated_at: AwareDatetime
    state_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def execution_is_consistent(self) -> Self:
        if self.updated_at < self.started_at:
            raise ValueError("SWE-bench Case 更新时间早于开始时间")
        if self.workspace_head != self.base_commit:
            raise ValueError("SWE-bench Case workspace HEAD 偏离预注册 base commit")
        if self.turn_id is not None and self.thread_id is None:
            raise ValueError("SWE-bench Case Turn 必须归属于已绑定 Thread")
        if (self.turn_status is None) != (self.turn_id is None):
            raise ValueError("SWE-bench Case Turn ID 与状态必须同时存在")

        if self.status == "ready":
            if (
                self.thread_id is not None
                or self.turn_id is not None
                or self.turn_status is not None
                or self.error_code is not None
                or self.input_tokens
                or self.output_tokens
                or self.patch_sha256 != _EMPTY_PATCH_SHA256
            ):
                raise ValueError("SWE-bench ready Case 不能提前携带执行结果")
        elif self.status == "running":
            if self.thread_id is None or self.error_code is not None:
                raise ValueError("SWE-bench running Case 必须绑定 Thread 且不能携带错误")
            if self.turn_status in {
                TurnStatusV18.COMPLETED,
                TurnStatusV18.FAILED,
                TurnStatusV18.CANCELLED,
                TurnStatusV18.INTERRUPTED,
            }:
                raise ValueError("SWE-bench running Case 不能携带终态 Turn")
        elif self.status == "completed":
            if (
                self.thread_id is None
                or self.turn_id is None
                or self.turn_status != TurnStatusV18.COMPLETED
                or self.error_code is not None
                or self.patch_sha256 == _EMPTY_PATCH_SHA256
            ):
                raise ValueError("SWE-bench completed Case 必须冻结成功 Turn 与非空 Patch")
        elif self.status == "failed":
            if self.error_code is None or self.turn_status not in {
                None,
                TurnStatusV18.COMPLETED,
                TurnStatusV18.FAILED,
                TurnStatusV18.CANCELLED,
                TurnStatusV18.INTERRUPTED,
            }:
                raise ValueError("SWE-bench failed Case 必须携带稳定错误码")
        elif self.turn_status not in {None, TurnStatusV18.CANCELLED}:
            raise ValueError("SWE-bench cancelled Case 的 Turn 状态不一致")

        if self.state_sha256 != swebench_case_execution_state_digest(self):
            raise ValueError("SWE-bench Case 执行状态摘要不匹配")
        return self


class SweBenchSuiteExecutionState(_SweBenchExecutionContract):
    """按预注册顺序保存全部 Case，并冗余可重算的恢复游标与 token 汇总。"""

    spec_version: Literal["harnessix.swebench-suite-execution-state/v1"] = (
        "harnessix.swebench-suite-execution-state/v1"
    )
    plan_sha256: str = Field(pattern=_SHA256)
    model_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
    status: SweBenchExecutionStatus
    case_states: tuple[SweBenchCaseExecutionState, ...] = Field(
        min_length=SWE_BENCH_CASE_COUNT,
        max_length=SWE_BENCH_CASE_COUNT,
    )
    completed_case_ids: tuple[str, ...] = Field(max_length=SWE_BENCH_CASE_COUNT)
    current_case_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+$",
    )
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    started_at: AwareDatetime
    updated_at: AwareDatetime
    state_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def progress_is_recomputable(self) -> Self:
        if self.updated_at < self.started_at:
            raise ValueError("SWE-bench Suite 更新时间早于开始时间")
        if any(state.plan_sha256 != self.plan_sha256 for state in self.case_states):
            raise ValueError("SWE-bench Suite Case 与预注册计划摘要不一致")
        instance_ids = tuple(state.instance_id for state in self.case_states)
        run_ids = tuple(state.run_id for state in self.case_states)
        if len(set(instance_ids)) != len(instance_ids) or len(set(run_ids)) != len(run_ids):
            raise ValueError("SWE-bench Suite Case 或 Run ID 重复")
        if any(state.started_at < self.started_at for state in self.case_states) or any(
            state.updated_at > self.updated_at for state in self.case_states
        ):
            raise ValueError("SWE-bench Suite 时间范围未覆盖 Case 状态")

        completed_count = 0
        for state in self.case_states:
            if state.status != "completed":
                break
            completed_count += 1
        completed = self.case_states[:completed_count]
        pending = self.case_states[completed_count:]
        active = tuple(state for state in pending if state.status != "ready")
        if any(state.status == "completed" for state in pending):
            raise ValueError("SWE-bench Suite 只能确认连续完成前缀")
        if len(active) > 1 or (active and active[0].instance_id != pending[0].instance_id):
            raise ValueError("SWE-bench Suite 只能执行完成前缀后的当前 Case")

        expected_completed = tuple(state.instance_id for state in completed)
        expected_current = active[0].instance_id if active else None
        if self.completed_case_ids != expected_completed:
            raise ValueError("SWE-bench Suite 完成前缀不可重算")
        if self.current_case_id != expected_current:
            raise ValueError("SWE-bench Suite 当前 Case 不可重算")
        if self.input_tokens != sum(state.input_tokens for state in self.case_states) or (
            self.output_tokens != sum(state.output_tokens for state in self.case_states)
        ):
            raise ValueError("SWE-bench Suite 累计 token 不可重算")

        if self.status == "ready":
            if completed or active:
                raise ValueError("SWE-bench ready Suite 不能已有执行进度")
        elif self.status == "running":
            if not completed and not active:
                raise ValueError("SWE-bench running Suite 缺少执行进度")
            if active and active[0].status != "running":
                raise ValueError("SWE-bench running Suite 的当前 Case 状态不一致")
        elif self.status == "completed":
            if len(completed) != SWE_BENCH_CASE_COUNT or active:
                raise ValueError("SWE-bench completed Suite 必须完成全部预注册 Case")
        elif self.status in {"failed", "cancelled"}:
            if not active or active[0].status != self.status:
                raise ValueError("SWE-bench Suite 终态与当前 Case 状态不一致")

        if self.state_sha256 != swebench_suite_execution_state_digest(self):
            raise ValueError("SWE-bench Suite 执行状态摘要不匹配")
        return self


def _state_digest(state: ContractModel) -> str:
    payload = state.model_dump(mode="json", exclude={"state_sha256"}, warnings="error")
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def swebench_case_execution_state_digest(state: SweBenchCaseExecutionState) -> str:
    return _state_digest(state)


def swebench_suite_execution_state_digest(state: SweBenchSuiteExecutionState) -> str:
    return _state_digest(state)


def validate_swebench_case_execution_state(
    plan: SweBenchPreRegistrationPlan,
    state: SweBenchCaseExecutionState,
) -> SweBenchCaseExecutionState:
    """严格重验状态，并绑定到计划中的唯一 Case 身份。"""

    checked = SweBenchCaseExecutionState.model_validate_json(state.model_dump_json(), strict=True)
    expected = next(
        (case for case in plan.cases if case.instance_id == checked.instance_id),
        None,
    )
    if (
        checked.plan_sha256 != plan.plan_sha256
        or expected is None
        or checked.base_commit != expected.base_commit
    ):
        raise ValueError("SWE-bench Case 执行状态与预注册计划不一致")
    return checked


def validate_swebench_suite_execution_state(
    plan: SweBenchPreRegistrationPlan,
    state: SweBenchSuiteExecutionState,
) -> SweBenchSuiteExecutionState:
    """严格重验 Suite，并拒绝模型、Case 身份或预注册顺序漂移。"""

    checked = SweBenchSuiteExecutionState.model_validate_json(state.model_dump_json(), strict=True)
    expected_cases = tuple((case.instance_id, case.base_commit) for case in plan.cases)
    observed_cases = tuple((case.instance_id, case.base_commit) for case in checked.case_states)
    if (
        checked.plan_sha256 != plan.plan_sha256
        or checked.model_name != plan.model_name
        or observed_cases != expected_cases
    ):
        raise ValueError("SWE-bench Suite 执行状态与预注册计划或顺序不一致")
    return checked


def build_swebench_case_execution_state(
    plan: SweBenchPreRegistrationPlan,
    instance_id: str,
    run_id: UUID,
    *,
    status: SweBenchExecutionStatus = "ready",
    thread_id: UUID | None = None,
    turn_id: UUID | None = None,
    turn_status: TurnStatusV18 | None = None,
    workspace_head: str | None = None,
    patch_sha256: str = _EMPTY_PATCH_SHA256,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_code: str | None = None,
    started_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> SweBenchCaseExecutionState:
    """从冻结计划构造一个带完整性摘要的 Case 状态。"""

    case = next((item for item in plan.cases if item.instance_id == instance_id), None)
    if case is None:
        raise ValueError("SWE-bench Case 不属于预注册计划")
    started = started_at or utc_now()
    updated = updated_at or started
    candidate = SweBenchCaseExecutionState.model_construct(
        spec_version="harnessix.swebench-case-execution-state/v1",
        plan_sha256=plan.plan_sha256,
        instance_id=case.instance_id,
        base_commit=case.base_commit,
        run_id=run_id,
        status=status,
        thread_id=thread_id,
        turn_id=turn_id,
        turn_status=turn_status,
        workspace_head=case.base_commit if workspace_head is None else workspace_head,
        patch_sha256=patch_sha256,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        error_code=error_code,
        started_at=started,
        updated_at=updated,
        state_sha256="0" * 64,
    )
    state = SweBenchCaseExecutionState.model_validate(
        {
            **candidate.model_dump(mode="python", exclude={"state_sha256"}, warnings="error"),
            "state_sha256": swebench_case_execution_state_digest(candidate),
        },
        strict=True,
    )
    return validate_swebench_case_execution_state(plan, state)


def build_swebench_suite_execution_state(
    plan: SweBenchPreRegistrationPlan,
    case_states: Sequence[SweBenchCaseExecutionState],
) -> SweBenchSuiteExecutionState:
    """汇总有序 Case 状态，生成可重算的 Suite 恢复游标。"""

    states = tuple(case_states)
    expected_cases = tuple((case.instance_id, case.base_commit) for case in plan.cases)
    observed_cases = tuple(
        (case_state.instance_id, case_state.base_commit) for case_state in states
    )
    if observed_cases != expected_cases:
        raise ValueError("SWE-bench Case 状态与预注册顺序不一致")
    checked_states = tuple(
        validate_swebench_case_execution_state(plan, case_state) for case_state in states
    )

    completed_count = 0
    for case_state in checked_states:
        if case_state.status != "completed":
            break
        completed_count += 1
    completed = checked_states[:completed_count]
    active = tuple(state for state in checked_states[completed_count:] if state.status != "ready")
    if len(completed) == SWE_BENCH_CASE_COUNT:
        status: SweBenchExecutionStatus = "completed"
    elif active:
        status = active[0].status
    elif completed:
        status = "running"
    else:
        status = "ready"

    candidate = SweBenchSuiteExecutionState.model_construct(
        spec_version="harnessix.swebench-suite-execution-state/v1",
        plan_sha256=plan.plan_sha256,
        model_name=plan.model_name,
        status=status,
        case_states=checked_states,
        completed_case_ids=tuple(state.instance_id for state in completed),
        current_case_id=active[0].instance_id if active else None,
        input_tokens=sum(state.input_tokens for state in checked_states),
        output_tokens=sum(state.output_tokens for state in checked_states),
        started_at=min(state.started_at for state in checked_states),
        updated_at=max(state.updated_at for state in checked_states),
        state_sha256="0" * 64,
    )
    suite_state = SweBenchSuiteExecutionState.model_validate(
        {
            **candidate.model_dump(mode="python", exclude={"state_sha256"}, warnings="error"),
            "state_sha256": swebench_suite_execution_state_digest(candidate),
        },
        strict=True,
    )
    return validate_swebench_suite_execution_state(plan, suite_state)
