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
from harnessix.product_ui.configure_cli import configure_main

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
        # 百炼文档（2026-11-11核对）：这两个型号是混合思考、默认开启，可用enable_thinking关闭。
        (BAILIAN, "qwen3.8-max", ("dashscope_enable_thinking", False)),
        (BAILIAN, "qwen3.8-flash", ("dashscope_enable_thinking", False)),
        # 同系列里只能思考的型号没有可用的开关。
        (BAILIAN, "qwen3.8-2.4t-a95b", None),
        ("https://api.openai.test/v1", "qwen3.8-flash", None),
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
    # 畸形参数只拒绝该次调用，回合继续（本地模型偶发非法JSON）。
    assert provider.repair_tool_arguments
    wire = _provider_config(config)
    assert wire.repair_tool_arguments
    # 同机自托管与推理、构建共用CPU：读工具时限放宽到20秒。
    assert config.workspace_read_timeout_seconds == 20
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
    assert "stable_prompt_prefix" not in remote and "repair_tool_arguments" not in remote
    assert "workspace_read_timeout_seconds" not in build_product_config(_draft()).model_dump()
    assert _provider_config(build_product_config(_draft())).prompt_cache is None
    assert not _provider_config(build_product_config(_draft())).repair_tool_arguments
    remote_profile = build_product_config(_draft()).profiles[0]
    assert (remote_profile.timeout_seconds, remote_profile.io_timeout_seconds) == (120, 30)


