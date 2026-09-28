from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.models import TextContent
from harnessix.agent.runtime import AgentRuntime
from harnessix.context.contracts import ContextBuildInput, ContextInspectionV3
from harnessix.context.engine import ContextPreparationError
from harnessix.context.sources import ContextSourceError
from harnessix.product_config.agent_context import (
    CODING_INSTRUCTIONS_VERSION,
    PRODUCT_CONTEXT_INPUT_LIMIT,
    build_product_agent_context,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from tests.agent.test_publication import CANARY, protected
from tests.context.test_compaction_runtime import SequenceProvider, accounted_text


def _request(root: Path, **updates: object) -> ContextBuildInput:
    return ContextBuildInput(
        thread_id=uuid4(), turn_id=uuid4(), model_step=1, workspace=str(root), **updates
    )


async def test_shared_context_preserves_trust_sources_budget_and_does_not_capture_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "AGENTS.md").write_text("维护公开接口，增加失败恢复测试。", encoding="utf-8")
    (tmp_path / "AGENTS.override.md").write_text("项目覆盖规则", encoding="utf-8")
    (tmp_path / ".env").write_text("PRIVATE_CANARY=must-not-publish", encoding="utf-8")
    monkeypatch.setenv("ORDINARY_CONTEXT_CANARY", "must-not-capture-environment")
    monkeypatch.setenv("BAILIAN_API_KEY", "must-not-capture-credential")
    policy = build_product_agent_context(tmp_path, max_output_tokens=4096)
    prepared = await policy.context.prepare(_request(tmp_path), CancelToken())

    assert isinstance(prepared.inspection, ContextInspectionV3)
    assert prepared.inspection.available_input_tokens == PRODUCT_CONTEXT_INPUT_LIMIT
    assert prepared.inspection.limits.reserved_output_tokens == 4096
    fragments = json.loads(prepared.instructions or "")["fragments"]
    runtime = [item for item in fragments if item["kind"] == "runtime_instruction"]
    assert len(runtime) == 1 and runtime[0]["source"] == CODING_INSTRUCTIONS_VERSION
    assert runtime[0]["trust"] == "runtime"
    assert "基线" in runtime[0]["content"] and "最终工作区" in runtime[0]["content"]
    projects = [item for item in fragments if item["kind"] == "project_instruction"]
    assert [item["source"] for item in projects] == ["AGENTS.override.md"]
    assert projects[0]["trust"] == "project"
    assert "维护公开接口" not in (prepared.instructions or "")
    assert "must-not" not in (prepared.instructions or "")
    assert "项目覆盖规则" not in prepared.inspection.model_dump_json()
    assert [item.source_id for item in prepared.inspection.sources] == [
        "project/instructions",
        "workspace/layout",
        "environment/runtime",
    ]
    async with CodingToolRuntime(tmp_path) as tools:
        assert all(
            item.workspace_scope == tools.workspace_scope for item in prepared.inspection.sources
        )


async def test_project_instruction_refresh_changes_persistable_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("原项目规则", encoding="utf-8")
    policy = build_product_agent_context(tmp_path)
    first = await policy.context.prepare(_request(tmp_path), CancelToken())
    path.write_text("新的项目规则，不扩大接口。", encoding="utf-8")
    second = await policy.context.prepare(_request(tmp_path), CancelToken())
    assert first.inspection.instruction_fingerprint != second.inspection.instruction_fingerprint
    assert "原项目规则" not in (second.instructions or "")
    assert "新的项目规则" in (second.instructions or "")


@pytest.mark.parametrize("invalid", [True, 0, 1_000_001, "4096"])
def test_output_reserve_rejects_invalid_configuration(tmp_path: Path, invalid: object) -> None:
    with pytest.raises(ValueError):
        build_product_agent_context(tmp_path, max_output_tokens=invalid)  # type: ignore[arg-type]


async def test_fixed_history_overflow_fails_without_provider_request(tmp_path: Path) -> None:
    policy = build_product_agent_context(tmp_path)
    with pytest.raises(ContextPreparationError) as error:
        await policy.context.prepare(
            _request(tmp_path, history_documents=("x" * (PRODUCT_CONTEXT_INPUT_LIMIT + 1),)),
            CancelToken(),
        )
    assert error.value.code == "context_budget_exceeded"


async def test_oversized_project_instruction_is_not_silently_ignored(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("x\n" * 33_000, encoding="utf-8")
    with pytest.raises(ContextSourceError) as error:
        await build_product_agent_context(tmp_path).context.prepare(
            _request(tmp_path), CancelToken()
        )
    assert error.value.code == "context_source_too_large"


async def test_project_instruction_with_active_secret_is_rejected_before_provider(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text(f"不能公开的内容：{CANARY}", encoding="utf-8")
    path = tmp_path / "state.db"
    provider = SequenceProvider([accounted_text("不应请求模型。")])
    policy = build_product_agent_context(workspace)
    with protected() as scope:
        async with AgentRuntime(
            SQLiteSessionStore(path),
            provider,
            async_context=policy.context,
            compaction=policy.compaction,
            summary_provider=provider,
            public_output_protection=scope,
        ) as runtime:
            thread = await runtime.create_thread(str(workspace))
            turn = await runtime.run_turn(thread.thread_id, "调查源码", request_id="secret-source")
    assert turn.status == "failed" and turn.error is not None
    assert turn.error.code == "public_output_secret_leak"
    assert not provider.requests
    assert CANARY not in turn.model_dump_json()
    assert CANARY.encode() not in path.read_bytes()


async def test_default_compaction_survives_reopen_and_accounts_summary_attempt(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "state.db"
    normal = SequenceProvider([accounted_text("事实" * 8000) for _ in range(3)])
    policy = build_product_agent_context(workspace)
    async with AgentRuntime(
        SQLiteSessionStore(path),
        normal,
        async_context=policy.context,
        compaction=policy.compaction,
        summary_provider=normal,
    ) as runtime:
        thread = await runtime.create_thread(str(workspace))
        for index in range(3):
            turn = await runtime.run_turn(
                thread.thread_id, "调查实现与测试", request_id=f"seed-{index}"
            )
            assert turn.status == "completed"

    final_provider = SequenceProvider([accounted_text("已完成验证。")])
    summary = SequenceProvider([accounted_text("保留任务、已验证事实和未完成检查。")])
    reopened = SQLiteSessionStore(path)
    fresh = build_product_agent_context(workspace)
    async with AgentRuntime(
        reopened,
        final_provider,
        async_context=fresh.context,
        compaction=fresh.compaction,
        summary_provider=summary,
    ) as runtime:
        final = await runtime.run_turn(thread.thread_id, "继续修复", request_id="continue")

    assert final.status == "completed"
    assert len(final.compactions) == 1 and final.compactions[0].status == "summarized"
    assert final.usage_is_complete and final.usage.input_tokens == 20
    assert len(summary.requests) == 1 and summary.requests[0].tools == ()
    assert summary.requests[0].remaining_tokens == 2048
    assert len(final.context_inspections) == 1
    assert len((await reopened.get_thread(thread.thread_id)).compaction_windows) == 1
    assert final_provider.requests[0].instructions is not None
    assert all(
        not isinstance(item.content, TextContent)
        or "derived_history" in item.content.text
        or item.content.text != "事实" * 8000
        for item in final_provider.requests[0].history[:-3]
    )
