from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.agent.model_text import protect_request
from harnessix.agent.models import Budget, Item, ItemStatus, TextContent, Turn
from harnessix.agent.runtime import AgentRuntime
from harnessix.context.contracts import ContextBuildInput, ContextInspectionV3
from harnessix.context.engine import ContextPreparationError
from harnessix.context.sources import ContextSourceError
from harnessix.context.tool_result_view import history_document
from harnessix.domain.models import ToolDescriptor
from harnessix.models.contracts import ModelRequest
from harnessix.product_config.agent_context import (
    CODING_INSTRUCTIONS_VERSION,
    build_product_agent_context,
)
from harnessix.product_config.context_budget import PRODUCT_CONTEXT_INPUT_LIMIT
from harnessix.secrets.publication import MAX_SCAN_BYTES
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
    assert "pre-edit baseline" in runtime[0]["content"]
    assert "final workspace" in runtime[0]["content"]
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


async def test_post_patch_only_context_matches_forced_validation_workflow(tmp_path: Path) -> None:
    policy = build_product_agent_context(tmp_path, require_pre_edit_profile=False)
    prepared = await policy.context.prepare(_request(tmp_path), CancelToken())
    fragments = json.loads(prepared.instructions or "")["fragments"]
    runtime = next(item for item in fragments if item["kind"] == "runtime_instruction")
    instructions = " ".join(runtime["content"].split())

    assert "post-patch-only validation" in instructions
    assert "run_profile stays hidden before the first patch" in instructions
    assert "locating the exact named definition before editing" in instructions
    assert "40-60 line windows and max_utf8_bytes=4096" in instructions
    assert "expand only for missing evidence" in instructions
    assert "grep is not regex" in instructions
    assert "use git_review when available" in instructions
    assert "at most four source inspections" not in instructions
    assert "After every successful patch, call the forced run_profile" in instructions
    assert "Only process_nonzero_exit opens diagnosis and correction" in instructions
    assert "unknown state, or unavailable output fails closed" in instructions
    assert "pre-edit baseline" not in instructions


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


def test_declared_model_window_scales_context_and_compaction_limits(tmp_path: Path) -> None:
    fixed = build_product_agent_context(tmp_path, max_output_tokens=4096)
    windowed = build_product_agent_context(
        tmp_path, max_output_tokens=4096, context_window_tokens=262_144
    )

    # 估算器按UTF-8字节计；声明窗口后按每Token 3字节换算，窗口三分之一触发、压到六分之一。
    assert windowed.compaction.trigger_history_tokens == 262_144 * 3 // 3
    assert windowed.compaction.policy.target_history_tokens == 262_144 * 3 // 6
    assert windowed.compaction.policy.max_summary_input_tokens == (262_144 - 4096 - 1536) * 3
    assert windowed.compaction.max_summary_output_tokens == 4096
    assert windowed.compaction.policy.summary_reserve_tokens == 20_480
    # 未声明窗口时与原固定上限和摘要容量一致。
    assert fixed.compaction.max_summary_output_tokens == 2048
    assert fixed.compaction.policy.summary_reserve_tokens == 12_288
    assert fixed.compaction.trigger_history_tokens == PRODUCT_CONTEXT_INPUT_LIMIT // 2
    assert fixed.compaction.policy.target_history_tokens == PRODUCT_CONTEXT_INPUT_LIMIT // 4
    assert fixed.compaction.policy.max_summary_input_tokens == PRODUCT_CONTEXT_INPUT_LIMIT


async def test_declared_model_window_raises_the_context_input_limit(tmp_path: Path) -> None:
    windowed = build_product_agent_context(
        tmp_path, max_output_tokens=4096, context_window_tokens=262_144
    )
    prepared = await windowed.context.prepare(_request(tmp_path), CancelToken())

    assert prepared.inspection.available_input_tokens == (262_144 - 4096 - 1536) * 3
    assert prepared.inspection.limits.reserved_output_tokens == 4096


@pytest.mark.parametrize("window", [2 * (4096 + 1536), 2_097_153, True])
def test_model_window_must_leave_room_for_output_and_compaction(
    tmp_path: Path, window: object
) -> None:
    with pytest.raises(ValueError, match="上下文窗口"):
        build_product_agent_context(
            tmp_path,
            max_output_tokens=4096,
            context_window_tokens=window,  # type: ignore[arg-type]
        )