def _configure_to(tmp_path: Path, *extra: str) -> ProductConfigV2:
    path = tmp_path / "config.json"
    configure_main(
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


def test_declared_context_window_reaches_the_profile_and_stays_optional():
    windowed = build_product_config(_draft(context_window_tokens=262_144)).profiles[0]
    assert windowed.context_window_tokens == 262_144
    plain = build_product_config(_draft()).profiles[0]
    # 未声明时不序列化，已有配置的摘要不变。
    assert plain.context_window_tokens is None
    assert "context_window_tokens" not in plain.model_dump()
    with pytest.raises(ValidationError):
        ModelProfile.model_validate({**plain.model_dump(), "context_window_tokens": "262144"})


def test_completion_guard_is_explicit_and_strict():
    """完成守卫默认关闭、不序列化（已有配置摘要不变），向导不预设；只接受布尔值。"""
    plain = build_product_config(_draft())
    assert plain.completion_guard is False
    assert "completion_guard" not in plain.model_dump()
    dumped = plain.model_dump(mode="json")
    guarded = ProductConfigV2.model_validate_json(json.dumps({**dumped, "completion_guard": True}))
    assert guarded.completion_guard is True
    assert guarded.model_dump(mode="json")["completion_guard"] is True
    with pytest.raises(ValidationError):
        ProductConfigV2.model_validate_json(json.dumps({**dumped, "completion_guard": "yes"}))


def test_compaction_point_defaults_to_half_window_and_rejects_unknown_policies():
    """压缩点默认在窗口一半处、不序列化（已有配置摘要不变），向导不预设；只接受已命名策略。"""
    plain = build_product_config(_draft(context_window_tokens=262_144))
    assert plain.compaction == "half_window"
    dumped = plain.model_dump(mode="json")
    assert "compaction" not in dumped
    early = ProductConfigV2.model_validate_json(
        json.dumps({**dumped, "compaction": "third_window"})
    )
    assert early.compaction == "third_window"
    assert early.model_dump(mode="json")["compaction"] == "third_window"
    assert product_config_digest(early) != product_config_digest(plain)
    for invalid in ("quarter_window", "1/3", 3, None):
        with pytest.raises(ValidationError):
            ProductConfigV2.model_validate_json(json.dumps({**dumped, "compaction": invalid}))


def test_compact_tool_results_is_explicit_and_maps_to_text_tool_results():
    """向导默认为OpenAI Chat开启紧凑工具结果；未开启时Provider发送JSON工具结果。"""
    protocol, omit = thinking_preset(LOCAL_VLLM, "qwen3.8-flash-next") or (None, False)
    local = build_product_config(
        _draft(
            base_url=LOCAL_VLLM,
            model="qwen3.8-flash-next",
            thinking_protocol=protocol,
            omit_tool_choice=omit,
        )
    )
    assert local.providers[0].compact_tool_results is True
    assert _provider_config(local).tool_result_format == "text"
    plain = local.model_copy(
        update={
            "providers": (local.providers[0].model_copy(update={"compact_tool_results": False}),)
        }
    )
    assert "compact_tool_results" not in plain.providers[0].model_dump()
    assert _provider_config(plain).tool_result_format == "json"
    anthropic = {
        "provider_id": "primary",
        "kind": "anthropic",
        "base_url": "https://api.anthropic.test",
        "credential": local.providers[0].credential.model_dump(),
        "compact_tool_results": True,
    }
    with pytest.raises(ValidationError):
        ProviderDefinition.model_validate(anthropic)


def _configure(path: Path, *arguments: str) -> ProductConfigV2:
    configure_main(["--config", str(path), "--non-interactive", *arguments])
    return load_product_config(path).config


_BAILIAN_QWEN = ("--vendor", "bailian", "--model", "qwen3.8-flash")
_UNKNOWN_ENDPOINT = (
    "--provider-kind",
    "openai_chat",
    "--base-url",
    "https://llm.example.com/v1",
    "--model",
    "some-model",
)


@pytest.mark.parametrize(("mode", "enabled"), [("off", False), ("every_step", True)])
def test_an_explicit_thinking_mode_reaches_bailian_qwen(tmp_path, capsys, mode, enabled):
    """Beta反馈HX-03：该模型在服务端默认思考，--thinking off曾被写进配置却从未发给模型。"""
    config = _configure(tmp_path / "config.json", *_BAILIAN_QWEN, "--thinking", mode)
    capsys.readouterr()
    provider, profile = config.providers[0], config.profiles[0]
    assert provider.thinking_protocol == "dashscope_enable_thinking"
    assert not provider.omit_tool_choice
    assert effective_thinking(profile, provider) == mode
    wire = _provider_config(config)
    assert wire.enable_thinking is enabled and wire.output_token_parameter == "max_tokens"


def test_bailian_qwen_thinks_on_tool_steps_only_by_default(tmp_path, capsys):
    """没有写--thinking时：带工具的请求照常思考，摘要等无工具请求不思考。

    真实运行里，32K窗口的摘要请求有1,228 Token的输出额度，其中1,149个被思考占用，摘要因截断作废；
    此前该型号默认不发送任何思考参数，每个请求都按服务端默认思考。
    """
    config = _configure(tmp_path / "config.json", *_BAILIAN_QWEN)
    capsys.readouterr()
    provider, profile = config.providers[0], config.profiles[0]
    assert provider.thinking_protocol == "dashscope_enable_thinking"
    assert not provider.omit_tool_choice
    assert profile.thinking is None and effective_thinking(profile, provider) == "tool_steps"
    assert profile.context_window_tokens == 1_000_000 and profile.max_output_tokens == 32_768
    wire = _provider_config(config)
    assert wire.enable_thinking is True and wire.thinking_tool_steps_only
    assert wire.output_token_parameter == "max_tokens"


@pytest.mark.parametrize(
    ("window", "thinking", "max_output"),
    [
        # 四分之一窗口放不下思考所需的16384输出：默认不思考，输出上限是窗口的四分之一。
        (32_768, "off", 8_192),
        (60_000, "off", 15_000),
        # 四分之一窗口刚好够思考。
        (65_536, "tool_steps", 16_384),
        (131_072, "tool_steps", 32_768),
    ],
)
def test_a_declared_window_keeps_the_default_output_within_a_quarter(
    tmp_path, capsys, window, thinking, max_output
):
    """默认输出预留曾是窗口允许的最大值（32K时14,847，约45%），历史只剩很小的空间。"""
    config = _configure(
        tmp_path / "config.json", *_BAILIAN_QWEN, "--context-window-tokens", str(window)
    )
    capsys.readouterr()
    provider, profile = config.providers[0], config.profiles[0]
    assert effective_thinking(profile, provider) == thinking
    assert profile.max_output_tokens == max_output <= window // 4


def test_thinking_the_user_asked_for_keeps_the_room_the_window_allows(tmp_path, capsys):
    """显式要求思考时推理计入输出，不按四分之一收紧。"""
    config = _configure(
        tmp_path / "config.json",
        *_BAILIAN_QWEN,
        "--context-window-tokens",
        "40000",
        "--thinking",
        "every_step",
    )
    capsys.readouterr()
    profile = config.profiles[0]
    assert effective_thinking(profile, config.providers[0]) == "every_step"
    assert profile.max_output_tokens == (40_000 - 1) // 2 - 1536 == 18_463


@pytest.mark.parametrize("mode", ["off", "tool_steps"])
def test_a_thinking_flag_no_switch_can_carry_is_refused_with_the_way_out(tmp_path, capsys, mode):
    path = tmp_path / "config.json"
    with pytest.raises(SystemExit) as failed:
        _configure(path, *_UNKNOWN_ENDPOINT, "--thinking", mode)
    assert failed.value.code == 2 and not path.exists()
    err = capsys.readouterr().err
    assert err.startswith("配置没有完成：这个服务和模型没有已核对的思考开关")
    assert "--thinking-protocol none" in err and "dashscope_enable_thinking" in err


def test_thinking_off_with_protocol_none_means_no_thinking_parameter(tmp_path, capsys):
    config = _configure(
        tmp_path / "config.json",
        *_UNKNOWN_ENDPOINT,
        "--thinking",
        "off",
        "--thinking-protocol",
        "none",
    )
    capsys.readouterr()
    assert config.providers[0].thinking_protocol is None
    assert _provider_config(config).enable_thinking is None


def test_configurations_written_before_the_new_defaults_think_at_medium(tmp_path, capsys):
    """旧配置文件不变，运行时在核实过档位的模型上按中档思考（ADR 0146第2版）。"""
    # 旧向导为百炼qwen3.8写下的配置没有思考协议：此前不发送任何思考参数，服务端按最强档思考。
    old_qwen = build_product_config(
        _draft(model="qwen3.8-flash", output_token_parameter="max_tokens")
    )
    assert old_qwen.providers[0].thinking_protocol is None
    old_wire = _provider_config(old_qwen)
    assert old_wire.enable_thinking is True and old_wire.reasoning_effort == "medium"
    # 旧向导在40K窗口下保留思考并给出窗口允许的最大输出上限：这样的配置照常加载、照常思考。
    new_40k = _configure(
        tmp_path / "config.json", *_BAILIAN_QWEN, "--context-window-tokens", "40000"
    )
    capsys.readouterr()
    assert effective_thinking(new_40k.profiles[0], new_40k.providers[0]) == "off"
    data = new_40k.model_dump(mode="json")
    data["profiles"][0].pop("thinking")
    data["profiles"][0]["max_output_tokens"] = 18_463
    old_40k = ProductConfigV2.model_validate_json(json.dumps(data))
    assert effective_thinking(old_40k.profiles[0], old_40k.providers[0]) == "tool_steps"
    wire = _provider_config(old_40k)
    assert wire.enable_thinking is True and wire.max_output_tokens == 18_463
    assert wire.reasoning_effort == "medium"


def test_verified_models_default_to_medium_thinking_intensity(tmp_path, capsys):
    """百炼qwen3.8不设强度时按服务端最强档思考：真实试用里第一步思考了十分钟、28,502 Token。

    向导对核实过档位的模型默认写入中档，线上随开启思考的请求发送reasoning_effort。
    """
    for model in ("qwen3.8-flash", "qwen3.8-max"):
        config = _configure(tmp_path / f"{model}.json", "--vendor", "bailian", "--model", model)
        profile = config.profiles[0]
        assert profile.thinking_effort == "medium" and profile.thinking_budget_tokens is None
        wire = _provider_config(config)
        assert wire.enable_thinking is True and wire.reasoning_effort == "medium"
        assert wire.thinking_budget is None
    capsys.readouterr()


@pytest.mark.parametrize(
    ("effort", "wire_value"), [("low", "low"), ("medium", "medium"), ("high", None)]
)
def test_each_intensity_tier_maps_to_its_wire_value(tmp_path, capsys, effort, wire_value):
    """高档不发送档位参数：这两个型号不发送时就是服务端默认的最强档。"""
    config = _configure(tmp_path / "config.json", *_BAILIAN_QWEN, "--thinking-effort", effort)
    capsys.readouterr()
    assert config.profiles[0].thinking_effort == effort
    assert _provider_config(config).reasoning_effort == wire_value


def test_an_explicit_thinking_token_cap_replaces_the_intensity_tier(tmp_path, capsys):
    """qwen3.8-max不接受档位和上限同时出现：给了上限就只发上限。"""
    config = _configure(tmp_path / "config.json", *_BAILIAN_QWEN, "--thinking-budget", "4000")
    capsys.readouterr()
    profile = config.profiles[0]
    assert profile.thinking_budget_tokens == 4000 and profile.thinking_effort is None
    wire = _provider_config(config)
    assert wire.thinking_budget == 4000 and wire.reasoning_effort is None
    # 百炼预设用max_tokens（只限制正文）：思考上限照样可以发送。
    assert wire.output_token_parameter == "max_tokens"


@pytest.mark.parametrize(
    "arguments",
    [
        (*_BAILIAN_QWEN, "--thinking-effort", "low", "--thinking-budget", "4000"),
        (*_BAILIAN_QWEN, "--thinking", "off", "--thinking-effort", "low"),
        ("--vendor", "deepseek", "--thinking-effort", "low"),
        ("--vendor", "bailian", "--model", "deepseek-v4-pro-0813", "--thinking-budget", "4000"),
        (*_UNKNOWN_ENDPOINT, "--thinking-effort", "medium"),
    ],
)
def test_intensity_settings_that_cannot_reach_the_model_are_refused(tmp_path, capsys, arguments):
    path = tmp_path / "config.json"
    with pytest.raises(SystemExit):
        configure_main(["--config", str(path), "--non-interactive", *arguments])
    assert "--thinking-effort" in capsys.readouterr().err and not path.exists()


def test_unverified_endpoints_and_no_thinking_get_no_intensity_setting(tmp_path, capsys):
    deepseek = _configure(tmp_path / "deepseek.json", "--vendor", "deepseek")
    off = _configure(tmp_path / "off.json", *_BAILIAN_QWEN, "--thinking", "off")
    small = _configure(tmp_path / "small.json", *_BAILIAN_QWEN, "--context-window-tokens", "32768")
    capsys.readouterr()
    for config in (deepseek, off, small):
        dumped = config.profiles[0].model_dump(mode="json")
        assert "thinking_effort" not in dumped and "thinking_budget_tokens" not in dumped
        assert _provider_config(config).reasoning_effort is None


def test_configurations_without_an_intensity_send_medium_without_changing_the_file():
    """没有强度字段的Profile序列化不变，运行时发送中档（ADR 0146第2版）。"""
    config = _bailian(model="qwen3.8-flash")
    data = config.model_dump(mode="json")
    assert data["profiles"][0].pop("thinking_effort") == "medium"
    old = ProductConfigV2.model_validate_json(json.dumps(data))
    assert "thinking_effort" not in old.profiles[0].model_dump(mode="json")
    wire = _provider_config(old)
    assert wire.enable_thinking is True and wire.reasoning_effort == "medium"


@pytest.mark.parametrize(
    "change",
    [
        {"thinking_effort": "low", "thinking_budget_tokens": 4000},
        {"thinking": "off", "thinking_effort": "low"},
        {"thinking_budget_tokens": 32_768},
    ],
)
def test_inconsistent_intensity_settings_are_rejected_by_the_contract(change):
    data = _bailian(model="qwen3.8-flash").model_dump(mode="json")
    data["profiles"][0].pop("thinking_effort", None)
    data["profiles"][0].update(change)
    with pytest.raises(ValidationError):
        ProductConfigV2.model_validate_json(json.dumps(data))


def test_intensity_needs_a_protocol_that_carries_it():
    data = build_product_config(
        _draft(base_url=DEEPSEEK, model="deepseek-v4", thinking_protocol="deepseek_thinking_type")
    ).model_dump(mode="json")
    data["profiles"][0]["thinking_effort"] = "low"
    with pytest.raises(ValidationError):
        ProductConfigV2.model_validate_json(json.dumps(data))
