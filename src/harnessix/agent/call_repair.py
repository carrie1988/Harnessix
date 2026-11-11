"""提案阶段的确定性调用修复：把模型常见的错误调用改写为等价的正式调用。

只在调用形成前改写，依据是评测中被拒调用的实际分布；改写后的规范调用才进入审批、审计与执行，
不放宽任何校验。无法唯一确定意图的调用原样保留，由工具原有的严格校验照旧拒绝并给出提示：
- read_file只携带存档编号与分页参数时改为read_artifact；带着grep的query且全部字段都属于
  grep合同时改为grep（用错工具名的搜索）；
- read_artifact的offset与limit接受纯数字字符串，页大小超过上限时截到上限。存档分页核验按
  调用记录中的参数重算，所以这一修复必须在调用形成前完成，不能像工作区读工具那样留到执行时；
- apply_patch_batch（Workspace Patch合同）的结构修复：外层的单文件字段并入files；文件项上的
  old_text/new_text包成edits；只有edits（或operation误写为replace而没有content）时取edit；
  值为null的未知字段、文本"null"的mode视为未给出；create缺mode取默认0644，edit与delete去掉
  默认mode；同一路径的多个edit项并成一项。带值的未知字段、edit同时给出content、
  old_text与new_text相同的编辑等意图不明的写法不改；
- 前置条件补全只用宿主已掌握的事实，规划与执行前仍按补上的值核对：
  edit的expected_sha256缺失或过期、而每段old_text都在当前文件中唯一出现时，补当前文件的
  SHA-256（精确锚定已证明模型看到的是要改的当前内容）；replace与delete缺expected_sha256时，
  补本线程历史中模型最近一次得到的该文件完整摘要（文件此后变化则照常因SHA不符被拒）；
  缺path而expected_sha256恰好是历史中唯一一个文件的最近摘要时补该路径；
  replace缺mode时沿用现有文件的可执行位。模型给出的replace/delete SHA不改写。
"""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Container, Mapping, Sequence
from typing import Any, Protocol

from pydantic import JsonValue

from harnessix.agent.file_observations import observed_file_digests
from harnessix.agent.models import Item
from harnessix.artifacts.contracts import ReadArtifactInput
from harnessix.domain.models import ToolDescriptor
from harnessix.models.contracts import ToolCallCompleted, ToolCallRejected
from harnessix.tools.argument_repair import (
    ARTIFACT_ID,
    is_artifact_read,
    repair_read_arguments,
)

_ARTIFACT_TOOL = "read_artifact"
_ARTIFACT_PAGE_FIELDS = ("offset", "limit")
_PATCH_TOOL = "apply_patch_batch"
_PATCH_FILE_FIELDS = ("operation", "path", "expected_sha256", "content", "edits", "mode")
_PATCH_EDIT_FIELDS = ("old_text", "new_text")
# edit保留现有文件的真实权限（ADR 0068第2版）；模型附带的普通文件权限是噪声，可执行权限则意图不明。
_DEFAULT_FILE_MODE = 420
_EXECUTABLE_FILE_MODE = 493
_FILE_MODE_TEXT = {"420": 420, "493": 493, "0644": 420, "0755": 493}
# 模型把“未给出”写成文本；只用于可为null的mode。
_NULL_TEXT = frozenset({"", "null", "none"})

Arguments = dict[str, JsonValue]
Proposal = ToolCallCompleted | ToolCallRejected


class WorkspaceFiles(Protocol):
    """补全前置条件所需的当前工作区事实；读不到时返回None，补全随之放弃。"""

    async def read_workspace_file(self, path: str) -> bytes | None: ...

    async def workspace_file_mode(self, path: str) -> int | None: ...


def repair_proposal(
    proposal: Proposal, definitions: Mapping[str, ToolDescriptor], *, reroute: bool
) -> Proposal:
    """返回结构修复后的提案；definitions是本步可执行的工具，reroute为False时不改工具名。"""
    if not isinstance(proposal, ToolCallCompleted):
        return proposal
    tool, arguments = proposal.tool, proposal.arguments
    if reroute:
        tool, arguments = (
            reroute_artifact_read(tool, arguments, definitions)
            or reroute_grep_shaped_read(tool, arguments, definitions)
            or (tool, arguments)
        )
    if tool == _ARTIFACT_TOOL and tool in definitions:
        arguments = repair_read_arguments(tool, arguments, ReadArtifactInput)
    elif _is_workspace_patch(tool, definitions):
        arguments = repair_patch_arguments(arguments)
    if (tool, arguments) == (proposal.tool, proposal.arguments):
        return proposal
    return proposal.model_copy(update={"tool": tool, "arguments": arguments})