def _request_at(estimated: int, catalog: tuple[ToolDescriptor, ...]) -> ModelRequest:
    """历史、工具与指令的估算合计为estimated；少量大文本项只考验字节容量，不触及节点数上限。"""
    remaining = estimated - sum(
        len(
            json.dumps(
                tool.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        for tool in catalog
    )
    history: list[Item] = []
    while remaining > 0:
        item = Item(
            item_id=uuid4(), status=ItemStatus.COMPLETED, content=TextContent(kind="user_message")
        )
        text = "a" * min(65_536, max(0, remaining - len(history_document(item).encode())))
        item = Item(
            item_id=item.item_id,
            status=ItemStatus.COMPLETED,
            content=TextContent(kind="user_message", text=text),
        )
        history.append(item)
        remaining -= len(history_document(item).encode())
    return ModelRequest(
        thread_id=uuid4(),
        turn_id=uuid4(),
        step=1,
        history=tuple(history),
        tools=catalog,
        catalog_tools=catalog,
        budget=Budget(),
    )


async def test_window_derived_limits_keep_outbound_requests_within_the_protection_scan(
    tmp_path: Path,
) -> None:
    window = 2_097_152
    policy = build_product_agent_context(
        tmp_path, max_output_tokens=4096, context_window_tokens=window
    )
    # 最大窗口按每Token 3字节换算远超出站请求的扫描容量；换算结果截到扫描容量的四分之三。
    input_limit = policy.compaction.policy.max_summary_input_tokens
    assert input_limit == MAX_SCAN_BYTES * 3 // 4
    assert policy.compaction.trigger_history_tokens == input_limit // 3
    assert policy.compaction.policy.target_history_tokens == input_limit // 6
    prepared = await policy.context.prepare(_request(tmp_path), CancelToken())
    assert prepared.inspection.available_input_tokens == input_limit

    async with CodingToolRuntime(tmp_path) as tools:
        catalog = tools.definitions()
    with protected() as scope:
        # 估算恰在上限的请求另带完整线上目录，仍能通过出站保护扫描。
        await protect_request(_request_at(input_limit, catalog), scope, CancelToken())
        with pytest.raises(KernelError) as caught:
            await protect_request(
                _request_at((window - 4096 - 1536) * 3, catalog), scope, CancelToken()
            )
    assert caught.value.code == "public_output_limit"


async def test_small_declared_window_compacts_before_the_fixed_threshold(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "state.db"
    seeds = SequenceProvider([accounted_text("事实" * 400) for _ in range(14)])
    policy = build_product_agent_context(
        workspace, max_output_tokens=4096, context_window_tokens=32_768
    )
    # 小窗口的摘要容量随压缩目标收缩，仍为保留历史留出空间。
    assert policy.compaction.trigger_history_tokens == 32_768
    assert policy.compaction.policy.target_history_tokens == 16_384
    assert policy.compaction.policy.summary_reserve_tokens == 4096
    assert policy.compaction.max_summary_output_tokens == 819
    # 种子历史在固定上限下累积到小窗口触发点与固定触发点之间；单组较小，近期组放得进压缩目标。
    seeding = build_product_agent_context(workspace, max_output_tokens=4096)
    async with AgentRuntime(
        SQLiteSessionStore(path),
        seeds,
        async_context=seeding.context,
        compaction=seeding.compaction,
        summary_provider=seeds,
    ) as runtime:
        thread = await runtime.create_thread(str(workspace))
        for index in range(14):
            seeded = await runtime.run_turn(
                thread.thread_id, "调查实现与测试", request_id=f"seed-{index}"
            )
            assert seeded.status == "completed" and not seeded.compactions

    async def continue_turn(window: int | None) -> Turn:
        final_provider = SequenceProvider([accounted_text("已完成验证。")])
        summary = SequenceProvider([accounted_text("保留任务、已验证事实和未完成检查。")])
        fresh = build_product_agent_context(
            workspace, max_output_tokens=4096, context_window_tokens=window
        )
        async with AgentRuntime(
            SQLiteSessionStore(path),
            final_provider,
            async_context=fresh.context,
            compaction=fresh.compaction,
            summary_provider=summary,
        ) as runtime:
            return await runtime.run_turn(
                thread.thread_id, "继续修复", request_id=f"continue-{window}"
            )

    # 同一历史在固定上限下不压缩，声明的小窗口则在其三分之一处压缩。
    fixed = await continue_turn(None)
    assert fixed.status == "completed" and not fixed.compactions
    windowed = await continue_turn(32_768)
    assert windowed.status == "completed"
    assert [compaction.status for compaction in windowed.compactions] == ["summarized"]
