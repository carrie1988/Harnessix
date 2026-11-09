"""内置Coding Tool：执行有界文件发现、Glob与文本搜索。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, Protocol

from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES, MAX_ARTIFACT_RECORDS
from harnessix.tools.contracts import MAX_LINE_BYTES, ReadToolError
from harnessix.tools.files import _decode
from harnessix.tools.grep_matcher import LineMatcher, build_line_matcher, literal_fallback_args
from harnessix.tools.grep_matcher import execution_contract as regex_execution_contract
from harnessix.tools.patterns import BasenamePattern, PathPattern, include_pattern
from harnessix.tools.search_contracts import (
    IGNORED_DIRECTORIES,
    MAX_GREP_CONTEXT_LINES,
    MAX_MATCH_BYTES,
    MAX_SEARCH_DEPTH,
    MAX_SEARCH_ENTRIES,
    MAX_SEARCH_FILE_BYTES,
    MAX_SEARCH_NAMES_BYTES,
    MAX_SEARCH_RECORD_BYTES,
    MAX_SEARCH_TOTAL_BYTES,
    MAX_SNAPSHOT_GREP_CONTEXT_LINES,
    MAX_SNAPSHOT_SEARCH_ENTRIES,
    MAX_SNAPSHOT_SEARCH_NAMES_BYTES,
    MAX_SNAPSHOT_SEARCH_RECORD_BYTES,
    MAX_SNAPSHOT_SEARCH_TOTAL_BYTES,
    GlobInput,
    GlobOutput,
    GrepContext,
    GrepContextLine,
    GrepInput,
    GrepMatch,
    GrepOutput,
    GrepSnapshotContext,
    GrepSnapshotInput,
    GrepSnapshotMatch,
    GrepSnapshotOutput,
    SearchInput,
    SearchStats,
    SnapshotSearchStats,
)
from harnessix.tools.workspace import ReadOperation, Workspace, digest, identity, revision_state


def execution_contract(
    *, enable_regex_search: bool = False, snapshot_grep: bool = False
) -> dict[str, object]:
    """原字面量Grep与Glob合同不变；快照与正则Grep另有整仓库上限、文件名include与更宽上下文。"""
    rules: dict[str, object] = {
        "version": "bounded-search/v3",
        "start_path": "regular-file-or-directory",
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
        "max_grep_context_lines": MAX_GREP_CONTEXT_LINES,
    }
    if snapshot_grep or enable_regex_search:
        rules.update(
            version="bounded-search/v5",
            snapshot_grep={
                "include": "basename-at-any-depth-without-slash-else-segments-and-globstar",
                "max_entries": MAX_SNAPSHOT_SEARCH_ENTRIES,
                "max_names_bytes": MAX_SNAPSHOT_SEARCH_NAMES_BYTES,
                "max_total_bytes": MAX_SNAPSHOT_SEARCH_TOTAL_BYTES,
                "max_record_bytes": MAX_SNAPSHOT_SEARCH_RECORD_BYTES,
                "max_context_lines": MAX_SNAPSHOT_GREP_CONTEXT_LINES,
            },
        )
    if enable_regex_search:
        rules.update(
            version="bounded-search/v6",
            grep="case-sensitive-re2-or-explicit-literal-lines",
            regex=regex_execution_contract(),
        )
    return rules


class SearchCapture:
    """预览之外的记录仍有硬上限；不将超过预算的前缀当作完整正文。"""

    def __init__(self) -> None:
        self.body = bytearray()
        self.records = 0
        self.complete = False

    def append(self, encoded: str) -> None:
        data = (encoded + "\n").encode("utf-8")
        if len(self.body) + len(data) > MAX_ARTIFACT_BYTES or self.records >= MAX_ARTIFACT_RECORDS:
            raise ReadToolError("limit_exceeded")
        self.body.extend(data)
        self.records += 1


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
    max_entries: int = 0
    max_names_bytes: int = 0
    max_total_bytes: int = 0
    stats_type: type[SearchStats] = SearchStats

    def snapshot(self) -> SearchStats:
        return self.stats_type.model_validate(
            {name: getattr(self, name) for name in SearchStats.model_fields}
        )


def grep_include(args: GrepInput) -> PathPattern | BasenamePattern:
    """快照与正则Grep的include按文件名匹配任意深度；原字面量合同保持逐段路径语义。"""
    if isinstance(args, GrepSnapshotInput):
        return include_pattern(args.include)
    return PathPattern(args.include)


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

    try:
        with workspace.open(args.path, operation, directory=False) as fd:
            info = os.fstat(fd)
            scan.entries_scanned = 1
            scan.names_bytes = len(os.fsencode(args.path.rsplit("/", 1)[-1]))
            return [_Candidate(args.path, info)]
    except ReadToolError as error:
        if error.code != "wrong_file_type":
            raise

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
                    scan.entries_scanned > scan.max_entries
                    or scan.names_bytes > scan.max_names_bytes
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
    def __init__(self, args: SearchInput) -> None:
        self.maximum = args.max_results
        self.max_bytes = (
            MAX_SNAPSHOT_SEARCH_RECORD_BYTES
            if isinstance(args, GrepSnapshotInput)
            else MAX_SEARCH_RECORD_BYTES
        )
        self.count = self.bytes = 0
        self.reason: Literal["result_limit", "output_limit"] | None = None

    def accept(self, encoded: str) -> bool:
        if self.count >= self.maximum:
            self.reason = "result_limit"
        elif self.bytes + len(encoded.encode("utf-8")) + 1 > self.max_bytes:
            self.reason = "output_limit"
        if self.reason is not None:
            return False
        self.count += 1
        self.bytes += len(encoded.encode("utf-8")) + 1
        return True


def _relative(path: str, root: str) -> str:
    if root == ".":
        return path
    if path == root:
        return path.rsplit("/", 1)[-1]
    return path[len(root) + 1 :]


def glob(
    workspace: Workspace,
    args: GlobInput,
    operation: ReadOperation,
    *,
    capture: SearchCapture | None = None,
) -> GlobOutput:
    scan, pattern, records = _scan_for(args), PathPattern(args.pattern), _Records(args)
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
        encoded = json.dumps(node.path, ensure_ascii=False)
        if capture is not None:
            capture.append(encoded)
        if records.accept(encoded):
            paths.append(node.path)
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


def _read(fd: int, operation: ReadOperation, scan: _Scan) -> str | None:
    if os.fstat(fd).st_size > MAX_SEARCH_FILE_BYTES:
        scan.oversized_files += 1
        return None
    data = bytearray()
    while True:
        operation.checkpoint()
        remaining = min(MAX_SEARCH_FILE_BYTES - len(data), scan.max_total_bytes - scan.bytes_read)
        chunk = os.read(fd, min(65536, remaining + 1))
        scan.bytes_read += len(chunk)
        data.extend(chunk)
        if len(data) > MAX_SEARCH_FILE_BYTES or scan.bytes_read > scan.max_total_bytes:
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


class _LineStats(Protocol):
    long_lines: int


def _line_text(raw: str) -> str:
    return raw[:-1].removesuffix("\r") if raw.endswith("\n") else raw


def grep_line_matches(
    path: str,
    revision: str,
    text: str,
    args: GrepInput,
    operation: ReadOperation,
    scan: _LineStats,
    *,
    content: bytes | None = None,
    matcher: LineMatcher | None = None,
) -> Iterator[GrepMatch]:
    """单一内容观察内用有界滚动窗口生成命中；POSIX与原生读取端复用。"""
    if matcher is None:
        matcher = build_line_matcher(args, operation)
    match_type: type[GrepMatch] = GrepMatch
    context_type: type[GrepContext] = GrepContext
    snapshot_fields: dict[str, str | int] = {}
    if isinstance(args, GrepSnapshotInput):
        operation.checkpoint()
        # 严格_decode不去BOM、不转换CRLF、不归一化Unicode；编码可还原同次读取字节。
        # 原生端直接复用Observation.content；摘要与下面的显示片段截断无关。
        raw_content = text.encode("utf-8") if content is None else content
        snapshot_fields = {
            "content_sha256": hashlib.sha256(raw_content).hexdigest(),
            "file_bytes": len(raw_content),
        }
        operation.checkpoint()
        match_type, context_type = GrepSnapshotMatch, GrepSnapshotContext
    before: deque[str] = deque(maxlen=args.context_lines)
    ahead: deque[str] = deque()
    with io.StringIO(text, newline="\n") as lines:
        for _ in range(args.context_lines + 1):
            operation.checkpoint()
            raw = lines.readline()
            if not raw:
                break
            ahead.append(raw)
        number = 0
        while ahead:
            operation.checkpoint()
            raw = ahead.popleft()
            number += 1
            if len(raw.encode("utf-8")) > MAX_LINE_BYTES:
                scan.long_lines += 1
            else:
                line = _line_text(raw)
                index = matcher(line)
                operation.checkpoint()
                if index >= 0:
                    snippet, truncated = _preview(line, index)
                    context = None
                    if args.context_lines:
                        views = []
                        for position, neighbor in enumerate((*before, raw, *ahead)):
                            preview = (
                                (snippet, truncated)
                                if position == len(before)
                                else _preview(_line_text(neighbor), 0)
                            )
                            views.append(
                                GrepContextLine(text=preview[0], text_truncated=preview[1])
                            )
                        context = context_type(start_line=number - len(before), lines=tuple(views))
                    yield match_type.model_validate(
                        {
                            "path": path,
                            "line": number,
                            "text": snippet,
                            "text_truncated": truncated,
                            "revision": revision,
                            "context": context,
                            **snapshot_fields,
                        }
                    )
            before.append(raw)
            operation.checkpoint()
            following = lines.readline()
            if following:
                ahead.append(following)


@dataclass
class LineGaps:
    """字面量回退扫描不重复计入长行等缺口；缺口只由正则扫描统计。"""

    long_lines: int = 0


class LiteralFallback:
    """正则查询含元字符时，在同一次内容观察内另按字面量收集命中；正则整次无命中才采用。

    模型常把含括号、点号的源码片段当字面量查找，按正则解释后静默无命中。回退不增加文件读取；
    字面量捕获超出上限时放弃回退，正则结果不受影响。
    """

    def __init__(
        self, args: GrepInput, operation: ReadOperation, capture: SearchCapture | None
    ) -> None:
        self.args = args
        self.matcher = build_line_matcher(args, operation)
        self.records = _Records(args)
        self.matches: list[GrepMatch] = []
        self.capture = None if capture is None else SearchCapture()
        self.abandoned = False

    @classmethod
    def start(
        cls, args: GrepInput, operation: ReadOperation, capture: SearchCapture | None
    ) -> LiteralFallback | None:
        literal = literal_fallback_args(args)
        return None if literal is None else cls(literal, operation, capture)

    def observe(self, hits: Iterator[GrepMatch]) -> None:
        if self.abandoned:
            return
        try:
            for hit in hits:
                encoded = hit.model_dump_json()
                if self.capture is not None:
                    self.capture.append(encoded)
                if self.records.accept(encoded):
                    self.matches.append(hit)
                elif self.capture is None:
                    break
        except ReadToolError as error:
            if error.code != "limit_exceeded":
                raise
            self.abandoned = True

    def replaces(self, regex_matches: list[GrepMatch]) -> bool:
        return not regex_matches and not self.abandoned and bool(self.matches)

    def publish_to(self, capture: SearchCapture | None) -> None:
        if capture is not None and self.capture is not None:
            capture.body.extend(self.capture.body)
            capture.records += self.capture.records


def grep_result(
    args: GrepInput,
    stats: SearchStats,
    matches: list[GrepMatch],
    reason: Literal["result_limit", "output_limit"] | None,
    fallback: LiteralFallback | None,
    capture: SearchCapture | None,
) -> GrepOutput:
    """组装搜索结果；正则无命中而字面量有命中时改用字面量结果并标记。"""
    fields: dict[str, object] = {}
    if fallback is not None and fallback.replaces(matches):
        fallback.publish_to(capture)
        matches, reason = fallback.matches, fallback.records.reason
        fields["literal_fallback"] = True
    output_type = GrepSnapshotOutput if isinstance(args, GrepSnapshotInput) else GrepOutput
    return output_type.model_validate(
        {
            "path": args.path,
            "matches": tuple(matches),
            "stats": stats,
            "scan_complete": reason is None and not stats.has_gaps,
            "truncated": reason is not None,
            "truncation_reason": reason,
            **fields,
        }
    )


def grep(
    workspace: Workspace,
    args: GrepInput,
    operation: ReadOperation,
    *,
    capture: SearchCapture | None = None,
) -> GrepOutput:
    matcher = build_line_matcher(args, operation)
    fallback = LiteralFallback.start(args, operation, capture)
    scan, pattern, records = _scan_for(args), grep_include(args), _Records(args)
    matches: list[GrepMatch] = []
    for node in _collect(workspace, args, operation, scan):
        operation.checkpoint()
        if not pattern.matches(_relative(node.path, args.path)):
            continue
        try:
            with _open_candidate(workspace, node, operation, directory=False) as fd:
                state = revision_state(os.fstat(fd))
                revision = digest((workspace.scope, node.path, state))
                text = _read(fd, operation, scan)
                if isinstance(args, GrepSnapshotInput) and state != revision_state(os.fstat(fd)):
                    raise ReadToolError("workspace_changed")
                if text is None:
                    continue
                for hit in grep_line_matches(
                    node.path, revision, text, args, operation, scan, matcher=matcher
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
                        grep_line_matches(
                            node.path,
                            revision,
                            text,
                            fallback.args,
                            operation,
                            LineGaps(),
                            matcher=fallback.matcher,
                        )
                    )
        except PermissionError:
            scan.unreadable_entries += 1
        if records.reason is not None and capture is None:
            break
    stats = scan.snapshot()
    if capture is not None:
        capture.complete = not stats.has_gaps
    return grep_result(args, stats, matches, records.reason, fallback, capture)
