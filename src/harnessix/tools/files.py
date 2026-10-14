"""内置Coding Tool：执行有界、无链接跟随的Workspace文件读取。"""

from __future__ import annotations

import hashlib
import io
import os
import stat
from typing import BinaryIO, Literal

from harnessix.tools.contracts import (
    MAX_DIRECTORY_ENTRIES,
    MAX_LINE_BYTES,
    MAX_SCAN_BYTES,
    MAX_TEXT_BYTES,
    DirectoryEntry,
    ListFilesInput,
    ListFilesOutput,
    ReadFileInput,
    ReadFileInputV2,
    ReadFileInputV3,
    ReadFileInputV4,
    ReadFileOutput,
    ReadFileSnapshotOutput,
    ReadFileViewOutput,
    ReadToolError,
)
from harnessix.tools.definition_view import definition_page
from harnessix.tools.workspace import ReadOperation, Workspace, digest, revision_state


def _check_revision(expected: str | None, actual: str) -> None:
    if expected is not None and expected != actual:
        raise ReadToolError("page_changed")


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise ReadToolError("invalid_utf8") from None
    if any((ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in text):
        raise ReadToolError("binary_file")
    return text


def list_files(
    workspace: Workspace,
    args: ListFilesInput,
    operation: ReadOperation,
) -> ListFilesOutput:
    with workspace.open(args.path, operation, directory=True) as fd:
        entries: list[DirectoryEntry] = []
        observed: list[tuple[str, tuple[int, ...]]] = []
        scanned_bytes = 0
        with os.scandir(fd) as iterator:
            for count, entry in enumerate(iterator, start=1):
                operation.checkpoint()
                scanned_bytes += len(os.fsencode(entry.name))
                if count > MAX_DIRECTORY_ENTRIES or scanned_bytes > MAX_SCAN_BYTES:
                    raise ReadToolError("limit_exceeded")
                relative = entry.name if args.path == "." else f"{args.path}/{entry.name}"
                try:
                    workspace.parts(relative)
                except ReadToolError:
                    continue
                info = entry.stat(follow_symlinks=False)
                kind: Literal["file", "directory", "symlink", "special"] = (
                    "directory"
                    if stat.S_ISDIR(info.st_mode)
                    else "file"
                    if stat.S_ISREG(info.st_mode)
                    else "symlink"
                    if stat.S_ISLNK(info.st_mode)
                    else "special"
                )
                entries.append(DirectoryEntry(name=entry.name, kind=kind))
                # 目录页不绑定子文件内容，但替换同名对象或改变类型会使游标失效。
                observed.append((entry.name, (info.st_dev, info.st_ino, info.st_mode)))
        revision = digest(
            (workspace.scope, args.path, revision_state(os.fstat(fd)), sorted(observed))
        )
        _check_revision(args.expected_revision, revision)
        entries.sort(key=lambda entry: entry.name)
        if args.offset > len(entries):
            raise ReadToolError("offset_out_of_range")
        page = tuple(entries[args.offset : args.offset + args.limit])
        next_offset = args.offset + len(page)
        truncated = next_offset < len(entries)
        result = ListFilesOutput(
            path=args.path,
            entries=page,
            revision=revision,
            truncated=truncated,
            next_offset=next_offset if truncated else None,
        )
    return result


def read_file(
    workspace: Workspace,
    args: ReadFileInput,
    operation: ReadOperation,
) -> ReadFileOutput:
    with workspace.open(args.path, operation, directory=False) as fd:
        revision = digest((workspace.scope, args.path, revision_state(os.fstat(fd))))
        _check_revision(args.expected_revision, revision)
        args, anchor_line = _resolve_fd_anchor(fd, args, operation)
        result = _read_file_page(fd, args, revision, operation)
        _require_visible_anchor(result, anchor_line)
    return result


def _resolve_read_anchor(
    stream: BinaryIO, args: ReadFileInput, operation: ReadOperation
) -> tuple[ReadFileInput, int | None]:
    """完整有界扫描才能证明唯一；重复、非法尾部和取消都不返回部分成功。"""
    if not isinstance(args, ReadFileInputV3) or args.anchor is None:
        return args, None
    found = None
    scanned = number = 0
    while True:
        operation.checkpoint()
        raw = stream.readline(min(MAX_LINE_BYTES + 1, MAX_SCAN_BYTES - scanned + 1))
        scanned += len(raw)
        if scanned > MAX_SCAN_BYTES or len(raw) > MAX_LINE_BYTES:
            raise ReadToolError("limit_exceeded")
        if not raw:
            break
        number += 1
        text = _decode(raw)
        offset = text.find(args.anchor)
        if offset >= 0:
            if found is not None or text.find(args.anchor, offset + 1) >= 0:
                raise ReadToolError("anchor_ambiguous")
            found = number
    if found is None:
        raise ReadToolError("anchor_not_found")
    return args.model_copy(
        update={"start_line": max(1, found - args.lines_before), "anchor": None, "lines_before": 0}
    ), found


def _resolve_fd_anchor(
    fd: int, args: ReadFileInput, operation: ReadOperation
) -> tuple[ReadFileInput, int | None]:
    if not isinstance(args, ReadFileInputV3) or args.anchor is None:
        return args, None
    with os.fdopen(fd, "rb", buffering=8192, closefd=False) as stream:
        resolved = _resolve_read_anchor(stream, args, operation)
    os.lseek(fd, 0, os.SEEK_SET)
    return resolved


def _require_visible_anchor(page: ReadFileOutput, anchor_line: int | None) -> None:
    if anchor_line is not None and not (
        page.end_line is not None and page.start_line <= anchor_line <= page.end_line
    ):
        raise ReadToolError("anchor_outside_page")


def _read_file_page(
    fd: int, args: ReadFileInput, revision: str, operation: ReadOperation
) -> ReadFileOutput:
    """复用旧文本分页规则；FD仍由外层Workspace观察统一拥有和复核。"""

    lines: list[str] = []
    text_limit = args.max_utf8_bytes if isinstance(args, ReadFileInputV2) else MAX_TEXT_BYTES
    scanned = returned = line_number = 0
    reason: Literal["line_limit", "byte_limit"] | None = None
    # 有界预读最多额外8 KiB；closefd=False，不转移FD所有权。
    with os.fdopen(fd, "rb", buffering=8192, closefd=False) as stream:
        while True:
            operation.checkpoint()
            raw = stream.readline(min(MAX_LINE_BYTES + 1, MAX_SCAN_BYTES - scanned + 1))
            scanned += len(raw)
            if scanned > MAX_SCAN_BYTES:
                raise ReadToolError("limit_exceeded")
            if not raw:
                break
            line_number += 1
            if line_number >= args.start_line and len(lines) >= args.max_lines:
                reason = "line_limit"
                break
            if len(raw) > MAX_LINE_BYTES:
                raise ReadToolError("limit_exceeded")
            text = _decode(raw)
            if line_number < args.start_line:
                continue
            if returned + len(raw) > text_limit:
                reason = "byte_limit"
                break
            returned += len(raw)
            lines.append(text)
    if not lines and (args.start_line > 1 or line_number > 0):
        raise ReadToolError("offset_out_of_range")
    return ReadFileOutput(
        path=args.path,
        text="".join(lines),
        start_line=args.start_line,
        end_line=args.start_line + len(lines) - 1 if lines else None,
        utf8_bytes=returned,
        revision=revision,
        truncated=reason is not None,
        truncation_reason=reason,
        next_line=args.start_line + len(lines) if reason else None,
    )


def _complete_content_digest(fd: int, file_bytes: int, operation: ReadOperation) -> str:
    """计算有界完整原始摘要；文件增长或缩小不能成为部分成功结果。"""

    hasher = hashlib.sha256()
    scanned = 0
    while True:
        operation.checkpoint()
        chunk = os.read(fd, min(65536, file_bytes - scanned + 1))
        scanned += len(chunk)
        if scanned > file_bytes or (not chunk and scanned != file_bytes):
            raise ReadToolError("workspace_changed")
        if not chunk:
            return hasher.hexdigest()
        hasher.update(chunk)


def read_file_snapshot(
    workspace: Workspace,
    args: ReadFileInput,
    operation: ReadOperation,
) -> ReadFileSnapshotOutput:
    """在同一FD观察中取得分页与完整前镜像摘要，不改变旧读取API。"""

    if isinstance(args, ReadFileInputV4) and args.view == "definition":
        return _read_definition_snapshot(workspace, args, operation)
    with workspace.open(args.path, operation, directory=False) as fd:
        observed = os.fstat(fd)
        revision = digest((workspace.scope, args.path, revision_state(observed)))
        _check_revision(args.expected_revision, revision)
        content_sha256 = None
        if observed.st_size <= MAX_SCAN_BYTES:
            content_sha256 = _complete_content_digest(fd, observed.st_size, operation)
            os.lseek(fd, 0, os.SEEK_SET)
        args, anchor_line = _resolve_fd_anchor(fd, args, operation)
        page = _read_file_page(fd, args, revision, operation)
        _require_visible_anchor(page, anchor_line)
        if revision_state(observed) != revision_state(os.fstat(fd)):
            raise ReadToolError("workspace_changed")
        result = ReadFileSnapshotOutput(
            **page.model_dump(),
            file_bytes=observed.st_size,
            content_sha256=content_sha256,
            digest_status="complete" if content_sha256 is not None else "omitted_limit",
        )
    return result


def _capture_definition_content(
    fd: int, observed: os.stat_result, operation: ReadOperation
) -> bytes:
    """同一Workspace FD只捕获一次完整原始字节，投影与摘要不再重读路径。"""

    if observed.st_size > MAX_SCAN_BYTES:
        raise ReadToolError("limit_exceeded")
    chunks: list[bytes] = []
    scanned = 0
    while True:
        operation.checkpoint()
        chunk = os.read(fd, min(65536, observed.st_size - scanned + 1))
        scanned += len(chunk)
        if scanned > observed.st_size or (not chunk and scanned != observed.st_size):
            raise ReadToolError("workspace_changed")
        if not chunk:
            operation.checkpoint()
            if revision_state(observed) != revision_state(os.fstat(fd)):
                raise ReadToolError("workspace_changed")
            return b"".join(chunks)
        chunks.append(chunk)


def _read_definition_snapshot(
    workspace: Workspace, args: ReadFileInputV4, operation: ReadOperation
) -> ReadFileViewOutput:
    with workspace.open(args.path, operation, directory=False) as fd:
        observed = os.fstat(fd)
        revision = digest((workspace.scope, args.path, revision_state(observed)))
        _check_revision(args.expected_revision, revision)
        if not args.path.endswith((".py", ".pyi")):
            raise ReadToolError("definition_unavailable")
        content = _capture_definition_content(fd, observed, operation)
        with io.BytesIO(content) as stream:
            _, anchor_line = _resolve_read_anchor(stream, args, operation)
        if anchor_line is None:
            raise ReadToolError("definition_unavailable")
        result = definition_page(content, args, anchor_line, revision, operation, _decode)
    return result
