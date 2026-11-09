"""内置Coding Tool：校验Glob和Grep模式的长度及语法边界。"""

from __future__ import annotations

from fnmatch import fnmatchcase

from harnessix.tools.search_contracts import validate_pattern


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
