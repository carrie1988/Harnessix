"""命令账本：从Thread持久工具事实推导三类宿主已经观察到、模型却常误判的事实（ADR 0135）。

- 回归：同一条Shell命令上一次运行以0退出，之后有补丁成功，这一次以非零退出；
- 补丁反复：同一份补丁内容第二次被回滚；
- 原地重复：同一条命令第三次得到相同退出码与逐字节相同的输出，其间没有补丁成功。

说明只陈述事实（哪一步通过、其间有哪些补丁），不判断因果，也不建议怎么修。推导只读本Thread
各Turn的原始Item，不读模型历史视图，压缩前后结论相同；每条事实只在它成立的那一步给出一次：
回归只看紧邻的上一次运行，所以命令重新通过之前不会重复；另两类只在次数恰好到阈值时给出。

非职责：不读取命令输出正文（退出码被管道或`; echo`掩盖的检查不在此判定），不观察Shell命令
自己对工作区的修改，不强制回滚。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple
from uuid import UUID

from harnessix.agent.models import (
    ItemStatus,
    TextContent,
    Thread,
    ToolCallContent,
    ToolCallRejectionContent,
    ToolResultContent,
    Turn,
)
from harnessix.delivery.rollback_action import WORKSPACE_ROLLBACK_TOOL
from harnessix.delivery.trusted_action import WORKSPACE_PATCH_TOOL

REPEATED_RUN_THRESHOLD = 3
REPEATED_ROLLBACK_THRESHOLD = 2
_SHELL_TOOL_PREFIXES = ("exec_command.", "host_command.")
_MAX_LISTED_PATCHES = 3
_MAX_LISTED_PATHS = 2
_MAX_PATH_CHARS = 60
_MAX_COMMAND_CHARS = 200


def command_ledger_notices(verified_tools: Collection[str]) -> CommandLedgerNotices | None:
    """账本只对Shell命令成立：没有通过验证的命令工具时不启用（固定Profile流程另有门禁说明）。"""
    if not any(tool.startswith(_SHELL_TOOL_PREFIXES) for tool in verified_tools):
        return None
    return CommandLedgerNotices(rollback_available=WORKSPACE_ROLLBACK_TOOL in verified_tools)


class _Position(NamedTuple):
    """Turn在Thread中的序号，以及该Turn内模型第几次给出响应（没有任何Item的响应不计）。"""

    turn: int
    response: int


class _Settled(NamedTuple):
    position: _Position
    call: ToolCallContent
    result: ToolResultContent


@dataclass(frozen=True, slots=True)
class _Patch:
    """一次成功的工作区补丁或回滚。"""

    transaction_id: str | None
    diff_sha256: str | None
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Run:
    """某条命令最近一次有退出码的运行。"""

    position: _Position
    returncode: int
    output_sha256: tuple[str, str] | None
    patches_before: int
    # 含本次在内，退出码与输出都相同、其间没有补丁的连续运行次数。
    identical_runs: int


@dataclass(frozen=True, slots=True)
class CommandLedgerNotices:
    """ToolStepNoticeSource的产品实现，由command_ledger_notices按已验证能力创建。"""

    # 回滚工具已验证可用时，回归说明才提到它。
    rollback_available: bool = False

    def step_notice(self, thread: Thread, turn: Turn) -> str | None:
        settled, current = _settled_calls(thread, turn)
        ledger = _Ledger(current, turn.model_steps, self.rollback_available)
        facts = [fact for each in settled if (fact := ledger.observe(each)) is not None]
        return "\n".join(facts) or None


def _settled_calls(thread: Thread, turn: Turn) -> tuple[list[_Settled], _Position]:
    """本Thread到当前Turn为止已结算的调用（按落账顺序）与当前步骤的位置。

    不含Fork继承的历史：分叉点之后源Thread对工作区的修改不在本Thread的账上。
    """
    settled: list[_Settled] = []
    current = _Position(0, 0)
    for turn_index, each in enumerate(thread.turns):
        calls: dict[UUID, ToolCallContent] = {}
        response, in_response = 0, False
        for item in each.items:
            if item.status is not ItemStatus.COMPLETED:
                continue
            content = item.content
            from_model = isinstance(content, ToolCallContent | ToolCallRejectionContent) or (
                isinstance(content, TextContent) and content.kind != "user_message"
            )
            if from_model and not in_response:
                response += 1
            in_response = from_model
            if isinstance(content, ToolCallContent):
                calls[content.call_id] = content
            elif isinstance(content, ToolResultContent) and content.call_id in calls:
                settled.append(
                    _Settled(_Position(turn_index, response), calls[content.call_id], content)
                )
        current = _Position(turn_index, response)
        if each.turn_id == turn.turn_id:
            break
    return settled, current


@dataclass(slots=True)
class _Ledger:
    """按落账顺序逐个观察已结算调用；只为current这一步的结果生成说明。"""

    current: _Position
    current_step: int
    rollback_available: bool
    patches: list[_Patch] = field(default_factory=list)
    applied: dict[str, _Patch] = field(default_factory=dict)
    rollbacks: Counter[str] = field(default_factory=Counter)
    runs: dict[tuple[str, str], _Run] = field(default_factory=dict)

    def observe(self, settled: _Settled) -> str | None:
        if settled.call.tool in (WORKSPACE_PATCH_TOOL, WORKSPACE_ROLLBACK_TOOL):
            return self._patched(settled)
        if settled.call.tool.startswith(_SHELL_TOOL_PREFIXES):
            return self._ran(settled)
        return None

    def _patched(self, settled: _Settled) -> str | None:
        position, call, result = settled
        output = result.output
        if result.outcome != "succeeded" or not isinstance(output, dict):
            return None
        patch = _Patch(
            _lowercase_text(output.get("transaction_id")),
            _lowercase_text(output.get("diff_sha256")),
            _changed_paths(output),
        )
        self.patches.append(patch)
        if call.tool == WORKSPACE_PATCH_TOOL:
            if patch.transaction_id is not None:
                self.applied[patch.transaction_id] = patch
            return None
        original = self.applied.get(_lowercase_text(call.arguments.get("transaction_id")) or "")
        if original is None or original.diff_sha256 is None:
            return None
        self.rollbacks[original.diff_sha256] += 1
        times = self.rollbacks[original.diff_sha256]
        if position != self.current or times != REPEATED_ROLLBACK_THRESHOLD:
            return None
        return (
            f"The same patch to {_listed_paths(original.paths)} has now been applied and rolled "
            f"back {times} times in this thread; its diff was identical each time."
        )

    def _ran(self, settled: _Settled) -> str | None:
        position, call, result = settled
        command = call.arguments.get("cmd")
        exited = _exit(result)
        if not isinstance(command, str) or exited is None:
            return None
        returncode, output_sha256 = exited
        key = (call.tool, _normalised(command))
        previous = self.runs.get(key)
        since = self.patches[previous.patches_before :] if previous is not None else []
        identical_runs = 1
        if (
            previous is not None
            and not since
            and output_sha256 is not None
            and (previous.returncode, previous.output_sha256) == (returncode, output_sha256)
        ):
            identical_runs = previous.identical_runs + 1
        patches_before = len(self.patches)
        self.runs[key] = _Run(position, returncode, output_sha256, patches_before, identical_runs)
        if position != self.current:
            return None
        if previous is not None and previous.returncode == 0 and returncode != 0 and since:
            return self._regression(command, previous.position, returncode, since)
        if identical_runs == REPEATED_RUN_THRESHOLD:
            return (
                f"`{_excerpt(command)}` has now run {identical_runs} times with exit code "
                f"{returncode} and byte-identical output; no workspace patch was applied between "
                "these runs."
            )
        return None

    def _regression(
        self, command: str, passed: _Position, returncode: int, since: Sequence[_Patch]
    ) -> str:
        fact = (
            f"`{_excerpt(command)}` exited 0 {self._when(passed)} and exits {returncode} now "
            f"(step {self.current_step}). Workspace patches applied in between: "
            f"{_listed_patches(since)}."
        )
        latest = since[-1].transaction_id
        if self.rollback_available and latest is not None:
            fact += (
                " The latest one can be narrowed with another patch or undone with "
                f"{WORKSPACE_ROLLBACK_TOOL} (transaction_id={latest})."
            )
        return fact

    def _when(self, position: _Position) -> str:
        if position.turn != self.current.turn:
            return "in an earlier turn"
        return f"at step {self.current_step - (self.current.response - position.response)}"


def _exit(result: ToolResultContent) -> tuple[int, tuple[str, str] | None] | None:
    """退出码与stdout/stderr观察摘要；超时、被终止或启动失败不是一次退出，返回None。"""
    output = result.output
    if not isinstance(output, dict) or output.get("stop_reason") != "exited":
        return None
    returncode = output.get("returncode")
    if type(returncode) is not int:
        return None
    stdout, stderr = (_stream_sha256(output.get(name)) for name in ("stdout", "stderr"))
    return returncode, (stdout, stderr) if stdout is not None and stderr is not None else None


def _stream_sha256(stream: object) -> str | None:
    return _lowercase_text(stream.get("observed_sha256")) if isinstance(stream, dict) else None


def _lowercase_text(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and value else None


def _changed_paths(output: Mapping[str, object]) -> tuple[str, ...]:
    changes = output.get("changes")
    if not isinstance(changes, list):
        return ()
    return tuple(
        path
        for change in changes
        if isinstance(change, dict) and isinstance(path := change.get("path"), str)
    )


def _normalised(command: str) -> str:
    """同一条命令的判定：忽略首尾空白、行尾空白与换行符差异；行内空白可能有语义，不折叠。"""
    return "\n".join(line.rstrip() for line in command.strip().splitlines())


def _excerpt(command: str) -> str:
    """单行、有界的命令摘录；过长时保留首尾，检查本身通常写在命令末尾。"""
    text = " ".join(command.split())
    if len(text) <= _MAX_COMMAND_CHARS:
        return text
    tail = _MAX_COMMAND_CHARS // 3
    return f"{text[: _MAX_COMMAND_CHARS - tail]} … {text[-tail:]}"


def _listed_paths(paths: Sequence[str]) -> str:
    shown = [
        path if len(path) <= _MAX_PATH_CHARS else "…" + path[-_MAX_PATH_CHARS:]
        for path in paths[:_MAX_LISTED_PATHS]
    ]
    if len(paths) > len(shown):
        shown.append(f"+{len(paths) - len(shown)} more")
    return ", ".join(shown) or "unknown files"


def _listed_patches(patches: Sequence[_Patch]) -> str:
    """按应用顺序列出最近几次补丁；更早的只报数量。"""
    shown = patches[-_MAX_LISTED_PATCHES:]
    listed = "; ".join(
        f"transaction_id={patch.transaction_id} ({_listed_paths(patch.paths)})"
        if patch.transaction_id is not None
        else f"({_listed_paths(patch.paths)})"
        for patch in shown
    )
    omitted = len(patches) - len(shown)
    return f"{omitted} earlier, then {listed}" if omitted else listed