async def fill_patch_preconditions(
    proposal: Proposal,
    definitions: Mapping[str, ToolDescriptor],
    workspace: WorkspaceFiles,
    history: Sequence[Item] = (),
) -> Proposal:
    """用当前文件与本线程历史补全缺失的前置条件；事实不足或不唯一时保持原值，交严格校验拒绝。"""
    if not isinstance(proposal, ToolCallCompleted) or not _is_workspace_patch(
        proposal.tool, definitions
    ):
        return proposal
    files = proposal.arguments.get("files")
    if not isinstance(files, list):
        return proposal
    observed: dict[str, str] | None = None
    filled: list[JsonValue] = []
    for entry in files:
        if isinstance(entry, dict):
            if _needs_observed_digest(entry):
                # 只在确有缺口时扫描历史：多数调用是SHA齐全的edit。
                observed = observed_file_digests(history) if observed is None else observed
                entry = _with_observed_identity(entry, observed)
            entry = await _with_current_facts(entry, workspace)
        filled.append(entry)
    if filled == files:
        return proposal
    return proposal.model_copy(update={"arguments": {**proposal.arguments, "files": filled}})


def _needs_observed_digest(entry: Mapping[str, JsonValue]) -> bool:
    if entry.get("path") is None:
        return entry.get("expected_sha256") is not None
    return entry.get("operation") in ("replace", "delete") and entry.get("expected_sha256") is None


def _with_observed_identity(
    entry: dict[str, JsonValue], observed: Mapping[str, str]
) -> dict[str, JsonValue]:
    """按历史中的最近摘要补path或expected_sha256；摘要对应多个路径时不补。"""
    path, sha256 = entry.get("path"), entry.get("expected_sha256")
    if path is None:
        paths = [seen for seen, digest in observed.items() if digest == sha256]
        return {**entry, "path": paths[0]} if len(paths) == 1 else entry
    if isinstance(path, str) and path in observed:
        return {**entry, "expected_sha256": observed[path]}
    return entry


async def _with_current_facts(
    entry: dict[str, JsonValue], workspace: WorkspaceFiles
) -> dict[str, JsonValue]:
    operation, path = entry.get("operation"), entry.get("path")
    if operation == "edit":
        current = await _anchored_sha256(entry, workspace)
        if current is not None and entry.get("expected_sha256") != current:
            return {**entry, "expected_sha256": current}
    elif operation == "replace" and entry.get("mode") is None and isinstance(path, str):
        mode = await workspace.workspace_file_mode(path)
        if mode is not None:
            # 合同只表达0644与0755：沿用现有文件属主的可执行位，新内容不凭空改变可执行性。
            executable = bool(mode & stat.S_IXUSR)
            return {**entry, "mode": _EXECUTABLE_FILE_MODE if executable else _DEFAULT_FILE_MODE}
    return entry


def reroute_artifact_read(
    tool: str, arguments: Arguments, offered: Container[str]
) -> tuple[str, Arguments] | None:
    """read_file携带存档编号与分页参数时改为read_artifact；本步未开放read_artifact时不改道。

    path若是工作区路径（与存档编号不同）则意图不唯一，不改道；分页参数按read_artifact的修复
    规则换算（纯数字字符串、页大小截到上限）。
    """
    if tool != "read_file" or _ARTIFACT_TOOL not in offered or not is_artifact_read(arguments):
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
    return _ARTIFACT_TOOL, repair_read_arguments(_ARTIFACT_TOOL, rerouted, ReadArtifactInput)


def reroute_grep_shaped_read(
    tool: str, arguments: Arguments, definitions: Mapping[str, ToolDescriptor]
) -> tuple[str, Arguments] | None:
    """read_file带着grep的query、且全部字段都属于本步grep合同时改为grep。

    read_file没有query字段，这样的调用只能是用错工具名的搜索；带有anchor、start_line等
    read_file专属字段时意图不唯一，不改道，交read_file原有校验。
    """
    grep = definitions.get("grep")
    if tool != "read_file" or grep is None or "query" not in arguments:
        return None
    properties = grep.input_schema.get("properties")
    if not isinstance(properties, Mapping) or arguments.keys() - properties.keys():
        return None
    return "grep", dict(arguments)


