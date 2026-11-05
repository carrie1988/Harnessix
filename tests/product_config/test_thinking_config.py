"""产品配置的思考设置：已求证端点默认只在工具步骤思考，旧配置的序列化、摘要与行为不变。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import (
    ModelProfile,
    ProductConfigV2,
    ProviderDefinition,
    effective_thinking,
    product_config_digest,
)
from harnessix.product_config.product_contracts import ConfigurationDraft
from harnessix.product_config.runtime import default_provider_factory
from harnessix.product_config.wizard import build_product_config, thinking_preset
from harnessix.product_ui.cli import _configure

BAILIAN = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEEPSEEK = "https://api.deepseek.com"
LOCAL_VLLM = "http://127.0.0.1:18002/v1"


def _draft(**changes) -> ConfigurationDraft:
    fields = {"provider_kind": "openai_chat", "base_url": BAILIAN, "model": "deepseek-v4-pro-0813"}
    return ConfigurationDraft.model_validate({**fields, **changes})


def _bailian(**changes) -> ProductConfigV2:
    return build_product_config(
        _draft(thinking_protocol="dashscope_enable_thinking", omit_tool_choice=True, **changes)
    )


def _provider_config(config: ProductConfigV2):
    provider = default_provider_factory(config.providers[0], config.profiles[0], "sk-test-key")
    try:
        return provider.config
    finally:
        asyncio.run(provider.aclose())


def test_existing_configs_keep_their_serialization_digest_and_wire_defaults():
    plain = build_product_config(_draft(output_token_parameter="max_tokens"))
    dumped = plain.model_dump(mode="json")
    assert "thinking_protocol" not in dumped["providers"][0]
    assert "omit_tool_choice" not in dumped["providers"][0]
    assert "thinking" not in dumped["profiles"][0]
    assert product_config_digest(ProductConfigV2.model_validate_json(json.dumps(dumped))) == (
        product_config_digest(plain)
    )
    assert effective_thinking(plain.profiles[0], plain.providers[0]) == "off"
    wire = _provider_config(plain)
    assert wire.enable_thinking is None and not wire.thinking_tool_steps_only
    assert not wire.omit_tool_choice and wire.max_output_tokens == 4096


def test_declared_protocol_defaults_to_tool_step_thinking_with_v40_limits():
    config = _bailian()
    provider, profile = config.providers[0], config.profiles[0]
    assert effective_thinking(profile, provider) == "tool_steps" and profile.thinking is None
    assert provider.output_token_parameter == "max_tokens"
    assert profile.max_output_tokens == 32_768 and profile.max_chunks == 80_000
    assert profile.max_response_bytes == 12 * 1024 * 1024 and profile.timeout_seconds == 900
    wire = _provider_config(config)
    assert wire.enable_thinking is True and wire.thinking_format is None
    assert wire.thinking_tool_steps_only and wire.omit_tool_choice
    assert wire.output_token_parameter == "max_tokens" and wire.max_output_tokens == 32_768


@pytest.mark.parametrize(
    ("mode", "enabled", "tool_steps"),
    [("tool_steps", True, True), ("every_step", True, False), ("off", False, False)],
)
def test_thinking_modes_map_to_explicit_provider_switches(mode, enabled, tool_steps):
    wire = _provider_config(_bailian(thinking=mode))
    # 百炼DeepSeek默认开启思考：关闭时也必须显式发送false。
    assert wire.enable_thinking is enabled and wire.thinking_tool_steps_only is tool_steps


def test_deepseek_protocol_uses_the_thinking_type_switch():
    config = build_product_config(
        _draft(
            base_url=DEEPSEEK, model="deepseek-v4-pro", thinking_protocol="deepseek_thinking_type"
        )
    )
    wire = _provider_config(config)
    assert wire.thinking_format == "thinking_type" and wire.enable_thinking is True
    assert wire.thinking_tool_steps_only and not wire.omit_tool_choice


@pytest.mark.parametrize(
    "change",
    [
        {"max_output_tokens": 8192},
        {"max_chunks": 10_000},
        {"max_response_bytes": 2_097_152},
    ],
)
def test_thinking_profiles_reject_limits_that_cannot_hold_the_reasoning(change):
    data = _bailian().model_dump(mode="json")
    data["profiles"][0].update(change)
    with pytest.raises(ValidationError):
        ProductConfigV2.model_validate_json(json.dumps(data))
    data["profiles"][0]["thinking"] = "off"
    relaxed = ProductConfigV2.model_validate_json(json.dumps(data))
    assert effective_thinking(relaxed.profiles[0], relaxed.providers[0]) == "off"


def test_thinking_requires_a_declared_protocol_and_max_tokens():
    data = build_product_config(_draft(output_token_parameter="max_tokens")).model_dump(mode="json")
    data["profiles"][0]["thinking"] = "tool_steps"
    with pytest.raises(ValidationError):
        ProductConfigV2.model_validate_json(json.dumps(data))
    with pytest.raises(ValidationError):
        ProviderDefinition.model_validate_json(
            json.dumps(
                {
                    **_bailian().providers[0].model_dump(mode="json"),
                    "output_token_parameter": "max_completion_tokens",
                }
            )
        )
    with pytest.raises(ValidationError):
        ConfigurationDraft.model_validate(
            {
                "provider_kind": "anthropic",
                "base_url": "https://api.anthropic.com",
                "model": "m",
                "thinking_protocol": "dashscope_enable_thinking",
            }
        )
    with pytest.raises(ValidationError):
        _draft(thinking="tool_steps")
    assert ModelProfile.model_fields["thinking"].default is None


@pytest.mark.parametrize(
    ("base_url", "model", "expected"),
    [
        (BAILIAN, "deepseek-v4-pro-0813", ("dashscope_enable_thinking", True)),
        (DEEPSEEK, "deepseek-v4-pro", ("deepseek_thinking_type", False)),
        (DEEPSEEK, "deepseek-flash", ("deepseek_thinking_type", False)),
        (BAILIAN, "qwen3.8-max", None),
        (LOCAL_VLLM, "qwen3.8-flash-next", ("vllm_chat_template", False)),
        ("https://api.openai.test/v1", "gpt-test", None),
    ],
)
def test_presets_cover_only_verified_endpoints_and_model_families(base_url, model, expected):
    assert thinking_preset(base_url, model) == expected


def test_self_hosted_loopback_endpoint_gets_the_local_preset():
    protocol, omit = thinking_preset(LOCAL_VLLM, "qwen3.8-flash-next") or (None, False)
    config = build_product_config(
        _draft(
            base_url=LOCAL_VLLM,
            model="qwen3.8-flash-next",
            thinking_protocol=protocol,
            omit_tool_choice=omit,
        )
    )
    provider = config.providers[0]
    assert provider.thinking_protocol == "vllm_chat_template"
    assert provider.normalize_tool_arguments and provider.pricing == "free"
    assert provider.stable_prompt_prefix
    wire = _provider_config(config)
    assert wire.thinking_format == "chat_template_kwargs" and wire.enable_thinking is True
    assert wire.base_url == LOCAL_VLLM
    # 只有开头一条system消息：逐请求说明在末尾user消息，前缀跨请求不变。
    assert wire.prompt_cache == "stable_tail"
    # 本地prefill慢且多会话共享副本：首包与单请求时限放宽到Profile上限内。
    profile = config.profiles[0]
    assert (profile.timeout_seconds, profile.io_timeout_seconds) == (1800.0, 300.0)


def test_only_loopback_endpoints_may_declare_free_pricing():
    remote = build_product_config(_draft()).providers[0].model_dump()
    with pytest.raises(ValidationError):
        ProviderDefinition.model_validate({**remote, "pricing": "free"})
    assert "pricing" not in remote and "normalize_tool_arguments" not in remote
    assert "stable_prompt_prefix" not in remote
    assert _provider_config(build_product_config(_draft())).prompt_cache is None
    remote_profile = build_product_config(_draft()).profiles[0]
    assert (remote_profile.timeout_seconds, remote_profile.io_timeout_seconds) == (120, 30)


def _configure_to(tmp_path: Path, *extra: str) -> ProductConfigV2:
    path = tmp_path / "config.json"
    _configure(
        [
            "--config",
            str(path),
            "--non-interactive",
            "--provider-kind",
            "openai_chat",
            "--base-url",
            BAILIAN,
            "--model",
            "deepseek-v4-pro-0813",
            *extra,
        ]
    )
    return load_product_config(path).config


def test_configure_turns_thinking_on_by_default_for_a_verified_endpoint(tmp_path, capsys):
    config = _configure_to(tmp_path)
    capsys.readouterr()
    provider, profile = config.providers[0], config.profiles[0]
    assert provider.thinking_protocol == "dashscope_enable_thinking" and provider.omit_tool_choice
    assert effective_thinking(profile, provider) == "tool_steps"


@pytest.mark.parametrize(
    ("extra", "protocol", "mode", "omit"),
    [
        (("--thinking", "off"), "dashscope_enable_thinking", "off", True),
        (("--thinking-protocol", "none"), None, "off", True),
        (("--tool-choice", "send"), "dashscope_enable_thinking", "tool_steps", False),
    ],
)
def test_configure_flags_override_the_preset(tmp_path, capsys, extra, protocol, mode, omit):
    config = _configure_to(tmp_path, *extra)
    capsys.readouterr()
    provider, profile = config.providers[0], config.profiles[0]
    assert provider.thinking_protocol == protocol and provider.omit_tool_choice is omit
    assert effective_thinking(profile, provider) == mode
