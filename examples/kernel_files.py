"""真实临时文件的只读 Kernel 验收；Scripted Provider，不是自主编码 Eval。"""

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
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime


def prepare(parent: Path) -> Path:
    root = parent / "repo"
    root.mkdir()
    (root / "main.py").write_text("print('你好，Harnessix')\n", encoding="utf-8")
    return root


async def exercise(directory: Path) -> None:
    root = await asyncio.to_thread(prepare, directory)
    provider = ScriptedProvider(
        [
            [
                ResponseStarted(response_id="list"),
                ToolCallCompleted(call_id="list-1", tool="list_files", arguments={}),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            [
                ResponseStarted(response_id="read"),
                ToolCallCompleted(
                    call_id="read-1", tool="read_file", arguments={"path": "main.py"}
                ),
                ResponseCompleted(finish_reason="tool_calls"),
            ],
            [
                ResponseStarted(response_id="answer"),
                TextStarted(content_id="answer"),
                TextCompleted(content_id="answer", text="已列目录并读取文件；未修改代码。"),
                ResponseCompleted(),
            ],
        ]
    )
    store = SQLiteSessionStore(directory / "session.db")
    async with CodingToolRuntime(root) as tools:
        async with AgentRuntime(store, provider, tools) as runtime:
            thread = await runtime.create_thread(str(root))
            turn = await runtime.run_turn(thread.thread_id, "只读验收", request_id="read-files")
    assert turn.status == TurnStatus.COMPLETED
    results = [item.content for item in turn.items if isinstance(item.content, ToolResultContent)]
    assert len(results) == 2 and all(result.outcome == "succeeded" for result in results)
    assert isinstance(results[1].output, dict)
    assert results[1].output["text"] == "print('你好，Harnessix')\n"
    reopened = SQLiteSessionStore(store.path)
    assert replay(await reopened.events(thread.thread_id)) == await reopened.get_thread(
        thread.thread_id
    )
    print("目录与文件读取通过，SQLite 重开/Replay 一致；未执行写入工具或模型 API 请求。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-read-") as directory:
        asyncio.run(exercise(Path(directory)))


if __name__ == "__main__":
    main()
