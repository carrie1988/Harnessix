"""云端点的代理：显式配置优先，否则按主流工具读取HTTPS_PROXY等环境变量；模型请求与端点探测
走同一条路由；已有配置时按提示的摘要替换。"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from harnessix.product_config import endpoint_probe
from harnessix.product_config import runtime as product_runtime
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import ProductConfigV2, ProviderDefinition
from harnessix.product_config.endpoint_probe import probe_chat_endpoint, probe_provider_endpoint
from harnessix.product_config.product_contracts import ConfigurationDraft
from harnessix.product_config.runtime import default_provider_factory
from harnessix.product_config.wizard import build_product_config
from harnessix.product_ui.cli import _configure

BAILIAN = "https://dashscope.aliyuncs.com/compatible-mode/v1"
PROXY = "http://127.0.0.1:7890"


def _config(kind: str = "openai_chat", **changes: object) -> ProductConfigV2:
    base_url = BAILIAN if kind == "openai_chat" else "https://api.anthropic.com"
    fields = {"provider_kind": kind, "base_url": base_url, "model": "qwen3.8-flash"}
    return build_product_config(ConfigurationDraft.model_validate(fields | changes))


def _provider(**changes: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "provider_id": "primary",
        "kind": "openai_chat",
        "base_url": BAILIAN,
        "credential": {"name": "model-api-key", "version": "environment-v1"},
    }
    return fields | changes


def test_configure_records_the_proxy_on_the_provider(tmp_path: Path, capsys) -> None:
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
            "qwen3.8-flash",
            "--proxy-url",
            PROXY,
        ]
    )
    capsys.readouterr()

    assert load_product_config(path).config.providers[0].proxy_url == PROXY


def test_adding_a_proxy_to_an_existing_config_follows_the_printed_hint(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "config.json"
    arguments = [
        "--config",
        str(path),
        "--non-interactive",
        "--provider-kind",
        "openai_chat",
        "--base-url",
        BAILIAN,
        "--model",
        "qwen3.8-flash",
    ]
    _configure(arguments)
    capsys.readouterr()

    with pytest.raises(SystemExit):
        _configure([*arguments, "--proxy-url", PROXY])
    error = json.loads(capsys.readouterr().err)
    assert error["code"] == "product_config_exists"
    hint = error["message"].split("要替换它，加 ")[1].split()
    assert hint[:2] == ["--replace", "--expected-source-sha256"]

    _configure([*arguments, "--proxy-url", PROXY, *hint])
    capsys.readouterr()
    assert load_product_config(path).config.providers[0].proxy_url == PROXY


def test_configs_without_a_proxy_keep_their_serialization() -> None:
    assert "proxy_url" not in _config().model_dump(mode="json")["providers"][0]


@pytest.mark.parametrize(
    "proxy",
    [
        "https://127.0.0.1:7890",
        "http://10.0.0.2:7890",
        "http://proxy.example.com:8080",
        "http://user:secret@127.0.0.1:7890",
        "socks5://127.0.0.1:7890",
        "http://127.0.0.1",
    ],
)
def test_only_a_plain_loopback_http_proxy_is_accepted(proxy: str) -> None:
    with pytest.raises(ValidationError):
        ProviderDefinition.model_validate(_provider(proxy_url=proxy))


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "http://127.0.0.1:8000/v1"},
        {"base_url": "http://192.168.1.20:8000/v1", "self_hosted": True},
    ],
)
def test_configure_refuses_a_proxy_for_local_and_self_hosted_endpoints(
    changes: dict[str, object],
) -> None:
    fields = {"provider_kind": "openai_chat", "model": "qwen-local", "proxy_url": PROXY}
    with pytest.raises(ValidationError, match="不经代理"):
        ConfigurationDraft.model_validate(fields | changes)


@pytest.mark.parametrize("kind", ["openai_chat", "anthropic"])
def test_model_requests_use_the_configured_proxy(kind: str) -> None:
    config = _config(kind, proxy_url=PROXY)
    provider = default_provider_factory(config.providers[0], config.profiles[0], "sk-test-key")
    try:
        assert provider.config.proxy_url == PROXY
    finally:
        asyncio.run(provider.aclose())


@pytest.mark.parametrize(
    ("kind", "builder"),
    [("openai_chat", "openai_proxy_transport"), ("anthropic", "anthropic_proxy_transport")],
)
def test_model_requests_follow_environment_proxies_unless_configured(
    monkeypatch: pytest.MonkeyPatch, kind: str, builder: str
) -> None:
    chosen: list[str | None] = []
    original = getattr(product_runtime, builder)

    def capture(proxy: str | None) -> object:
        chosen.append(proxy)
        return original(proxy)

    monkeypatch.setattr(product_runtime, builder, capture)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.example:8080")
    for config in (_config(kind), _config(kind, proxy_url=PROXY)):
        provider = default_provider_factory(config.providers[0], config.profiles[0], "sk-test-key")
        asyncio.run(provider.aclose())
    assert chosen == ["http://proxy.corp.example:8080", PROXY]


def test_doctor_probe_uses_the_same_proxy_choice_and_warns_about_socks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str | None] = []

    def probe(*_args: object, proxy_url: str | None, **_kwargs: object) -> list[object]:
        seen.append(proxy_url)
        return []

    monkeypatch.setattr(endpoint_probe, "probe_chat_endpoint", probe)
    provider = _config().providers[0]
    environ = {"https_proxy": "http://proxy.corp.example:8080"}
    assert probe_provider_endpoint(provider, "m", "k", environ) == []
    socks = probe_provider_endpoint(provider, "m", "k", {"ALL_PROXY": "socks5://127.0.0.1:1"})
    assert [(item.check, item.status) for item in socks] == [("proxy", "warning")]
    assert seen == ["http://proxy.corp.example:8080", None]


@contextmanager
def _forward_proxy() -> Iterator[tuple[str, list[str]]]:
    """收到绝对URI请求即说明客户端经过了它；直接按端点身份回答。"""
    requested: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return

        def _reply(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            requested.append(self.path)
            self._reply({"data": [{"id": "m"}]})

        def do_POST(self) -> None:
            requested.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            arguments = json.dumps({"items": ["alpha", "beta"]})
            call = {"function": {"name": "record_items", "arguments": arguments}}
            self._reply({"choices": [{"message": {"tool_calls": [call]}}]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", requested
    finally:
        server.shutdown()


def test_probe_goes_through_the_configured_proxy_and_ignores_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    endpoint = "http://cloud.example.test/v1"
    with _forward_proxy() as (proxy, requested):
        results = probe_chat_endpoint(endpoint, "m", "key", timeout=5, proxy_url=proxy)

    assert [(item.check, item.status) for item in results] == [
        ("model", "passed"),
        ("tool_call", "passed"),
    ]
    assert requested == [f"{endpoint}/models", f"{endpoint}/chat/completions"]


def test_probe_without_a_proxy_does_not_borrow_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _forward_proxy() as (proxy, requested):
        monkeypatch.setenv("HTTP_PROXY", proxy)
        monkeypatch.setenv("http_proxy", proxy)
        results = probe_chat_endpoint("http://cloud.example.test/v1", "m", "key", timeout=5)

    assert requested == []
    assert [(item.check, item.status) for item in results] == [("endpoint", "failed")]
