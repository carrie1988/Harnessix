"""Workspace Patch的紧凑模型可见合同：同一严格解码模型，较短描述与Schema。

只改变模型看到的文字与Schema形状；参数仍由 `WorkspacePatchInput` 以JSON模式严格校验，
因此非法组合（如edit携带content）照旧在宿主侧失败，不放宽任何执行、审批或恢复语义。
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, JsonValue

from harnessix.delivery.trusted_action_contracts import WorkspacePatchInput

WorkspacePatchContract = Literal["standard", "compact"]
WORKSPACE_PATCH_COMPACT_VERSION = "harnessix.workspace-patch-compact/v1"

COMPACT_WORKSPACE_PATCH_DESCRIPTION = (
    "Approval-gated transactional write of up to 16 workspace text files. Prefer operation "
    '"edit": path, expected_sha256 (full content_sha256 from a complete read_file of that '
    "file, never revision) and edits [{old_text, new_text}]; copy each old_text exactly from "
    "observed source, unique in the file, edits non-overlapping, all edits of one file in one "
    'entry. "create": path, content, mode 420 or 493. "replace": rewrite a fully read file '
    'with expected_sha256, content and mode. "delete": expected_sha256. content is full file '
    "text, not a diff."
)

_EXACT_EDIT: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["old_text", "new_text"],
    "properties": {"old_text": {"type": "string"}, "new_text": {"type": "string"}},
}

_COMPACT_INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["files"],
    "properties": {
        "files": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["operation", "path"],
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["edit", "create", "replace", "delete"],
                    },
                    "path": {"type": "string"},
                    "expected_sha256": {"type": "string"},
                    "edits": {"type": "array", "minItems": 1, "maxItems": 32, "items": _EXACT_EDIT},
                    "content": {"type": "string"},
                    "mode": {"type": "integer", "enum": [420, 493]},
                },
            },
        }
    },
}


def compact_workspace_patch_schema() -> dict[str, JsonValue]:
    return deepcopy(_COMPACT_INPUT_SCHEMA)


def decode_compact_workspace_patch(arguments: dict[str, JsonValue]) -> BaseModel:
    """与Router标准解码完全相同的JSON模式校验；省略字段按原合同默认为null。"""
    return WorkspacePatchInput.model_validate_json(
        json.dumps(arguments, ensure_ascii=False, allow_nan=False)
    )
