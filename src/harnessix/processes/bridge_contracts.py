"""Agent调用与Process Action的持久身份契约；不依赖Agent运行时。"""

from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import Field, field_validator, model_validator

from harnessix.tools.contracts import ReadContract, Revision
from harnessix.tools.workspace import digest

PROCESS_ACTION_POLICY: Literal["host-process-action/v1"] = "host-process-action/v1"
PROCESS_AGENT_POLICY: Literal["agent-host-process/v1"] = "agent-host-process/v1"
PROCESS_ACTION_NAMESPACE = UUID("6bdd9f7c-58cf-56ba-9561-45aa4030bf71")


def process_call_request_id(
    thread_id: UUID,
    turn_id: UUID,
    call_id: UUID,
    workspace: str,
    call_fingerprint: str,
) -> str:
    """生成不含Action状态的稳定Agent调用身份。"""
    return digest(
        (
            PROCESS_AGENT_POLICY,
            str(thread_id),
            str(turn_id),
            str(call_id),
            workspace,
            call_fingerprint,
        )
    )


def process_action_identity(
    request_id: str,
    action_fingerprint: str,
    action_tool_version: str,
    binding_fingerprint: str,
    principal_fingerprint: str,
) -> str:
    return digest(
        (
            PROCESS_AGENT_POLICY,
            request_id,
            action_fingerprint,
            action_tool_version,
            binding_fingerprint,
            principal_fingerprint,
        )
    )


def process_binding_from_version(version: str) -> str:
    prefix = f"{PROCESS_ACTION_POLICY}."
    binding = version.removeprefix(prefix)
    if (
        not version.startswith(prefix)
        or len(binding) != 64
        or any(character not in "0123456789abcdef" for character in binding)
    ):
        raise ValueError("进程Action工具版本缺少有效宿主绑定")
    return binding


class AgentProcessCallPlan(ReadContract):
    """可进入Agent事件的有界计划；完整argv仍由ToolCall和Action保存。"""

    version: Literal["agent-host-process/v1"] = PROCESS_AGENT_POLICY
    thread_id: UUID
    turn_id: UUID
    call_id: UUID
    workspace: str = Field(min_length=1, max_length=4096)
    call_fingerprint: Revision
    request_id: Revision
    action_id: UUID
    action_fingerprint: Revision
    action_tool_version: str = Field(min_length=1, max_length=128)
    binding_fingerprint: Revision
    principal_fingerprint: Revision
    idempotency_key: str = Field(min_length=1, max_length=256)
    program: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    arguments_sha256: Revision
    timeout_seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)
    approval_fingerprint: Revision

    @field_validator("workspace")
    @classmethod
    def absolute_workspace(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("Process调用工作区必须使用绝对路径")
        return value

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        request_id = process_call_request_id(
            self.thread_id,
            self.turn_id,
            self.call_id,
            self.workspace,
            self.call_fingerprint,
        )
        identity = process_action_identity(
            request_id,
            self.action_fingerprint,
            self.action_tool_version,
            self.binding_fingerprint,
            self.principal_fingerprint,
        )
        if self.request_id != request_id:
            raise ValueError("Process稳定请求与Agent调用归属不一致")
        if self.action_id != uuid5(PROCESS_ACTION_NAMESPACE, identity):
            raise ValueError("Process Action ID与稳定调用身份不一致")
        if self.idempotency_key != f"agent-process:{identity}":
            raise ValueError("Process幂等键与稳定调用身份不一致")
        if process_binding_from_version(self.action_tool_version) != self.binding_fingerprint:
            raise ValueError("Process工具版本与宿主绑定不一致")
        if self.approval_fingerprint != digest(
            self.model_dump(mode="json", exclude={"approval_fingerprint"})
        ):
            raise ValueError("Process审批指纹与完整调用计划不一致")
        return self
