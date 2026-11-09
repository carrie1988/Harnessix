"""通用执行按实际反馈推进，不能继续注入固定的Patch→Profile阶段约束。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.context.contracts import ContextBuildInput
from harnessix.product_config.agent_context import build_product_agent_context


async def test_generic_context_preserves_task_rules_without_forced_repair_stages(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("运行项目已有测试，不修改锁文件。", encoding="utf-8")
    context = build_product_agent_context(tmp_path, generic_execution=True, max_output_tokens=4096)
    request = ContextBuildInput(
        thread_id=uuid4(), turn_id=uuid4(), model_step=1, workspace=str(tmp_path)
    )
    prepared = await context.context.prepare(request, CancelToken())
    fragments = json.loads(prepared.instructions or "")["fragments"]
    runtime = [item for item in fragments if item["kind"] == "runtime_instruction"]
    assert len(runtime) == 1
    assert runtime[0]["source"] == "harnessix.generic-execution-instructions/v4"
    instructions = runtime[0]["content"]
    assert "exec_command.<profile>" in instructions
    assert "Exit zero alone does not prove business" in instructions
    assert "content_sha256" in instructions
    assert "pre-edit baseline" not in instructions
    assert "Only process_nonzero_exit opens" not in instructions
    assert "force" not in instructions
    assert "Network is disabled" in instructions
    assert "do not replay the command blindly" in instructions
    project = [item for item in fragments if item["kind"] == "project_instruction"]
    assert len(project) == 1 and "不修改锁文件" in project[0]["content"]


@pytest.mark.parametrize(
    "options",
    [
        {"generic_execution": "true"},
        {"generic_execution": True, "allow_optional_pre_edit_profile": True},
        {"generic_execution": True, "compact_instructions": True},
    ],
)
def test_generic_context_rejects_conflicting_workflow_modes(tmp_path: Path, options: dict):
    with pytest.raises(ValueError):
        build_product_agent_context(tmp_path, **options)
