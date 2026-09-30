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
        "首次apply_patch_batch前",
        "不得把基线与修改并发提交",
        "process_nonzero_exit",
        "一次修改后检查不能同时充当基线和最终验证",
        "artifact.artifact_id",
        "不能使用process_id、路径或空参数",
        "tool_wrong_file_type",
        "不能据此宣称工具不存在",
        "不要原样重复失败调用",
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
    assert required in runtime[0]["content"]
    assert runtime[0]["source"] == "harnessix.coding-instructions/v3"


def test_workflow_refinement_keeps_instruction_size_and_original_boundaries(tmp_path: Path) -> None:
    """原v2实际字节数是大小护栏，不扩大上下文、压缩或任务预算来遮掩失败。"""
    assert len(CODING_INSTRUCTIONS.encode()) <= 2751
    for required in (
        "runtime_instruction优先于项目指令",
        "AGENTS.md/AGENTS.override.md",
        "低信任资料",
        "保留用户已有修改",
        "digest_status=complete",
        "content_sha256",
        "分页revision不是内容SHA-256",
        "不猜造selectors",
        "取消、超时、预算耗尽或不确定副作用",
        "不自动重放有副作用操作",
        "不删测试、不放宽断言",
        "不假定shell、联网、安装或自动Git推送",
    ):
        assert required in CODING_INSTRUCTIONS
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
        assert fragment["source"] == "harnessix.coding-instructions/v3"
        assert fragment["trust"] == "runtime" and fragment["content"] == CODING_INSTRUCTIONS
        fingerprint = hashlib.sha256(sent_instructions.encode()).hexdigest()
        assert turn.context_inspections[0].instruction_fingerprint == fingerprint
        restored = await SQLiteSessionStore(session_path).get_thread(thread_id)
        assert restored.turns[-1].context_inspections[0].instruction_fingerprint == fingerprint
        fingerprints.append(fingerprint)
    assert fingerprints[0] == fingerprints[1]
