"""旧配置在核实过思考档位的模型上运行时按中档思考，不用重新配置（负责人2026-11-13决定）。

负责人本机：0.10.0b28，配置是11月10日生成的，没有思考协议也没有强度，百炼qwen3.8-flash按服务端默认的最强档思考；
一个任务的第一步想了11分23秒、用满32,768个输出Token仍没有写出任何内容。此前只在启动时提示“重新运行configure”。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.product_config import runtime as product_runtime
from harnessix.product_config.codec import ProductConfigSnapshot, load_product_config
from harnessix.product_config.default_thinking import with_default_thinking_effort
from harnessix.product_config.runtime import (
    build_provider_bundle,
    environment_secret_provider,
    select_profile,
)
from harnessix.product_ui.configure_cli import configure_main
from tests.contracts.provider import model_request


def _configure(path: Path, *arguments: str) -> Path:
    configure_main(["--config", str(path), "--non-interactive", *arguments])
    return path


def _without(path: Path, target: Path, *, profile_fields=(), provider_fields=()) -> Path:  # type: ignore[no-untyped-def]
    """去掉若干字段，得到旧版本向导写出的配置。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    for field in profile_fields:
        data["profiles"][0].pop(field, None)
    for field in provider_fields:
        data["providers"][0].pop(field, None)
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    target.chmod(0o600)
    return target


def _active(path: Path):  # type: ignore[no-untyped-def]
    snapshot = load_product_config(path)
    assert isinstance(snapshot, ProductConfigSnapshot)
    profile = snapshot.config.profile_chain(None)[0]
    provider = next(p for p in snapshot.config.providers if p.provider_id == profile.provider_id)
    return provider, profile


@pytest.fixture
def bailian(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Path]:
    current = _configure(tmp_path / "current.json", "--vendor", "bailian")
    configs = {
        "current": current,
        # 0.10.0b19～b22：有思考协议、没有强度。
        "b19": _without(current, tmp_path / "b19.json", profile_fields=("thinking_effort",)),
        # 更早（负责人本机的配置）：连思考协议都没有。
        "older": _without(
            current,
            tmp_path / "older.json",
            profile_fields=("thinking_effort",),
            provider_fields=("thinking_protocol",),
        ),
    }
    capsys.readouterr()
    return configs


@pytest.mark.parametrize("name", ["b19", "older"])
def test_old_configs_on_verified_models_think_at_medium(bailian, name: str) -> None:
    provider, profile = _active(bailian[name])
    assert profile.thinking_effort is None
    upgraded_provider, upgraded_profile = with_default_thinking_effort(provider, profile)
    current_provider, current_profile = _active(bailian["current"])
    assert upgraded_profile.thinking_effort == current_profile.thinking_effort == "medium"
    assert upgraded_provider.thinking_protocol == current_provider.thinking_protocol
    assert upgraded_provider.omit_tool_choice == current_provider.omit_tool_choice


@pytest.mark.parametrize(
    "arguments",
    [
        ("--thinking-effort", "low"),
        ("--thinking-effort", "high"),
        ("--thinking-budget", "4000"),
        ("--thinking", "off"),
    ],
)
def test_an_explicit_thinking_setting_is_kept(tmp_path: Path, capsys, arguments) -> None:  # type: ignore[no-untyped-def]
    path = _configure(tmp_path / "explicit.json", "--vendor", "bailian", *arguments)
    capsys.readouterr()
    provider, profile = _active(path)
    assert with_default_thinking_effort(provider, profile) == (provider, profile)


@pytest.mark.parametrize(
    ("vendor", "model"),
    [("bailian", "deepseek-v4-pro"), ("deepseek", "deepseek-v4-flash")],
)
def test_models_without_verified_tiers_are_unchanged(tmp_path: Path, capsys, vendor, model) -> None:  # type: ignore[no-untyped-def]
    current = _configure(tmp_path / "c.json", "--vendor", vendor, "--model", model)
    older = _without(current, tmp_path / "o.json", profile_fields=("thinking_effort",))
    capsys.readouterr()
    provider, profile = _active(older)
    assert with_default_thinking_effort(provider, profile) == (provider, profile)


async def test_an_old_config_sends_the_medium_effort_to_the_service(
    bailian, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实OpenAI Chat适配器经产品装配：负责人本机那种配置发出的请求打开思考并带上medium。"""
    sent: list[dict] = []

    def capture(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "stop here", "type": "invalid"}})

    monkeypatch.setattr(
        product_runtime, "openai_proxy_transport", lambda _proxy: httpx.MockTransport(capture)
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fixture-key-0123456789")
    snapshot = load_product_config(bailian["older"])
    assert isinstance(snapshot, ProductConfigSnapshot)
    bundle = await build_provider_bundle(
        snapshot,
        select_profile(snapshot, None),
        environment_secret_provider(snapshot.config),
        audit=None,
    )
    async with bundle:
        [event async for event in bundle.stream(model_request(with_tools=True), CancelToken())]
    body = sent[0]
    assert body.get("enable_thinking") is True and body.get("reasoning_effort") == "medium"
