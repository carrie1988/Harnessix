"""搜索→按 revision 读取→回答的离线验收；固定决策夹具，不是自主编码 Eval。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime


class SearchFixtureProvider:
    """确定性消费实际工具输出；无需凭据、网络或可选供应商 SDK。"""

    async def stream(self, request, cancel):
        cancel.checkpoint()
        results = [i.content for i in request.history if isinstance(i.content, ToolResultContent)]
        assert all(result.outcome == "succeeded" for result in results)
        yield ResponseStarted(response_id=f"search-{request.step}")
        if request.step == 1:
            tool, args = "glob", {"pattern": "**/*.py"}
        elif request.step == 2:
            assert results[-1].output["paths"] == ["src/main.py"]
            tool, args = "grep", {"path": "src", "query": "def target", "include": "*.py"}
        elif request.step == 3:
            hit = results[-1].output["matches"][0]
            tool, args = (
                "read_file",
                {
                    "path": hit["path"],
                    "start_line": hit["line"],
                    "expected_revision": hit["revision"],
                    "max_lines": 2,
                },
            )
        else:
            assert request.step == 4
            assert results[-1].output["text"] == "def target():\n    return 42\n"
            yield TextStarted(content_id="answer")
            yield TextCompleted(content_id="answer", text="已定位并读取 target；未修改任何代码。")
            yield ResponseCompleted()
            return
        yield ToolCallCompleted(call_id=f"search-{request.step}", tool=tool, arguments=args)
        yield ResponseCompleted(finish_reason="tool_calls")


def prepare(parent: Path) -> Path:
    root = parent / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/main.py").write_text(
        "# 搜索夹具\ndef target():\n    return 42\n", encoding="utf-8"
    )
    return root


async def exercise(directory: Path) -> None:
    root = await asyncio.to_thread(prepare, directory)
    store = SQLiteSessionStore(directory / "session.db")
    async with CodingToolRuntime(root) as tools:
        async with AgentRuntime(store, SearchFixtureProvider(), scoped_tools=tools) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "搜索后读取函数", request_id="search")
    assert turn.status == TurnStatus.COMPLETED
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert len(results) == 3 and all(result.outcome == "succeeded" for result in results)
    reopened = SQLiteSessionStore(store.path)
    assert replay(await reopened.events(thread.thread_id)) == await reopened.get_thread(
        thread.thread_id
    )
    print("glob → grep → revision 读取通过，SQLite 重开/Replay 一致；未调用模型 API 或写工具。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-search-") as directory:
        asyncio.run(exercise(Path(directory)))


if __name__ == "__main__":
    main()
