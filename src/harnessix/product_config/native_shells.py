"""本机命令Profile允许的Shell（ADR 0111、0113；Windows见2026-11-10用户决定W2）。

POSIX：固定sh/bash/dash/zsh加`-c`或`-lc`，可用于本机沙箱或沙箱外。
Windows：没有本机沙箱，只允许沙箱外的PowerShell，参数固定，每条命令都需用户批准。
模型命令总是作为最后一个参数追加。
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Protocol

from harnessix.processes.supervision_contracts import ProcessCapabilityProbe

# POSIX绝对路径，或带盘符的Windows绝对路径（可含空格，如Program Files）。
NATIVE_PROGRAM_PATTERN = (
    r"^(?:/[A-Za-z0-9._/+:-]+(?:/[A-Za-z0-9._+:-]+)*"
    r"|[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n\x00]+\\)*[^\\/:*?\"<>|\r\n\x00]+\.[Ee][Xx][Ee])$"
)
_POSIX_SHELLS = frozenset({"sh", "bash", "dash", "zsh"})
_POWERSHELLS = frozenset({"powershell.exe", "pwsh.exe"})
_POWERSHELL_OPTIONS = ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command")
_UTF8_OUTPUT = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
# 不加载用户配置、不交互；输出按UTF-8编码，与其他平台的输出解码一致。
# 执行计划的环境变量名合同不允许括号，ProgramFiles(x86)不能随环境继承（见sandbox/native_policy）；
# 命令运行前由PowerShell向系统查询该目录并补上，Visual Studio等工具链脚本照常能读到。
POWERSHELL_ARGUMENTS = (
    *_POWERSHELL_OPTIONS,
    _UTF8_OUTPUT + "${env:ProgramFiles(x86)}=[Environment]::GetFolderPath('ProgramFilesX86');",
)
# 0.10.0b13及更早版本写入显式配置的参数，仍然接受。
_LEGACY_POWERSHELL_ARGUMENTS = (*_POWERSHELL_OPTIONS, _UTF8_OUTPUT)


# PowerShell以-Command运行时，退出码只看最后一条语句成功与否：原生程序退出2、42都报成1
# （Beta Issue #12）。用户命令之后另起一行补一段固定代码：最后一条语句失败时带出原生程序的真实
# 退出码，没有原生退出码（cmdlet出错、命令不存在）时仍为1。另起一行，用户命令末尾的注释吞不掉它；
# 用户命令自己exit时它不会执行。审批时显示的仍是用户命令本身。
POWERSHELL_EXIT_TRAILER = "\nif (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE } exit 1 }"


class _ShellProgram(Protocol):
    @property
    def program(self) -> str: ...


def shell_command_text(profile: _ShellProgram, command: str) -> str:
    """交给Shell的命令正文：PowerShell在命令后补上带出原生退出码的一段，其他Shell原样。"""

    if PureWindowsPath(profile.program).name.lower() in _POWERSHELLS:
        return command + POWERSHELL_EXIT_TRAILER
    return command


def fixed_shell(program: str, arguments: tuple[str, ...], backend: str) -> bool:
    """Profile只能固定一种允许的Shell与参数；PowerShell只用于沙箱外后端。"""

    if program.startswith("/"):
        return PurePosixPath(program).name in _POSIX_SHELLS and arguments in {("-c",), ("-lc",)}
    return (
        PureWindowsPath(program).name.lower() in _POWERSHELLS
        and arguments in (POWERSHELL_ARGUMENTS, _LEGACY_POWERSHELL_ARGUMENTS)
        and backend == "host"
    )


def native_owner_problem(capability: ProcessCapabilityProbe | None, backend: str) -> str | None:
    """Windows只有沙箱外命令；其他平台须为POSIX Process Owner。"""

    if capability is None:
        return "process_owner_unavailable"
    if capability.platform == "windows":
        return None if backend == "host" else "native_backend_platform_mismatch"
    return None if capability.platform == "posix" else "process_owner_unavailable"


def windows_powershell(environ: Mapping[str, str]) -> str | None:
    """优先PATH中的PowerShell 7，否则用系统自带的Windows PowerShell 5.1。"""

    found = shutil.which("pwsh.exe", path=environ.get("PATH"))
    if found:
        return str(Path(found).resolve())
    root = environ.get("SYSTEMROOT") or environ.get("SystemRoot")
    if not root:
        return None
    candidate = PureWindowsPath(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return str(candidate) if Path(candidate).is_file() else None
