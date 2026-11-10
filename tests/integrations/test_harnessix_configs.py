"""Harbor任务容器内Harnessix配置生成：不依赖Harbor包，本地全量回归即可覆盖。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harnessix.product_config.agent_context import build_product_agent_context
from harnessix.product_config.contracts import ProductConfigV2
from integrations.harbor.harnessix_configs import (
    CONTEXT_WINDOW_TOKENS,
    EVAL_ALLOWED_OVERRIDES,
    apply_eval_overrides,
    product_config,
)
from integrations.harbor.task_environment import MODEL


def test_harbor_product_config_declares_the_local_model_window() -> None:
    profile = json.loads(product_config(18001))["profiles"][0]
    assert profile["context_window_tokens"] == CONTEXT_WINDOW_TOKENS[MODEL] == 262_144
    other = json.loads(product_config(18001, model="other-model"))["profiles"][0]
    assert "context_window_tokens" not in other


def test_the_harbor_arm_sends_compact_tool_results():
    """评测组别沿用产品向导的默认值：紧凑工具结果开启。"""
    provider = json.loads(product_config(18001))["providers"][0]
    assert provider["kind"] == "openai_chat" and provider["compact_tool_results"] is True


def test_the_harbor_arm_turns_on_the_completion_guard():
    """评测组别是无人值守任务：纯文本提前收工改为补发继续提示。"""
    config = json.loads(product_config(18001))
    assert config["completion_guard"] is True


def test_eval_overrides_are_the_documented_eval_product_differences():
    """评测只覆盖已登记、有文档记录的差异；新增覆盖项必须先在这里登记。"""
    assert EVAL_ALLOWED_OVERRIDES == {"completion_guard", "compaction"}


def test_the_harbor_arm_keeps_the_eval_budget_compaction_point(tmp_path: Path) -> None:
    """评测预算口径：262144 Token窗口在262144字节处压缩、压到131072字节，与改为可配置前完全一致。

    产品默认在窗口一半处压缩、压到四分之一；评测的累计Token预算要求更早压缩（已知差异）。
    """
    config = ProductConfigV2.model_validate_json(product_config(18001))
    assert config.compaction == "third_window"
    profile = config.profiles[0]
    assert profile.context_window_tokens == 262_144
    context = build_product_agent_context(
        tmp_path,
        max_output_tokens=profile.max_output_tokens,
        context_window_tokens=profile.context_window_tokens,
        context_compaction=config.compaction,
    )
    assert profile.max_output_tokens == 4096
    assert context.compaction.trigger_history_tokens == 262_144
    assert context.compaction.policy.target_history_tokens == 131_072
    assert context.compaction.policy.summary_reserve_tokens == 20_480
    assert context.compaction.policy.max_summary_input_tokens == (262_144 - 4096 - 1536) * 3
    assert context.compaction.max_summary_output_tokens == 4096
    product_default = build_product_agent_context(
        tmp_path,
        max_output_tokens=profile.max_output_tokens,
        context_window_tokens=profile.context_window_tokens,
    )
    assert product_default.compaction.trigger_history_tokens == 393_216
    assert product_default.compaction.policy.target_history_tokens == 196_608


def test_eval_overrides_reject_fields_outside_the_whitelist():
    config = ProductConfigV2.model_validate_json(product_config(18001))
    assert apply_eval_overrides(config, {"completion_guard": False}).completion_guard is False
    with pytest.raises(SystemExit, match="workspace_read_timeout_seconds"):
        apply_eval_overrides(config, {"workspace_read_timeout_seconds": 60})
    # 白名单内的字段仍按产品合同重新校验，非法压缩点不能借覆盖进入配置。
    with pytest.raises(ValueError):
        apply_eval_overrides(config, {"compaction": "quarter_window"})


def test_harnessix_arms_only_use_known_agent_options():
    """新增评测选项必须先在这里登记，避免评测悄悄用上用户拿不到的配置。"""
    allowed = {
        "action_config",
        "action_config_template",
        "approval_mode",
        "bundle_root",
        "external_boundary",
        "install_bundle",
        "max_steps",
        "max_tokens",
        "model_relays",
        # 评测基础设施：智能体运行阶段断开外网，各组一致，不是产品配置。
        "network_isolation",
        "normalize_workspace_modes",
        # 任务模板是已知差异，由不带模板的产品默认组对照。
        "prompt_template_path",
        "python_executable",
        "server_config",
        "server_config_template",
        "state_directory",
        "timeout_seconds",
        "workspace",
    }
    root = Path(__file__).resolve().parents[2] / "benchmarks" / "harbor"
    seen: set[str] = set()
    for path in root.rglob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        arms = document.get("arms", []) if isinstance(document, dict) else []
        for arm in arms:
            if "harnessix" in str(arm.get("system", "")).lower():
                seen |= set(arm["agent"].get("kwargs", {}))
    assert seen and seen <= allowed, sorted(seen - allowed)
