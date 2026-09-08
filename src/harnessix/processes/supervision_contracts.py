from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel
from harnessix.execution.contracts import EnvironmentBinding, canonical_digest
from harnessix.tools.contracts import Revision
from harnessix.workspace.contracts import PlatformKind

ProcessInvocation = Literal["argv", "posix_sh", "cmd", "powershell"]
ProcessTerminal = Literal["pipe", "pty"]
ProcessInput = Literal["closed", "pipe"]
ProcessLifecycle = Literal["foreground", "background"]
ProcessLaunchKind = Literal["host", "container"]
ProcessLeaseState = Literal[
    "prepared", "starting", "running", "stopping", "exited", "failed", "unknown"
]
ProcessStopReason = Literal[
    "exited",
    "timeout",
    "cancelled",
    "closed",
    "output_limit",
    "input_limit",
    "io_error",
    "host_lost",
    "launch_failed",
    "cleanup_failed",
    "unknown",
]

MAX_PROCESS_ARGUMENT_BYTES = 64 * 1024
MAX_PROCESS_INPUT_BYTES = 1024 * 1024
MAX_PROCESS_OUTPUT_BYTES = 64 * 1024 * 1024


class SupervisionContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ProcessSpec(SupervisionContract):
    spec_version: Literal["harnessix.process-spec/v1"] = "harnessix.process-spec/v1"
    process_id: UUID = Field(default_factory=uuid4)
    invocation: ProcessInvocation
    argv: tuple[str, ...] = Field(default=(), max_length=128, repr=False)
    shell_source: str | None = Field(
        default=None, max_length=MAX_PROCESS_ARGUMENT_BYTES, repr=False
    )
    terminal: ProcessTerminal = "pipe"
    stdin: ProcessInput = "closed"
    lifecycle: ProcessLifecycle = "foreground"
    timeout_seconds: float = Field(default=300.0, gt=0, le=86400)
    output_bytes: int = Field(default=8 * 1024 * 1024, ge=1, le=MAX_PROCESS_OUTPUT_BYTES)
    input_bytes: int = Field(default=0, ge=0, le=MAX_PROCESS_INPUT_BYTES)
    columns: int = Field(default=120, ge=20, le=1000)
    rows: int = Field(default=40, ge=5, le=1000)
    digest: Revision

    @field_validator("argv")
    @classmethod
    def bounded_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        try:
            size = sum(len(item.encode("utf-8")) + 1 for item in value)
        except UnicodeError:
            raise ValueError("进程argv不是有效UTF-8") from None
        if any(not item or "\0" in item for item in value) or size > MAX_PROCESS_ARGUMENT_BYTES:
            raise ValueError("进程argv为空、包含NUL或超过字节上限")
        return value

    @model_validator(mode="after")
    def invocation_shape_and_digest(self) -> Self:
        if self.invocation == "argv":
            if not self.argv or self.shell_source is not None:
                raise ValueError("argv模式必须仅提供非空argv")
        elif self.argv or self.shell_source is None or not self.shell_source.strip():
            raise ValueError("Shell模式必须仅提供非空source")
        if self.shell_source is not None:
            try:
                source_size = len(self.shell_source.encode("utf-8"))
            except UnicodeError:
                raise ValueError("Shell source不是有效UTF-8") from None
            if "\0" in self.shell_source or source_size > MAX_PROCESS_ARGUMENT_BYTES:
                raise ValueError("Shell source包含NUL或超过字节上限")
        if self.stdin == "pipe" and self.input_bytes == 0:
            raise ValueError("开启stdin时输入预算必须大于零")
        if self.stdin == "closed" and self.input_bytes != 0:
            raise ValueError("关闭stdin时输入预算必须为零")
        if self.digest != process_spec_digest(self):
            raise ValueError("ProcessSpec摘要不一致")
        return self


def process_spec_digest(spec: ProcessSpec) -> str:
    return canonical_digest(spec.model_dump(mode="json", exclude={"digest"}, warnings="error"))


class ProcessCapabilityProbe(SupervisionContract):
    spec_version: Literal["harnessix.process-capability/v1"] = "harnessix.process-capability/v1"
    platform: PlatformKind
    owner_backend: Literal["posix_session", "windows_job_object"]
    invocation_modes: tuple[ProcessInvocation, ...] = Field(min_length=1, max_length=4)
    supports_pipe: Literal[True] = True
    supports_pty: bool
    supports_background: Literal[True] = True
    supports_process_tree: Literal[True] = True
    atomic_containment: Literal[True] = True
    implementation_digest: Revision
    digest: Revision

    @model_validator(mode="after")
    def capability_shape_and_digest(self) -> Self:
        expected_backend = "windows_job_object" if self.platform == "windows" else "posix_session"
        if self.owner_backend != expected_backend:
            raise ValueError("Process owner后端与平台不一致")
        if self.invocation_modes != tuple(sorted(set(self.invocation_modes))):
            raise ValueError("Process调用模式必须唯一且有序")
        if self.digest != process_capability_digest(self):
            raise ValueError("Process能力摘要不一致")
        return self


def process_capability_digest(probe: ProcessCapabilityProbe) -> str:
    return canonical_digest(probe.model_dump(mode="json", exclude={"digest"}, warnings="error"))


