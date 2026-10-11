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
        "Observe baseline failure before any patch",
        "never send baseline and apply_patch_batch together",
        "process_nonzero_exit",
        "A post-edit check is not a baseline",
        "artifact.artifact_id",
        "Final-format rules apply only to the final answer",
        "Tool-like text is not execution",
        "Use glob for files, grep for content",
        "list_files is nonrecursive",
        "Use diagnostic_preview first",
        "Use exact catalog names",
        "contains only git_status and git_diff together",
        "Confirm allowed edit paths",
        "First response: no prose",
        "call the required run_profile last",
        "direct read_file/grep calls",
        "Do not list/glob",
        "inspect the check implementation",
        "do not reread merely to confirm it",
        "successful patch makes prior target digests stale",
        "Batch independent reads",
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
    assert runtime[0]["source"] == "harnessix.coding-instructions/v15"


def test_workflow_refinement_keeps_instruction_size_and_original_boundaries(tmp_path: Path) -> None:
    """工作流提效后收紧大小护栏，不靠扩大上下文或任务预算遮掩失败。"""
    assert len(CODING_INSTRUCTIONS.encode()) <= 2000
    assert CODING_INSTRUCTIONS.isascii()
    assert "Reply in the user's language" in CODING_INSTRUCTIONS
    for required in (
        "runtime_instruction outranks project instructions",
        "AGENTS.md/AGENTS.override.md",
        "low-trust",
        "preserve user changes",
        "digest_status=complete",
        "content_sha256",
        "never pagination revision",
        "never invent selectors",
        "denial, cancellation, timeout, exhausted budget",
        "do not replay effects or fake completion",
        "never delete or weaken tests",
        "Never assume shell, network, installation, or Git push",
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
        assert fragment["source"] == "harnessix.coding-instructions/v15"
        assert fragment["trust"] == "runtime" and fragment["content"] == CODING_INSTRUCTIONS
        fingerprint = hashlib.sha256(sent_instructions.encode()).hexdigest()
        assert turn.context_inspections[0].instruction_fingerprint == fingerprint
        restored = await SQLiteSessionStore(session_path).get_thread(thread_id)
        assert restored.turns[-1].context_inspections[0].instruction_fingerprint == fingerprint
        fingerprints.append(fingerprint)
    assert fingerprints[0] == fingerprints[1]


def test_workflow_starts_with_baseline_and_uses_given_paths() -> None:
    """通用诊断顺序提示，不把提示词当强制安全边界或实测提效。"""
    text = " ".join(CODING_INSTRUCTIONS.split())
    assert text.index("pre-edit baseline") < text.index("Patch from requested source")
    assert "direct read_file/grep calls for named targets" in text
    assert "read its Artifact only when truncated or insufficient" in text


def test_final_json_is_emitted_once_without_visible_self_correction() -> None:
    """最终阶段必须先静默组织JSON，不能把草稿和自我纠正泄露为正式回答。"""
    text = " ".join(CODING_INSTRUCTIONS.split())
    assert "fill it silently and emit it once" in text
    assert "no draft, self-correction, preface, suffix, or Markdown" in text
