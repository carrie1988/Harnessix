"""内置Coding Tool：校验Glob和Grep模式的长度及语法边界，并按Grep的include筛选候选文件。"""

from __future__ import annotations

from collections.abc import Iterator
from fnmatch import fnmatchcase
from typing import Protocol

from harnessix.tools.contracts import ReadToolError
from harnessix.tools.search_contracts import GrepInput, GrepSnapshotInput, validate_pattern

# 进入Grep合同摘要的include语义：与此前“静默返回空结果”的版本不是同一合同。
GREP_INCLUDE_RULE = "not-applied-to-file-start-and-fails-when-no-file-matches"


def relative_to_start(path: str, start: str) -> str:
    """候选路径相对搜索起点的写法；起点本身是文件时取文件名。"""
    if start == ".":
        return path
    if path == start:
        return path.rsplit("/", 1)[-1]
    return path[len(start) + 1 :]


def grep_include(args: GrepInput) -> PathPattern | BasenamePattern:
    """快照与正则Grep的include按文件名匹配任意深度；原字面量合同保持逐段路径语义。"""
    if isinstance(args, GrepSnapshotInput):
        return include_pattern(args.include)
    return PathPattern(args.include)


class _Located(Protocol):
    @property
    def path(self) -> str: ...


def select_included[C: _Located](candidates: list[C], args: GrepInput, start: str) -> list[C]:
    """按include筛选要搜索的文件；POSIX与原生读取端共用。

    起点本身是文件时不筛选：模型已点名要搜的文件，include若写成相对仓库根的通配会把它筛掉。
    起点下有文件却无一匹配include时如实失败：一个文件也没搜的空结果会被读成“文件里没有”。
    """
    if len(candidates) == 1 and candidates[0].path == start:
        return candidates
    pattern = grep_include(args)
    selected = [node for node in candidates if pattern.matches(relative_to_start(node.path, start))]
    if candidates and not selected:
        raise ReadToolError("no_files_searched")
    return selected


def included_while_walking[C: _Located](
    candidates: Iterator[C], args: GrepInput, start: str
) -> Iterator[C]:
    """快照与正则Grep边遍历边筛选（ADR 0162第2版），规则与select_included相同；其余Grep先遍历完。

    起点本身是文件时不筛选；整棵树走完、有文件却无一符合include时失败。时限先到或已达max_results时
    调用方不再取，不判断“没有文件符合”。
    """
    if not isinstance(args, GrepSnapshotInput):
        yield from select_included(list(candidates), args, start)
        return
    pattern, seen, selected = grep_include(args), False, False
    for node in candidates:
        if node.path == start:
            yield node
            return
        seen = True
        if pattern.matches(relative_to_start(node.path, start)):
            selected = True
            yield node
    if seen and not selected:
        raise ReadToolError("no_files_searched")


def include_pattern(pattern: str) -> PathPattern | BasenamePattern:
    """grep的include：不含“/”时按文件名匹配任意深度（与rg -g一致），否则按起点相对路径逐段匹配。"""
    if "/" in pattern:
        return PathPattern(pattern)
    return BasenamePattern(pattern)


class BasenamePattern:
    """只匹配相对路径的最后一段，目录深度不限。"""

    def __init__(self, pattern: str) -> None:
        self._pattern = validate_pattern(pattern)

    def matches(self, path: str) -> bool:
        return fnmatchcase(path.rsplit("/", 1)[-1], self._pattern)


class PathPattern:
    """标准库处理单段通配；有界状态表处理 **，不递归展开文件系统。"""

    def __init__(self, pattern: str) -> None:
        self._parts = tuple(validate_pattern(pattern).split("/"))

    def matches(self, path: str) -> bool:
        names = path.split("/")
        previous = [True] + [False] * len(names)
        for pattern in self._parts:
            current = [False] * (len(names) + 1)
            if pattern == "**":
                current[0] = previous[0]
                for index in range(1, len(current)):
                    current[index] = previous[index] or current[index - 1]
            else:
                for index, name in enumerate(names, start=1):
                    current[index] = previous[index - 1] and fnmatchcase(name, pattern)
            previous = current
        return previous[-1]
