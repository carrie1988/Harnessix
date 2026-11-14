"""Windows原生Glob/Grep：有界遍历、稳定复查与可选Artifact捕获。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from harnessix.tools import search
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.files import _decode
from harnessix.tools.grep_matcher import LineMatcher, build_line_matcher
from harnessix.tools.patterns import PathPattern, grep_include
from harnessix.tools.search_contracts import (
    IGNORED_DIRECTORIES,
    MAX_SEARCH_DEPTH,
    MAX_SEARCH_ENTRIES,
    MAX_SEARCH_FILE_BYTES,
    MAX_SEARCH_NAMES_BYTES,
    MAX_SEARCH_RECORD_BYTES,
    MAX_SEARCH_TOTAL_BYTES,
    MAX_SNAPSHOT_SEARCH_ENTRIES,
    MAX_SNAPSHOT_SEARCH_NAMES_BYTES,
    MAX_SNAPSHOT_SEARCH_RECORD_BYTES,
    MAX_SNAPSHOT_SEARCH_TOTAL_BYTES,
    GlobInput,
    GlobOutput,
    GrepInput,
    GrepMatch,
    GrepOutput,
    GrepSnapshotInput,
    SearchInput,
    SearchStats,
    SnapshotSearchStats,
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
    max_entries: int = 0
    max_names_bytes: int = 0
    max_total_bytes: int = 0
    stats_type: type[SearchStats] = SearchStats

    def snapshot(self) -> SearchStats:
        return self.stats_type.model_validate(
            {name: getattr(self, name) for name in SearchStats.model_fields}
        )


def _scan_for(args: SearchInput) -> _Scan:
    """快照与正则搜索用整仓库上限，其余沿用原合同上限；运行时读取模块常量。"""
    if isinstance(args, GrepSnapshotInput):
        return _Scan(
            max_entries=MAX_SNAPSHOT_SEARCH_ENTRIES,
            max_names_bytes=MAX_SNAPSHOT_SEARCH_NAMES_BYTES,
            max_total_bytes=MAX_SNAPSHOT_SEARCH_TOTAL_BYTES,
            stats_type=SnapshotSearchStats,
        )
    return _Scan(
        max_entries=MAX_SEARCH_ENTRIES,
        max_names_bytes=MAX_SEARCH_NAMES_BYTES,
        max_total_bytes=MAX_SEARCH_TOTAL_BYTES,
    )


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: str
    identity: tuple[object, ...]
    size: int


class _Records:
    def __init__(self, args: SearchInput) -> None:
        self.maximum = args.max_results
        self.max_bytes = (
            MAX_SNAPSHOT_SEARCH_RECORD_BYTES
            if isinstance(args, GrepSnapshotInput)
            else MAX_SEARCH_RECORD_BYTES
        )
        self.count = 0
        self.bytes = 0
        self.reason: Literal["result_limit", "output_limit"] | None = None

    def accept(self, encoded: str) -> bool:
        size = len(encoded.encode("utf-8")) + 1
        if self.count >= self.maximum:
            self.reason = "result_limit"
        elif self.bytes + size > self.max_bytes:
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
    """收集时观察一个列出的普通文件。

    与POSIX的收集相同：有多个硬链接（或其间被换成Reparse Point）的文件只计入忽略条目，不读，
    也不让整次搜索失败；此前它以workspace_changed结束整次搜索。消失或变成目录仍是workspace_changed。
    """
    try:
        observed = port.observe(
            path, operation, include_content=False, max_bytes=MAX_SEARCH_FILE_BYTES
        )
    except ReadToolError as error:
        if error.code == "path_denied":
            scan.ignored_entries += 1
            return None
        if error.code == "io_failed":
            scan.unreadable_entries += 1
            return None
        if error.code in {"not_found", "wrong_file_type"}:
            raise ReadToolError("workspace_changed") from None
        raise
    if observed.kind != "file":
        raise ReadToolError("workspace_changed")
    return _Candidate(path=path, identity=observed.identity, size=observed.size)


def _collect(
    port: WindowsReadPort,
    args: SearchInput,
    operation: ReadOperation,
    scan: _Scan,
    wanted: Callable[[str], bool],
) -> list[_Candidate]:
    """遍历并观察候选文件；不符合模式（wanted，按相对搜索起点的路径）的文件只计入扫描条目、不打开。

    Windows上观察一个文件就要打开它并取最终路径。此前不符合include的文件也逐个观察：真实的Dify检出
    16,108个文件里只有2,866个.ts，整仓grep仍5秒超时（Beta Issue #24）。
    """
    candidates: list[_Candidate] = []
    root = port.path(args.path)
    observed = port.observe(root, operation, include_content=False)
    if observed.kind == "file":
        scan.entries_scanned = 1
        scan.names_bytes = len(root.rsplit("/", 1)[-1].encode("utf-8"))
        return [_Candidate(path=root, identity=observed.identity, size=observed.size)]
    require_kind(observed, "directory")

    def visit(path: str, depth: int) -> None:
        operation.checkpoint()
        if depth > MAX_SEARCH_DEPTH:
            raise ReadToolError("limit_exceeded")
        before = (
            observed
            if depth == 0 and path == root
            else port.observe(path, operation, include_content=False)
        )
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
            elif kind == "file" and wanted(relative_path(relative, root)):
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

    visit(root, 0)
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
    if scan.entries_scanned > scan.max_entries or scan.names_bytes > scan.max_names_bytes:
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

    scan, pattern, records = _scan_for(args), PathPattern(args.pattern), _Records(args)
    paths: list[str] = []
    root = port.path(args.path)
    for candidate in _collect(port, args, operation, scan, pattern.matches):
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


def _candidate_content(
    port: WindowsReadPort, candidate: _Candidate, operation: ReadOperation
) -> Observation:
    try:
        observed = port.observe_candidate(
            candidate.path,
            operation,
            content=True,
            max_bytes=MAX_SEARCH_FILE_BYTES,
        )
    except ReadToolError as error:
        # 收集时未超上限，读时超了：文件在其间变化。
        if error.code == "limit_exceeded":
            raise ReadToolError("workspace_changed") from None
        raise
    if candidate.identity != observed.identity or candidate.size != observed.size:
        raise ReadToolError("workspace_changed")
    return observed


def _load_text(
    port: WindowsReadPort,
    candidate: _Candidate,
    operation: ReadOperation,
    scan: _Scan,
) -> tuple[Observation, str] | None:
    # 大小上限用收集时记下的大小，读内容时照旧核对身份与大小。此前读内容前
    # 还要再打开一次只为核对元数据，Windows上每个文件多一次打开（Beta Issue #21）。
    if candidate.size > MAX_SEARCH_FILE_BYTES:
        scan.oversized_files += 1
        return None
    if scan.bytes_read + candidate.size > scan.max_total_bytes:
        raise ReadToolError("limit_exceeded")
    try:
        observed = _candidate_content(port, candidate, operation)
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
    matcher: LineMatcher,
    fallback: search.LiteralFallback | None = None,
) -> None:
    if isinstance(args, GrepSnapshotInput):
        operation.checkpoint()
    revision = file_revision(port, candidate.path, observed)
    if isinstance(args, GrepSnapshotInput):
        operation.checkpoint()
    for hit in search.grep_line_matches(
        candidate.path,
        revision,
        text,
        args,
        operation,
        scan,
        content=observed.content,
        matcher=matcher,
    ):
        encoded = hit.model_dump_json()
        if capture is not None:
            capture.append(encoded)
        if records.accept(encoded):
            matches.append(hit)
        elif capture is None:
            break
    if fallback is not None and not matches:
        fallback.observe(
            search.grep_line_matches(
                candidate.path,
                revision,
                text,
                fallback.args,
                operation,
                search.LineGaps(),
                content=observed.content,
                matcher=fallback.matcher,
            )
        )


def _included(
    port: WindowsReadPort, args: GrepInput, operation: ReadOperation, scan: _Scan
) -> list[_Candidate]:
    """要搜索的文件，规则与POSIX端的select_included相同（ADR 0129），筛选在收集时完成。

    起点本身是文件时不按include筛选；起点下有文件却无一符合include时如实失败，
    一个文件也没搜的空结果会被读成“文件里没有”。不符合include的文件不打开（Beta Issue #24）。
    """
    pattern, seen = grep_include(args), 0

    def wanted(path: str) -> bool:
        nonlocal seen
        seen += 1
        return pattern.matches(path)

    candidates = _collect(port, args, operation, scan, wanted)
    if seen and not candidates:
        raise ReadToolError("no_files_searched")
    return candidates


def grep_files(
    port: WindowsReadPort,
    args: GrepInput,
    operation: ReadOperation,
    *,
    capture: search.SearchCapture | None,
) -> GrepOutput:
    """共享匹配器查找文本行，逐文件执行元数据—内容双重观察；字面量回退规则与POSIX端相同。"""

    matcher = build_line_matcher(args, operation)
    fallback = search.LiteralFallback.start(args, operation, capture)
    scan, records = _scan_for(args), _Records(args)
    matches: list[GrepMatch] = []
    timed_out = False
    try:
        for candidate in _included(port, args, operation, scan):
            operation.checkpoint()
            loaded = _load_text(port, candidate, operation, scan)
            if loaded is not None:
                observed, text = loaded
                _append_matches(
                    port,
                    candidate,
                    observed,
                    text,
                    args,
                    operation,
                    scan,
                    records,
                    matches,
                    capture,
                    matcher,
                    fallback,
                )
            if records.reason is not None and capture is None:
                break
    except ReadToolError as error:
        if not search.timed_out_with_results(error, args, matches, fallback):
            raise
        timed_out = True
    stats, _ = _capture_status(capture, scan, records)
    if capture is not None and timed_out:
        capture.complete = False
    return search.grep_result(
        args, stats, matches, records.reason, fallback, capture, timed_out=timed_out
    )
