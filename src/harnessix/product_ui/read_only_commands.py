"""完成证据里不算“检查”的命令：只读或只做搜索、不运行项目代码（Beta Issue #36的后续）。

0.10.0b50：一轮没有修好的任务，最后一条命令是`Select-String … | Select-Object`，完成证据却是
“修改后检查通过”。这里认出两类命令，判定不了的一律仍算检查（与此前相同）：

- 沙箱外命令免询问规则（ADR 0128）认定的内置只读命令：`ls`、`cat`、`Get-Content`、`Test-Path`、
  `git status`等；
- 管道里第一个程序是搜索或读取程序、其后只有整理输出的程序，如`Select-String … | Select-Object`、
  `grep … | head`。出现`;`、`&&`、重定向或子命令时不算。

命令末尾为带出退出码追加的`; exit $?`（PowerShell为`exit $LASTEXITCODE`）先去掉再判断。
"""

from __future__ import annotations

import os
import re
from typing import Final

from harnessix.product_config.command_rules import ShellFamily, read_only_paths, simple_command

_EXIT_PASSTHROUGH: Final = re.compile(r"\s*;\s*exit\s+\$(\?|LASTEXITCODE)\s*$", re.IGNORECASE)
_SEARCH: Final = frozenset(
    {
        *("select-string", "sls", "findstr", "grep", "egrep", "fgrep", "rg", "ag"),
        *("get-content", "gc", "cat", "type", "head", "tail", "less", "more"),
        *("get-childitem", "gci", "ls", "dir", "tree"),
    }
)
_SHAPING: Final = frozenset(
    {
        *("select-object", "select", "sort-object", "sort", "measure-object", "measure"),
        *("where-object", "where", "out-string", "format-table", "ft", "format-list", "fl"),
        *("select-string", "sls", "findstr", "grep", "rg", "head", "tail", "wc", "uniq", "cut"),
        *("more", "less", "out-host"),
    }
)
# 引号外出现即不认：串联、后台、重定向、子命令与变量展开。
_COMPLEX: Final = (";", "&", ">", "<", "`", "$(", "\n")


def is_read_only_command(command: str) -> bool:
    """这条命令只读或只做搜索；判定不了时返回False（仍算检查）。"""

    text = _EXIT_PASSTHROUGH.sub("", command).strip()
    shell: ShellFamily = "powershell" if os.name == "nt" else "posix"
    words = simple_command(text, shell)
    if words is not None and read_only_paths(words, shell) is not None:
        return True
    segments = _pipeline(text)
    if not segments:
        return False
    programs = [_program(segment) for segment in segments]
    return programs[0] in _SEARCH and all(name in _SHAPING for name in programs[1:])


def _pipeline(text: str) -> list[str]:
    """按引号外的`|`切分管道；遇到`||`或其他复合写法返回空表。"""

    segments: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "|":
            if text[index + 1 : index + 2] == "|":
                return []
            segments.append("".join(current))
            current = []
            index += 1
            continue
        elif any(text.startswith(token, index) for token in _COMPLEX):
            return []
        current.append(char)
        index += 1
    if quote:
        return []
    segments.append("".join(current))
    return segments if all(segment.strip() for segment in segments) else []


def _program(segment: str) -> str:
    name = segment.split()[0].strip("'\"")
    name = re.split(r"[\\/]", name)[-1].lower()
    return name.removesuffix(".exe")
