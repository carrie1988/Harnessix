"""固定Git只读命令；不接受模型命令、路径或配置注入。"""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.processes.contracts import (
    MAX_CAPTURE_BYTES,
    ProcessLimits,
    ProcessRequest,
    ProcessResult,
)
from harnessix.tools.contracts import MAX_RESULT_BYTES, ReadContract, ReadToolError
from harnessix.tools.git_contracts import (
    MAX_GIT_DIFF_TEXT_BYTES,
    GitDiffInput,
    GitDiffOutput,
    GitStatusEntry,
    GitStatusInput,
    GitStatusOutput,
)

if TYPE_CHECKING:
    from harnessix.processes.git_read_windows import WindowsGitReadProcess
    from harnessix.processes.owner_protocol import OutputRedactionSource
    from harnessix.processes.runtime import HostProcessRuntime

_TIMEOUT_SECONDS: Final = 5.0
_STDERR_BYTES: Final = 16 * 1024
_ENVIRONMENT: Final = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_LITERAL_PATHSPECS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}
_GLOBAL_ARGUMENTS: Final = (
    "--no-pager",
    "--no-optional-locks",
    "-c",
    "color.ui=false",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def _git_arguments(*, for_delivery: bool) -> tuple[str, ...]:
    """按原生平台冻结参数；交付基准目的不改变原默认读取合同。"""
    arguments: tuple[str, ...] = _GLOBAL_ARGUMENTS
    if os.name == "nt":
        from harnessix.processes.git_read_windows import WINDOWS_GIT_ARGUMENTS

        arguments = WINDOWS_GIT_ARGUMENTS
    if for_delivery:
        arguments += (
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.attributesFile=" + os.devnull,
            "-c",
            "submodule.recurse=false",
        )
    return arguments


class GitReadRuntime:
    """对一个明确仓库根和一个受信Git可执行文件提供两项只读能力。"""

    def __init__(
        self,
        root: Path,
        executable: Path,
        *,
        state_directory: Path | None = None,
        output_redaction: OutputRedactionSource | None = None,
        for_delivery: bool = False,
    ) -> None:
        self._root = root.absolute() if os.name == "nt" else root.resolve(strict=True)
        if any(ord(character) < 32 or ord(character) == 127 for character in str(self._root)):
            raise KernelError("git_workspace_denied", "Git工作区路径包含控制字符")
        self._executable = executable
        self._state_directory = state_directory
        self._output_redaction = output_redaction
        self._for_delivery = for_delivery
        self._global_arguments = _git_arguments(for_delivery=for_delivery)
        sample = self._runtime()
        self._binding_fingerprint = sample.binding_fingerprint

    def contract(self) -> dict[str, object]:
        return {
            "implementation": (
                "git-baseline-read/v1"
                if self._for_delivery
                else "git-read/windows-job-v1"
                if os.name == "nt"
                else "git-read/v1"
            ),
            "binding": self._binding_fingerprint,
            "timeout_seconds": _TIMEOUT_SECONDS,
            "max_capture_bytes": MAX_CAPTURE_BYTES,
            "max_diff_text_bytes": MAX_GIT_DIFF_TEXT_BYTES,
            "max_result_bytes": MAX_RESULT_BYTES,
            "pager": False,
            "optional_locks": False,
            "external_diff": False,
            "textconv": False,
            "fsmonitor": False,
            "executable_filters": False,
            "configuration_includes": False,
            "submodule_queries": False,
        }

    def _runtime(self) -> HostProcessRuntime | WindowsGitReadProcess:
        return _build_git_process(
            self._root,
            self._executable,
            self._state_directory,
            self._output_redaction,
            for_delivery=self._for_delivery,
        )

    async def execute(
        self, args: GitStatusInput | GitDiffInput, cancel: CancelToken
    ) -> ReadContract:
        await self._require_repository_root(cancel)
        await _reject_git_helpers(self, cancel)
        if isinstance(args, GitStatusInput):
            result = await self._run(
                (
                    *self._global_arguments,
                    "status",
                    "--porcelain=v2",
                    "--branch",
                    "--untracked-files=all",
                    "--ignore-submodules=all",
                    "-z",
                ),
                cancel,
            )
            return _status(result, args.limit)
        diff_args = [
            *self._global_arguments,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            f"--unified={args.context_lines}",
        ]
        if args.target == "staged":
            diff_args.append("--cached")
        diff_args.append("--")
        result = await self._run(tuple(diff_args), cancel)
        return _diff(result, args.target)

    async def _require_repository_root(self, cancel: CancelToken) -> None:
        result = await self._run(
            (*self._global_arguments, "rev-parse", "--show-toplevel"), cancel, repository_check=True
        )
        try:
            root = result.stdout.data().decode("utf-8", errors="strict")
        except UnicodeError:
            raise ReadToolError("invalid_utf8") from None
        if not repository_root_matches(root, str(self._root), windows=os.name == "nt"):
            raise ReadToolError("path_denied")

    async def _run(
        self,
        arguments: tuple[str, ...],
        cancel: CancelToken,
        *,
        repository_check: bool = False,
    ) -> ProcessResult:
        return await _run_git_process(
            self._runtime(), self._binding_fingerprint, arguments, cancel, repository_check
        )


async def _run_git_process(
    process: HostProcessRuntime | WindowsGitReadProcess,
    fingerprint: str,
    arguments: tuple[str, ...],
    cancel: CancelToken,
    repository_check: bool,
) -> ProcessResult:
    async with process as runtime:
        if runtime.binding_fingerprint != fingerprint:
            raise KernelError("process_binding_changed", "Git只读宿主绑定已变化")
        try:
            result = await runtime.run(
                ProcessRequest(
                    program="git", arguments=arguments, timeout_seconds=_TIMEOUT_SECONDS
                ),
                cancel,
            )
        except ReadToolError as error:
            if error.code == "not_found" and not repository_check:
                raise ReadToolError("io_failed") from None
            raise
    if result.stop_reason == "cancelled":
        raise TurnCancelled
    if result.stop_reason == "timeout":
        raise ReadToolError("timeout")
    if (
        result.stop_reason != "exited"
        or result.returncode != 0
        or not result.stdout.eof
        or not result.stderr.eof
    ):
        if repository_check:
            raise ReadToolError("not_found")
        raise ReadToolError("io_failed")
    return result


async def _reject_git_helpers(runtime: GitReadRuntime, cancel: CancelToken) -> None:
    # 只读取配置键名，避免把HTTP Header等配置值持久写入Process输出。
    result = await runtime._run(  # noqa: SLF001 - 固定查询是同一Git端口的前置条件
        (*runtime._global_arguments, "config", "--no-includes", "--null", "--name-only", "--list"),
        cancel,
    )
    if result.stdout.truncated:
        raise ReadToolError("limit_exceeded")
    if any(_git_helper_key(key) for key in result.stdout.data().split(b"\0")):
        raise ReadToolError("path_denied")


def _git_helper_key(key: bytes) -> bool:
    """配置只读取键名；首检与交付双观察共用同一帮助器拒绝规则。"""
    return (
        re.match(rb"^(?:include(?:if)?\.|filter\..*\.(?:clean|smudge|process)$)", key.lower())
        is not None
    )


def _build_git_process(
    root: Path,
    executable: Path,
    state_directory: Path | None,
    output_redaction: OutputRedactionSource | None,
    *,
    for_delivery: bool = False,
) -> HostProcessRuntime | WindowsGitReadProcess:
    if os.name == "nt":
        from harnessix.processes.git_read_windows import WindowsGitReadProcess

        return WindowsGitReadProcess(
            root, executable, state_directory, output_redaction, for_delivery=for_delivery
        )
    # 延迟导入避免processes.runtime复用tools.runtime._drain时形成初始化环。
    from harnessix.processes.runtime import HostProcessRuntime

    return HostProcessRuntime(
        root,
        {"git": executable},
        environment={
            **_ENVIRONMENT,
            **(
                {
                    "GIT_NO_REPLACE_OBJECTS": "1",
                    "GIT_NO_LAZY_FETCH": "1",
                    "GIT_ALLOW_PROTOCOL": "",
                }
                if for_delivery
                else {}
            ),
        },
        limits=ProcessLimits(
            max_timeout_seconds=_TIMEOUT_SECONDS,
            stdout_bytes=MAX_CAPTURE_BYTES,
            stderr_bytes=_STDERR_BYTES,
            stop_output_bytes=(9 if for_delivery else 8) * MAX_CAPTURE_BYTES,
        ),
    )


def repository_root_matches(observed: str, expected: str, *, windows: bool) -> bool:
    """只移除一个行结束符；Windows容纳Git正斜杠和大小写，不接受父仓库。"""
    value = observed[:-2] if windows and observed.endswith("\r\n") else observed.removesuffix("\n")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    if windows:
        return ntpath.isabs(value) and ntpath.normcase(ntpath.normpath(value)) == ntpath.normcase(
            ntpath.normpath(expected)
        )
    return value == expected


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ReadToolError("invalid_utf8") from None


def _status(result: ProcessResult, limit: int) -> GitStatusOutput:
    raw = result.stdout.data()
    if result.stdout.truncated:
        raise ReadToolError("limit_exceeded")
    branch = head_oid = upstream = None
    ahead = behind = None
    entries: list[GitStatusEntry] = []
    records = raw.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        text = _decode(record)
        if text.startswith("# branch.oid "):
            value = text.removeprefix("# branch.oid ")
            head_oid = None if value == "(initial)" else value
        elif text.startswith("# branch.head "):
            value = text.removeprefix("# branch.head ")
            branch = None if value == "(detached)" else value
        elif text.startswith("# branch.upstream "):
            upstream = text.removeprefix("# branch.upstream ")
        elif text.startswith("# branch.ab "):
            values = text.removeprefix("# branch.ab ").split(" ")
            if len(values) != 2 or not values[0].startswith("+") or not values[1].startswith("-"):
                raise ReadToolError("io_failed")
            try:
                ahead, behind = int(values[0][1:]), int(values[1][1:])
            except ValueError:
                raise ReadToolError("io_failed") from None
        elif text.startswith("1 "):
            fields = text.split(" ", 8)
            if len(fields) != 9:
                raise ReadToolError("io_failed")
            entries.append(_entry(fields[8], "ordinary", fields[1], fields[2]))
        elif text.startswith("2 "):
            fields = text.split(" ", 9)
            if len(fields) != 10 or index >= len(records):
                raise ReadToolError("io_failed")
            original = _decode(records[index])
            index += 1
            entries.append(_entry(fields[9], "renamed", fields[1], fields[2], original))
        elif text.startswith("u "):
            fields = text.split(" ", 10)
            if len(fields) != 11:
                raise ReadToolError("io_failed")
            entries.append(_entry(fields[10], "unmerged", fields[1], fields[2]))
        elif text.startswith("? "):
            entries.append(_entry(text[2:], "untracked", "??", None))
        elif not text.startswith("! "):
            raise ReadToolError("io_failed")
    selected: list[GitStatusEntry] = []
    for entry in entries[:limit]:
        candidate = GitStatusOutput(
            branch=branch,
            head_oid=head_oid,
            upstream=upstream,
            ahead=ahead,
            behind=behind,
            entries=(*selected, entry),
            total_entries=len(entries),
            truncated=len(entries) > len(selected) + 1,
            revision=hashlib.sha256(raw).hexdigest(),
        )
        if len(candidate.model_dump_json().encode()) > MAX_RESULT_BYTES:
            break
        selected.append(entry)
    return GitStatusOutput(
        branch=branch,
        head_oid=head_oid,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        entries=tuple(selected),
        total_entries=len(entries),
        truncated=len(entries) > len(selected),
        revision=hashlib.sha256(raw).hexdigest(),
    )


def _entry(
    path: str,
    kind: Literal["ordinary", "renamed", "unmerged", "untracked"],
    xy: str,
    submodule: str | None,
    original_path: str | None = None,
) -> GitStatusEntry:
    if len(xy) != 2:
        raise ReadToolError("io_failed")
    try:
        return GitStatusEntry(
            path=path,
            original_path=original_path,
            kind=kind,
            index_status=xy[0],
            worktree_status=xy[1],
            submodule=submodule,
        )
    except ValueError:
        raise ReadToolError("limit_exceeded") from None


def _diff(result: ProcessResult, target: Literal["worktree", "staged"]) -> GitDiffOutput:
    data = result.stdout.data()
    prefix = data[:MAX_GIT_DIFF_TEXT_BYTES]
    while True:
        try:
            text = prefix.decode("utf-8", errors="strict")
            break
        except UnicodeDecodeError as error:
            if error.reason != "unexpected end of data" or error.start < len(prefix) - 4:
                raise ReadToolError("invalid_utf8") from None
            prefix = prefix[: error.start]
    return GitDiffOutput(
        target=target,
        text=text,
        utf8_bytes=len(prefix),
        observed_bytes=result.stdout.observed_bytes,
        observed_sha256=result.stdout.observed_sha256,
        truncated=result.stdout.observed_bytes > len(prefix),
    )
