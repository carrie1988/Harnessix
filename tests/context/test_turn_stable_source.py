"""同一Turn内只观察一次的来源包装（ADR 0153）：系统指令不因工作区根目录条目变化而在Turn中途改变。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from harnessix.agent.cancellation import CancelToken
from harnessix.context.contracts import ContextBuildInput
from harnessix.context.sources import WorkspaceContextSource
from harnessix.context.turn_stable_source import _MAX_THREADS, TurnStableContextSource
from harnessix.product_config.agent_context import build_product_agent_context


def _request(root: Path, thread: UUID, turn: UUID, step: int) -> ContextBuildInput:
    return ContextBuildInput(thread_id=thread, turn_id=turn, model_step=step, workspace=str(root))


async def test_the_overview_is_observed_once_per_turn_and_again_in_the_next_turn(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    source = TurnStableContextSource(WorkspaceContextSource(tmp_path))
    thread, turn = uuid4(), uuid4()
    first = await source.observe(_request(tmp_path, thread, turn, 1), CancelToken())

    # 模型在根目录新建一个临时文件：同一Turn的后续步骤仍是首次观察。
    (tmp_path / "repro.py").write_text("print(1)\n", encoding="utf-8")
    later = await source.observe(_request(tmp_path, thread, turn, 7), CancelToken())
    assert later == first and "repro.py" not in later.documents[0].content

    following = await source.observe(_request(tmp_path, thread, uuid4(), 1), CancelToken())
    assert following != first and "repro.py" in following.documents[0].content
    assert source.source_id == WorkspaceContextSource(tmp_path).source_id
    assert source.kind == WorkspaceContextSource(tmp_path).kind


async def test_threads_do_not_share_an_observation_and_the_memory_is_bounded(
    tmp_path: Path,
) -> None:
    source = TurnStableContextSource(WorkspaceContextSource(tmp_path))
    first_thread, first_turn = uuid4(), uuid4()
    before = await source.observe(_request(tmp_path, first_thread, first_turn, 1), CancelToken())
    (tmp_path / "new.txt").write_text("x", encoding="utf-8")

    other = await source.observe(_request(tmp_path, uuid4(), uuid4(), 1), CancelToken())
    assert "new.txt" in other.documents[0].content
    again = await source.observe(_request(tmp_path, first_thread, first_turn, 2), CancelToken())
    assert again == before

    for _ in range(_MAX_THREADS):
        await source.observe(_request(tmp_path, uuid4(), uuid4(), 1), CancelToken())
    # 被淘汰的Thread只是重新观察一次，不报错。
    evicted = await source.observe(_request(tmp_path, first_thread, first_turn, 3), CancelToken())
    assert "new.txt" in evicted.documents[0].content


async def test_product_instructions_stay_identical_within_a_turn_when_the_root_changes(
    tmp_path: Path,
) -> None:
    """开发集60个试次里有26次系统指令在Turn中途变化，全部来自一级目录概览；每次都使服务端重算整段历史。"""
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    context = build_product_agent_context(tmp_path, max_output_tokens=4096, generic_execution=True)
    thread, turn = uuid4(), uuid4()

    first = await context.context.prepare(_request(tmp_path, thread, turn, 1), CancelToken())
    (tmp_path / "scratch_test.py").write_text("assert True\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    second = await context.context.prepare(_request(tmp_path, thread, turn, 2), CancelToken())
    assert second.instructions == first.instructions

    next_turn = await context.context.prepare(_request(tmp_path, thread, uuid4(), 1), CancelToken())
    layout = next(
        item["content"]
        for item in json.loads(next_turn.instructions or "")["fragments"]
        if item["source"] == "workspace/layout.json"
    )
    assert "scratch_test.py" in layout and next_turn.instructions != first.instructions
