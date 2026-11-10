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
    assert runtime[0]["source"] == "harnessix.generic-execution-instructions/v5"
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


async def _runtime_fragment(root: Path, **options: bool) -> dict[str, str]:
    context = build_product_agent_context(root, max_output_tokens=4096, **options)
    request = ContextBuildInput(
        thread_id=uuid4(), turn_id=uuid4(), model_step=1, workspace=str(root)
    )
    prepared = await context.context.prepare(request, CancelToken())
    fragments = json.loads(prepared.instructions or "")["fragments"]
    (runtime,) = [item for item in fragments if item["kind"] == "runtime_instruction"]
    return runtime


async def test_external_boundary_instructions_state_only_what_holds_there(tmp_path: Path):
    """外部边界（ADR 0121）：命令在任务环境中直接运行、改动保留。

    不沿用容器指令的只读工作区、断网与/tmp逐次清空说法，也不得到本机沙箱指令。
    """
    generic = await _runtime_fragment(tmp_path, generic_execution=True)
    external = await _runtime_fragment(
        tmp_path, generic_execution=True, external_boundary_execution=True
    )
    assert generic["source"] == "harnessix.generic-execution-instructions/v5"
    assert external["source"] == "harnessix.external-boundary-execution-instructions/v1"
    text = external["content"]
    for required in (
        "Commands run directly in the task environment, starting at the workspace root",
        "writes to the workspace, /tmp and\ninstalled packages persist into later calls",
        "cd\nand exported variables do not carry over",
        "edit source only with trusted patches",
        "Use apply_patch_batch for source changes.",
        "Read tools read files inside the workspace, with paths relative to its root",
        "reachable only through exec_command",
        "Network access depends on the environment.",
    ):
        assert required in text, required
    for contradicting in (
        "Inside execution containers",
        "workspace is read-only",
        "snapshot",
        "vanish",
        "Network is disabled",
        "no network",
        "temporary per invocation",
        "Do not assume that packages",
        "scratch work in one",
        "/harnessix/deps",
        "local sandbox",
        "host_command",
    ):
        assert contradicting.lower() not in text.lower(), contradicting
    # 执行环境一段以外与通用执行指令逐字相同：只改如实描述的执行语义，不改任务规则。
    environment_start = generic["content"].index("The host fixes the image")
    environment_end = generic["content"].index("After a definitive failure")
    assert text.startswith(generic["content"][:environment_start])
    assert text.endswith(generic["content"][environment_end:])


@pytest.mark.parametrize(
    "options",
    [
        {"external_boundary_execution": True},
        {"external_boundary_execution": "true", "generic_execution": True},
        {"external_boundary_execution": True, "generic_execution": True, "native_execution": True},
    ],
)
def test_external_boundary_instructions_need_generic_execution_without_native(
    tmp_path: Path, options: dict
):
    with pytest.raises(ValueError):
        build_product_agent_context(tmp_path, max_output_tokens=4096, **options)
