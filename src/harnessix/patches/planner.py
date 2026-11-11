"""受管Patch：把已验证意图冻结为可执行且可重放的计划。"""

from __future__ import annotations

import errno
import hashlib
import os
import stat

from harnessix.agent.errors import KernelError
from harnessix.patches.contracts import (
    MAX_PATCH_BYTES,
    ExactEdit,
    PatchManifest,
    PatchProposal,
    PreparedPatch,
)
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.files import _decode
from harnessix.tools.workspace import ReadOperation, Workspace, digest, revision_state


def _checkpoint(operation: ReadOperation) -> None:
    try:
        operation.checkpoint()
    except ReadToolError as error:
        raise KernelError(f"patch_{error.code}", "Patch 准备或复核已超时") from None


def _read_image(
    workspace: Workspace, path: str, operation: ReadOperation, expected_revision: str
) -> tuple[bytes, str, int]:
    try:
        with workspace.open(path, operation, directory=False) as fd:
            info = os.fstat(fd)
            mode = stat.S_IMODE(info.st_mode)
            if mode & ~0o777:
                raise KernelError("patch_unsupported_mode", "Patch 不支持特殊文件权限")
            if info.st_size > MAX_PATCH_BYTES:
                raise KernelError("patch_limit_exceeded", "Patch 完整前镜像超限")
            revision = digest((workspace.scope, path, revision_state(info)))
            if revision != expected_revision:
                raise KernelError("patch_source_changed", "前镜像 revision 已变化，请重新读取")
            content = bytearray()
            while True:
                operation.checkpoint()
                chunk = os.read(fd, min(65536, MAX_PATCH_BYTES - len(content) + 1))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_PATCH_BYTES:
                    raise KernelError("patch_limit_exceeded", "Patch 完整前镜像超限")
            body = bytes(content)
            _decode(body)
        return body, revision, mode
    except ReadToolError as error:
        raise KernelError(f"patch_{error.code}", "Patch 前镜像读取未完成") from None
    except OSError as error:
        code = {
            errno.ENOENT: "not_found",
            errno.EACCES: "path_denied",
            errno.EPERM: "path_denied",
            errno.ENOTDIR: "wrong_file_type",
        }.get(error.errno or 0, "io_failed")
        raise KernelError(f"patch_{code}", "Patch 前镜像读取未完成") from None


def _line_number(before: bytes, offset: int) -> int:
    return before.count(b"\n", 0, offset) + 1


def _missing_anchor_message(before: bytes, old: bytes, new: bytes) -> str:
    """锚点缺失时给出可行动的定位事实：改动是否已在文件中、首个锚点行是否仍存在及其行号。"""
    message = "old_text was not found in the file"
    applied = before.find(new) if new.strip() else -1
    if applied >= 0:
        return (
            f"{message}, but its new_text is already there at line "
            f"{_line_number(before, applied)}: this edit looks already applied, so remove it "
            "and resend the other edits"
        )
    first = next((line.strip() for line in old.splitlines() if line.strip()), b"")
    lines = [line.strip() for line in before.splitlines()]
    numbers = [index + 1 for index, line in enumerate(lines) if first and line == first]
    if not numbers:
        return (
            f"{message}, and neither was its first non-blank line; read_file the part you "
            "want to change and copy old_text from the current text"
        )
    shown = ", ".join(str(number) for number in numbers[:5])
    return (
        f"{message}: its first non-blank line is at line {shown}, but the lines after it differ "
        "from the file (an earlier patch may have changed them); read_file from that line and "
        "copy old_text from the current text"
    )


def _ambiguous_anchor_message(before: bytes, old: bytes) -> str:
    """锚点不唯一时给出各处行号：模型按行号补上相邻行即可唯一定位。"""
    numbers: list[str] = []
    start = before.find(old)
    while start >= 0 and len(numbers) < 5:
        numbers.append(str(_line_number(before, start)))
        start = before.find(old, start + 1)
    more = " and more" if start >= 0 else ""
    return (
        f"old_text matches more than one place (lines {', '.join(numbers)}{more}); add the "
        "line before or after the place you mean to old_text and new_text until it matches once"
    )


def _edit_ranges(
    before: bytes,
    edits: tuple[ExactEdit, ...],
    operation: ReadOperation,
    max_bytes: int,
) -> list[tuple[int, int, bytes]]:
    """每条拒绝以edits下标开头：模型只需改正这一处，其余编辑原样重发。"""
    located: list[tuple[int, int, bytes, int]] = []
    for index, edit in enumerate(edits):
        _checkpoint(operation)
        old, new = edit.old_text.encode(), edit.new_text.encode()
        start = before.find(old)
        if start < 0:
            raise KernelError(
                "patch_context_not_found",
                f"edits[{index}]: {_missing_anchor_message(before, old, new)}",
            )
        if before.find(old, start + 1) >= 0:
            raise KernelError(
                "patch_ambiguous_context",
                f"edits[{index}]: {_ambiguous_anchor_message(before, old)}",
            )
        located.append((start, start + len(old), new, index))
    located.sort(key=lambda item: item[0])
    end, previous, size = 0, 0, len(before)
    for start, stop, new, index in located:
        if start < end:
            raise KernelError(
                "patch_overlapping_edits",
                f"edits[{previous}] and edits[{index}] overlap: both old_text cover line "
                f"{_line_number(before, start)}; merge them into one edit",
            )
        end, previous = stop, index
        size += len(new) - (stop - start)
    if size > max_bytes:
        raise KernelError(
            "patch_limit_exceeded",
            "the file would exceed the patch size limit after these edits; make the change "
            "smaller or use the command tool",
        )
    return [(start, stop, new) for start, stop, new, _ in located]


