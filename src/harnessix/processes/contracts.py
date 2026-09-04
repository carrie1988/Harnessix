from __future__ import annotations

import base64
import hashlib
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from harnessix.domain.models import ContractModel

MAX_CAPTURE_BYTES = 1024 * 1024


class ProcessContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ProcessRequest(ProcessContract):
    program: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    arguments: tuple[str, ...] = Field(default=(), max_length=128, repr=False)
    timeout_seconds: float = Field(default=30.0, gt=0, le=3600)

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any("\0" in arg for arg in value) or sum(len(a.encode()) + 1 for a in value) > 65536:
            raise ValueError("进程参数包含NUL或超过UTF-8字节上限")
        return value


class ProcessLimits(ProcessContract):
    max_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    stdout_bytes: int = Field(default=24576, ge=0, le=MAX_CAPTURE_BYTES)
    stderr_bytes: int = Field(default=24576, ge=0, le=MAX_CAPTURE_BYTES)
    stop_output_bytes: int = Field(default=8 * MAX_CAPTURE_BYTES, ge=1, le=64 * MAX_CAPTURE_BYTES)
    terminate_grace_seconds: float = Field(default=0.2, ge=0, le=5)
    pipe_drain_seconds: float = Field(default=0.5, gt=0, le=5)


class ProcessStream(ProcessContract):
    data_base64: str = Field(max_length=4 * ((MAX_CAPTURE_BYTES + 2) // 3), repr=False)
    captured_bytes: int = Field(ge=0, le=MAX_CAPTURE_BYTES)
    observed_bytes: int = Field(ge=0)
    observed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    truncated: bool
    eof: bool

    def data(self) -> bytes:
        return base64.b64decode(self.data_base64, validate=True)

    def text(self) -> str:
        return self.data().decode("utf-8", errors="strict")

    @model_validator(mode="after")
    def consistent_capture(self) -> Self:
        data = self.data()
        if (
            base64.b64encode(data).decode("ascii") != self.data_base64
            or len(data) != self.captured_bytes
            or self.observed_bytes < self.captured_bytes
            or self.truncated != (self.observed_bytes > self.captured_bytes)
            or (
                self.observed_bytes == self.captured_bytes
                and hashlib.sha256(data).hexdigest() != self.observed_sha256
            )
        ):
            raise ValueError("进程流前缀/字节数/摘要不一致")
        return self


StopReason = Literal[
    "exited", "timeout", "cancelled", "closed", "output_limit", "io_error", "cleanup_failed"
]


class ProcessResult(ProcessContract):
    version: Literal["host-process-result/v1"] = "host-process-result/v1"
    pid: int = Field(ge=2)
    returncode: int = Field(ge=-128, le=255)
    stop_reason: StopReason
    termination: Literal["none", "term", "kill", "failed"]
    stdout: ProcessStream
    stderr: ProcessStream
    elapsed_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def consistent_termination(self) -> Self:
        if (self.stop_reason == "cleanup_failed") != (self.termination == "failed"):
            raise ValueError("组终止失败必须显式归因")
        return self
