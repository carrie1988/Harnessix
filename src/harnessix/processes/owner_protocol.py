"""受监督进程：定义宿主与独立Process Owner之间的控制帧。"""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, TypeAdapter, field_validator, model_validator

from harnessix.agent.errors import KernelError
from harnessix.processes.supervision_contracts import (
    MAX_PROCESS_ARGUMENT_BYTES,
    MAX_PROCESS_INPUT_BYTES,
    MAX_PROCESS_OUTPUT_BYTES,
    ProcessInput,
    ProcessStopReason,
    ProcessTerminal,
    SupervisionContract,
)
from harnessix.secrets.provider import MAX_SECRET_BYTES
from harnessix.secrets.redaction import secret_patterns
from harnessix.tools.contracts import Revision

MAX_OWNER_CONTROL_FRAME_BYTES = 1024 * 1024
MAX_OWNER_ENVIRONMENT_BYTES = 128 * 1024


class ProcessOwnerStart(SupervisionContract):
    spec_version: Literal["harnessix.process-owner-start/v1"] = "harnessix.process-owner-start/v1"
    process_id: UUID
    owner_identity: Revision
    owner_token: Revision = Field(repr=False)
    argv: tuple[str, ...] = Field(min_length=1, max_length=128, repr=False)
    cwd: str = Field(min_length=1, max_length=4096)
    environment: dict[str, str] = Field(max_length=128, repr=False)
    secret_names: tuple[str, ...] = Field(default=(), max_length=32)
    terminal: ProcessTerminal
    stdin: ProcessInput
    deadline: datetime
    output_bytes: int = Field(ge=1, le=MAX_PROCESS_OUTPUT_BYTES)
    input_bytes: int = Field(ge=0, le=MAX_PROCESS_INPUT_BYTES)
    columns: int = Field(ge=20, le=1000)
    rows: int = Field(ge=5, le=1000)
    terminate_grace_seconds: float = Field(default=0.5, ge=0, le=5)

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        size = sum(len(item.encode("utf-8")) + 1 for item in value)
        if any(not item or "\0" in item for item in value) or size > MAX_PROCESS_ARGUMENT_BYTES:
            raise ValueError("Process owner argv无效")
        return value

    @model_validator(mode="after")
    def start_shape(self) -> Self:
        if not os.path.isabs(self.cwd) or "\0" in self.cwd:
            raise ValueError("Process owner cwd无效")
        if self.deadline.tzinfo is None:
            raise ValueError("Process owner deadline缺少时区")
        if self.stdin == "pipe" and self.input_bytes == 0:
            raise ValueError("Process owner stdin预算无效")
        if self.stdin == "closed" and self.input_bytes != 0:
            raise ValueError("Process owner关闭stdin时预算必须为零")
        names = tuple(self.environment)
        if (
            any(
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) is None or "\0" in value
                for name, value in self.environment.items()
            )
            or sum(
                len(name.encode("utf-8")) + len(value.encode("utf-8")) + 2
                for name, value in self.environment.items()
            )
            > MAX_OWNER_ENVIRONMENT_BYTES
            or self.secret_names != tuple(sorted(set(self.secret_names)))
            or not set(self.secret_names) <= set(names)
        ):
            raise ValueError("Process owner环境无效")
        return self


class OutputRedactionSource(Protocol):
    """持久前保护材料的只读结构端口；不存在环境注入target。"""

    def output_redaction_values(self) -> tuple[bytes, ...]: ...


