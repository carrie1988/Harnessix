from __future__ import annotations

from fnmatch import fnmatchcase

from harnessix.tools.search_contracts import validate_pattern


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
