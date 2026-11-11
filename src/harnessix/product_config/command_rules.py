"""沙箱外命令的免询问判定（ADR 0128）：内置只读命令与用户保存的命令前缀规则。

只看命令文本，不执行命令。判定不了就交给用户：复合命令、重定向、变量与子表达式、转义和非ASCII字符
一律不免询问。只读命令的路径参数必须是工作区内的相对路径，并沿用读工具的敏感名称规则（.env、密钥
等），免询问读到的内容不会多于读工具。会递归读出文件内容或带执行参数的命令（grep、find等）不在内置
只读集合内，Agent用自带的搜索工具即可。
"""

from __future__ import annotations

import re
import shlex
from typing import Final, Literal

from harnessix.tools.contracts import ReadToolError
from harnessix.tools.workspace import WorkspaceReadPolicy

ShellFamily = Literal["posix", "powershell"]
# list只输出名称与元数据，允许通配符和任意选项；read输出文件内容，选项与路径都从严。
_Kind = Literal["list", "read"]

_COMPOUND: Final = frozenset("|&;<>(){}$`\n\r")
_SHELL_ONLY: Final = {"posix": frozenset("\\"), "powershell": frozenset(",@")}
_GLOB: Final = frozenset("*?[]")
_POSIX_READ_ONLY: Final[dict[str, _Kind]] = {
    "ls": "list",
    "pwd": "list",
    "stat": "list",
    "du": "list",
    "tree": "list",
    "which": "list",
    "echo": "list",
    "cat": "read",
    "head": "read",
    "tail": "read",
    "wc": "read",
    "diff": "read",
    "file": "read",
}
_POWERSHELL_READ_ONLY: Final[dict[str, _Kind]] = {
    **dict.fromkeys(("get-childitem", "gci", "ls", "dir"), "list"),
    **dict.fromkeys(("get-location", "gl", "pwd"), "list"),
    **dict.fromkeys(("test-path", "get-item", "gi", "resolve-path", "rvpa"), "list"),
    **dict.fromkeys(("get-command", "gcm", "write-output", "echo", "write"), "list"),
    **dict.fromkeys(("get-content", "gc", "cat", "type", "get-filehash"), "read"),
}
_GIT_READ_ONLY: Final = frozenset({"status", "diff", "log", "show", "rev-parse", "ls-files"})
# 写文件、执行外部程序或读工作区外文件的git选项。
_GIT_DENIED_OPTIONS: Final = frozenset({"--output", "--ext-diff", "--no-index"})
_POSIX_READ_OPTION: Final = re.compile(r"-[A-Za-z]|-\d+|-[nc][+-]?\d+|--[a-z][a-z-]*(=.*)?")
_POWERSHELL_OPTION: Final = re.compile(r"-[A-Za-z]+")
_DRIVE: Final = re.compile(r"[A-Za-z]:")
_POLICY: Final = WorkspaceReadPolicy()

# 不提供“以后不再询问”：删除、提权、能执行任意内联代码的Shell与命令。
_NEVER_REMEMBER: Final = frozenset(
    {
        *("rm", "rmdir", "del", "rd", "erase", "remove-item", "ri", "dd", "mkfs", "format"),
        *("sudo", "su", "doas", "runas", "shutdown", "reboot"),
        *("sh", "bash", "zsh", "dash", "cmd", "powershell", "pwsh", "env", "xargs", "eval"),
        *("invoke-expression", "iex", "start-process", "saps", "start"),
    }
)
_INLINE_CODE_OPTIONS: Final = frozenset({"-c", "-e", "--eval", "-p", "--print", "-r"})
_INTERPRETERS: Final = frozenset(
    {"python", "python3", "py", "node", "ruby", "perl", "php", "deno", "bun"}
)
# 带子命令的工具：规则保存到子命令一级（如 git status *）。
_SUBCOMMAND_TOOLS: Final = frozenset(
    {
        *("git", "npm", "pnpm", "yarn", "uv", "pip", "pip3", "cargo", "go", "dotnet"),
        *("poetry", "docker", "kubectl", "conda", "gradle", "mvn", "make"),
    }
)
# 再下一级才是实际运行的程序（如 uv run pytest *、python -m unittest *）。
_RUNNERS: Final = frozenset(
    {
        *((tool, "run") for tool in ("uv", "npm", "pnpm", "yarn", "poetry")),
        *((tool, "-m") for tool in ("python", "python3", "py")),
    }
)


