"""搜索记录超过预览后仍可分页读取；固定离线 Provider，不是自主编码 Eval。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from harnessix.agent.models import ToolResultContent, TurnStatus
from harnessix.agent.reducer import replay
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.contracts import (
    ResponseCompleted,
    ResponseStarted,
    TextCompleted,
    TextStarted,
    ToolCallCompleted,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime


class ArtifactFixtureProvider:
    async def stream(self, request, cancel):
        cancel.checkpoint()
        outputs = [i.content for i in request.history if isinstance(i.content, ToolResultContent)]
        assert all(output.outcome == "succeeded" for output in outputs)
        yield ResponseStarted(response_id=f"artifact-{request.step}")
        if request.step == 1:
            tool, args = "grep", {"query": "target", "max_results": 2}
        elif request.step == 2:
            output = outputs[-1].output
            assert len(output["preview"]["matches"]) == 2 and output["artifact"]["records"] == 300
            tool, args = (
                "read_artifact",
                {"artifact_id": output["artifact"]["artifact_id"], "offset": 298},
            )
        else:
            assert request.step == 3
            page = outputs[-1].output
            assert [json.loads(line)["line"] for line in page["text"].splitlines()] == [299, 300]
            assert page["next_offset"] is None
            yield TextStarted(content_id="answer")
            yield TextCompleted(
                content_id="answer", text="预览只有两行，归档中成功读取了第 299/300 个命中。"
            )
            yield ResponseCompleted()
            return
        yield ToolCallCompleted(call_id=f"artifact-{request.step}", tool=tool, arguments=args)
        yield ResponseCompleted(finish_reason="tool_calls")


async def exercise(directory: Path) -> None:
    root = directory / "repo"
    root.mkdir()
    (root / "main.py").write_text("target 中文\n" * 300, encoding="utf-8")
    session = SQLiteSessionStore(directory / "session.db")
    artifacts = SQLiteArtifactStore(session)
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        async with AgentRuntime(
            session, ArtifactFixtureProvider(), scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(
                thread.thread_id, "归档搜索后读取后续命中", request_id="artifact"
            )
    assert turn.status == TurnStatus.COMPLETED
    reopened = SQLiteSessionStore(session.path)
    assert replay(await reopened.events(thread.thread_id)) == await reopened.get_thread(
        thread.thread_id
    )
    print("300 条记录事务归档、预览外分页及 Replay 通过；未调用模型 API 或写工具。")


def main() -> None:
    with TemporaryDirectory(prefix="harnessix-artifact-") as directory:
        asyncio.run(exercise(Path(directory)))


if __name__ == "__main__":
    main()
