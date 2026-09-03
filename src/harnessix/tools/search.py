from __future__ import annotations

import io
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from harnessix.tools.contracts import MAX_LINE_BYTES, ReadToolError
from harnessix.tools.files import _decode
from harnessix.tools.patterns import PathPattern
from harnessix.tools.search_contracts import (
    IGNORED_DIRECTORIES,
    MAX_MATCH_BYTES,
    MAX_SEARCH_DEPTH,
    MAX_SEARCH_ENTRIES,
    MAX_SEARCH_FILE_BYTES,
    MAX_SEARCH_NAMES_BYTES,
    MAX_SEARCH_RECORD_BYTES,
    MAX_SEARCH_TOTAL_BYTES,
    GlobInput,
    GlobOutput,
    GrepInput,
    GrepMatch,
    GrepOutput,
    SearchInput,
    SearchStats,
)
from harnessix.tools.workspace import ReadOperation, Workspace, digest, identity, revision_state


def execution_contract() -> dict[str, object]:
    return {
        "version": "bounded-search/v1",
        "pattern": "case-sensitive-segments-and-globstar",
        "grep": "case-sensitive-literal-lines",
        "ignored_directories": IGNORED_DIRECTORIES,
        "max_entries": MAX_SEARCH_ENTRIES,
        "max_names_bytes": MAX_SEARCH_NAMES_BYTES,
        "max_depth": MAX_SEARCH_DEPTH,
        "max_file_bytes": MAX_SEARCH_FILE_BYTES,
        "max_total_bytes": MAX_SEARCH_TOTAL_BYTES,
        "max_record_bytes": MAX_SEARCH_RECORD_BYTES,
        "max_match_bytes": MAX_MATCH_BYTES,
    }


@dataclass
class _Scan:
    entries_scanned: int = 0
    files_read: int = 0
    bytes_read: int = 0
    ignored_entries: int = 0
    unreadable_entries: int = 0
    oversized_files: int = 0
    invalid_utf8_files: int = 0
    binary_files: int = 0
    long_lines: int = 0
    names_bytes: int = 0

    def snapshot(self) -> SearchStats:
        return SearchStats.model_validate(
            {name: getattr(self, name) for name in SearchStats.model_fields}
        )


@dataclass(frozen=True)
class _Candidate:
    path: str
    info: os.stat_result


@contextmanager
def _open_candidate(
    workspace: Workspace, node: _Candidate, operation: ReadOperation, *, directory: bool
) -> Iterator[int]:
    try:
        with workspace.open(node.path, operation, directory=directory) as fd:
            if identity(os.fstat(fd)) != identity(node.info):
                raise ReadToolError("workspace_changed")
            yield fd
    except FileNotFoundError:
        raise ReadToolError("workspace_changed") from None
    except ReadToolError as error:
        if error.code in {"path_denied", "wrong_file_type"}:
            raise ReadToolError("workspace_changed") from None
        raise


def _collect(
    workspace: Workspace, args: SearchInput, operation: ReadOperation, scan: _Scan
) -> list[_Candidate]:
    candidates: list[_Candidate] = []

    def visit(path: str, depth: int, expected: _Candidate | None = None) -> None:
        operation.checkpoint()
        if depth > MAX_SEARCH_DEPTH:
            raise ReadToolError("limit_exceeded")
        context = (
            _open_candidate(workspace, expected, operation, directory=True)
            if expected
            else workspace.open(path, operation, directory=True)
        )
        directories = []
        with context as fd, os.scandir(fd) as entries:
            for entry in entries:
                operation.checkpoint()
                scan.entries_scanned += 1
                scan.names_bytes += len(os.fsencode(entry.name))
                if (
                    scan.entries_scanned > MAX_SEARCH_ENTRIES
                    or scan.names_bytes > MAX_SEARCH_NAMES_BYTES
                ):
                    raise ReadToolError("limit_exceeded")
                relative = entry.name if path == "." else f"{path}/{entry.name}"
                try:
                    workspace.parts(relative)
                except ReadToolError:
                    scan.ignored_entries += 1
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except PermissionError:
                    scan.unreadable_entries += 1
                    continue
                except FileNotFoundError:
                    raise ReadToolError("workspace_changed") from None
                node = _Candidate(relative, info)
                if stat.S_ISDIR(info.st_mode):
                    if not args.include_ignored and entry.name.casefold() in IGNORED_DIRECTORIES:
                        scan.ignored_entries += 1
                    else:
                        directories.append(node)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    candidates.append(node)
                else:
                    scan.ignored_entries += 1
        for node in sorted(directories, key=lambda node: node.path):
            try:
                visit(node.path, depth + 1, node)
            except PermissionError:
                scan.unreadable_entries += 1

    visit(args.path, 0)
    return sorted(candidates, key=lambda node: node.path)


