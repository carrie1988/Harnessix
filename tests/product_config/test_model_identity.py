"""模型身份说明按用户配置生成（Beta Issue #17）。

0.10.0b32：百炼qwen3.8-flash被问“你是哪个模型”时两轮自称Anthropic的Claude，并否认自己是千问；
状态栏与会话记录都是qwen3.8-flash。运行时指令此前没有任何身份说明。
"""

from __future__ import annotations

from types import SimpleNamespace

from harnessix.context.contracts import ContextFragmentKind
from harnessix.product_config.model_identity import MODEL_IDENTITY_SOURCE, model_identity_fragment


def _profile(model: str, provider_id: str) -> SimpleNamespace:
    return SimpleNamespace(model=model, provider_id=provider_id)


def _provider(provider_id: str, base_url: str) -> SimpleNamespace:
    return SimpleNamespace(provider_id=provider_id, base_url=base_url)


def test_the_configured_model_and_vendor_are_named() -> None:
    fragment = model_identity_fragment(
        [_profile("qwen3.8-flash", "primary")],
        [_provider("primary", "https://dashscope.aliyuncs.com/compatible-mode/v1")],
    )
    assert fragment.kind is ContextFragmentKind.RUNTIME_INSTRUCTION
    assert fragment.source == MODEL_IDENTITY_SOURCE
    assert '"qwen3.8-flash" served by 阿里云百炼 (dashscope.aliyuncs.com)' in fragment.content
    assert "Harnessix Code" in fragment.content
    assert "never claim to be a model or vendor that it does not name" in fragment.content
    # 说明来自配置，不替任何厂商背书：不出现配置以外的模型或厂商。
    assert "Claude" not in fragment.content and "Anthropic" not in fragment.content


def test_fallback_models_are_listed_after_the_primary_one() -> None:
    fragment = model_identity_fragment(
        [_profile("qwen3.8-flash", "bailian"), _profile("deepseek-flash", "deepseek")],
        [
            _provider("bailian", "https://ws-1a2b.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
            _provider("deepseek", "https://api.deepseek.com"),
        ],
    )
    content = fragment.content
    assert content.index('"qwen3.8-flash"') < content.index("fall back to")
    assert content.index("fall back to") < content.index('"deepseek-flash"')
    # 百炼北京的业务空间专属域名按百炼说明（Issue #6的同一张对照）。
    assert "阿里云百炼 (dashscope.aliyuncs.com)" in content
    assert "DeepSeek 官方 (api.deepseek.com)" in content


def test_self_hosted_and_unknown_endpoints_are_named_by_host() -> None:
    fragment = model_identity_fragment(
        [_profile("Qwen3-Coder-30B", "local")], [_provider("local", "http://192.168.1.20:8000/v1")]
    )
    assert '"Qwen3-Coder-30B" served by 192.168.1.20.' in fragment.content