def simple_command(cmd: str, shell: ShellFamily) -> tuple[str, ...] | None:
    """把一条简单命令切成单词；复合命令或无法可靠解析时返回None（交给用户决定）。"""

    forbidden = _COMPOUND | _SHELL_ONLY[shell]
    if any(ch in forbidden or not (" " <= ch <= "~" or ch == "\t") for ch in cmd):
        return None
    lexer = shlex.shlex(cmd, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    try:
        words = tuple(lexer)
    except ValueError:
        return None
    return words or None


def read_only_paths(words: tuple[str, ...], shell: ShellFamily) -> tuple[str, ...] | None:
    """内置只读命令返回它引用的工作区相对路径（可为空）；不是只读命令返回None。

    调用方还须确认这些路径解析符号链接后仍在工作区内（文本判断看不到符号链接）。
    """

    name = _command_name(words[0], shell)
    if name == "git":
        return _git_paths(words, shell)
    kind = (_POWERSHELL_READ_ONLY if shell == "powershell" else _POSIX_READ_ONLY).get(name)
    if kind is None:
        return None
    paths: list[str] = []
    for word in words[1:]:
        value = word
        if word.startswith("-"):
            if not _option_allowed(word, shell, kind):
                return None
            if "=" not in word:
                continue
            value = word.split("=", 1)[1]
        path = _workspace_path(value, shell, globs=kind == "list")
        if path is None:
            return None
        paths.append(path)
    return tuple(paths)


def rule_prefix(words: tuple[str, ...], shell: ShellFamily) -> tuple[str, ...] | None:
    """“以后不再询问”保存的命令前缀；删除、提权与内联代码类命令返回None，只能逐次批准。"""

    name = _command_name(words[0], shell)
    if name in _NEVER_REMEMBER or "/" in name or "\\" in name:
        return None
    if name in _INTERPRETERS and any(word in _INLINE_CODE_OPTIONS for word in words[1:3]):
        return None
    following = [word for word in words[1:3]]
    if len(following) == 2 and (name, following[0]) in _RUNNERS and _plain(following[1]):
        return (name, following[0], following[1])
    if name in _SUBCOMMAND_TOOLS and following and _plain(following[0]):
        return (name, following[0])
    return (name,)


def rule_matches(prefix: tuple[str, ...], words: tuple[str, ...], shell: ShellFamily) -> bool:
    """简单命令以已保存的前缀开头；PowerShell命令名不区分大小写。"""

    if len(words) < len(prefix):
        return False
    head = (_command_name(words[0], shell), *words[1 : len(prefix)])
    if shell == "powershell":
        return tuple(word.lower() for word in head) == tuple(word.lower() for word in prefix)
    return head == prefix


def allowed_relative_path(path: str) -> bool:
    """正斜杠相对路径不越出工作区且不含敏感名称；供调用方复核解析符号链接后的实际路径。"""

    return _workspace_path(path, "posix", globs=True) is not None


def rule_label(prefix: tuple[str, ...]) -> str:
    return " ".join(prefix) + " *"


def _command_name(word: str, shell: ShellFamily) -> str:
    if shell == "powershell":
        return word.lower().removesuffix(".exe")
    return word


def _plain(word: str) -> bool:
    return (
        bool(word) and not word.startswith("-") and re.fullmatch(r"[\w.:@/+-]+", word) is not None
    )


def _option_allowed(word: str, shell: ShellFamily, kind: _Kind) -> bool:
    if shell == "powershell":
        # 拒绝“-参数:值”写法：值会绕过路径检查。
        return _POWERSHELL_OPTION.fullmatch(word) is not None
    return kind == "list" or _POSIX_READ_OPTION.fullmatch(word) is not None


def _git_paths(words: tuple[str, ...], shell: ShellFamily) -> tuple[str, ...] | None:
    if len(words) < 2 or words[1] not in _GIT_READ_ONLY:
        return None
    paths: list[str] = []
    for word in words[2:]:
        if word == "--":
            continue
        if word.split("=", 1)[0] in _GIT_DENIED_OPTIONS:
            return None
        value = word
        if word.startswith("-"):
            if "=" not in word:
                continue
            value = word.split("=", 1)[1]
        # “版本:路径”只检查路径部分；版本范围等不是路径的参数也要通过同一检查。
        path = _workspace_path(value.rsplit(":", 1)[-1] or ".", shell, globs=True, colon=True)
        if path is None or _DRIVE.match(value.replace("\\", "/")) is not None:
            return None
        paths.append(path)
    return tuple(paths)


def _workspace_path(
    word: str, shell: ShellFamily, *, globs: bool, colon: bool = False
) -> str | None:
    """工作区内的相对路径原样返回（统一为正斜杠）；绝对路径、越界、敏感名称等返回None。"""

    text = word
    if shell == "powershell":
        text = word.replace("\\", "/")
        # 驱动器与提供程序路径（C:、Env:、HKLM:）、8.3短名称；Win32会去掉段尾的点和空格。
        if (":" in text and not colon) or "~" in text:
            return None
        if any(part not in (".", "..") and part.endswith((".", " ")) for part in text.split("/")):
            return None
    if not text or text.startswith("~") or _DRIVE.match(text) is not None:
        return None
    if not globs and any(ch in _GLOB for ch in text):
        return None
    while text.startswith("./"):
        text = text[2:]
    if text in ("", "."):
        return "."
    try:
        _POLICY.parts(text.rstrip("/") or ".")
    except (ReadToolError, ValueError):
        return None
    return text
