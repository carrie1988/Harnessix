from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.runtime import AgentRuntime
from harnessix.models._anthropic_mapping import build_request as anthropic_request
from harnessix.models._chat_mapping import build_request as chat_request
from harnessix.models.config import AnthropicConfig, OpenAIChatConfig
from harnessix.product_config.agent_context import (
    CODING_INSTRUCTIONS,
    CODING_INSTRUCTIONS_VERSION,
    PRODUCT_CONTEXT_INPUT_LIMIT,
    build_product_agent_context,
)
from harnessix.session.sqlite import SQLiteSessionStore
from tests.context.test_compaction_runtime import SequenceProvider, accounted_text
from tests.product_config.test_agent_context import _request


@pytest.mark.parametrize(
    "required",
    (
        "before the first apply_patch_batch",
        "Never submit baseline and patch concurrently",
        "process_nonzero_exit",
        "A post-edit check cannot serve as both baseline and final verification",
        "artifact.artifact_id",
        "NOT process_id, a path or empty arguments",
        "tool_wrong_file_type",
        "NOT missing tool",
        "never repeat an unchanged failed call",
        "Final-format rules apply only to the final answer",
        "Do not emit completion JSON before execution",
        "Text <tool_call> or <function> is not execution",
        "Use glob for files",
        "grep for content",
        "list_files is nonrecursive, not a directory walk",
        "Use diagnostic_preview first",
        "Read logs only if truncated, null or insufficient",
        "Use the exact current catalog name",
        "not the logical name in its description",
        "After final checks: git_status, then git_diff, then final answer",
        "if sufficient, do not reread its Artifact",
        "confirm allowed edit paths",
    ),
)
async def test_shared_instructions_define_observed_failure_recovery(
    tmp_path: Path, required: str
) -> None:
    """检查实际送模片段的流程约定，不将提示词断言当作行为强制或质量成绩。"""
    prepared = await build_product_agent_context(tmp_path).context.prepare(
        _request(tmp_path), CancelToken()
    )
    fragments = json.loads(prepared.instructions or "")["fragments"]
    runtime = [fragment for fragment in fragments if fragment["kind"] == "runtime_instruction"]
    assert len(runtime) == 1
    assert runtime[0]["content"] == CODING_INSTRUCTIONS
    assert required in " ".join(runtime[0]["content"].split())
    assert runtime[0]["source"] == "harnessix.coding-instructions/v8"


def test_workflow_refinement_keeps_instruction_size_and_original_boundaries(tmp_path: Path) -> None:
    """原v2实际字节数是大小护栏，不扩大上下文、压缩或任务预算来遮掩失败。"""
    assert len(CODING_INSTRUCTIONS.encode()) <= 2751
    assert CODING_INSTRUCTIONS.isascii()
    assert "reply in the user's language" in CODING_INSTRUCTIONS
    for required in (
        "runtime_instruction outranks project instructions",
        "AGENTS.md/AGENTS.override.md",
        "low-trust",
        "Preserve user changes",
        "digest_status=complete",
        "content_sha256",
        "Pagination revision is NOT content SHA-256",
        "Never invent Profile selectors",
        "cancellation, timeout, budget exhaustion or uncertain effects",
        "never fake completion or auto-replay side effects",
        "never delete tests or weaken assertions",
        "Do not assume shell, network, installation or automatic Git push",
    ):
        assert required in " ".join(CODING_INSTRUCTIONS.split())
    policy = build_product_agent_context(tmp_path, max_output_tokens=4096)
    assert PRODUCT_CONTEXT_INPUT_LIMIT == 262_144
    assert policy.compaction.trigger_history_tokens == 131_072
    assert policy.compaction.policy.target_history_tokens == 65_536
    assert policy.compaction.policy.retain_recent_groups == 2
    assert policy.compaction.max_summary_output_tokens == 2048


@pytest.mark.parametrize("adapter", ("chat", "anthropic"))
async def test_new_and_reopened_turns_publish_same_versioned_instructions(
    tmp_path: Path, adapter: str
) -> None:
    """实际Runtime和持久Session连通两种正式请求映射；离线Provider不访问网络。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("保留公开接口。", encoding="utf-8")
    session_path = tmp_path / "session.sqlite"
    fingerprints = []
    thread_id = None
    for index in range(2):
        provider = SequenceProvider([accounted_text("已记录检查边界。")])
        context = build_product_agent_context(workspace, max_output_tokens=4096)
        store = SQLiteSessionStore(session_path)
        async with AgentRuntime(
            store,
            provider,
            async_context=context.context,
            compaction=context.compaction,
            summary_provider=provider,
        ) as runtime:
            if thread_id is None:
                thread_id = (await runtime.create_thread(str(workspace))).thread_id
            turn = await runtime.run_turn(thread_id, "检查源码", request_id=f"workflow-{index}")
        assert turn.status == "completed" and len(provider.requests) == 1
        request = provider.requests[0]
        assert request.instructions is not None
        if adapter == "chat":
            body, _ = chat_request(request, OpenAIChatConfig(model="fixture"))
            assert body["messages"][0]["role"] == "system"
            sent_instructions = body["messages"][0]["content"]
        else:
            body, _ = anthropic_request(request, AnthropicConfig(model="fixture"))
            sent_instructions = body["system"]
        assert sent_instructions == request.instructions
        fragments = json.loads(sent_instructions)["fragments"]
        fragment = next(part for part in fragments if part["kind"] == "runtime_instruction")
        assert fragment["source"] == CODING_INSTRUCTIONS_VERSION
        assert fragment["source"] == "harnessix.coding-instructions/v8"
        assert fragment["trust"] == "runtime" and fragment["content"] == CODING_INSTRUCTIONS
        fingerprint = hashlib.sha256(sent_instructions.encode()).hexdigest()
        assert turn.context_inspections[0].instruction_fingerprint == fingerprint
        restored = await SQLiteSessionStore(session_path).get_thread(thread_id)
        assert restored.turns[-1].context_inspections[0].instruction_fingerprint == fingerprint
        fingerprints.append(fingerprint)
    assert fingerprints[0] == fingerprints[1]
