"""Windows原生List/Read工具实现，复用公共分页与revision合同。"""

from __future__ import annotations

import io
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
from harnessix.tools.files import _check_revision, _decode
from harnessix.tools.windows_read_port import (
    WindowsReadPort,
    file_revision,
    require_kind,
    same_observation,
)
from harnessix.tools.workspace import ReadOperation, digest


def list_files(
    port: WindowsReadPort,
    args: ListFilesInput,
    operation: ReadOperation,
) -> ListFilesOutput:
    """列出一个稳定目录快照，并在返回前复查目录身份。"""

    path = port.path(args.path)
    observed = port.observe(path, operation, include_content=False)
    require_kind(observed, "directory")
    if observed.entries is None:
        raise ReadToolError("io_failed")
    entries: list[DirectoryEntry] = []
    scanned_bytes = 0
    for count, (name, kind) in enumerate(observed.entries, start=1):
        operation.checkpoint()
        scanned_bytes += len(name.encode("utf-8"))
        if count > MAX_DIRECTORY_ENTRIES or scanned_bytes > MAX_SCAN_BYTES:
            raise ReadToolError("limit_exceeded")
        relative = name if path == "." else f"{path}/{name}"
        try:
            port.policy.parts(relative)
        except ReadToolError:
            continue
        entries.append(DirectoryEntry(name=name, kind=kind))
    entries.sort(key=lambda entry: entry.name)
    revision = digest(
        (
            port.scope,
            path,
            observed.identity,
            tuple((entry.name, entry.kind) for entry in entries),
        )
    )
    _check_revision(args.expected_revision, revision)
    if args.offset > len(entries):
        raise ReadToolError("offset_out_of_range")
    page = tuple(entries[args.offset : args.offset + args.limit])
    next_offset = args.offset + len(page)
    truncated = next_offset < len(entries)
    after = port.observe(path, operation, include_content=False)
    if not same_observation(observed, after):
        raise ReadToolError("workspace_changed")
    return ListFilesOutput(
        path=args.path,
        entries=page,
        revision=revision,
        truncated=truncated,
        next_offset=next_offset if truncated else None,
    )


def _read_lines(
    content: bytes,
    args: ReadFileInput,
    operation: ReadOperation,
) -> tuple[list[str], int, int, Literal["line_limit", "byte_limit"] | None]:
    lines: list[str] = []
    scanned = returned = line_number = 0
    reason: Literal["line_limit", "byte_limit"] | None = None
    with io.BytesIO(content) as stream:
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
    return lines, returned, line_number, reason


def read_file(
    port: WindowsReadPort,
    args: ReadFileInput,
    operation: ReadOperation,
) -> ReadFileOutput:
    """读取UTF-8文本页并绑定文件内容revision。"""

    path = port.path(args.path)
    observed = port.observe(path, operation, max_bytes=MAX_SCAN_BYTES)
    require_kind(observed, "file")
    if observed.content is None:
        raise ReadToolError("io_failed")
    revision = file_revision(port, path, observed)
    _check_revision(args.expected_revision, revision)
    lines, returned, line_number, reason = _read_lines(observed.content, args, operation)
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
