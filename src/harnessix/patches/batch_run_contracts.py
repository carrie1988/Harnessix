"""运行终止原因与文件效果分离；不改变既有组审批契约。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from harnessix.patches.batch_contracts import MAX_BATCH_FILES
from harnessix.patches.managed_contracts import PatchState
from harnessix.tools.contracts import ReadContract, Revision

MAX_RUN_EVENT_BYTES = 1024
StopReason = Literal["completed", "cancelled", "timeout", "failed", "interrupted"]
MemberEffect = Literal["not_applied", "applied", "unknown"]
BatchEffect = Literal["not_applied", "applied", "partial", "unknown"]
APPLIED = frozenset({"applied", "observed_after"})


def member_effect(state: PatchState) -> MemberEffect:
    if state in APPLIED:
        return "applied"
    if state in {"pending", "approved", "failed", "observed_before"}:
        return "not_applied"
    return "unknown"


def ordered_progress(states: tuple[PatchState, ...]) -> None:
    stopped = False
    for state in states:
        if state == "rejected" or (stopped and state != "pending"):
            raise ValueError("组成员不是成功前缀、至多一个未成功成员和未开始后缀")
        if state not in APPLIED:
            stopped = True


class BatchRunRecord(ReadContract):
    version: Literal["managed-patch-batch-run/v1"] = "managed-patch-batch-run/v1"
    batch_id: UUID
    workspace_id: UUID
    approval_fingerprint: Revision
    phase: Literal["started", "finished"]
    stop_reason: StopReason | None = None
    error_code: str | None = Field(default=None, max_length=128, pattern=r"^[a-z0-9_]+$")

    @model_validator(mode="after")
    def valid_terminal(self) -> Self:
        if (self.phase == "started") != (self.stop_reason is None):
            raise ValueError("运行阶段与终止原因不一致")
        if self.phase == "started" or self.stop_reason in {"completed", "interrupted"}:
            if self.error_code is not None:
                raise ValueError("该阶段不能携带执行错误")
        if len(self.model_dump_json().encode()) > MAX_RUN_EVENT_BYTES:
            raise ValueError("运行事件超过字节上限")
        return self


class BatchMemberEffect(ReadContract):
    plan_id: UUID
    state: PatchState
    effect: MemberEffect
    error_code: str | None = Field(default=None, max_length=128, pattern=r"^[a-z0-9_]+$")

    @model_validator(mode="after")
    def known_effect(self) -> Self:
        if self.state == "rejected" or self.effect != member_effect(self.state):
            raise ValueError("成员效果与单文件事实不符")
        return self


def aggregate(members: tuple[BatchMemberEffect, ...]) -> BatchEffect:
    effects = {member.effect for member in members}
    if "unknown" in effects:
        return "unknown"
    if effects == {"applied"}:
        return "applied"
    return "partial" if "applied" in effects else "not_applied"


class BatchExecutionResult(ReadContract):
    version: Literal["managed-patch-batch-result/v1"] = "managed-patch-batch-result/v1"
    run: BatchRunRecord
    members: tuple[BatchMemberEffect, ...] = Field(min_length=1, max_length=MAX_BATCH_FILES)
    effect: BatchEffect

    @model_validator(mode="after")
    def valid_effect(self) -> Self:
        if len({member.plan_id for member in self.members}) != len(self.members):
            raise ValueError("成员身份重复")
        ordered_progress(tuple(member.state for member in self.members))
        if self.effect != aggregate(self.members):
            raise ValueError("整组效果不一致")
        if self.run.stop_reason == "completed" and self.effect != "applied":
            raise ValueError("正常完成必须有全部已归因效果")
        return self
