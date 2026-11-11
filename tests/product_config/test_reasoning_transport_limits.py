"""服务端默认思考的云端模型用思考型传输限额，避免长代码生成时被30秒空闲读取误判断线（0.10.0b8）。"""

from __future__ import annotations

from harnessix.product_config.product_contracts import ConfigurationDraft
from harnessix.product_config.wizard import build_product_config, reasons_by_default

BAILIAN = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _profile(base_url: str, model: str):
    draft = ConfigurationDraft.model_validate(
        {"provider_kind": "openai_chat", "base_url": base_url, "model": model}
    )
    config = build_product_config(draft)
    return config.providers[0], config.profiles[0]


def test_bailian_qwen38_gets_reasoning_transport_limits_without_thinking_parameters() -> None:
    for model in ("qwen3.8-flash", "qwen3.8-max"):
        assert reasons_by_default(BAILIAN, model)
        provider, profile = _profile(BAILIAN, model)
        # 2026-11-10 macOS实测：写文件工具参数整段静默生成25秒以上，30秒空闲读取会误判断线。
        assert profile.io_timeout_seconds == 180.0
        assert profile.timeout_seconds == 900.0
        assert profile.max_output_tokens == 32_768
        # 只放宽传输限额，不改请求：不声明思考协议，也不发送思考开关。
        assert provider.thinking_protocol is None
        assert profile.thinking is None


def test_other_cloud_models_keep_the_default_limits() -> None:
    for base_url, model in (
        ("https://api.openai.com/v1", "gpt-test"),
        (BAILIAN, "qwen-plus"),
    ):
        assert not reasons_by_default(base_url, model)
        _, profile = _profile(base_url, model)
        assert profile.io_timeout_seconds == 30
        assert profile.timeout_seconds == 120
        assert profile.max_output_tokens == 4096