def apply_exact_edits(
    before: bytes,
    edits: tuple[ExactEdit, ...],
    operation: ReadOperation,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """把唯一锚定且不重叠的精确编辑应用到完整UTF-8前镜像。"""

    ranges = _edit_ranges(
        before,
        edits,
        operation,
        MAX_PATCH_BYTES if max_bytes is None else max_bytes,
    )
    chunks: list[bytes] = []
    offset = 0
    for start, stop, new in ranges:
        _checkpoint(operation)
        chunks.extend((before[offset:start], new))
        offset = stop
    chunks.append(before[offset:])
    after = b"".join(chunks)
    if before == after:
        raise KernelError(
            "patch_no_change",
            "these edits leave the file exactly as it is; remove this entry or change new_text",
        )
    return after


def _target(before: bytes, proposal: PatchProposal, operation: ReadOperation) -> bytes:
    """保留受管Patch内部验证接缝，并把实际编辑委托给通用精确编辑器。"""

    return apply_exact_edits(before, proposal.edits, operation)


def _manifest(
    workspace: Workspace, proposal: PatchProposal, before: bytes, after: bytes, mode: int
) -> PatchManifest:
    data = {
        "version": "patch-plan/v1",
        "path": proposal.path,
        "workspace_scope": workspace.scope,
        "source_revision": proposal.expected_revision,
        "source_mode": mode,
        "proposal_sha256": digest(proposal.model_dump(mode="json")),
        "before_sha256": hashlib.sha256(before).hexdigest(),
        "after_sha256": hashlib.sha256(after).hexdigest(),
        "before_bytes": len(before),
        "after_bytes": len(after),
        "edit_count": len(proposal.edits),
    }
    return PatchManifest.model_validate({**data, "fingerprint": digest(data)})


def prepare_patch(
    workspace: Workspace, proposal: PatchProposal, operation: ReadOperation
) -> PreparedPatch:
    """同步、协作取消的宿主准备器；调用方在线程中运行时必须等待其退出。"""
    _checkpoint(operation)
    proposal = PatchProposal.model_validate_json(proposal.model_dump_json())
    before, _, mode = _read_image(workspace, proposal.path, operation, proposal.expected_revision)
    after = _target(before, proposal, operation)
    _checkpoint(operation)
    return PreparedPatch(
        _manifest(workspace, proposal, before, after, mode), proposal, before, after
    )


def validate_prepared(
    workspace: Workspace, prepared: PreparedPatch, operation: ReadOperation
) -> None:
    """只核对计划内部内容与工作区绑定，不观察当前文件。"""
    _checkpoint(operation)
    try:
        proposal = PatchProposal.model_validate_json(prepared.proposal.model_dump_json())
        manifest = PatchManifest.model_validate_json(prepared.manifest.model_dump_json())
    except ValueError:
        raise KernelError("patch_plan_corrupt", "Patch 计划契约不一致") from None
    if (
        type(prepared.before) is not bytes
        or type(prepared.after) is not bytes
        or len(prepared.before) > MAX_PATCH_BYTES
        or len(prepared.after) > MAX_PATCH_BYTES
    ):
        raise KernelError("patch_plan_corrupt", "Patch 私有内容不符合契约")
    if workspace.scope != manifest.workspace_scope:
        raise KernelError("patch_workspace_changed", "Patch 工作区能力已变化")
    try:
        expected = _manifest(
            workspace, proposal, prepared.before, prepared.after, manifest.source_mode
        )
        _decode(prepared.before)
        if expected != manifest or _target(prepared.before, proposal, operation) != prepared.after:
            raise ValueError("载荷不一致")
    except KernelError as error:
        if error.code == "patch_timeout":
            raise
        raise KernelError("patch_plan_corrupt", "Patch 提案、正文或 manifest 不一致") from None
    except (ValueError, ReadToolError):
        raise KernelError("patch_plan_corrupt", "Patch 提案、正文或 manifest 不一致") from None


def verify_prepared(
    workspace: Workspace, prepared: PreparedPatch, operation: ReadOperation
) -> None:
    """只读复核，不是与未来文件提交原子关联的 compare-and-swap。"""
    validate_prepared(workspace, prepared, operation)
    manifest = prepared.manifest
    current, revision, mode = _read_image(
        workspace, manifest.path, operation, manifest.source_revision
    )
    if (
        current != prepared.before
        or revision != manifest.source_revision
        or mode != manifest.source_mode
    ):
        raise KernelError("patch_source_changed", "Patch 前镜像已变化，未修改文件")
    _checkpoint(operation)
