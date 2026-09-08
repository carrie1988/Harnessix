from __future__ import annotations

import base64
import os
import re
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from harnessix.processes.supervision_contracts import (
    MAX_PROCESS_ARGUMENT_BYTES,
    MAX_PROCESS_INPUT_BYTES,
    MAX_PROCESS_OUTPUT_BYTES,
    ProcessInput,
    ProcessStopReason,
    ProcessTerminal,
    SupervisionContract,
)
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
