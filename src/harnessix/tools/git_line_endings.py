"""用户实际生效的换行设置：Git只读查询按用户的core.autocrlf与core.eol比较工作区与索引。

产品的Git读取隔离系统级与用户级配置（ADR 0043）。Git for Windows安装时在系统配置里写入
`core.autocrlf=true`，工作区是CRLF、对象是LF；隔离后git_status与git_diff会把整个文件报成每行都改了。
这里单独执行一次只读的`git config`，能看到系统级、用户级与仓库配置，但只取这两个键，不展开include；
得到的值以`-c`传给状态与差异查询，其余配置照旧隔离，Hook与过滤器照旧禁用。

职责：探测命令与环境、执行一次探测、解析输出、生成`-c`参数。
非职责：构建进程端口（见tools.git），交付提交（ADR 0068）。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Final, Protocol

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.processes.contracts import ProcessRequest, ProcessResult
from harnessix.tools.contracts import ReadToolError

# 另匹配git init总会写入的core.repositoryformatversion：两个换行键都没设置时也有输出、以0退出，
# Windows端口不必把“没有设置”当成读取失败。解析时忽略它。
LINE_ENDING_PROBE: Final = (
    "config",
    "--no-includes",
    "--null",
    "--get-regexp",
    r"^core\.(autocrlf|eol|repositoryformatversion)$",
)
_TRUE: Final = frozenset({"true", "yes", "on", "1"})
_FALSE: Final = frozenset({"false", "no", "off", "0", ""})
_EOL: Final = frozenset({"lf", "crlf", "native"})


class _GitProcess(Protocol):
    async def run(self, request: ProcessRequest, cancel: CancelToken) -> ProcessResult: ...


async def probe_line_endings(
    process: AbstractAsyncContextManager[_GitProcess],
    global_arguments: tuple[str, ...],
    timeout_seconds: float,
    cancel: CancelToken,
) -> tuple[str, ...] | None:
    """执行一次探测并返回`-c`参数；两个键都没设置时为空元组。

    探测不成立（超时、输出不完整、其他读取失败）时返回None：调用方按隔离读取的原行为继续，
    下次读取再探测。取消照常向上传播。
    """
    request = ProcessRequest(
        program="git",
        arguments=(*global_arguments, *LINE_ENDING_PROBE),
        timeout_seconds=timeout_seconds,
    )
    async with process as bound:
        try:
            result = await bound.run(request, cancel)
        except ReadToolError as error:
            # Windows端口把非零退出报成not_found；两个键都没设置时git config以1退出。
            return () if error.code == "not_found" else None
    if result.stop_reason == "cancelled":
        raise TurnCancelled
    if (
        result.stop_reason != "exited"
        or result.termination != "none"
        or not result.stdout.eof
        or result.stdout.truncated
    ):
        return None
    body = result.stdout.data()
    if result.returncode == 1 and not body:
        return ()
    if result.returncode != 0:
        return None
    return line_ending_arguments(body)


class UserLineEndings:
    """一个Git读取端口的换行设置：首次成功探测后在端口生命周期内复用，探测不成立时下次再试。"""

    def __init__(
        self,
        process: Callable[[], AbstractAsyncContextManager[_GitProcess]],
        global_arguments: tuple[str, ...],
        timeout_seconds: float,
    ) -> None:
        self._process = process
        self._global_arguments = global_arguments
        self._timeout_seconds = timeout_seconds
        self._arguments: tuple[str, ...] | None = None

    async def arguments(self, cancel: CancelToken) -> tuple[str, ...]:
        """状态与差异查询前追加的`-c`参数；探测不成立时本次为空。"""
        if self._arguments is None:
            probed = await probe_line_endings(
                self._process(), self._global_arguments, self._timeout_seconds, cancel
            )
            if probed is None:
                return ()
            self._arguments = probed
        return self._arguments


def probe_environment(
    isolated: Mapping[str, str], *, windows: bool, source: Mapping[str, str] = os.environ
) -> dict[str, str]:
    """探测用的环境：不再关闭系统级配置，用户级配置经GIT_CONFIG_GLOBAL指向宿主解析出的文件。

    不传HOME等变量（Process环境策略拒绝它们）；没有用户级配置文件时仍指向空设备。
    """
    environment = {name: value for name, value in isolated.items() if name != "GIT_CONFIG_NOSYSTEM"}
    configured = global_config_path(windows=windows, source=source)
    if configured is not None:
        environment["GIT_CONFIG_GLOBAL"] = str(configured)
    return environment


def global_config_path(*, windows: bool, source: Mapping[str, str] = os.environ) -> Path | None:
    """Git读取的用户级配置文件：优先`~/.gitconfig`，否则XDG位置的`git/config`；都不存在时为None。

    两者同时存在时Git会都读，这里只取优先级更高的`~/.gitconfig`；只写在XDG文件里的换行设置此时不生效。
    """
    home = _home(windows=windows, source=source)
    if home is None:
        return None
    xdg = source.get("XDG_CONFIG_HOME")
    xdg_root = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".config"
    for candidate in (home / ".gitconfig", xdg_root / "git" / "config"):
        text = str(candidate)
        if not any(ord(character) < 32 for character in text) and candidate.is_file():
            return candidate
    return None


def _home(*, windows: bool, source: Mapping[str, str]) -> Path | None:
    """按Git的规则定位用户主目录；Git for Windows依次看HOME、HOMEDRIVE+HOMEPATH、USERPROFILE。"""
    candidates = [source.get("HOME")]
    if windows:
        drive, path = source.get("HOMEDRIVE"), source.get("HOMEPATH")
        candidates += [drive + path if drive and path else None, source.get("USERPROFILE")]
    for value in candidates:
        if value and Path(value).is_absolute() and Path(value).is_dir():
            return Path(value)
    return None


def line_ending_arguments(stdout: bytes) -> tuple[str, ...]:
    """解析`--null --get-regexp`输出（键与值以换行分隔，记录以NUL结束），后出现的同名键生效。

    只接受Git认可的取值；无法识别的值按未设置处理，与隔离读取的原行为一致。
    """
    values: dict[str, str] = {}
    for record in stdout.split(b"\0"):
        key, separator, raw = record.partition(b"\n")
        name = key.decode("ascii", errors="replace").lower()
        if name not in {"core.autocrlf", "core.eol"}:
            continue
        # 只写键名的布尔项（`[core] autocrlf`）按真值处理，输出里没有换行和值。
        value = raw.decode("utf-8", errors="replace") if separator else "true"
        values[name] = value.strip().lower()
    arguments: list[str] = []
    autocrlf = _autocrlf(values.get("core.autocrlf"))
    if autocrlf is not None:
        arguments += ["-c", f"core.autocrlf={autocrlf}"]
    eol = values.get("core.eol")
    if eol in _EOL:
        arguments += ["-c", f"core.eol={eol}"]
    return tuple(arguments)


def _autocrlf(value: str | None) -> str | None:
    if value is None:
        return None
    if value == "input":
        return "input"
    if value in _TRUE:
        return "true"
    if value in _FALSE:
        return "false"
    return None
