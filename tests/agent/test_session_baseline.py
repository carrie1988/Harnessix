"""会话基线：会话开始前已有的未提交改动被记录、告知模型、在Git结果中标记，并在完成前复核。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.completion_guard import NO_CHANGE_NOTICE
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    EventDraft,
    Item,
    ItemFinished,
    ItemStatus,
    TextContent,
    Thread,
    ToolCallContent,
    ToolResultContent,
    Turn,
    TurnStatus,
    Usage,
)
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.session_baseline import (
    MAX_BASELINE_ENTRIES,
    MAX_BASELINE_RECORD_BYTES,
    BaselineLoss,
    SessionBaseline,
    baseline_announcement,
    baseline_loss_item_id,
    baseline_loss_notice,
    is_session_baseline_notice,
    lost_changes,
    mark_pre_existing,
    recorded_baseline,
    session_baseline_item_id,
)
from harnessix.domain.models import utc_now
from harnessix.models.scripted import ScriptedProvider
from harnessix.product_ui.projection import ProductViewState, ProjectedItem
from harnessix.product_ui.rendering import transcript_lines
from harnessix.protocol.contracts import PublicItem, PublicTextContent
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.contracts import MAX_RESULT_BYTES, ReadToolError
from harnessix.tools.git_contracts import GitStatusEntry, GitStatusOutput
from tests.agent.helpers import RecordingTools, answer, tool_step
from tests.product_ui.test_tui_usability import _thread

HEAD = "a" * 40
CONFIG = "build/test-config.xml"
NOTES = "notes.txt"


def changed(path: str, code: str = ".M") -> GitStatusEntry:
    return GitStatusEntry(
        path=path,
        kind="ordinary",
        index_status=code[0],
        worktree_status=code[1],
        submodule="N...",
    )


def untracked(path: str) -> GitStatusEntry:
    return GitStatusEntry(path=path, kind="untracked", index_status="?", worktree_status="?")


def status(*entries: GitStatusEntry, head: str = HEAD, total: int | None = None) -> GitStatusOutput:
    count = len(entries) if total is None else total
    return GitStatusOutput(
        branch="main",
        head_oid=head,
        entries=entries,
        total_entries=count,
        truncated=count > len(entries),
        revision="0" * 64,
    )


UNREADABLE = {
    "no_git": None,
    "not_a_repository": ReadToolError("not_found"),
    "git_timeout": KernelError("process_timeout", "Git读取超时"),
    "io_error": OSError("git"),
}


class FakeWorkspace:
    """观察端口替身：按当前条目回答，记录每次请求的路径。"""

    def __init__(self, *entries: GitStatusEntry) -> None:
        self.entries = list(entries)
        self.head = HEAD
        # 设为UNREADABLE的键即模拟读不到；"too_many"模拟改动超出读取上限。
        self.state = "readable"
        self.requests: list[tuple[str, ...]] = []

    async def workspace_changes(
        self, cancel: CancelToken, *, paths: tuple[str, ...] = ()
    ) -> GitStatusOutput | None:
        cancel.checkpoint()
        self.requests.append(paths)
        if self.state == "too_many":
            raise ReadToolError("limit_exceeded")
        if self.state in UNREADABLE:
            failure = UNREADABLE[self.state]
            if failure is None:
                return None
            raise failure
        visible = [entry for entry in self.entries if not paths or entry.path in paths]
        return status(*visible, head=self.head)

    def drop(self, path: str) -> None:
        self.entries = [entry for entry in self.entries if entry.path != path]


def turn_of(*items: Item, model_steps: int = 0, status: TurnStatus = TurnStatus.ACCEPTED) -> Turn:
    user = Item(
        item_id=uuid4(),
        status=ItemStatus.COMPLETED,
        content=TextContent(kind="user_message", text="fix the bug"),
    )
    return Turn(
        turn_id=uuid4(),
        request_id=str(uuid4()),
        request_fingerprint="0" * 64,
        status=status,
        budget=Budget(),
        items=(user, *items),
        model_steps=model_steps,
        created_at=utc_now(),
    )


def thread_of(*turns: Turn) -> Thread:
    now = utc_now()
    active = next((turn.turn_id for turn in turns if turn.status == TurnStatus.ACCEPTED), None)
    return Thread(
        thread_id=uuid4(),
        workspace="/workspace",
        sequence=1,
        active_turn_id=active,
        turns=turns,
        created_at=now,
        updated_at=now,
    )


def notice_item(events: list[EventDraft]) -> Item:
    """宿主说明事件落账后的Item。"""
    finished = events[-1].payload
    assert isinstance(finished, ItemFinished)
    return Item(item_id=finished.item_id, status=ItemStatus.COMPLETED, content=finished.content)


def notice_text(events: list[EventDraft]) -> str:
    content = notice_item(events).content
    assert isinstance(content, TextContent) and content.kind == "user_message"
    return content.text


async def announced(workspace: FakeWorkspace) -> tuple[Thread, list[EventDraft]]:
    turn = turn_of()
    thread = thread_of(turn)
    return thread, await baseline_announcement(workspace, thread, turn.turn_id, CancelToken())


async def running_thread(workspace: FakeWorkspace, **turn_fields: object) -> Thread:
    """已记录基线、第一轮正在进行的Thread。"""
    thread, events = await announced(workspace)
    turn = thread.turns[0].model_copy(
        update={
            "items": (*thread.turns[0].items, notice_item(events)),
            "status": TurnStatus.CALLING_MODEL,
            "model_steps": 1,
            **turn_fields,
        }
    )
    return thread.model_copy(update={"turns": (turn,)})


async def test_pre_existing_changes_are_announced_with_a_readable_record() -> None:
    workspace = FakeWorkspace(changed(CONFIG), changed("src/app.py", "M."), untracked(NOTES))
    thread, events = await announced(workspace)

    text = notice_text(events)
    assert text.startswith("[Harnessix host notice] Before this session started")
    assert "They are not your changes" in text and "unless the user asks" in text
    assert notice_item(events).item_id == session_baseline_item_id(thread.turns[0].turn_id)
    record = json.loads(text.rsplit("\n", 1)[-1])
    assert record == {
        "spec_version": "harnessix.session-baseline/v1",
        "head_oid": HEAD,
        # 与模型在git status --short里看到的写法一致。
        "changes": [f" M {CONFIG}", "M  src/app.py", f"?? {NOTES}"],
        "total_entries": 3,
        "truncated": False,
    }
    assert workspace.requests == [()]


@pytest.mark.parametrize("state", ["readable", *UNREADABLE])
async def test_nothing_is_recorded_for_a_clean_or_unreadable_workspace(state: str) -> None:
    workspace = FakeWorkspace(*(() if state == "readable" else (changed(CONFIG),)))
    workspace.state = state
    _, events = await announced(workspace)
    assert events == []


async def test_a_large_change_list_is_bounded_and_says_it_is_incomplete() -> None:
    entries = [changed(f"src/module_{index:04}.py") for index in range(200)]
    turn = turn_of()
    thread = thread_of(turn)

    class Paged(FakeWorkspace):
        async def workspace_changes(
            self, cancel: CancelToken, *, paths: tuple[str, ...] = ()
        ) -> GitStatusOutput | None:
            return status(*entries, total=5000)

    events = await baseline_announcement(Paged(), thread, turn.turn_id, CancelToken())
    text = notice_text(events)
    record = SessionBaseline.model_validate_json(text.rsplit("\n", 1)[-1])
    assert len(record.changes) == MAX_BASELINE_ENTRIES
    assert record.total_entries == 5000 and record.truncated
    assert f"only the first {MAX_BASELINE_ENTRIES} entries" in text
    assert "that you did not create or change yourself as pre-existing too" in text


async def test_long_paths_keep_the_record_within_its_byte_bound() -> None:
    entries = [changed(f"{index:03}/" + "深" * 600) for index in range(50)]
    turn = turn_of()
    thread = thread_of(turn)
    workspace = FakeWorkspace(*entries)
    events = await baseline_announcement(workspace, thread, turn.turn_id, CancelToken())
    record_line = notice_text(events).rsplit("\n", 1)[-1]
    record = SessionBaseline.model_validate_json(record_line)
    assert 0 < len(record.changes) < 50 and record.truncated
    assert len(record_line.encode()) <= MAX_BASELINE_RECORD_BYTES + 256


async def test_changes_too_many_to_read_are_announced_without_a_list() -> None:
    workspace = FakeWorkspace()
    workspace.state = "too_many"
    _, events = await announced(workspace)
    text = notice_text(events)
    assert "too many to list" in text
    record = SessionBaseline.model_validate_json(text.rsplit("\n", 1)[-1])
    assert record.changes == () and record.total_entries is None and record.truncated


async def test_the_baseline_is_recorded_once_before_the_session_touches_the_workspace() -> None:
    workspace = FakeWorkspace(changed(CONFIG))
    started = turn_of(model_steps=1, status=TurnStatus.COMPLETED)
    later = turn_of()
    # 已有模型步骤运行过：此刻的未提交改动可能出自本会话，不能当作“开始前已有”。
    assert (
        await baseline_announcement(
            workspace, thread_of(started, later), later.turn_id, CancelToken()
        )
        == []
    )
    assert workspace.requests == []

    thread = await running_thread(workspace)
    retried = turn_of()
    unstarted = thread.turns[0].model_copy(update={"model_steps": 0, "status": TurnStatus.FAILED})
    again = thread.model_copy(update={"turns": (unstarted, retried), "active_turn_id": None})
    assert await baseline_announcement(workspace, again, retried.turn_id, CancelToken()) == []
    assert recorded_baseline(again) == recorded_baseline(thread)


async def test_the_record_is_read_back_from_thread_history_only() -> None:
    thread = await running_thread(FakeWorkspace(changed(CONFIG), untracked(NOTES)))
    baseline = recorded_baseline(thread)
    assert baseline is not None
    assert baseline.codes == {CONFIG: " M", NOTES: "??"}
    assert recorded_baseline(thread_of(turn_of())) is None
    # 用户自己发来同样的文字不是记录：记录由Item身份确定。
    forged = Item(
        item_id=uuid4(),
        status=ItemStatus.COMPLETED,
        content=TextContent(kind="user_message", text=baseline.model_dump_json()),
    )
    assert recorded_baseline(thread_of(turn_of(forged))) is None


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ((changed(CONFIG), untracked(NOTES)), ()),
        # 改动被还原：路径不再出现在状态里。
        ((untracked(NOTES),), (CONFIG,)),
        # 未跟踪文件被删除。
        ((changed(CONFIG),), (NOTES,)),
        ((), (CONFIG, NOTES)),
        # 已修改的文件在会话中被删除，原改动同样丢失。
        ((changed(CONFIG, ".D"), untracked(NOTES)), (CONFIG,)),
        # 暂存或继续修改不是丢失。
        ((changed(CONFIG, "M."), changed(NOTES, "A.")), ()),
        ((changed(CONFIG, "MM"), untracked(NOTES)), ()),
    ],
)
def test_lost_changes(current: tuple[GitStatusEntry, ...], expected: tuple[str, ...]) -> None:
    baseline = SessionBaseline(head_oid=HEAD, changes=(f" M {CONFIG}", f"?? {NOTES}"))
    assert lost_changes(baseline, status(*current)) == expected


def test_lost_changes_is_silent_when_the_status_cannot_tell() -> None:
    baseline = SessionBaseline(head_oid=HEAD, changes=(f" D {CONFIG}", f" M {NOTES}"))
    # 开始前就已删除的文件仍是删除状态：不算丢失。
    assert lost_changes(baseline, status(changed(CONFIG, ".D"), changed(NOTES))) == ()
    # 提交或切换分支后，改动进入了HEAD或换了比较基准。
    assert lost_changes(baseline, status(head="b" * 40)) == ()
    assert lost_changes(baseline, status(changed(NOTES), total=300)) == ()


def result(output: object, outcome: str = "succeeded") -> ToolResultContent:
    return ToolResultContent(call_id=uuid4(), outcome=outcome, output=output)


def test_git_results_mark_pre_existing_entries() -> None:
    baseline = SessionBaseline(head_oid=HEAD, changes=(f" M {CONFIG}", f"?? {NOTES}"))
    observed = status(changed(CONFIG), changed("src/app.py"), untracked(NOTES))
    plain = result(observed.model_dump(mode="json"))

    marked = mark_pre_existing("git_status", plain, baseline, max_chars=65_536)
    assert isinstance(marked.output, dict)
    flags = {entry["path"]: entry.get("pre_existing") for entry in marked.output["entries"]}
    assert flags == {CONFIG: True, "src/app.py": None, NOTES: True}
    assert "not your changes" in marked.output["pre_existing_note"]
    assert marked.output["total_entries"] == 3 and marked.call_id == plain.call_id

    review = result({"spec_version": "harnessix.git-review/v1", "status": plain.output, "diff": {}})
    reviewed = mark_pre_existing("git_review", review, baseline, max_chars=65_536)
    assert isinstance(reviewed.output, dict)
    assert reviewed.output["status"] == marked.output
    assert reviewed.output["diff"] == {}


def test_git_results_stay_unchanged_when_there_is_nothing_to_mark() -> None:
    baseline = SessionBaseline(head_oid=HEAD, changes=(f" M {CONFIG}",))
    own = result(status(changed("src/app.py")).model_dump(mode="json"))
    listed = result(status(changed(CONFIG)).model_dump(mode="json"))
    assert mark_pre_existing("git_status", own, baseline, max_chars=65_536) is own
    assert mark_pre_existing("git_status", listed, None, max_chars=65_536) is listed
    assert mark_pre_existing("git_diff", listed, baseline, max_chars=65_536) is listed
    failed = result(None, outcome="failed")
    assert mark_pre_existing("git_status", failed, baseline, max_chars=65_536) is failed
    malformed = result({"entries": "none"})
    assert mark_pre_existing("git_status", malformed, baseline, max_chars=65_536) is malformed


def test_an_incomplete_baseline_warns_about_unmarked_entries() -> None:
    baseline = SessionBaseline(head_oid=HEAD, changes=(f" M {CONFIG}",), truncated=True)
    own = result(status(changed("src/app.py")).model_dump(mode="json"))
    marked = mark_pre_existing("git_status", own, baseline, max_chars=65_536)
    assert isinstance(marked.output, dict)
    assert marked.output["entries"] == own.output["entries"]
    assert (
        "record of changes that existed before this session is incomplete"
        in (marked.output["pre_existing_note"])
    )


def test_marking_never_pushes_a_result_over_its_limits() -> None:
    baseline = SessionBaseline(head_oid=HEAD, changes=(f" M {CONFIG}",))
    listed = result(status(changed(CONFIG)).model_dump(mode="json"))
    tight = len(listed.model_dump_json()) + 10
    assert mark_pre_existing("git_status", listed, baseline, max_chars=tight) is listed
    padded = result({**listed.output, "padding": "x" * (MAX_RESULT_BYTES - 400)})
    assert mark_pre_existing("git_status", padded, baseline, max_chars=10**6) is padded


async def test_a_reverted_change_is_reported_once_for_the_turn() -> None:
    workspace = FakeWorkspace(changed(CONFIG), untracked(NOTES))
    thread = await running_thread(workspace)
    turn_id = thread.turns[0].turn_id
    assert await baseline_loss_notice(workspace, thread, turn_id, CancelToken()) == []

    workspace.drop(CONFIG)
    events = await baseline_loss_notice(workspace, thread, turn_id, CancelToken())
    text = notice_text(events)
    assert text.startswith("[Harnessix host notice] Your last response had no tool call")
    assert "restore each file" in text and "If the user asked for this change" in text
    assert BaselineLoss.model_validate_json(text.rsplit("\n", 1)[-1]).paths == (CONFIG,)
    assert notice_item(events).item_id == baseline_loss_item_id(turn_id)
    # 复核只查询基线里的路径，结果不受工作区其他改动数量影响。
    assert workspace.requests[-1] == (CONFIG, NOTES)

    told = thread.turns[0].model_copy(
        update={"items": (*thread.turns[0].items, notice_item(events))}
    )
    workspace.drop(NOTES)
    after = thread.model_copy(update={"turns": (told,)})
    assert await baseline_loss_notice(workspace, after, turn_id, CancelToken()) == []


async def test_a_lost_path_is_never_reported_again_in_later_turns() -> None:
    workspace = FakeWorkspace(changed(CONFIG), untracked(NOTES))
    thread = await running_thread(workspace)
    first = thread.turns[0]
    workspace.drop(CONFIG)
    events = await baseline_loss_notice(workspace, thread, first.turn_id, CancelToken())
    done = first.model_copy(
        update={"items": (*first.items, notice_item(events)), "status": TurnStatus.COMPLETED}
    )

    second = turn_of(model_steps=1, status=TurnStatus.CALLING_MODEL)
    later = thread.model_copy(update={"turns": (done, second), "active_turn_id": second.turn_id})
    # 用户要求的还原只在发生的那一轮提示一次。
    assert await baseline_loss_notice(workspace, later, second.turn_id, CancelToken()) == []

    workspace.drop(NOTES)
    events = await baseline_loss_notice(workspace, later, second.turn_id, CancelToken())
    assert BaselineLoss.model_validate_json(notice_text(events).rsplit("\n", 1)[-1]).paths == (
        NOTES,
    )


async def test_no_loss_notice_without_a_baseline_or_without_budget() -> None:
    workspace = FakeWorkspace()
    plain = turn_of(model_steps=1, status=TurnStatus.CALLING_MODEL)
    assert (
        await baseline_loss_notice(workspace, thread_of(plain), plain.turn_id, CancelToken()) == []
    )
    assert workspace.requests == []

    dirty = FakeWorkspace(changed(CONFIG))
    last_step = await running_thread(dirty, budget=Budget(max_steps=1))
    dirty.drop(CONFIG)
    turn_id = last_step.turns[0].turn_id
    assert await baseline_loss_notice(dirty, last_step, turn_id, CancelToken()) == []
    spent = await running_thread(
        FakeWorkspace(changed(CONFIG)),
        budget=Budget(max_tokens=1000),
        usage=Usage(input_tokens=1000, output_tokens=0),
    )
    assert (
        await baseline_loss_notice(FakeWorkspace(), spent, spent.turns[0].turn_id, CancelToken())
        == []
    )


async def test_loss_check_is_silent_when_the_workspace_cannot_be_read() -> None:
    workspace = FakeWorkspace(changed(CONFIG))
    thread = await running_thread(workspace)
    turn_id = thread.turns[0].turn_id
    workspace.drop(CONFIG)
    for state in (*UNREADABLE, "too_many"):
        workspace.state = state
        assert await baseline_loss_notice(workspace, thread, turn_id, CancelToken()) == []
    workspace.state = "readable"
    assert await baseline_loss_notice(workspace, thread, turn_id, CancelToken()) != []


class RevertingTools(RecordingTools):
    """执行工具即还原一个开始前已有的改动：模型把它当成自己留下的垃圾清理掉了。"""

    def __init__(self, workspace: FakeWorkspace, path: str) -> None:
        super().__init__()
        self.workspace = workspace
        self.path = path

    async def execute(self, call: ToolCallContent, cancel: CancelToken) -> ToolResultContent:
        self.workspace.drop(self.path)
        return await super().execute(call, cancel)


def texts(items: Iterable[Item]) -> list[tuple[str, str]]:
    return [
        (entry.content.kind, entry.content.text)
        for entry in items
        if isinstance(entry.content, TextContent)
    ]


async def test_runtime_tells_the_model_before_its_first_step(tmp_path: Path) -> None:
    workspace = FakeWorkspace(changed(CONFIG), untracked(NOTES))
    provider = ScriptedProvider([answer("Nothing to change."), answer("Still nothing.")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, session_baseline=workspace) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        first = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="one")
        second = await runtime.run_turn(thread.thread_id, "thanks", request_id="two")

    assert first.status == second.status == TurnStatus.COMPLETED
    sent = texts(provider.requests[0].history)
    assert sent[0] == ("user_message", "fix the bug")
    assert sent[1][0] == "user_message" and f" M {CONFIG}" in sent[1][1]
    assert first.items[1].item_id == session_baseline_item_id(first.turn_id)
    # 基线只在会话开始时记录一次；后续Turn不再重复说明，也不把本会话的改动算进去。
    assert [kind for kind, _ in texts(second.items)] == ["user_message", "assistant_message"]
    assert len(provider.requests) == 2
    events = await store.events(thread.thread_id)
    assert replay(events) == await store.get_thread(thread.thread_id)


@pytest.mark.parametrize("state", ["disabled", "readable", "not_a_repository"])
async def test_runtime_adds_nothing_without_pre_existing_changes(
    tmp_path: Path, state: str
) -> None:
    workspace = FakeWorkspace()
    workspace.state = state
    provider = ScriptedProvider([answer("Done.")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    observer = None if state == "disabled" else workspace
    async with AgentRuntime(store, provider, session_baseline=observer) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="one")

    assert completed.status == TurnStatus.COMPLETED
    assert texts(completed.items) == [
        ("user_message", "fix the bug"),
        ("assistant_message", "Done."),
    ]
    assert len(provider.requests) == 1


async def test_runtime_asks_once_to_restore_a_change_reverted_during_cleanup(
    tmp_path: Path,
) -> None:
    """真实案例的一般形态：环境在开始前改好的配置文件被模型当作垃圾还原，最终答复照常给出。"""
    workspace = FakeWorkspace(changed(CONFIG))
    tools = RevertingTools(workspace, CONFIG)
    provider = ScriptedProvider(
        [
            tool_step("test.read"),
            answer("Fixed; the workspace is clean."),
            answer("I could not restore it; the earlier content is gone."),
        ]
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools, session_baseline=workspace) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="one")

    # 提示一次；模型答复后照常完成，即使文件没有恢复也不循环。
    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 3
    last = texts(provider.requests[2].history)[-2:]
    assert last[0] == ("assistant_message", "Fixed; the workspace is clean.")
    assert last[1][0] == "user_message" and "those changes are gone now" in last[1][1]
    assert json.loads(last[1][1].rsplit("\n", 1)[-1])["paths"] == [CONFIG]
    notices = [item for item in completed.items if is_notice(completed.turn_id, item)]
    assert [item.item_id for item in notices] == [
        session_baseline_item_id(completed.turn_id),
        baseline_loss_item_id(completed.turn_id),
    ]
    events = await store.events(thread.thread_id)
    assert any(getattr(event.payload, "reason", None) == "steering" for event in events)
    assert replay(events) == await store.get_thread(thread.thread_id)


def is_notice(turn_id: UUID, item: Item) -> bool:
    return is_session_baseline_notice(turn_id, item.item_id)


async def test_runtime_does_not_repeat_a_requested_revert_in_the_next_turn(
    tmp_path: Path,
) -> None:
    workspace = FakeWorkspace(changed(CONFIG))
    tools = RevertingTools(workspace, CONFIG)
    provider = ScriptedProvider(
        [
            tool_step("test.read"),
            answer("Reverted the file."),
            answer("The user asked me to discard that change, so I left it reverted."),
        ]
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools, session_baseline=workspace) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        first = await runtime.run_turn(thread.thread_id, "discard my change", request_id="one")
    follow_up = ScriptedProvider([answer("You're welcome.")])
    async with AgentRuntime(store, follow_up, tools, session_baseline=workspace) as runtime:
        second = await runtime.run_turn(thread.thread_id, "thanks", request_id="two")

    assert first.status == second.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 3 and len(follow_up.requests) == 1
    assert not any(is_notice(second.turn_id, item) for item in second.items)


async def test_runtime_stays_silent_when_the_change_is_still_there(tmp_path: Path) -> None:
    workspace = FakeWorkspace(changed(CONFIG))
    provider = ScriptedProvider([tool_step("test.read"), answer("Fixed.")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store, provider, RecordingTools(), session_baseline=workspace
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="one")

    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 2
    assert workspace.requests == [(), (CONFIG,)]


async def test_runtime_completes_on_the_last_budgeted_step_instead_of_failing(
    tmp_path: Path,
) -> None:
    workspace = FakeWorkspace(changed(CONFIG))
    tools = RevertingTools(workspace, CONFIG)
    provider = ScriptedProvider([tool_step("test.read"), answer("Fixed.")])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(store, provider, tools, session_baseline=workspace) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(
            thread.thread_id, "fix the bug", request_id="one", budget=Budget(max_steps=2)
        )

    # 没有剩余步骤时补发只会让Turn以预算耗尽失败：照常完成，不提示。
    assert completed.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 2
    assert not any(
        item.item_id == baseline_loss_item_id(completed.turn_id) for item in completed.items
    )


async def test_the_loss_notice_precedes_the_completion_guard_and_neither_repeats(
    tmp_path: Path,
) -> None:
    workspace = FakeWorkspace(changed(CONFIG))
    tools = RevertingTools(workspace, CONFIG)
    provider = ScriptedProvider(
        [tool_step("test.read"), answer("Done."), answer("Left it."), answer("Final summary.")]
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with AgentRuntime(
        store, provider, tools, completion_guard=True, session_baseline=workspace
    ) as runtime:
        thread = await runtime.create_thread(str(tmp_path))
        completed = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="one")

    # 复核先落账，本步按Steering重入；完成守卫在下一次无工具调用的回复上照常判定。
    assert completed.status == TurnStatus.COMPLETED
    hosted = [text for kind, text in texts(completed.items) if kind == "user_message"][2:]
    assert len(hosted) == 2 and len(provider.requests) == 4
    assert "those changes are gone now" in hosted[0]
    # 完成守卫第2版在提示后写明剩余的模型调用次数，这里只核对提示本身。
    assert hosted[1].startswith(NO_CHANGE_NOTICE)


def test_the_notices_are_not_shown_as_user_messages_in_the_transcript() -> None:
    thread = _thread()
    turn_id = thread.thread_id

    def entry(item_id: UUID, text: str) -> ProjectedItem:
        return ProjectedItem(
            item=PublicItem(
                item_id=item_id,
                status="completed",
                content=PublicTextContent(kind="user_message", text=text),
            ),
            turn_id=turn_id,
            first_cursor=1,
            last_cursor=1,
            final=True,
        )

    view = ProductViewState(
        thread=thread,
        durable_cursor=3,
        items=(
            entry(uuid4(), "修复这个问题"),
            entry(session_baseline_item_id(turn_id), "[Harnessix host notice] Before this…"),
            entry(baseline_loss_item_id(turn_id), "[Harnessix host notice] Your last…"),
        ),
    )
    assert [line.text for line in transcript_lines(view)] == ["修复这个问题"]
