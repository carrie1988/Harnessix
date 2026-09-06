"""固定Git只读命令；不接受模型命令、路径或配置注入。"""

from __future__ import annotations

import hashlib
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


class GitReadRuntime:
    """对一个明确仓库根和一个受信Git可执行文件提供两项只读能力。"""

    def __init__(self, root: Path, executable: Path) -> None:
        self._root = root.resolve(strict=True)
        if any(ord(character) < 32 or ord(character) == 127 for character in str(self._root)):
            raise KernelError("git_workspace_denied", "Git工作区路径包含控制字符")
        self._executable = executable
        sample = self._runtime()
        self._binding_fingerprint = sample.binding_fingerprint

    def contract(self) -> dict[str, object]:
        return {
            "implementation": "git-read/v1",
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
        }

    def _runtime(self) -> HostProcessRuntime:
        # 延迟导入避免processes.runtime复用tools.runtime._drain时形成初始化环。
        from harnessix.processes.runtime import HostProcessRuntime

        return HostProcessRuntime(
            self._root,
            {"git": self._executable},
            environment=_ENVIRONMENT,
            limits=ProcessLimits(
                max_timeout_seconds=_TIMEOUT_SECONDS,
                stdout_bytes=MAX_CAPTURE_BYTES,
                stderr_bytes=_STDERR_BYTES,
                stop_output_bytes=8 * MAX_CAPTURE_BYTES,
            ),
        )

    async def execute(
        self, args: GitStatusInput | GitDiffInput, cancel: CancelToken
    ) -> ReadContract:
        await self._require_repository_root(cancel)
        if isinstance(args, GitStatusInput):
            result = await self._run(
                (
                    *_GLOBAL_ARGUMENTS,
                    "status",
                    "--porcelain=v2",
                    "--branch",
                    "--untracked-files=all",
                    "-z",
                ),
                cancel,
            )
            return _status(result, args.limit)
        diff_args = [
            *_GLOBAL_ARGUMENTS,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            f"--unified={args.context_lines}",
        ]
        if args.target == "staged":
            diff_args.append("--cached")
        diff_args.append("--")
        result = await self._run(tuple(diff_args), cancel)
        return _diff(result, args.target)

    async def _require_repository_root(self, cancel: CancelToken) -> None:
        result = await self._run(
            (*_GLOBAL_ARGUMENTS, "rev-parse", "--show-toplevel"), cancel, repository_check=True
        )
        try:
            root = result.stdout.data().decode("utf-8", errors="strict")
        except UnicodeError:
            raise ReadToolError("invalid_utf8") from None
        if root.removesuffix("\n") != str(self._root):
            raise ReadToolError("path_denied")

    async def _run(
        self,
        arguments: tuple[str, ...],
        cancel: CancelToken,
        *,
        repository_check: bool = False,
    ) -> ProcessResult:
        async with self._runtime() as runtime:
            result = await runtime.run(
                ProcessRequest(
                    program="git", arguments=arguments, timeout_seconds=_TIMEOUT_SECONDS
                ),
                cancel,
            )
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