class ProcessLaunchBinding(SupervisionContract):
    spec_version: Literal["harnessix.process-launch-binding/v1"] = (
        "harnessix.process-launch-binding/v1"
    )
    kind: ProcessLaunchKind
    platform: PlatformKind
    plan_fingerprint: Revision
    intent_arguments_digest: Revision
    process_spec_digest: Revision
    capability_digest: Revision
    environment: tuple[EnvironmentBinding, ...] = Field(default=(), max_length=128)
    digest: Revision

    @model_validator(mode="after")
    def complete_binding(self) -> Self:
        comparison = str.casefold if self.platform == "windows" else lambda value: value
        names = [comparison(item.name) for item in self.environment]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("Process启动环境不符合平台排序或唯一性")
        if self.digest != process_launch_binding_digest(self):
            raise ValueError("Process启动绑定摘要不一致")
        return self


def process_launch_binding_digest(binding: ProcessLaunchBinding) -> str:
    return canonical_digest(binding.model_dump(mode="json", exclude={"digest"}, warnings="error"))


class ProcessOutputObservation(SupervisionContract):
    observed_bytes: int = Field(ge=0)
    persisted_bytes: int = Field(ge=0, le=MAX_PROCESS_OUTPUT_BYTES)
    sha256: Revision
    persisted_sha256: Revision
    truncated: bool
    eof: bool

    @model_validator(mode="after")
    def consistent_output(self) -> Self:
        if self.persisted_bytes > self.observed_bytes or self.truncated != (
            self.persisted_bytes < self.observed_bytes
        ):
            raise ValueError("Process输出观察不一致")
        empty_digest = hashlib.sha256(b"").hexdigest()
        if self.observed_bytes == 0 and self.sha256 != empty_digest:
            raise ValueError("空Process输出摘要不一致")
        if self.persisted_bytes == 0 and self.persisted_sha256 != empty_digest:
            raise ValueError("空Process持久输出摘要不一致")
        return self


def empty_process_output(*, eof: bool = False) -> ProcessOutputObservation:
    return ProcessOutputObservation(
        observed_bytes=0,
        persisted_bytes=0,
        sha256=hashlib.sha256(b"").hexdigest(),
        persisted_sha256=hashlib.sha256(b"").hexdigest(),
        truncated=False,
        eof=eof,
    )


class ProcessLease(SupervisionContract):
    spec_version: Literal["harnessix.process-lease/v1"] = "harnessix.process-lease/v1"
    process_id: UUID
    plan_id: UUID
    plan_fingerprint: Revision
    process_spec_digest: Revision
    capability_digest: Revision
    launch_binding_digest: Revision
    lifecycle: ProcessLifecycle
    state: ProcessLeaseState
    sequence: int = Field(ge=0)
    owner_token: Revision = Field(repr=False)
    owner_identity: Revision | None = None
    pid: int | None = Field(default=None, ge=2, le=2**32 - 1)
    deadline: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    returncode: int | None = Field(default=None, ge=-(2**31), le=2**32 - 1)
    stop_reason: ProcessStopReason | None = None
    stdout: ProcessOutputObservation = Field(default_factory=empty_process_output)
    stderr: ProcessOutputObservation = Field(default_factory=empty_process_output)

    @model_validator(mode="after")
    def lifecycle_shape(self) -> Self:
        if self.deadline.tzinfo is None:
            raise ValueError("Process Lease截止时间必须包含时区")
        identity_values = (self.owner_identity, self.pid, self.started_at)
        has_identity = all(value is not None for value in identity_values)
        if any(value is not None for value in identity_values) and not has_identity:
            raise ValueError("Process Lease运行身份只能完整存在或全部缺失")
        if self.state in {"running", "stopping", "exited"} and not has_identity:
            raise ValueError("Process Lease运行身份不完整")
        if self.state in {"prepared", "starting", "failed"} and has_identity:
            raise ValueError("Process Lease运行身份不完整")
        terminal = self.state in {"exited", "failed", "unknown"}
        if terminal != (self.stop_reason is not None and self.finished_at is not None):
            raise ValueError("Process Lease终态事实不完整")
        if self.state == "exited" and self.returncode is None:
            raise ValueError("退出Process必须包含returncode")
        if self.state != "exited" and self.returncode is not None:
            raise ValueError("非退出Process不能包含returncode")
        if self.state == "failed" and self.stop_reason != "launch_failed":
            raise ValueError("启动失败Process必须使用launch_failed原因")
        if self.started_at is not None and self.started_at.tzinfo is None:
            raise ValueError("Process启动时间必须包含时区")
        if self.finished_at is not None:
            if self.finished_at.tzinfo is None:
                raise ValueError("Process结束时间无效")
            if self.started_at is not None and self.finished_at < self.started_at:
                raise ValueError("Process结束时间早于启动时间")
        if self.state == "unknown" and self.stop_reason not in {
            "host_lost",
            "cleanup_failed",
            "unknown",
        }:
            raise ValueError("未知Process使用了确定性停止原因")
        return self


def process_lease_binding(lease: ProcessLease) -> tuple[object, ...]:
    return (
        lease.process_id,
        lease.plan_id,
        lease.plan_fingerprint,
        lease.process_spec_digest,
        lease.capability_digest,
        lease.launch_binding_digest,
        lease.lifecycle,
        lease.owner_token,
        lease.deadline,
    )
