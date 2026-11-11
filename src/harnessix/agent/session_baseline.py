"""会话基线：会话开始前工作区已有的未提交改动由宿主记录、告知模型，并在Turn完成前复核。

用户常带着未提交的修改开始会话，任务环境也可能在开始前改过工作区。模型在git status里看到这些
改动时无从判断来历，曾在收尾“清理”时把它们还原（一次评测中模型还原了环境预先改好的测试配置，
源码修复正确却整题失败；同批另有11次没有后果的误还原）。哪些改动先于会话存在，是宿主在第一个
模型步骤之前就能确定的事实，由宿主直接告知，不留给模型猜：

- Thread的第一个模型步骤之前，经git_status所用的同一Git只读端口观察一次工作区，把已有的未提交
  改动与未跟踪文件写成一条宿主说明（用户消息Item），随初始输入进入模型上下文；
- 说明的末行是一条JSON记录，宿主此后只读这一行：基线随Thread历史持久、重放确定，不另设存储，
  也不向Git对象库写入快照；
- git_status、git_review结果里属于基线的条目标记pre_existing；
- Turn将要完成时复核基线条目，已被还原或删除的补发一次宿主说明，让模型恢复。每个Turn至多一次，
  每个路径在整个Thread里只提示一次：用户明确要求的改动只多花一步，不循环、不逐轮重复。

不是Git仓库、未绑定Git、读取失败或工作区干净时不落任何事件，行为与未启用时相同。基线只记录
路径与状态码，不保存文件内容；宿主不替模型恢复文件。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import JsonValue

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.completion_guard import host_notice_events
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    EventDraft,
    TextContent,
    Thread,
    ToolResultContent,
    Turn,
    TurnStatus,
)
from harnessix.agent.reducer_support import get_turn
from harnessix.domain.models import ContractModel
from harnessix.tools.contracts import MAX_RESULT_BYTES, ReadToolError
from harnessix.tools.git_contracts import GitStatusEntry, GitStatusOutput

SESSION_BASELINE_VERSION: Literal["harnessix.session-baseline/v1"] = "harnessix.session-baseline/v1"
BASELINE_LOSS_VERSION: Literal["harnessix.session-baseline-loss/v1"] = (
    "harnessix.session-baseline-loss/v1"
)
# 记录进入模型上下文，压缩时又作为当前Turn的用户消息整条保留（计入小窗口模型的保留预算），
# 复核时这些路径还要全部放进一条Git命令行，条目数和字节数都必须有界。
# Git先列已跟踪文件的改动、后列未跟踪文件，截断时留下的是无法重新生成的那一部分。
MAX_BASELINE_ENTRIES = 64
MAX_BASELINE_RECORD_BYTES = 4 * 1024
MARKED_GIT_TOOLS = ("git_status", "git_review")

_ANNOUNCEMENT = (
    "[Harnessix host notice] Before this session started, the workspace already had the "
    "uncommitted changes recorded below (git status --short codes; ?? is an untracked file). "
    "They are not your changes. Leave these files as they are: do not revert, delete or clean "
    "them up, unless the user asks you to change them."
)
_ANNOUNCEMENT_INCOMPLETE = (
    " The record lists only the first {listed} entries: treat any other modified or untracked "
    "file that you did not create or change yourself as pre-existing too."
)
_ANNOUNCEMENT_UNLISTED = (
    "[Harnessix host notice] Before this session started, the workspace already had uncommitted "
    "changes, too many to list. They are not your changes. Leave every modified or untracked "
    "file that you did not create or change yourself as it is: do not revert, delete or clean "
    "it up, unless the user asks you to change it."
)
_LOSS_NOTICE = (
    "[Harnessix host notice] Your last response had no tool call, so it would end the task, but "
    "the files recorded below had uncommitted changes before this session started and those "
    "changes are gone now: the files were reverted or deleted. If you did that, restore each "
    "file to the content it had before the session (for example re-apply the diff you saw, or "
    "run git stash pop if you stashed it), then reply with your final summary. If the user "
    "asked for this change, or a file cannot be restored, leave it and say so in the summary."
)
_PRE_EXISTING_NOTE = (
    "Entries with pre_existing=true already had uncommitted changes before this session "
    "started. They are not your changes: leave them as they are unless the user asks. A diff "
    "of such a file also shows those earlier changes."
)
_PRE_EXISTING_INCOMPLETE = (
    "The host's record of changes that existed before this session is incomplete: an entry "
    "that you did not create or change yourself may be pre-existing too, so leave it as it is "
    "unless the user asks."
)


class WorkspaceChangeObserver(Protocol):
    """宿主观察工作区未提交改动的端口；产品由CodingToolRuntime经固定Git只读端口实现。"""

    async def workspace_changes(
        self, cancel: CancelToken, *, paths: tuple[str, ...] = ()
    ) -> GitStatusOutput | None:
        """相对HEAD的未提交改动与未跟踪文件，paths非空时只看这些路径；没有Git时为None。

        读取失败抛出ReadToolError、KernelError或OSError，其中改动多到超出读取上限是
        ReadToolError("limit_exceeded")。
        """
        ...


class SessionBaseline(ContractModel):
    """宿主说明末行的记录：changes逐条是“XY 路径”，即git status --short的写法。"""

    spec_version: Literal["harnessix.session-baseline/v1"] = SESSION_BASELINE_VERSION
    head_oid: str | None = None
    changes: tuple[str, ...] = ()
    # None表示改动多到状态读取超限，总数未知。
    total_entries: int | None = None
    truncated: bool = False

    @property
    def codes(self) -> dict[str, str]:
        """路径到两位状态码的映射。"""
        return {change[3:]: change[:2] for change in self.changes}


class BaselineLoss(ContractModel):
    """补发说明末行的记录：本次提示的路径；后续Turn据此不再重复提示。"""

    spec_version: Literal["harnessix.session-baseline-loss/v1"] = BASELINE_LOSS_VERSION
    paths: tuple[str, ...]


# 开始前的告知与完成前的复核签名相同，Runtime用同一方式落账。
BaselineNotice = Callable[
    [WorkspaceChangeObserver, Thread, UUID, CancelToken], Awaitable[list[EventDraft]]
]


def session_baseline_item_id(turn_id: UUID) -> UUID:
    """记录基线的宿主说明在其所在Turn内的稳定身份。"""
    return uuid5(turn_id, SESSION_BASELINE_VERSION)


def baseline_loss_item_id(turn_id: UUID) -> UUID:
    """基线改动丢失时补发说明的稳定身份；每个Turn至多一条。"""
    return uuid5(turn_id, BASELINE_LOSS_VERSION)


def is_session_baseline_notice(turn_id: UUID, item_id: UUID) -> bool:
    """这两条说明只给模型看；界面据此不把它们显示成用户发言。"""
    return item_id in (session_baseline_item_id(turn_id), baseline_loss_item_id(turn_id))


def recorded_baseline(thread: Thread) -> SessionBaseline | None:
    """Thread历史中记录的会话基线；没有记录时为None。"""
    for turn in thread.turns:
        baseline = _record(turn, session_baseline_item_id(turn.turn_id), SessionBaseline)
        if baseline is not None:
            return baseline
    return None


async def baseline_announcement(
    observer: WorkspaceChangeObserver, thread: Thread, turn_id: UUID, cancel: CancelToken
) -> list[EventDraft]:
    """会话尚未改动工作区时观察一次，返回告知已有改动的说明事件；无可告知时为空。"""
    if not _session_untouched(thread, turn_id):
        return []
    try:
        status = await _observed_changes(observer, cancel)
    except ReadToolError:
        # 改动多到超出Git状态读取上限：列不出条目，但“开始前已有大量未提交改动”仍是确定的事实。
        baseline: SessionBaseline | None = SessionBaseline(truncated=True)
    else:
        baseline = None if status is None else _bounded_baseline(status)
    if baseline is None:
        return []
    return host_notice_events(turn_id, session_baseline_item_id(turn_id), _announcement(baseline))


async def baseline_loss_notice(
    observer: WorkspaceChangeObserver, thread: Thread, turn_id: UUID, cancel: CancelToken
) -> list[EventDraft]:
    """Turn将要完成时复核基线：有未提示过的条目被还原或删除则返回补发说明事件，否则为空。"""
    baseline = recorded_baseline(thread)
    turn = get_turn(thread, turn_id)
    if (
        baseline is None
        or not baseline.changes
        or _item_text(turn, baseline_loss_item_id(turn_id)) is not None
        # 预算用尽时Turn无法再走一步，补发只会把完成变成失败。
        or turn.model_steps >= turn.budget.max_steps
        or turn.usage.total_tokens >= turn.budget.max_tokens
    ):
        return []
    try:
        status = await _observed_changes(observer, cancel, tuple(baseline.codes))
    except ReadToolError:
        return []
    if status is None:
        return []
    reported = {path for earlier in thread.turns for path in _reported_losses(earlier)}
    lost = tuple(path for path in lost_changes(baseline, status) if path not in reported)
    if not lost:
        return []
    notice = f"{_LOSS_NOTICE}\n{BaselineLoss(paths=lost).model_dump_json()}"
    return host_notice_events(turn_id, baseline_loss_item_id(turn_id), notice)


def lost_changes(baseline: SessionBaseline, current: GitStatusOutput) -> tuple[str, ...]:
    """基线条目中改动已不在（路径不再出现在状态里）或文件在会话中被删除的路径。

    HEAD已移动（提交、切换分支）或当前状态不完整时，无法据状态判断改动去向，返回空。
    """
    if current.head_oid != baseline.head_oid or current.truncated:
        return ()
    present = {entry.path: _short_code(entry) for entry in current.entries}
    return tuple(
        path
        for path, code in baseline.codes.items()
        if path not in present or ("D" in present[path] and "D" not in code)
    )


def mark_pre_existing(
    tool: str, result: ToolResultContent, baseline: SessionBaseline | None, *, max_chars: int
) -> ToolResultContent:
    """给git_status、git_review结果中属于会话基线的条目加pre_existing标记，并附一句说明。

    结果形状不符、没有可说明的内容，或标记后超出结果上限时原样返回：标记不得让有效结果失效。
    """
    if baseline is None or tool not in MARKED_GIT_TOOLS or result.outcome != "succeeded":
        return result
    output = result.output
    status = output.get("status") if tool == "git_review" and isinstance(output, dict) else output
    entries = status.get("entries") if isinstance(status, dict) else None
    if (
        not isinstance(output, dict)
        or not isinstance(status, dict)
        or not isinstance(entries, list)
    ):
        return result
    codes = baseline.codes
    marked: list[JsonValue] = [
        {**entry, "pre_existing": True}
        if isinstance(entry, dict) and entry.get("path") in codes
        else entry
        for entry in entries
    ]
    notes = [
        *((_PRE_EXISTING_NOTE,) if marked != entries else ()),
        *((_PRE_EXISTING_INCOMPLETE,) if baseline.truncated else ()),
    ]
    if not notes:
        return result
    marked_status = {**status, "entries": marked, "pre_existing_note": " ".join(notes)}
    marked_output = {**output, "status": marked_status} if tool == "git_review" else marked_status
    candidate = result.model_copy(update={"output": marked_output})
    if (
        len(json.dumps(marked_output, ensure_ascii=False).encode()) > MAX_RESULT_BYTES
        or len(candidate.model_dump_json()) > max_chars
    ):
        return result
    return candidate


async def _observed_changes(
    observer: WorkspaceChangeObserver, cancel: CancelToken, paths: tuple[str, ...] = ()
) -> GitStatusOutput | None:
    """一次观察；读不到（不是仓库、Git不可用、超时）按没有观察处理，不影响Turn。

    只有超出读取上限照原样上抛：那是“改动多到列不出”，与“读不到”含义不同。
    """
    try:
        return await observer.workspace_changes(cancel, paths=paths)
    except ReadToolError as error:
        if error.code == "limit_exceeded":
            raise
        return None
    except (KernelError, OSError):
        return None


def _session_untouched(thread: Thread, turn_id: UUID) -> bool:
    """本Thread还没有运行过模型步骤、也没有记录过基线：此刻的工作区里没有本会话的改动。

    Fork继承的历史里已有来源会话的改动，子Thread开始时的工作区不是“会话之前”的状态，不记录。
    """
    return (
        thread.fork_snapshot is None
        and get_turn(thread, turn_id).status == TurnStatus.ACCEPTED
        and all(turn.model_steps == 0 for turn in thread.turns)
        and all(
            _item_text(turn, session_baseline_item_id(turn.turn_id)) is None
            for turn in thread.turns
        )
    )


def _bounded_baseline(status: GitStatusOutput) -> SessionBaseline | None:
    """把一次状态观察收成有界记录；工作区干净时为None。"""
    if not status.total_entries:
        return None
    changes: list[str] = []
    size = 0
    for entry in status.entries[:MAX_BASELINE_ENTRIES]:
        change = f"{_short_code(entry)} {entry.path}"
        size += len(json.dumps(change, ensure_ascii=False).encode()) + 1
        if size > MAX_BASELINE_RECORD_BYTES:
            break
        changes.append(change)
    return SessionBaseline(
        head_oid=status.head_oid,
        changes=tuple(changes),
        total_entries=status.total_entries,
        truncated=status.total_entries > len(changes),
    )


def _short_code(entry: GitStatusEntry) -> str:
    """porcelain v2用“.”表示未改动；换成模型在git status --short里见到的空格。"""
    return (entry.index_status + entry.worktree_status).replace(".", " ")


def _announcement(baseline: SessionBaseline) -> str:
    """告知正文加末行记录；列不出条目或记录不完整时照实说明，并让模型按“不是自己改的”推断其余文件。"""
    if not baseline.changes:
        prose = _ANNOUNCEMENT_UNLISTED
    elif baseline.truncated:
        prose = _ANNOUNCEMENT + _ANNOUNCEMENT_INCOMPLETE.format(listed=len(baseline.changes))
    else:
        prose = _ANNOUNCEMENT
    return f"{prose}\n{baseline.model_dump_json()}"


def _item_text(turn: Turn, item_id: UUID) -> str | None:
    """Turn内指定身份的文本Item正文；不存在时为None。"""
    for item in turn.items:
        if item.item_id == item_id and isinstance(item.content, TextContent):
            return item.content.text
    return None


def _record[Record: ContractModel](turn: Turn, item_id: UUID, model: type[Record]) -> Record | None:
    """读回宿主说明末行的记录；说明不存在或末行不是该记录时为None。"""
    text = _item_text(turn, item_id)
    if text is None:
        return None
    try:
        return model.model_validate_json(text.rsplit("\n", 1)[-1])
    except ValueError:
        return None


def _reported_losses(turn: Turn) -> tuple[str, ...]:
    """该Turn的补发说明已提示过的路径。"""
    loss = _record(turn, baseline_loss_item_id(turn.turn_id), BaselineLoss)
    return () if loss is None else loss.paths
