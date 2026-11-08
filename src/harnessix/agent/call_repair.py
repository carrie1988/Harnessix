"""提案阶段的确定性调用修复：把模型常见的错误调用改写为等价的正式调用。

只在调用形成前改写，依据是评测中被拒调用的实际分布；改写后的规范调用才进入审批、审计与执行，
不放宽任何校验。无法唯一确定意图的调用原样保留，由工具原有的严格校验照旧拒绝并给出提示：
- read_file只携带存档编号与分页参数时改为read_artifact；
- apply_patch_batch（Workspace Patch合同）：外层的单文件字段并入files，补齐只有edits时的
  edit操作，去掉edit上默认的mode与空content；
- edit的expected_sha256缺失或过期、而每段old_text都在当前文件中唯一出现时，由宿主读取当前文件
  补上其SHA-256：精确锚定已证明模型看到的是要改的当前内容。规划与执行前仍按该SHA核对，
  replace与delete仍要求模型给出正确SHA。
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Container, Mapping
from typing import Any

from pydantic import JsonValue

from harnessix.domain.models import ToolDescriptor
from harnessix.models.contracts import ToolCallCompleted, ToolCallRejected
from harnessix.tools.argument_repair import ARTIFACT_ID, is_artifact_read

_ARTIFACT_PAGE_FIELDS = ("offset", "limit")
_MAX_ARTIFACT_PAGE = 200
_PATCH_TOOL = "apply_patch_batch"
_PATCH_FILE_FIELDS = ("operation", "path", "expected_sha256", "content", "edits", "mode")
# edit保留现有文件的真实权限（ADR 0068第2版）；模型附带的普通文件权限是噪声，可执行权限则意图不明。
_DEFAULT_FILE_MODE = 420
_FILE_MODE_TEXT = {"420": 420, "493": 493, "0644": 420, "0755": 493}

Arguments = dict[str, JsonValue]
Proposal = ToolCallCompleted | ToolCallRejected
ReadWorkspaceFile = Callable[[str], Awaitable[bytes | None]]


def repair_proposal(
    proposal: Proposal, definitions: Mapping[str, ToolDescriptor], *, reroute: bool
) -> Proposal:
    """返回结构修复后的提案；definitions是本步可执行的工具，reroute为False时不改工具名。"""
    if not isinstance(proposal, ToolCallCompleted):
        return proposal
    rerouted = (
        reroute_artifact_read(proposal.tool, proposal.arguments, definitions) if reroute else None
    )
    if rerouted is not None:
        tool, arguments = rerouted
        return proposal.model_copy(update={"tool": tool, "arguments": arguments})
    if _is_workspace_patch(proposal.tool, definitions):
        repaired = repair_patch_arguments(proposal.arguments)
        if repaired != proposal.arguments:
            return proposal.model_copy(update={"arguments": repaired})
    return proposal


async def fill_patch_preconditions(
    proposal: Proposal, definitions: Mapping[str, ToolDescriptor], read: ReadWorkspaceFile
) -> Proposal:
    """为精确锚定的edit补上当前文件SHA-256；读不到、非UTF-8或锚点不唯一时保持原值。"""
    if not isinstance(proposal, ToolCallCompleted) or not _is_workspace_patch(
        proposal.tool, definitions
    ):
        return proposal
    files = proposal.arguments.get("files")
    if not isinstance(files, list):
        return proposal
    filled: list[JsonValue] = []
    for entry in files:
        if isinstance(entry, dict) and entry.get("operation") == "edit":
            current = await _anchored_sha256(entry, read)
            if current is not None and entry.get("expected_sha256") != current:
                entry = {**entry, "expected_sha256": current}
        filled.append(entry)
    if filled == files:
        return proposal
    return proposal.model_copy(update={"arguments": {**proposal.arguments, "files": filled}})


def reroute_artifact_read(
    tool: str, arguments: Arguments, offered: Container[str]
) -> tuple[str, Arguments] | None:
    """read_file携带存档编号与分页参数时改为read_artifact；本步未开放read_artifact时不改道。

    path若是工作区路径（与存档编号不同）则意图不唯一，不改道；页大小超过上限时截到上限。
    """
    if tool != "read_file" or "read_artifact" not in offered or not is_artifact_read(arguments):
        return None
    path = arguments.get("path")
    artifact_id = arguments.get("artifact_id", path)
    if (
        not isinstance(artifact_id, str)
        or ARTIFACT_ID.match(artifact_id) is None
        or (path is not None and path != artifact_id)
        or arguments.keys() - {"artifact_id", "path", *_ARTIFACT_PAGE_FIELDS}
    ):
        return None
    rerouted: Arguments = {"artifact_id": artifact_id}
    for field in _ARTIFACT_PAGE_FIELDS:
        if field in arguments:
            rerouted[field] = arguments[field]
    limit = rerouted.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > _MAX_ARTIFACT_PAGE:
        rerouted["limit"] = _MAX_ARTIFACT_PAGE
    return "read_artifact", rerouted


def repair_patch_arguments(arguments: Arguments) -> Arguments:
    """并入外层单文件字段并修整每个文件项；冲突或多文件时不并入。"""
    repaired = _merge_loose_file_fields(arguments)
    files = repaired.get("files")
    if not isinstance(files, list):
        return repaired
    return {
        **repaired,
        "files": [
            _repair_patch_file(entry) if isinstance(entry, dict) else entry for entry in files
        ],
    }


def _merge_loose_file_fields(arguments: Arguments) -> Arguments:
    loose = {name: arguments[name] for name in _PATCH_FILE_FIELDS if name in arguments}
    if not loose:
        return arguments
    files = arguments.get("files", [])
    if not isinstance(files, list) or len(files) > 1:
        return arguments
    first = files[0] if files else {}
    if not isinstance(first, dict):
        return arguments
    entry = dict(first)
    # 文件项中的null视为未给出；同名字段取值冲突时意图不唯一。
    if any(entry.get(name) not in (None, value) for name, value in loose.items()):
        return arguments
    rest = {name: value for name, value in arguments.items() if name not in loose}
    return {**rest, "files": [{**entry, **loose}]}


def _repair_patch_file(entry: dict[str, JsonValue]) -> dict[str, JsonValue]:
    repaired = dict(entry)
    _rename_digest(repaired)
    if "operation" not in repaired and repaired.get("edits") and repaired.get("content") is None:
        repaired["operation"] = "edit"
    mode = repaired.get("mode")
    if isinstance(mode, str) and mode in _FILE_MODE_TEXT:
        repaired["mode"] = _FILE_MODE_TEXT[mode]
    operation = repaired.get("operation")
    if operation == "edit":
        if repaired.get("mode") in (None, _DEFAULT_FILE_MODE):
            repaired.pop("mode", None)
        if "content" in repaired and repaired["content"] is None:
            del repaired["content"]
    elif operation in ("create", "replace") and repaired.get("edits") in (None, []):
        repaired.pop("edits", None)
    elif operation == "delete":
        for name in ("content", "edits", "mode"):
            if name in repaired and repaired[name] is None:
                del repaired[name]
    return repaired


def _rename_digest(entry: dict[str, JsonValue]) -> None:
    """read_file结果里的content_sha256即完整前镜像摘要；与expected_sha256相同或后者缺失时并入。"""
    if "content_sha256" not in entry:
        return
    if entry.get("expected_sha256") is None:
        entry["expected_sha256"] = entry.pop("content_sha256")
    elif entry["expected_sha256"] == entry["content_sha256"]:
        del entry["content_sha256"]


async def _anchored_sha256(entry: Mapping[str, JsonValue], read: ReadWorkspaceFile) -> str | None:
    path, edits = entry.get("path"), entry.get("edits")
    if not isinstance(path, str) or not isinstance(edits, list) or not edits:
        return None
    anchors = [edit.get("old_text") for edit in edits if isinstance(edit, dict)]
    texts = [anchor for anchor in anchors if isinstance(anchor, str) and anchor]
    if len(texts) != len(edits):
        return None
    body = await read(path)
    if body is None:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(text.count(anchor) != 1 for anchor in texts):
        return None
    return hashlib.sha256(body).hexdigest()


def _is_workspace_patch(tool: str, definitions: Mapping[str, ToolDescriptor]) -> bool:
    """只修复Workspace Patch合同（标准与紧凑Schema解码到同一模型）；字段不同的补丁工具原样保留。"""
    definition = definitions.get(tool)
    if tool != _PATCH_TOOL or definition is None:
        return False
    schema = definition.input_schema
    files = _resolve(schema.get("properties", {}).get("files"), schema)
    item = _resolve(files.get("items") if files else None, schema)
    properties = item.get("properties", {}) if item else {}
    return {"operation", "path", "expected_sha256", "edits"} <= properties.keys()


def _resolve(node: Any, schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not isinstance(node, Mapping):
        return None
    reference = node.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        resolved = schema.get("$defs", {}).get(reference.removeprefix("#/$defs/"))
        return resolved if isinstance(resolved, Mapping) else None
    return node
