"""Workspace身份与租约：执行跨平台Workspace路径规范化与成员校验。"""

from __future__ import annotations

import re

from harnessix.agent.errors import KernelError
from harnessix.workspace.contracts import PlatformKind

_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
    | {f"COM{index}" for index in "¹²³"}
    | {f"LPT{index}" for index in "¹²³"}
)
_DRIVE_PREFIX = re.compile(r"^[a-zA-Z]:")
# Windows为长名称生成的8.3短名称（如.env的ENV~1、.git的GIT~1）都含“~数字”；
# 按短名称打开会落到长名称上，绕过按名称的拒绝（ADR 0170）。
_WINDOWS_SHORT_NAME = re.compile(r"~[0-9]")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")


def normalize_workspace_path(value: str, platform: PlatformKind) -> str:
    """校验模型可提交的逻辑相对路径；不把宿主路径语义泄露到领域层。"""

    if platform not in {"posix", "windows"}:
        raise KernelError("workspace_platform_unsupported", "Workspace平台不受支持")
    if type(value) is not str:
        raise KernelError("workspace_path_denied", "Workspace路径必须是字符串")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise KernelError("workspace_path_denied", "Workspace路径不是有效UTF-8") from None
    if (
        not encoded
        or len(encoded) > 4096
        or value.startswith(("/", "\\"))
        or "\\" in value
        or _DRIVE_PREFIX.match(value)
        or _CONTROL_CHARACTER.search(value) is not None
    ):
        raise KernelError("workspace_path_denied", "Workspace路径不是规范相对路径")
    if value == ".":
        return value
    parts = value.split("/")
    if len(parts) > 128 or any(part in {"", ".", ".."} for part in parts):
        raise KernelError("workspace_path_denied", "Workspace路径包含非法段")
    if platform == "windows":
        for part in parts:
            if ":" in part or part.endswith((".", " ")):
                raise KernelError("workspace_path_denied", "Windows路径包含ADS或折叠段")
            stem = part.split(".", 1)[0].upper()
            if stem in _WINDOWS_RESERVED or len(part.encode("utf-16-le")) // 2 > 255:
                raise KernelError("workspace_path_denied", "Windows路径包含保留名或超长段")
    # 全部段已拒绝空值、点与父引用；原输入就是规范形式，无需再构造路径对象。
    return value


def has_windows_short_name(path: str) -> bool:
    """模型提交的Windows路径是否有段形如8.3短名称（ADR 0170）。

    只用于模型提交的读写路径入口：目录遍历得到的都是长名称；仓库里真名就含“~数字”的
    文件在Windows上不能经读取工具和Patch按这个名称访问，改用命令工具。
    """

    return any(_WINDOWS_SHORT_NAME.search(part) for part in path.split("/"))


def path_comparison_key(value: str, platform: PlatformKind) -> str:
    normalized = normalize_workspace_path(value, platform)
    return normalized.casefold() if platform == "windows" else normalized
