"""内置Coding Tool：执行有界文件发现、Glob与文本搜索。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal, Protocol

from harnessix.artifacts.contracts import MAX_ARTIFACT_BYTES, MAX_ARTIFACT_RECORDS
from harnessix.tools.contracts import MAX_LINE_BYTES, ReadToolError
from harnessix.tools.files import _decode
from harnessix.tools.grep_matcher import (
    LineMatcher,
    build_line_matcher,
    literal_fallback_args,
    whole_text_decides,
)
from harnessix.tools.patterns import PathPattern, relative_to_start, select_included
from harnessix.tools.search_contracts import (
    IGNORED_DIRECTORIES,
    MAX_MATCH_BYTES,
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
        if not pattern.matches(relative_to_start(node.path, args.path)):
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
    # 整行放得下就原样返回（含行首缩进），模型可直接复制为精确编辑锚点；超长行才从匹配点前截取。
    fits = len(text.encode("utf-8")) <= MAX_MATCH_BYTES
    start = 0 if fits else max(0, index - 16)
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
    operation.checkpoint()
    if whole_text_decides(args) and matcher(text) < 0:
        # 整份文本都不命中：不必逐行扫描、不必计算摘要，其中也没有可能藏着命中的超长行。
        return
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
            # UTF-8每个字符至多4字节：字符数不超过上限的四分之一时不必编码就知道没有超长。
            if len(raw) > MAX_LINE_BYTES // 4 and len(raw.encode("utf-8")) > MAX_LINE_BYTES:
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


def timed_out_with_results(
    error: ReadToolError,
    args: GrepInput,
    matches: Sequence[GrepMatch],
    fallback: LiteralFallback | None,
) -> bool:
    """读取时限到了：快照与正则Grep已经找到命中时返回这些命中（ADR 0162，Beta Issue #24）。

    Windows上第一次搜索新检出的大仓库，读文件内容要先经Defender扫描，同类工具同样要十几秒；
    此前超时一律没有结果。没有命中、或原字面量Grep，照旧以超时失败。
    """
    return (
        error.code == "timeout"
        and isinstance(args, GrepSnapshotInput)
        and bool(matches or (fallback is not None and fallback.matches))
    )


def grep_result(
    args: GrepInput,
    stats: SearchStats,
    matches: list[GrepMatch],
    reason: Literal["result_limit", "output_limit"] | None,
    fallback: LiteralFallback | None,
    capture: SearchCapture | None,
    *,
    timed_out: bool = False,
) -> GrepOutput:
    """组装搜索结果；正则无命中而字面量有命中时改用字面量结果并标记。

    timed_out：读取时限到了还没搜完，结果只含已找到的命中，标为time_limit（ADR 0162）。
    """
    fields: dict[str, object] = {}
    final: Literal["result_limit", "output_limit", "time_limit"] | None = reason
    if fallback is not None and fallback.replaces(matches):
        fallback.publish_to(capture)
        matches, final = fallback.matches, fallback.records.reason
        fields["literal_fallback"] = True
    if timed_out and final is None:
        final = "time_limit"
    output_type = GrepSnapshotOutput if isinstance(args, GrepSnapshotInput) else GrepOutput
    return output_type.model_validate(
        {
            "path": args.path,
            "matches": tuple(matches),
            "stats": stats,
            "scan_complete": final is None and not stats.has_gaps,
            "truncated": final is not None,
            "truncation_reason": final,
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
    scan, records = _scan_for(args), _Records(args)
    matches: list[GrepMatch] = []
    timed_out = False
    try:
        for node in select_included(_collect(workspace, args, operation, scan), args, args.path):
            operation.checkpoint()
            try:
                with _open_candidate(workspace, node, operation, directory=False) as fd:
                    state = revision_state(os.fstat(fd))
                    revision = digest((workspace.scope, node.path, state))
                    text = _read(fd, operation, scan)
                    if isinstance(args, GrepSnapshotInput) and state != revision_state(
                        os.fstat(fd)
                    ):
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
    except ReadToolError as error:
        if not timed_out_with_results(error, args, matches, fallback):
            raise
        timed_out = True
    stats = scan.snapshot()
    if capture is not None:
        capture.complete = not stats.has_gaps and not timed_out
    return grep_result(args, stats, matches, records.reason, fallback, capture, timed_out=timed_out)
