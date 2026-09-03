from harnessix.agent.models import ToolResultContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer


def step(tool="grep", **arguments):
    return [
        ResponseStarted(response_id="archive"),
        ToolCallCompleted(
            call_id="archive",
            tool=tool,
            arguments=arguments or {"query": "needle", "max_results": 2},
        ),
        ResponseCompleted(finish_reason="tool_calls"),
    ]


def results(turn):
    return [i.content for i in turn.items if isinstance(i.content, ToolResultContent)]


async def exercise(parent, *, policy=None, fault=None, tool="grep", args=None, count=300):
    root = parent / "repo"
    root.mkdir()
    (root / "main.py").write_text("needle 中文\n" * count, encoding="utf-8")
    store = SQLiteSessionStore(parent / "session.db")
    artifacts = SQLiteArtifactStore(store, policy=policy, fault=fault)
    async with CodingToolRuntime(root, artifacts=artifacts) as tools:
        provider = ScriptedProvider([step(tool, **(args or {})), answer()])
        async with AgentRuntime(
            store, provider, scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            thread = await runtime.create_thread(str(tools.workspace_root))
            turn = await runtime.run_turn(thread.thread_id, "归档搜索", request_id="archive")
        scope = tools.workspace_scope
    return store, artifacts, scope, thread, turn
