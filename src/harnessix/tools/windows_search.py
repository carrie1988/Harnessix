"""Windows原生Glob/Grep：有界遍历、稳定复查与可选Artifact捕获。"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Literal

from harnessix.tools import search
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
from harnessix.tools.windows_read_port import (
    Observation,
    WindowsReadPort,
    file_revision,
    relative_path,
    require_kind,
    same_observation,
)
from harnessix.tools.workspace import ReadOperation


@dataclass(slots=True)
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


@dataclass(frozen=True, slots=True)
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


def _candidate(
    port: WindowsReadPort,
    path: str,
    operation: ReadOperation,
    scan: _Scan,
) -> _Candidate | None:
    try:
        observed = port.observe_candidate(
            path,
            operation,
            content=False,
            max_bytes=MAX_SEARCH_FILE_BYTES,
        )
    except ReadToolError as error:
        if error.code != "io_failed":
            raise
        scan.unreadable_entries += 1
        return None
    return _Candidate(path=path, identity=observed.identity, size=observed.size)


def _collect(
    port: WindowsReadPort,
    args: SearchInput,
    operation: ReadOperation,
    scan: _Scan,
) -> list[_Candidate]:
    candidates: list[_Candidate] = []

    def visit(path: str, depth: int) -> None:
        operation.checkpoint()
        if depth > MAX_SEARCH_DEPTH:
            raise ReadToolError("limit_exceeded")
        before = port.observe(path, operation, include_content=False)
        require_kind(before, "directory")
        if before.entries is None:
            raise ReadToolError("io_failed")
        directories: list[str] = []
        for name, kind in before.entries:
            relative = _scan_entry(port, args, path, name, kind, operation, scan)
            if relative is None:
                continue
            if kind == "directory":
                directories.append(relative)
            elif kind == "file":
                found = _candidate(port, relative, operation, scan)
                if found is not None:
                    candidates.append(found)
        for directory in sorted(directories):
            try:
                visit(directory, depth + 1)
            except ReadToolError as error:
                if error.code != "io_failed":
                    raise
                scan.unreadable_entries += 1
        after = port.observe(path, operation, include_content=False)
        if not same_observation(before, after):
            raise ReadToolError("workspace_changed")

    visit(port.path(args.path), 0)
    return sorted(candidates, key=lambda item: item.path)


def _scan_entry(
    port: WindowsReadPort,
    args: SearchInput,
    parent: str,
    name: str,
    kind: str,
    operation: ReadOperation,
    scan: _Scan,
) -> str | None:
    operation.checkpoint()
    scan.entries_scanned += 1
    scan.names_bytes += len(name.encode("utf-8"))
    if scan.entries_scanned > MAX_SEARCH_ENTRIES or scan.names_bytes > MAX_SEARCH_NAMES_BYTES:
        raise ReadToolError("limit_exceeded")
    relative = name if parent == "." else f"{parent}/{name}"
    try:
        port.policy.parts(relative)
    except ReadToolError:
        scan.ignored_entries += 1
        return None
    if kind == "directory" and not args.include_ignored and name.casefold() in IGNORED_DIRECTORIES:
        scan.ignored_entries += 1
        return None
    if kind not in {"directory", "file"}:
        scan.ignored_entries += 1
        return None
    return relative


def _stable_metadata(
    port: WindowsReadPort,
    candidate: _Candidate,
    operation: ReadOperation,
) -> Observation:
    observed = port.observe_candidate(
        candidate.path,
        operation,
        content=False,
        max_bytes=MAX_SEARCH_FILE_BYTES,
    )
    if observed.identity != candidate.identity or observed.size != candidate.size:
        raise ReadToolError("workspace_changed")
    return observed


def _capture_status(
    capture: search.SearchCapture | None,
    scan: _Scan,
    records: _Records,
) -> tuple[SearchStats, bool]:
    stats = scan.snapshot()
    if capture is not None:
        capture.complete = not stats.has_gaps
    return stats, records.reason is None and not stats.has_gaps


def glob_files(
    port: WindowsReadPort,
    args: GlobInput,
    operation: ReadOperation,
    *,
    capture: search.SearchCapture | None,
) -> GlobOutput:
    """返回模式匹配路径，并在加入结果前复查文件身份。"""

    scan, pattern, records = _Scan(), PathPattern(args.pattern), _Records(args.max_results)
    paths: list[str] = []
    root = port.path(args.path)
    for candidate in _collect(port, args, operation, scan):
        operation.checkpoint()
        if not pattern.matches(relative_path(candidate.path, root)):
            continue
        try:
            _stable_metadata(port, candidate, operation)
        except ReadToolError as error:
            if error.code != "io_failed":
                raise
            scan.unreadable_entries += 1
            continue
        encoded = json.dumps(candidate.path, ensure_ascii=False)
        if capture is not None:
            capture.append(encoded)
        if records.accept(encoded):
            paths.append(candidate.path)
        elif capture is None:
            break
    stats, complete = _capture_status(capture, scan, records)
    return GlobOutput(
        path=args.path,
        paths=tuple(paths),
        stats=stats,
        scan_complete=complete,
        truncated=records.reason is not None,
        truncation_reason=records.reason,
    )


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


def _load_text(
    port: WindowsReadPort,
    candidate: _Candidate,
    operation: ReadOperation,
    scan: _Scan,
) -> tuple[Observation, str] | None:
    try:
        metadata = _stable_metadata(port, candidate, operation)
        if metadata.size > MAX_SEARCH_FILE_BYTES:
            scan.oversized_files += 1
            return None
        if scan.bytes_read + metadata.size > MAX_SEARCH_TOTAL_BYTES:
            raise ReadToolError("limit_exceeded")
        observed = port.observe_candidate(
            candidate.path,
            operation,
            content=True,
            max_bytes=MAX_SEARCH_FILE_BYTES,
        )
        if metadata.identity != observed.identity or metadata.size != observed.size:
            raise ReadToolError("workspace_changed")
    except ReadToolError as error:
        if error.code != "io_failed":
            raise
        scan.unreadable_entries += 1
        return None
    if observed.content is None:
        raise ReadToolError("io_failed")
    scan.files_read += 1
    scan.bytes_read += len(observed.content)
    text = _decode_search(observed.content, scan)
    return None if text is None else (observed, text)


def _preview(text: str, index: int) -> tuple[str, bool]:
    start = max(0, index - 16)
    snippet = text[start:].encode("utf-8")[:MAX_MATCH_BYTES].decode("utf-8", errors="ignore")
    return snippet, start > 0 or len(snippet) < len(text)


def _append_matches(
    port: WindowsReadPort,
    candidate: _Candidate,
    observed: Observation,
    text: str,
    args: GrepInput,
    operation: ReadOperation,
    scan: _Scan,
    records: _Records,
    matches: list[GrepMatch],
    capture: search.SearchCapture | None,
) -> None:
    revision = file_revision(port, candidate.path, observed)
    with io.StringIO(text, newline="\n") as lines:
        for number, line in enumerate(lines, start=1):
            operation.checkpoint()
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                scan.long_lines += 1
                continue
            line = line[:-1].removesuffix("\r") if line.endswith("\n") else line
            index = line.find(args.query)
            if index < 0:
                continue
            snippet, truncated = _preview(line, index)
            hit = GrepMatch(
                path=candidate.path,
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


def grep_files(
    port: WindowsReadPort,
    args: GrepInput,
    operation: ReadOperation,
    *,
    capture: search.SearchCapture | None,
) -> GrepOutput:
    """按字面量查找文本行，逐文件执行元数据—内容双重观察。"""

    scan, pattern, records = _Scan(), PathPattern(args.include), _Records(args.max_results)
    matches: list[GrepMatch] = []
    root = port.path(args.path)
    for candidate in _collect(port, args, operation, scan):
        operation.checkpoint()
        if not pattern.matches(relative_path(candidate.path, root)):
            continue
        loaded = _load_text(port, candidate, operation, scan)
        if loaded is not None:
            observed, text = loaded
            _append_matches(
                port, candidate, observed, text, args, operation, scan, records, matches, capture
            )
        if records.reason is not None and capture is None:
            break
    stats, complete = _capture_status(capture, scan, records)
    return GrepOutput(
        path=args.path,
        matches=tuple(matches),
        stats=stats,
        scan_complete=complete,
        truncated=records.reason is not None,
        truncation_reason=records.reason,
    )
