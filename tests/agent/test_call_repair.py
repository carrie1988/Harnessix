"""提案阶段的确定性调用修复：read_file携带存档编号时改道read_artifact。"""

from __future__ import annotations

import pytest

from harnessix.agent.call_repair import repair_proposal, reroute_artifact_read
from harnessix.agent.models import ToolCallContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import ToolCallCompleted, ToolCallRejected
from harnessix.models.scripted import ScriptedProvider
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.helpers import answer
from tests.artifacts.helpers import exercise, results, step

ARTIFACT = "01a1baa7-30d1-75c3-a388-635e4f1e75c6"
OFFERED = {"read_file", "read_artifact", "grep"}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # 评测中实际出现的写法：沿用read_artifact的参数名调用read_file。
        (
            {"artifact_id": ARTIFACT, "offset": 0, "limit": 100},
            {"artifact_id": ARTIFACT, "offset": 0, "limit": 100},
        ),
        ({"artifact_id": ARTIFACT}, {"artifact_id": ARTIFACT}),
        ({"path": ARTIFACT, "limit": 500}, {"artifact_id": ARTIFACT, "limit": 200}),
        (
            {"artifact_id": ARTIFACT, "path": ARTIFACT, "offset": 40},
            {"artifact_id": ARTIFACT, "offset": 40},
        ),
    ],
)
def test_artifact_shaped_read_file_becomes_read_artifact(arguments, expected):
    assert reroute_artifact_read("read_file", arguments, OFFERED) == ("read_artifact", expected)


@pytest.mark.parametrize(
    ("tool", "arguments", "offered"),
    [
        # 工作区路径与存档编号同时出现：意图不唯一。
        ("read_file", {"artifact_id": ARTIFACT, "path": "src/app.py"}, OFFERED),
        # 存档参数之外还有行分页字段：不猜测。
        ("read_file", {"artifact_id": ARTIFACT, "start_line": 3}, OFFERED),
        ("read_file", {"artifact_id": "not-an-artifact"}, OFFERED),
        ("read_file", {"path": "src/app.py", "offset": 0, "limit": 10}, OFFERED),
        # 本步未开放read_artifact：不得借改道调用目录之外的工具。
        ("read_file", {"artifact_id": ARTIFACT}, {"read_file"}),
        ("grep", {"artifact_id": ARTIFACT}, OFFERED),
    ],
)
def test_ambiguous_or_unoffered_reads_are_left_to_strict_validation(tool, arguments, offered):
    assert reroute_artifact_read(tool, arguments, offered) is None


def test_only_completed_proposals_are_repaired():
    rejected = ToolCallRejected(call_id="c1", reason="unregistered_tool", argument_chars=2)
    assert repair_proposal(rejected, OFFERED) is rejected
    proposal = ToolCallCompleted(
        call_id="c2", tool="read_file", arguments={"artifact_id": ARTIFACT}
    )
    repaired = repair_proposal(proposal, OFFERED)
    assert isinstance(repaired, ToolCallCompleted)
    assert (repaired.call_id, repaired.tool) == ("c2", "read_artifact")
    assert repair_proposal(proposal, ()) is proposal


async def test_runtime_records_and_executes_the_rerouted_read_artifact(tmp_path):
    store, artifacts, _, thread, turn = await exercise(tmp_path, count=3)
    ref = results(turn)[0].output["artifact"]["artifact_id"]
    async with CodingToolRuntime(tmp_path / "repo", artifacts=artifacts) as tools:
        provider = ScriptedProvider(
            [step("read_file", artifact_id=ref, offset=0, limit=500), answer()]
        )
        async with AgentRuntime(
            store, provider, scoped_tools=tools, artifacts=artifacts
        ) as runtime:
            final = await runtime.run_turn(thread.thread_id, "读取归档", request_id="read")

    calls = [item.content for item in final.items if isinstance(item.content, ToolCallContent)]
    # 历史记录的是规范的read_artifact调用，存档分页核验按原有工具身份进行。
    assert [(call.tool, call.arguments) for call in calls] == [
        ("read_artifact", {"artifact_id": ref, "offset": 0, "limit": 200})
    ]
    result = results(final)[0]
    assert result.outcome == "succeeded" and result.output["artifact"]["records"] == 3
