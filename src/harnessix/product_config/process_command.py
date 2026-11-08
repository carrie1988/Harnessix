"""通用命令Profile的严格公共输入；执行边界仍由宿主Profile冻结。"""

from __future__ import annotations

import json

from pydantic import ConfigDict, Field, JsonValue, ValidationError, field_validator

from harnessix.domain.models import ContractModel

PRODUCT_COMMAND_VERSION = "harnessix.product-command/v1"
MAX_COMMAND_BYTES = 16_384


class RunCommandInput(ContractModel):
    """模型仅提供固定Profile ID及一条有界UTF-8 Shell命令。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    cmd: str = Field(min_length=1, max_length=MAX_COMMAND_BYTES)

    @field_validator("cmd")
    @classmethod
    def bounded_command(cls, value: str) -> str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            raise ValueError("Command编码必须是有效UTF-8") from None
        if not value.strip() or "\x00" in value or size > MAX_COMMAND_BYTES:
            raise ValueError("Command必须非空白、不含NUL且最多16384 UTF-8字节")
        return value


def run_command_schema(profile_id: str) -> dict[str, JsonValue]:
    """广告实际必填字段；UTF-8字节上限由严格解码器再次校验。"""

    return {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "const": profile_id,
                "description": (
                    f'必填字段，必须显式传入固定值"{profile_id}"；工具名不能替代该字段。'
                ),
            },
            "cmd": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_COMMAND_BYTES,
                "pattern": r"^[^\u0000]+$",
                "description": (
                    "必填非空白Shell命令，允许管道、重定向和多行；必须是有效UTF-8、"
                    "不含NUL且最多16384 UTF-8字节。"
                ),
            },
        },
        "required": ["profile", "cmd"],
        "additionalProperties": False,
    }


def decode_run_command(
    profile_id: str,
    arguments: dict[str, JsonValue],
) -> RunCommandInput:
    try:
        checked = RunCommandInput.model_validate_json(
            json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        )
    except (ValidationError, ValueError, TypeError):
        raise ValueError("Command Profile参数无效") from None
    if checked.profile != profile_id:
        raise ValueError("Command Profile不匹配")
    return checked


MAX_HOST_REASON_BYTES = 500


class HostCommandInput(RunCommandInput):
    """沙箱外命令另需理由（ADR 0113第2.2节）：说明为什么沙箱内做不到，审批时展示给用户。"""

    reason: str = Field(min_length=1, max_length=MAX_HOST_REASON_BYTES)

    @field_validator("reason")
    @classmethod
    def bounded_reason(cls, value: str) -> str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            raise ValueError("理由编码必须是有效UTF-8") from None
        if not value.strip() or any(char in value for char in "\x00\r\n") or size > 500:
            raise ValueError("理由必须是非空白单行且最多500 UTF-8字节")
        return value


def host_command_schema(profile_id: str) -> dict[str, JsonValue]:
    schema = run_command_schema(profile_id)
    properties = dict(schema["properties"])  # type: ignore[arg-type]
    properties["reason"] = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_HOST_REASON_BYTES,
        "description": (
            "必填单行理由：为什么沙箱内的命令做不到（例如需要联网安装依赖），"
            "会在审批时展示给用户；最多500 UTF-8字节。"
        ),
    }
    return {**schema, "properties": properties, "required": ["profile", "cmd", "reason"]}


def decode_host_command(profile_id: str, arguments: dict[str, JsonValue]) -> HostCommandInput:
    try:
        checked = HostCommandInput.model_validate_json(
            json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        )
    except (ValidationError, ValueError, TypeError):
        raise ValueError("沙箱外命令参数无效") from None
    if checked.profile != profile_id:
        raise ValueError("Command Profile不匹配")
    return checked
