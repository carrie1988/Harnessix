"""会话基线在真实Git仓库上的端到端行为。

回归来源：任务环境在开始前改好了一个测试配置文件（未提交）；模型在git status里看到它，
收尾“清理”时用git checkout把它还原，源码修复正确却整题失败。这里收敛为一般形态：
开始前已有一处已跟踪文件的改动和一个未跟踪文件，会话中途被还原和删除。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Sequence
from pathlib import Path

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import TextContent, ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.agent.session_baseline import (
    BaselineLoss,
    SessionBaseline,
    baseline_loss_item_id,
    session_baseline_item_id,
)
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseStarted,
    ToolCallCompleted,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.tools.test_git import _command, _git

CONFIG = "build/test-config.xml"
ORIGINAL_CONFIG = "<project>\n</project>\n"
ENVIRONMENT_CONFIG = '<project>\n  <target name="test.instance"/>\n</project>\n'


class SessionProvider:
    """按步骤回放事件；每步开始前先执行该步的工作区动作，代表会话中命令工具的效果。"""

    def __init__(
        self, steps: Sequence[tuple[Callable[[], None] | None, list[ProviderEvent]]]
    ) -> None:
        self.steps = tuple(steps)
        self.requests: list[ModelRequest] = []

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        self.requests.append(request)
        action, events = self.steps[len(self.requests) - 1]
        if action is not None:
            action()
        for event in events:
            cancel.checkpoint()
            yield event


def git_status_call() -> list[ProviderEvent]:
    return [
        ResponseStarted(response_id="status"),
        ToolCallCompleted(call_id="status-1", tool="git_status", arguments={}),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def repository_with_environment_changes(root: Path) -> None:
    """已提交的源码与配置，加上会话开始前就存在的未提交改动。"""
    _command(root, "init", "-q")
    _command(root, "config", "user.name", "Harnessix Test")
    _command(root, "config", "user.email", "test@harnessix.invalid")
    (root / "build").mkdir()
    (root / "src").mkdir()
    (root / CONFIG).write_text(ORIGINAL_CONFIG, encoding="utf-8")
    (root / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    _command(root, "add", "-A")
    _command(root, "commit", "-qm", "initial")
    (root / CONFIG).write_text(ENVIRONMENT_CONFIG, encoding="utf-8")
    (root / "notes.txt").write_text("the user's scratch notes\n", encoding="utf-8")


def user_messages(request: ModelRequest) -> list[str]:
    return [
        item.content.text
        for item in request.history
        if isinstance(item.content, TextContent) and item.content.kind == "user_message"
    ]


async def test_reverted_environment_change_is_announced_marked_and_restored(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    repository_with_environment_changes(root)

    def edit_source() -> None:
        (root / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")

    def clean_up() -> None:
        # 模型把不是自己改的文件当成垃圾：git checkout -- 配置文件，并删除未跟踪文件。
        _command(root, "checkout", "--", CONFIG)
        (root / "notes.txt").unlink()

    def restore_config() -> None:
        (root / CONFIG).write_text(ENVIRONMENT_CONFIG, encoding="utf-8")

    provider = SessionProvider(
        [
            (edit_source, git_status_call()),
            (clean_up, answer("Fixed src/app.py; the workspace is clean.")),
            (restore_config, answer("Restored the config; notes.txt could not be restored.")),
        ]
    )
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with CodingToolRuntime(root, git_executable=_git()) as tools:
        async with AgentRuntime(store, provider, tools, session_baseline=tools) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="fix")

    assert turn.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 3

    # 第一步之前：模型已被告知哪些改动先于会话存在。
    task, announcement = user_messages(provider.requests[0])
    assert task == "fix the bug"
    assert turn.items[1].item_id == session_baseline_item_id(turn.turn_id)
    baseline = SessionBaseline.model_validate_json(announcement.rsplit("\n", 1)[-1])
    assert baseline.changes == (f" M {CONFIG}", "?? notes.txt")
    assert baseline.total_entries == 2 and not baseline.truncated

    # git_status里，开始前已有的条目与本会话的改动可以区分。
    status = next(
        item.content.output for item in turn.items if isinstance(item.content, ToolResultContent)
    )
    flags = {entry["path"]: entry.get("pre_existing") for entry in status["entries"]}
    assert flags == {CONFIG: True, "src/app.py": None, "notes.txt": True}
    assert "not your changes" in status["pre_existing_note"]

    # 完成前：被还原和被删除的两处各提示一次，模型恢复后照常完成。
    notice = user_messages(provider.requests[2])[-1]
    assert turn.items[-2].item_id == baseline_loss_item_id(turn.turn_id)
    assert BaselineLoss.model_validate_json(notice.rsplit("\n", 1)[-1]).paths == (
        CONFIG,
        "notes.txt",
    )
    assert (root / CONFIG).read_text(encoding="utf-8") == ENVIRONMENT_CONFIG
    events = await store.events(thread.thread_id)
    assert replay(events) == await store.get_thread(thread.thread_id)


async def test_untouched_environment_changes_cost_no_extra_step(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    repository_with_environment_changes(root)

    def edit_source() -> None:
        (root / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")

    provider = SessionProvider([(edit_source, answer("Fixed src/app.py."))])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with CodingToolRuntime(root, git_executable=_git()) as tools:
        async with AgentRuntime(store, provider, tools, session_baseline=tools) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(thread.thread_id, "fix the bug", request_id="fix")

    assert turn.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 1
    assert len(user_messages(provider.requests[0])) == 2


async def test_a_requested_commit_is_not_mistaken_for_a_revert(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    repository_with_environment_changes(root)

    def commit_everything() -> None:
        _command(root, "add", "-A")
        _command(root, "commit", "-qm", "the user asked for a commit")

    provider = SessionProvider([(commit_everything, answer("Committed."))])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with CodingToolRuntime(root, git_executable=_git()) as tools:
        async with AgentRuntime(store, provider, tools, session_baseline=tools) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(thread.thread_id, "commit my work", request_id="c")

    # 提交后改动进入HEAD、不再出现在状态里，但并没有丢失。
    assert turn.status == TurnStatus.COMPLETED
    assert len(provider.requests) == 1


async def test_a_workspace_that_is_not_a_repository_is_left_alone(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "notes.txt").write_text("not under version control\n", encoding="utf-8")
    provider = SessionProvider([(None, answer("Done."))])
    store = SQLiteSessionStore(tmp_path / "session.db")
    async with CodingToolRuntime(root, git_executable=_git()) as tools:
        async with AgentRuntime(store, provider, tools, session_baseline=tools) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(thread.thread_id, "tidy up", request_id="tidy")

    assert turn.status == TurnStatus.COMPLETED
    assert user_messages(provider.requests[0]) == ["tidy up"]
    assert [type(item.content).__name__ for item in turn.items] == ["TextContent", "TextContent"]
