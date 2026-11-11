"""配置向导先选服务商和模型（DeepSeek、阿里云百炼、智谱），地址、Key变量名与限额自动填写。"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigV2
from harnessix.product_config.vendors import VENDORS
from harnessix.product_config.wizard import known_context_window
from harnessix.product_ui import cli as product_cli
from harnessix.product_ui.cost_estimate import list_price_for

_KEY_VARIABLES = ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ZAI_API_KEY", "MODEL_API_KEY")


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _KEY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    """按顺序回答向导的提问，返回向导问过的提示。"""

    pending: Iterator[str] = iter(answers)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(pending)

    monkeypatch.setattr("builtins.input", answer)
    return prompts


def _configure(path: Path, *arguments: str) -> None:
    product_cli.code_main(["configure", "--config", str(path), *arguments])


def _config(path: Path) -> ProductConfigV2:
    return load_product_config(path).config


def test_the_menu_picks_bailian_and_its_cheap_default_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.json"
    _answers(monkeypatch, "2", "")

    _configure(path)

    out = capsys.readouterr().out
    assert "1) DeepSeek 官方" in out and "3) 智谱 GLM" in out and "4) 其他兼容" in out
    assert "qwen3.8-flash  便宜、速度快（推荐） · 输入 ¥0.8 / 输出 ¥2.7" in out
    config = _config(path)
    provider, profile = config.providers[0], config.profiles[0]
    assert provider.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert profile.model == "qwen3.8-flash" and profile.context_window_tokens == 1_000_000
    assert profile.io_timeout_seconds == 180.0 and profile.max_output_tokens == 32_768
    assert config.secret_sources[0].environment_variable == "DASHSCOPE_API_KEY"
    # 成功后说下一步，而不是打印JSON回执。
    assert "配置已保存：" in out and "harnessix code" in out
    if os.name != "nt":
        assert "export DASHSCOPE_API_KEY=你的API Key" in out


def test_scripts_choose_zhipu_with_vendor_and_get_its_endpoint_rules(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    _configure(path, "--vendor", "zhipu", "--non-interactive")

    config = _config(path)
    provider, profile = config.providers[0], config.profiles[0]
    assert provider.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert profile.model == "glm-5.3-flash"
    assert config.secret_sources[0].environment_variable == "ZAI_API_KEY"
    # 智谱只接受tool_choice=auto、只认max_tokens；GLM-5.3强制思考，不发送思考开关。
    assert provider.omit_tool_choice and provider.output_token_parameter == "max_tokens"
    assert provider.thinking_protocol is None
    assert profile.context_window_tokens == 1_000_000 and profile.io_timeout_seconds == 180.0


def test_a_chosen_deepseek_model_keeps_the_verified_thinking_preset(tmp_path: Path) -> None:
    path = tmp_path / "config.json"

    _configure(path, "--vendor", "deepseek", "--model", "deepseek-v4-pro", "--non-interactive")

    config = _config(path)
    assert config.providers[0].base_url == "https://api.deepseek.com"
    assert config.providers[0].thinking_protocol == "deepseek_thinking_type"
    assert config.secret_sources[0].environment_variable == "DEEPSEEK_API_KEY"


def test_wrong_menu_input_is_asked_again_and_other_takes_a_manual_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.json"
    prompts = _answers(monkeypatch, "9", "x", "4", "https://llm.example.com/v1", "my-model")

    _configure(path)

    out = capsys.readouterr().out
    assert out.count("请输入 1～4 之间的序号。") == 2
    # “其他”按OpenAI兼容接口处理，不再问Provider类型。
    assert not any("Provider类型" in prompt for prompt in prompts)
    config = _config(path)
    assert config.providers[0].kind == "openai_chat"
    assert config.profiles[0].model == "my-model"
    assert config.secret_sources[0].environment_variable == "MODEL_API_KEY"


def test_an_existing_config_is_kept_unless_the_user_agrees_to_replace_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.json"
    _configure(path, "--vendor", "deepseek", "--non-interactive")
    before = path.read_bytes()
    capsys.readouterr()

    prompts = _answers(monkeypatch, "n")
    _configure(path)
    assert "（当前模型 deepseek-flash）" in prompts[0]
    assert "已保留原有配置" in capsys.readouterr().out and path.read_bytes() == before

    _answers(monkeypatch, "y", "3", "3")
    _configure(path)
    assert _config(path).profiles[0].model == "glm-5.3"


def test_a_key_already_in_the_environment_is_not_asked_for_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "not-a-real-key")

    _configure(tmp_path / "config.json", "--vendor", "zhipu", "--non-interactive")

    out = capsys.readouterr().out
    assert "已检测到环境变量 ZAI_API_KEY" in out and "not-a-real-key" not in out
    assert "glm-5.3-flash（智谱 GLM） · 每百万 Token 输入 ¥0.8 / 输出 ¥2.8" in out


def test_errors_are_plain_chinese_unless_json_is_asked_for(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as failed:
        _configure(tmp_path / "config.json", "--non-interactive")
    assert failed.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("配置没有完成：缺少服务商：用 --vendor 选择") and not err.startswith("{")

    with pytest.raises(SystemExit):
        _configure(
            tmp_path / "config.json", "--vendor", "zhipu", "--provider-kind", "anthropic", "--json"
        )
    assert json.loads(capsys.readouterr().err)["code"] == "product_configure_invalid"


@pytest.mark.skipif(os.name != "posix", reason="目录权限位只在POSIX上检查")
def test_a_config_directory_others_can_read_is_named_with_the_fix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Beta反馈HX-12：只说“目录权限或身份不安全”，没有是哪个目录、要什么权限、怎么改。"""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)

    with pytest.raises(SystemExit) as failed:
        _configure(shared / "config.json", "--vendor", "bailian", "--non-interactive")

    assert failed.value.code == 2 and not (shared / "config.json").exists()
    err = capsys.readouterr().err
    assert err.startswith(f"配置没有完成：配置目录 {shared} 的权限是 755，需要 700")
    assert f"chmod 700 {shared}" in err

    shared.chmod(0o700)
    _configure(shared / "config.json", "--vendor", "bailian", "--non-interactive")
    assert "配置已保存" in capsys.readouterr().out


def test_every_menu_model_has_a_verified_price_and_context_window() -> None:
    # 菜单只列核对过的型号：价格与窗口缺一项，用户就会看到“费用未知”或拿到不完整的配置。
    for vendor in VENDORS:
        for item in vendor.models:
            assert list_price_for(vendor.base_url, item.model) is not None, item.model
            assert known_context_window(vendor.base_url, item.model) is not None, item.model