def repair_patch_arguments(arguments: Arguments) -> Arguments:
    """并入外层单文件字段、修整每个文件项并合并同一路径的edit项；冲突或多文件时不并入外层字段。"""
    repaired = _merge_loose_file_fields(arguments)
    files = repaired.get("files")
    if not isinstance(files, list):
        return repaired
    entries: list[JsonValue] = [
        _repair_patch_file(entry) if isinstance(entry, dict) else entry for entry in files
    ]
    return {**repaired, "files": _merge_same_path_edits(entries)}


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
    _drop_null_extras(repaired, _PATCH_FILE_FIELDS)
    _normalize_edits(repaired)
    _normalize_mode(repaired)
    # 有edits而没有content只能是精确编辑：operation缺失，或误写成需要完整content的replace。
    if (
        repaired.get("operation") in (None, "replace")
        and repaired.get("edits")
        and repaired.get("content") is None
    ):
        repaired["operation"] = "edit"
    operation = repaired.get("operation")
    if operation in ("edit", "delete"):
        if repaired.get("mode") in (None, _DEFAULT_FILE_MODE):
            repaired.pop("mode", None)
        for name in ("content", "edits") if operation == "delete" else ("content",):
            if name in repaired and repaired[name] is None:
                del repaired[name]
    elif operation in ("create", "replace"):
        if repaired.get("edits") in (None, []):
            repaired.pop("edits", None)
        if operation == "create" and repaired.get("mode") is None:
            repaired["mode"] = _DEFAULT_FILE_MODE
    return repaired


def _drop_null_extras(record: dict[str, JsonValue], fields: Container[str]) -> None:
    """合同之外、值为null的字段不携带意图，去掉；带值的未知字段保留，照常被拒。"""
    for name in [name for name, value in record.items() if value is None and name not in fields]:
        del record[name]


def _normalize_edits(entry: dict[str, JsonValue]) -> None:
    """单处编辑直接写在文件项上（old_text/new_text）时包成edits；已有edits时不合并。"""
    edits = entry.get("edits")
    if edits is None and all(isinstance(entry.get(name), str) for name in _PATCH_EDIT_FIELDS):
        entry["edits"] = [{name: entry.pop(name) for name in _PATCH_EDIT_FIELDS}]
    elif isinstance(edits, list):
        entry["edits"] = [_without_null_extras(edit) for edit in edits]


def _without_null_extras(edit: JsonValue) -> JsonValue:
    if not isinstance(edit, dict):
        return edit
    cleaned = dict(edit)
    _drop_null_extras(cleaned, _PATCH_EDIT_FIELDS)
    return cleaned


def _normalize_mode(entry: dict[str, JsonValue]) -> None:
    mode = entry.get("mode")
    if not isinstance(mode, str):
        return
    text = mode.strip().lower()
    if text in _FILE_MODE_TEXT:
        entry["mode"] = _FILE_MODE_TEXT[text]
    elif text in _NULL_TEXT:
        entry["mode"] = None


def _merge_same_path_edits(files: list[JsonValue]) -> list[JsonValue]:
    """同一路径的多个edit项并成一项：合同要求一个文件的全部编辑写在同一项里。"""
    positions: dict[str, list[int]] = {}
    for index, entry in enumerate(files):
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            positions.setdefault(str(entry["path"]), []).append(index)
    merged = list(files)
    absorbed: set[int] = set()
    for indexes in positions.values():
        combined = _combined_edit([files[index] for index in indexes])
        if combined is not None:
            merged[indexes[0]] = combined
            absorbed.update(indexes[1:])
    return [entry for index, entry in enumerate(merged) if index not in absorbed]


def _combined_edit(entries: Sequence[JsonValue]) -> dict[str, JsonValue] | None:
    """各项都是针对同一前镜像的edit（SHA相同或只有部分给出）才合并；编辑按出现顺序拼接。"""
    if len(entries) < 2:
        return None
    combined: dict[str, JsonValue] = {}
    edits: list[JsonValue] = []
    for entry in entries:
        own = entry.get("edits") if isinstance(entry, dict) else None
        if (
            not isinstance(entry, dict)
            or not isinstance(own, list)
            or entry.get("operation") != "edit"
            or entry.keys() - {"operation", "path", "expected_sha256", "edits"}
        ):
            return None
        digest = entry.get("expected_sha256")
        if digest is not None and combined.setdefault("expected_sha256", digest) != digest:
            return None
        combined = {**entry, **combined}
        edits.extend(own)
    return {**combined, "edits": edits}


def _rename_digest(entry: dict[str, JsonValue]) -> None:
    """read_file结果里的content_sha256即完整前镜像摘要；与expected_sha256相同或后者缺失时并入。"""
    if "content_sha256" not in entry:
        return
    if entry.get("expected_sha256") is None:
        entry["expected_sha256"] = entry.pop("content_sha256")
    elif entry["expected_sha256"] == entry["content_sha256"]:
        del entry["content_sha256"]


async def _anchored_sha256(entry: Mapping[str, JsonValue], workspace: WorkspaceFiles) -> str | None:
    path, edits = entry.get("path"), entry.get("edits")
    if not isinstance(path, str) or not isinstance(edits, list) or not edits:
        return None
    anchors = [edit.get("old_text") for edit in edits if isinstance(edit, dict)]
    texts = [anchor for anchor in anchors if isinstance(anchor, str) and anchor]
    if len(texts) != len(edits):
        return None
    body = await workspace.read_workspace_file(path)
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
