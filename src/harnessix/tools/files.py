from __future__ import annotations

import os
import stat
from typing import Literal

from harnessix.tools.contracts import (
    MAX_DIRECTORY_ENTRIES,
    MAX_LINE_BYTES,
    MAX_SCAN_BYTES,
    MAX_TEXT_BYTES,
    DirectoryEntry,
    ListFilesInput,
    ListFilesOutput,
    ReadFileInput,
    ReadFileOutput,
    ReadToolError,
)
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
        lines: list[str] = []
        scanned = returned = line_number = 0
        reason: Literal["line_limit", "byte_limit"] | None = None
        # 有界预读最多额外 8 KiB；closefd=False，FD 由 Workspace 统一回收。
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
                if returned + len(raw) > MAX_TEXT_BYTES:
                    reason = "byte_limit"
                    break
                returned += len(raw)
                lines.append(text)
        if not lines and (args.start_line > 1 or line_number > 0):
            raise ReadToolError("offset_out_of_range")
        result = ReadFileOutput(
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
    return result
