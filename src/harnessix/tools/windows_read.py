"""Windows原生只读工具：把Handle观察端口适配为现有Tool合同。"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from harnessix.agent.errors import KernelError
from harnessix.tools import search
from harnessix.tools.contracts import (
    MAX_DIRECTORY_ENTRIES,
    MAX_LINE_BYTES,
    MAX_SCAN_BYTES,
    MAX_TEXT_BYTES,
    DirectoryEntry,
    ListFilesInput,
    ListFilesOutput,
    ReadContract,
    ReadErrorCode,
    ReadFileInput,
    ReadFileOutput,
    ReadToolError,
)
from harnessix.tools.files import _check_revision, _decode
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
from harnessix.tools.workspace import ReadOperation, WorkspaceReadPolicy, digest
from harnessix.workspace.windows import WindowsWorkspaceRoot, windows_port_source_digest


class _Observation(Protocol):
    kind: Literal["file", "directory", "missing"]
    identity: tuple[object, ...]
    content: bytes | None
    size: int
    entries: tuple[tuple[str, Literal["file", "directory", "symlink", "special"]], ...] | None


class _WindowsRoot(Protocol):
    path: Path
    root_identity: tuple[object, ...]

    def observe(
        self,
        path: str,
        *,
        access: Literal["read", "write", "execute"],
        include_content: bool,
        max_bytes: int,
        checkpoint: Callable[[], None] | None,
    ) -> _Observation: ...

    def close(self) -> None: ...


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
    identity: tuple[object, ...]
    size: int


class _Records:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.count = 0
        self.bytes = 0
        self.reason: Literal["result_limit", "output_limit"] | None = None

    def accept(self, encoded: str) -> bool:
        size = len(encoded.encode("utf-8")) + 1
        if self.count >= self.maximum:
            self.reason = "result_limit"
        elif self.bytes + size > MAX_SEARCH_RECORD_BYTES:
            self.reason = "output_limit"
        if self.reason is not None:
            return False
        self.count += 1
        self.bytes += size
        return True


class WindowsReadRuntime:
    """持有Windows Workspace根Handle并实现List/Read/Glob/Grep。"""

    def __init__(
        self,
        root: Path,
        *,
        denied_paths: tuple[str, ...] = (),
        root_factory: Callable[[Path], _WindowsRoot] = WindowsWorkspaceRoot,
    ) -> None:
        self._policy = WorkspaceReadPolicy(denied_paths)
        self._root = root_factory(root)
        self.root = self._root.path
        self.scope = digest(
            {
                "policy": "windows-workspace-read/v1",
                "root": str(self.root).casefold(),
                "identity": self._root.root_identity,
                **self._policy.scope_fields(),
                "windows_port_source_digest": windows_port_source_digest(),
            }
        )

    def _path(self, path: str) -> str:
        parts = self._policy.parts(path)
        return "/".join(parts) if parts else "."

    @staticmethod
    def _same(first: _Observation, second: _Observation) -> bool:
        return (
            first.kind == second.kind
            and first.identity == second.identity
            and first.content == second.content
            and first.size == second.size
            and first.entries == second.entries
        )

    def _observe(
        self,
        path: str,
        operation: ReadOperation,
        *,
        include_content: bool = True,
        max_bytes: int = MAX_SCAN_BYTES,
    ) -> _Observation:
        operation.checkpoint()
        try:
            return self._root.observe(
                path,
                access="read",
                include_content=include_content,
                max_bytes=max_bytes,
                checkpoint=operation.checkpoint,
            )
        except KernelError as error:
            code = {
                "workspace_path_denied": "path_denied",
                "workspace_wrong_file_type": "wrong_file_type",
                "workspace_parent_missing": "not_found",
                "workspace_snapshot_limit": "limit_exceeded",
                "workspace_changed": "workspace_changed",
            }.get(error.code, "io_failed")
            raise ReadToolError(cast(ReadErrorCode, code)) from None

    @staticmethod
    def _require_kind(observed: _Observation, expected: Literal["file", "directory"]) -> None:
        if observed.kind == "missing":
            raise ReadToolError("not_found")
        if observed.kind != expected:
            raise ReadToolError("wrong_file_type")

    def inspect(self, operation: ReadOperation) -> str:
        observed = self._observe(".", operation, include_content=False)
        self._require_kind(observed, "directory")
        return self.scope

    def execute(
        self,
        args: ListFilesInput | ReadFileInput | GlobInput | GrepInput,
        operation: ReadOperation,
        *,
        capture: search.SearchCapture | None = None,
    ) -> ReadContract:
        if isinstance(args, ListFilesInput):
            return self.list_files(args, operation)
        if isinstance(args, GlobInput):
            return self.glob(args, operation, capture=capture)
        if isinstance(args, GrepInput):
            return self.grep(args, operation, capture=capture)
        return self.read_file(args, operation)

    def list_files(
        self,
        args: ListFilesInput,
        operation: ReadOperation,
    ) -> ListFilesOutput:
        path = self._path(args.path)
        observed = self._observe(path, operation, include_content=False)
        self._require_kind(observed, "directory")
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
                self._policy.parts(relative)
            except ReadToolError:
                continue
            entries.append(DirectoryEntry(name=name, kind=kind))
        entries.sort(key=lambda entry: entry.name)
        revision = digest(
            (
                self.scope,
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
        after = self._observe(path, operation, include_content=False)
        if not self._same(observed, after):
            raise ReadToolError("workspace_changed")
        return ListFilesOutput(
            path=args.path,
            entries=page,
            revision=revision,
            truncated=truncated,
            next_offset=next_offset if truncated else None,
        )

    def _file_revision(self, path: str, observed: _Observation) -> str:
        if observed.content is None:
            raise ReadToolError("io_failed")
        return digest(
            (
                self.scope,
                path,
                observed.identity,
                observed.size,
                hashlib.sha256(observed.content).hexdigest(),
            )
        )

    def read_file(
        self,
        args: ReadFileInput,
        operation: ReadOperation,
    ) -> ReadFileOutput:
        path = self._path(args.path)
        observed = self._observe(path, operation, max_bytes=MAX_SCAN_BYTES)
        self._require_kind(observed, "file")
        if observed.content is None:
            raise ReadToolError("io_failed")
        revision = self._file_revision(path, observed)
        _check_revision(args.expected_revision, revision)

        lines: list[str] = []
        scanned = returned = line_number = 0
        reason: Literal["line_limit", "byte_limit"] | None = None
        with io.BytesIO(observed.content) as stream:
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

    @staticmethod
    def _relative(path: str, root: str) -> str:
        return path if root == "." else path[len(root) + 1 :]

    def _collect(
        self,
        args: SearchInput,
        operation: ReadOperation,
        scan: _Scan,
    ) -> list[_Candidate]:
        candidates: list[_Candidate] = []

        def visit(path: str, depth: int) -> None:
            operation.checkpoint()
            if depth > MAX_SEARCH_DEPTH:
                raise ReadToolError("limit_exceeded")
            before = self._observe(path, operation, include_content=False)
            self._require_kind(before, "directory")
            if before.entries is None:
                raise ReadToolError("io_failed")
            directories: list[str] = []
            for name, kind in before.entries:
                operation.checkpoint()
                scan.entries_scanned += 1
                scan.names_bytes += len(name.encode("utf-8"))
                if (
                    scan.entries_scanned > MAX_SEARCH_ENTRIES
                    or scan.names_bytes > MAX_SEARCH_NAMES_BYTES
                ):
                    raise ReadToolError("limit_exceeded")
                relative = name if path == "." else f"{path}/{name}"
                try:
                    self._policy.parts(relative)
                except ReadToolError:
                    scan.ignored_entries += 1
                    continue
                if kind == "directory":
                    if not args.include_ignored and name.casefold() in IGNORED_DIRECTORIES:
                        scan.ignored_entries += 1
                    else:
                        directories.append(relative)
                elif kind == "file":
                    try:
                        observed = self._observe_candidate(relative, operation, content=False)
                    except ReadToolError as error:
                        if error.code == "io_failed":
                            scan.unreadable_entries += 1
                            continue
                        raise
                    candidates.append(
                        _Candidate(
                            path=relative,
                            identity=observed.identity,
                            size=observed.size,
                        )
                    )
                else:
                    scan.ignored_entries += 1
            for directory in sorted(directories):
                try:
                    visit(directory, depth + 1)
                except ReadToolError as error:
                    if error.code == "io_failed":
                        scan.unreadable_entries += 1
                    else:
                        raise
            after = self._observe(path, operation, include_content=False)
            if not self._same(before, after):
                raise ReadToolError("workspace_changed")

        visit(self._path(args.path), 0)
        return sorted(candidates, key=lambda candidate: candidate.path)

    def _observe_candidate(
        self,
        path: str,
        operation: ReadOperation,
        *,
        content: bool,
    ) -> _Observation:
        try:
            observed = self._observe(
                path,
                operation,
                include_content=content,
                max_bytes=MAX_SEARCH_FILE_BYTES,
            )
            self._require_kind(observed, "file")
            return observed
        except ReadToolError as error:
            if error.code in {"not_found", "path_denied", "wrong_file_type"}:
                raise ReadToolError("workspace_changed") from None
            raise

    def glob(
        self,
        args: GlobInput,
        operation: ReadOperation,
        *,
        capture: search.SearchCapture | None,
    ) -> GlobOutput:
        scan = _Scan()
        pattern = PathPattern(args.pattern)
        records = _Records(args.max_results)
        paths: list[str] = []
        root = self._path(args.path)
        for candidate in self._collect(args, operation, scan):
            operation.checkpoint()
            path = candidate.path
            if not pattern.matches(self._relative(path, root)):
                continue
            try:
                observed = self._observe_candidate(path, operation, content=False)
                if observed.identity != candidate.identity or observed.size != candidate.size:
                    raise ReadToolError("workspace_changed")
            except ReadToolError as error:
                if error.code == "io_failed":
                    scan.unreadable_entries += 1
                    continue
                raise
            encoded = json.dumps(path, ensure_ascii=False)
            if capture is not None:
                capture.append(encoded)
            if records.accept(encoded):
                paths.append(path)
            elif capture is None:
                break
        stats = scan.snapshot()
        if capture is not None:
            capture.complete = not stats.has_gaps
        return GlobOutput(
            path=args.path,
            paths=tuple(paths),
            stats=stats,
            scan_complete=records.reason is None and not stats.has_gaps,
            truncated=records.reason is not None,
            truncation_reason=records.reason,
        )

    @staticmethod
    def _preview(text: str, index: int) -> tuple[str, bool]:
        start = max(0, index - 16)
        snippet = text[start:].encode("utf-8")[:MAX_MATCH_BYTES].decode("utf-8", errors="ignore")
        return snippet, start > 0 or len(snippet) < len(text)

    @staticmethod
    def _decode_search(content: bytes, scan: _Scan) -> str | None:
        try:
            return _decode(content)
        except ReadToolError as error:
            if error.code == "invalid_utf8":
                scan.invalid_utf8_files += 1
            elif error.code == "binary_file":
                scan.binary_files += 1
            else:
                raise
            return None

    def grep(
        self,
        args: GrepInput,
        operation: ReadOperation,
        *,
        capture: search.SearchCapture | None,
    ) -> GrepOutput:
        scan = _Scan()
        pattern = PathPattern(args.include)
        records = _Records(args.max_results)
        matches: list[GrepMatch] = []
        root = self._path(args.path)
        for candidate in self._collect(args, operation, scan):
            operation.checkpoint()
            path = candidate.path
            if not pattern.matches(self._relative(path, root)):
                continue
            try:
                metadata = self._observe_candidate(path, operation, content=False)
                if metadata.identity != candidate.identity or metadata.size != candidate.size:
                    raise ReadToolError("workspace_changed")
                if metadata.size > MAX_SEARCH_FILE_BYTES:
                    scan.oversized_files += 1
                    continue
                if scan.bytes_read + metadata.size > MAX_SEARCH_TOTAL_BYTES:
                    raise ReadToolError("limit_exceeded")
                observed = self._observe_candidate(path, operation, content=True)
                if metadata.identity != observed.identity or metadata.size != observed.size:
                    raise ReadToolError("workspace_changed")
            except ReadToolError as error:
                if error.code == "io_failed":
                    scan.unreadable_entries += 1
                    continue
                raise
            if observed.content is None:
                raise ReadToolError("io_failed")
            scan.files_read += 1
            scan.bytes_read += len(observed.content)
            text = self._decode_search(observed.content, scan)
            if text is None:
                continue
            revision = self._file_revision(path, observed)
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
                    snippet, truncated = self._preview(line, index)
                    hit = GrepMatch(
                        path=path,
                        line=number,
                        text=snippet,
                        text_truncated=truncated,
                        revision=revision,
                    )
                    encoded = hit.model_dump_json()
                    if capture is not None:
                        capture.append(encoded)
                    if records.accept(encoded):
                        matches.append(hit)
                    elif capture is None:
                        break
            if records.reason is not None and capture is None:
                break
        stats = scan.snapshot()
        if capture is not None:
            capture.complete = not stats.has_gaps
        return GrepOutput(
            path=args.path,
            matches=tuple(matches),
            stats=stats,
            scan_complete=records.reason is None and not stats.has_gaps,
            truncated=records.reason is not None,
            truncation_reason=records.reason,
        )

    def close(self) -> None:
        self._root.close()