class _Records:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.count = self.bytes = 0
        self.reason: Literal["result_limit", "output_limit"] | None = None

    def accept(self, encoded: str) -> bool:
        if self.count >= self.maximum:
            self.reason = "result_limit"
        elif self.bytes + len(encoded.encode("utf-8")) + 1 > MAX_SEARCH_RECORD_BYTES:
            self.reason = "output_limit"
        if self.reason is not None:
            return False
        self.count += 1
        self.bytes += len(encoded.encode("utf-8")) + 1
        return True


def _relative(path: str, root: str) -> str:
    return path if root == "." else path[len(root) + 1 :]


def glob(workspace: Workspace, args: GlobInput, operation: ReadOperation) -> GlobOutput:
    scan, pattern, records = _Scan(), PathPattern(args.pattern), _Records(args.max_results)
    paths = []
    for node in _collect(workspace, args, operation, scan):
        operation.checkpoint()
        if not pattern.matches(_relative(node.path, args.path)):
            continue
        try:
            with _open_candidate(workspace, node, operation, directory=False):
                pass
        except PermissionError:
            scan.unreadable_entries += 1
            continue
        if not records.accept(json.dumps(node.path, ensure_ascii=False)):
            break
        paths.append(node.path)
    stats = scan.snapshot()
    return GlobOutput(
        path=args.path,
        paths=tuple(paths),
        stats=stats,
        scan_complete=records.reason is None and not stats.has_gaps,
        truncated=records.reason is not None,
        truncation_reason=records.reason,
    )


def _read(fd: int, operation: ReadOperation, scan: _Scan) -> str | None:
    if os.fstat(fd).st_size > MAX_SEARCH_FILE_BYTES:
        scan.oversized_files += 1
        return None
    data = bytearray()
    while True:
        operation.checkpoint()
        remaining = min(MAX_SEARCH_FILE_BYTES - len(data), MAX_SEARCH_TOTAL_BYTES - scan.bytes_read)
        chunk = os.read(fd, min(65536, remaining + 1))
        scan.bytes_read += len(chunk)
        data.extend(chunk)
        if len(data) > MAX_SEARCH_FILE_BYTES or scan.bytes_read > MAX_SEARCH_TOTAL_BYTES:
            raise ReadToolError("limit_exceeded")
        if not chunk:
            break
    scan.files_read += 1
    try:
        return _decode(bytes(data))
    except ReadToolError as error:
        if error.code == "invalid_utf8":
            scan.invalid_utf8_files += 1
        elif error.code == "binary_file":
            scan.binary_files += 1
        else:
            raise
        return None


def _preview(text: str, index: int) -> tuple[str, bool]:
    start = max(0, index - 16)
    # text 已严格验证 UTF-8；这里只丢弃切片末尾不完整的编码单元，不修复非法源数据。
    snippet = text[start:].encode("utf-8")[:MAX_MATCH_BYTES].decode("utf-8", errors="ignore")
    return snippet, start > 0 or len(snippet) < len(text)


def grep(workspace: Workspace, args: GrepInput, operation: ReadOperation) -> GrepOutput:
    scan, pattern, records = _Scan(), PathPattern(args.include), _Records(args.max_results)
    matches: list[GrepMatch] = []
    for node in _collect(workspace, args, operation, scan):
        operation.checkpoint()
        if not pattern.matches(_relative(node.path, args.path)):
            continue
        try:
            with _open_candidate(workspace, node, operation, directory=False) as fd:
                revision = digest((workspace.scope, node.path, revision_state(os.fstat(fd))))
                text = _read(fd, operation, scan)
                if text is None:
                    continue
                with io.StringIO(text, newline="\n") as lines:
                    for number, line in enumerate(lines, start=1):
                        operation.checkpoint()
                        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                            scan.long_lines += 1
                            continue
                        if line.endswith("\n"):
                            line = line[:-1].removesuffix("\r")
                        index = line.find(args.query)
                        if index < 0:
                            continue
                        snippet, truncated = _preview(line, index)
                        hit = GrepMatch(
                            path=node.path,
                            line=number,
                            text=snippet,
                            text_truncated=truncated,
                            revision=revision,
                        )
                        if not records.accept(hit.model_dump_json()):
                            break
                        matches.append(hit)
        except PermissionError:
            scan.unreadable_entries += 1
        if records.reason is not None:
            break
    stats = scan.snapshot()
    return GrepOutput(
        path=args.path,
        matches=tuple(matches),
        stats=stats,
        scan_complete=records.reason is None and not stats.has_gaps,
        truncated=records.reason is not None,
        truncation_reason=records.reason,
    )