class ProcessOwnerStartV2(SupervisionContract):
    """私有控制管道的新封套，保留原v1 Start字段及目标环境不变。"""

    spec_version: Literal["harnessix.process-owner-start/v2"] = "harnessix.process-owner-start/v2"
    start: ProcessOwnerStart = Field(repr=False)
    output_redaction_base64: tuple[
        Annotated[str, Field(min_length=8, max_length=4 * ((MAX_SECRET_BYTES + 2) // 3))], ...
    ] = Field(max_length=32, repr=False)

    def output_values(self) -> tuple[bytes, ...]:
        """解码限定材料，保持模型保护值与目标环境严格隔离。"""
        return tuple(
            base64.b64decode(value, validate=True) for value in self.output_redaction_base64
        )

    @model_validator(mode="after")
    def bounded_protection(self) -> Self:
        # 在Base64分配前限制整个编码池，允许各项规范填充的最多两字节余量。
        encoded_limit = 4 * ((MAX_SECRET_BYTES + 2 * len(self.output_redaction_base64)) // 3)
        if sum(map(len, self.output_redaction_base64)) > encoded_limit:
            raise ValueError("Process保护编码池超过上限")
        values = self.output_values()
        if (
            sum(map(len, values)) > MAX_SECRET_BYTES
            or any(len(value) < 4 or b"\0" in value for value in values)
            or tuple(base64.b64encode(value).decode("ascii") for value in values)
            != self.output_redaction_base64
        ):
            raise ValueError("Process持久前保护材料无效")
        injected = tuple(self.start.environment[name].encode() for name in self.start.secret_names)
        try:
            secret_patterns((*injected, *values))
        except KernelError:
            raise ValueError("Process联合保护模式超过上限") from None
        return self


def protected_owner_start(
    start: ProcessOwnerStart, source: OutputRedactionSource | None
) -> ProcessOwnerStart | ProcessOwnerStartV2:
    """父进程在创建Lease及Owner前检查快照和完整封套；无保护时保留v1字节合同。"""
    if source is None:
        return start
    try:
        values = source.output_redaction_values()
        if (
            type(values) is not tuple
            or len(values) > 32
            or any(type(value) is not bytes for value in values)
            or sum(map(len, values)) > MAX_SECRET_BYTES
        ):
            raise ValueError
        packet = ProcessOwnerStartV2(
            start=start,
            output_redaction_base64=tuple(base64.b64encode(value).decode() for value in values),
        )
        if len(packet.model_dump_json().encode()) + 1 > MAX_OWNER_CONTROL_FRAME_BYTES:
            raise ValueError
        return packet
    except Exception:
        raise KernelError(
            "process_output_protection_unavailable", "Process缺少有效持久前保护能力"
        ) from None


def decode_owner_start(body: bytes) -> tuple[ProcessOwnerStart, tuple[bytes, ...]]:
    """显式版本分派；不接受旧Owner静默忽略的新保护字段。"""
    packet: ProcessOwnerStart | ProcessOwnerStartV2 = TypeAdapter(
        Annotated[ProcessOwnerStart | ProcessOwnerStartV2, Field(discriminator="spec_version")]
    ).validate_json(body)
    if isinstance(packet, ProcessOwnerStartV2):
        return packet.start, packet.output_values()
    return packet, ()


class ProcessOwnerCommand(SupervisionContract):
    spec_version: Literal["harnessix.process-owner-command/v1"] = (
        "harnessix.process-owner-command/v1"
    )
    operation: Literal["stdin", "close_stdin", "resize", "stop"]
    data_base64: str | None = Field(default=None, max_length=4 * ((64 * 1024 + 2) // 3))
    columns: int | None = Field(default=None, ge=20, le=1000)
    rows: int | None = Field(default=None, ge=5, le=1000)
    reason: ProcessStopReason | None = None

    def data(self) -> bytes:
        return (
            b"" if self.data_base64 is None else base64.b64decode(self.data_base64, validate=True)
        )

    @model_validator(mode="after")
    def command_shape(self) -> Self:
        try:
            data = self.data()
        except ValueError:
            raise ValueError("Process owner stdin不是规范Base64") from None
        if self.data_base64 is not None and base64.b64encode(data).decode() != self.data_base64:
            raise ValueError("Process owner stdin不是规范Base64")
        if self.operation == "stdin":
            if (
                not data
                or len(data) > 64 * 1024
                or any(value is not None for value in (self.columns, self.rows, self.reason))
            ):
                raise ValueError("Process owner stdin命令无效")
        elif self.operation == "resize":
            if self.columns is None or self.rows is None or self.data_base64 or self.reason:
                raise ValueError("Process owner resize命令无效")
        elif self.operation == "stop":
            if (
                self.reason not in {"cancelled", "closed"}
                or self.data_base64
                or any(value is not None for value in (self.columns, self.rows))
            ):
                raise ValueError("Process owner stop命令无效")
        elif (
            self.data_base64
            or self.reason
            or any(value is not None for value in (self.columns, self.rows))
        ):
            raise ValueError("Process owner close_stdin命令无效")
        return self
