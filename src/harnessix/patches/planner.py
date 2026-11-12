"""受管Patch：把已验证意图冻结为可执行且可重放的计划。"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from bisect import bisect_left
from functools import cached_property

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

_LF, _CRLF = b"\n", b"\r\n"


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


class _FoldedNewlines:
    """CRLF折成LF后的正文及其到原始字节偏移的映射；用到时才计算，同一文件的各条编辑共用。"""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    @cached_property
    def text(self) -> bytes:
        return self._raw.replace(_CRLF, _LF)

    @cached_property
    def _breaks(self) -> list[int]:
        # 各CRLF在折叠正文中的位置：它之前的每个CRLF都已少一个字节。
        breaks: list[int] = []
        position = self._raw.find(_CRLF)
        while position >= 0:
            breaks.append(position - len(breaks))
            position = self._raw.find(_CRLF, position + len(_CRLF))
        return breaks

    def raw_offset(self, offset: int) -> int:
        """折叠正文偏移对应的原始偏移；落在换行上时指向CRLF的起点，映射出的区间不拆开换行。"""
        return offset + bisect_left(self._breaks, offset)


def _missing_anchor_message(folded: bytes, old: bytes, new: bytes) -> str:
    """锚点缺失时给出可行动的定位事实：改动是否已在文件中、首个锚点行是否仍存在及其行号。

    调用前已确认忽略CRLF/LF差异也找不到old_text；这里同样在折叠正文上比较，
    不把换行风格的差异说成内容不一致。行号按LF计数，与read_file的分页一致。
    """
    message = "精确编辑锚点不存在"
    if new.strip() and folded.find(new.replace(_CRLF, _LF)) >= 0:
        return (
            f"{message}；new_text已存在于当前文件（不计CRLF/LF换行差异），"
            "此编辑可能已经应用，请先read_file核对"
        )
    first = next((line.strip() for line in old.splitlines() if line.strip()), b"")
    lines = [line.strip() for line in folded.split(_LF)]
    numbers = [index + 1 for index, line in enumerate(lines) if first and line == first]
    if not numbers:
        return f"{message}；old_text首个非空行不在当前文件中，请先read_file取得当前内容"
    shown = ", ".join(str(number) for number in numbers[:5])
    return (
        f"{message}；old_text首个非空行（不计首尾空白）出现在当前文件第{shown}行，"
        "但old_text与该处内容不一致，且不只是CRLF/LF换行的差异（可能已被此前的Patch修改），"
        "请read_file该区域后按当前文本重写old_text"
    )


def _anchor_range(
    before: bytes, folded: _FoldedNewlines, edit: tuple[bytes, bytes], *, newline_aware: bool
) -> tuple[int, int]:
    """一条编辑要替换的原始字节区间：先逐字节匹配；一处也没有时忽略CRLF/LF差异再匹配一次。

    两步各自要求唯一：逐字节命中多处、或忽略换行后命中多处，都按锚点不唯一拒绝，不从中挑选。
    """
    old, new = edit
    start = before.find(old)
    if start >= 0:
        if before.find(old, start + 1) >= 0:
            raise KernelError("patch_ambiguous_context", "编辑锚点不唯一，请提供更完整上下文")
        # 以LF开头的old_text命中CRLF的后半时把\r一并纳入：区间不拆开换行，改写后不会留下孤立的\r。
        if newline_aware and old.startswith(_LF) and before[start - 1 : start] == b"\r":
            return start - 1, start + len(old)
        return start, start + len(old)
    needle = old.replace(_CRLF, _LF)
    start = folded.text.find(needle) if newline_aware else -1
    if start < 0:
        raise KernelError("patch_context_not_found", _missing_anchor_message(folded.text, old, new))
    if folded.text.find(needle, start + 1) >= 0:
        raise KernelError("patch_ambiguous_context", "编辑锚点不唯一，请提供更完整上下文")
    return folded.raw_offset(start), folded.raw_offset(start + len(needle))


def _newline_style(before: bytes, start: int, stop: int) -> bytes | None:
    """被替换处的换行风格；整个文件没有换行时为None。

    区域内有换行时取占多数的一种，持平取其中第一个；区域内没有换行时取所在行的行尾，
    所在行是没有行尾的末行时取上一行的行尾。
    """
    crlf = before.count(_CRLF, start, stop)
    lf = before.count(_LF, start, stop) - crlf
    if crlf != lf:
        return _CRLF if crlf > lf else _LF
    position = before.find(_LF, start)
    if position < 0:
        position = before.rfind(_LF, 0, start)
    if position < 0:
        return None
    return _CRLF if before[position - 1 : position] == b"\r" else _LF


def _written_text(before: bytes, start: int, stop: int, edit: tuple[bytes, bytes]) -> bytes:
    """new_text实际写入的字节：其中的换行按被替换处的风格写入，CRLF处保持CRLF，LF处保持LF。

    模型看到的文本与它发回的文本换行风格并不可靠（同一处编辑会混用两种），所以换行风格由文件决定。
    例外：折叠换行后old_text与new_text相同的编辑只可能是有意转换换行，原样写入。
    """
    old, new = edit
    if old.replace(_CRLF, _LF) == new.replace(_CRLF, _LF):
        return new
    style = _newline_style(before, start, stop)
    return new if style is None else new.replace(_CRLF, _LF).replace(_LF, style)


def _located_ranges(
    before: bytes,
    edits: tuple[ExactEdit, ...],
    operation: ReadOperation,
    max_bytes: int,
    *,
    newline_aware: bool,
) -> list[tuple[int, int, bytes]]:
    """各条编辑的原始字节区间与写入文本，按起点排序；重叠与大小上限都按真实字节判定。

    newline_aware为False时是ADR 0141之前的语义（只逐字节匹配、new_text原样写入），
    只用于复核此前保存的计划。
    """
    ranges: list[tuple[int, int, bytes]] = []
    folded = _FoldedNewlines(before)
    for edit in edits:
        _checkpoint(operation)
        texts = edit.old_text.encode(), edit.new_text.encode()
        start, stop = _anchor_range(before, folded, texts, newline_aware=newline_aware)
        new = _written_text(before, start, stop, texts) if newline_aware else texts[1]
        ranges.append((start, stop, new))
    ranges.sort(key=lambda item: item[0])
    end, size = 0, len(before)
    for start, stop, new in ranges:
        if start < end:
            raise KernelError("patch_overlapping_edits", "编辑区间重叠")
        end = stop
        size += len(new) - (stop - start)
    if size > max_bytes:
        raise KernelError("patch_limit_exceeded", "Patch 完整后镜像超限")
    return ranges


def _spliced(
    before: bytes, ranges: list[tuple[int, int, bytes]], operation: ReadOperation
) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    for start, stop, new in ranges:
        _checkpoint(operation)
        chunks.extend((before[offset:start], new))
        offset = stop
    chunks.append(before[offset:])
    return b"".join(chunks)


def _edit_ranges(
    before: bytes,
    edits: tuple[ExactEdit, ...],
    operation: ReadOperation,
    max_bytes: int,
    recorded_sha256: str | None = None,
) -> list[tuple[int, int, bytes]]:
    """各条编辑的原始字节区间与写入文本，按起点排序。

    recorded_sha256是已保存计划的后像摘要，只在复核时传入：ADR 0141之前保存的计划
    按当时的语义算出后像，升级后的回滚、恢复与Diff仍须认得它们；当时的语义算不出该摘要时
    按当前语义计算，由调用方照常比较。
    """
    if recorded_sha256 is not None:
        try:
            ranges = _located_ranges(before, edits, operation, max_bytes, newline_aware=False)
        except KernelError:
            # 当时的语义下编辑不成立，或已超时：交给下面按当前语义重算，超时会在那里再次抛出。
            pass
        else:
            if hashlib.sha256(_spliced(before, ranges, operation)).hexdigest() == recorded_sha256:
                return ranges
    return _located_ranges(before, edits, operation, max_bytes, newline_aware=True)


def apply_exact_edits(
    before: bytes,
    edits: tuple[ExactEdit, ...],
    operation: ReadOperation,
    *,
    max_bytes: int | None = None,
    recorded_sha256: str | None = None,
) -> bytes:
    """把唯一锚定且不重叠的精确编辑应用到完整UTF-8前镜像；recorded_sha256见_edit_ranges。"""

    limit = MAX_PATCH_BYTES if max_bytes is None else max_bytes
    ranges = _edit_ranges(before, edits, operation, limit, recorded_sha256)
    after = _spliced(before, ranges, operation)
    if before == after:
        raise KernelError("patch_no_change", "Patch 未产生内容变化")
    return after


def _target(
    before: bytes,
    proposal: PatchProposal,
    operation: ReadOperation,
    recorded_sha256: str | None = None,
) -> bytes:
    """保留受管Patch内部验证接缝，并把实际编辑委托给通用精确编辑器。"""

    return apply_exact_edits(before, proposal.edits, operation, recorded_sha256=recorded_sha256)


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
        # 传入已记录的后像摘要：此前版本保存的计划按当时的编辑语义复核，见_edit_ranges。
        if expected != manifest or (
            _target(prepared.before, proposal, operation, manifest.after_sha256) != prepared.after
        ):
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
