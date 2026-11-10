"""产品模型请求的代理选择：显式配置优先，其次按主流工具读取HTTPS_PROXY/HTTP_PROXY与NO_PROXY。"""

from __future__ import annotations

import pytest

from harnessix.models.proxy import (
    anthropic_proxy_transport,
    model_request_proxy,
    openai_proxy_transport,
    unsupported_proxy_notice,
)

CLOUD = "https://dashscope.aliyuncs.com/compatible-mode/v1"
CORPORATE = "http://user:secret@proxy.corp.example:8080"


@pytest.mark.parametrize("name", ["HTTPS_PROXY", "https_proxy"])
def test_https_endpoint_uses_the_https_proxy_variable(name: str) -> None:
    assert model_request_proxy(CLOUD, {name: CORPORATE}) == CORPORATE
    assert model_request_proxy(CLOUD, {"HTTP_PROXY": CORPORATE}) is None


def test_plain_http_endpoint_uses_the_http_proxy_variable() -> None:
    environ = {"HTTP_PROXY": CORPORATE, "HTTPS_PROXY": "http://other:1"}
    assert model_request_proxy("http://models.example/v1", environ) == CORPORATE


def test_explicit_configuration_wins_over_environment() -> None:
    proxy = model_request_proxy(
        CLOUD, {"HTTPS_PROXY": CORPORATE}, configured="http://127.0.0.1:7890"
    )
    assert proxy == "http://127.0.0.1:7890"


@pytest.mark.parametrize(
    ("base_url", "direct"),
    [("http://127.0.0.1:8000/v1", False), ("http://192.168.1.20:8000/v1", True)],
)
def test_local_and_self_hosted_endpoints_go_direct(base_url: str, direct: bool) -> None:
    environ = {"HTTPS_PROXY": CORPORATE, "HTTP_PROXY": CORPORATE}
    assert model_request_proxy(base_url, environ, direct=direct) is None


@pytest.mark.parametrize(
    ("no_proxy", "bypassed"),
    [
        ("*", True),
        ("aliyuncs.com", True),
        (".aliyuncs.com", True),
        ("other.example, dashscope.aliyuncs.com:443", True),
        ("scope.aliyuncs.com", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_no_proxy_matches_by_domain_suffix(no_proxy: str, bypassed: bool) -> None:
    environ = {"HTTPS_PROXY": CORPORATE, "NO_PROXY": no_proxy}
    assert (model_request_proxy(CLOUD, environ) is None) is bypassed


def test_socks_only_is_ignored_and_reported() -> None:
    environ = {"ALL_PROXY": "socks5://127.0.0.1:7890", "HTTPS_PROXY": "socks5h://127.0.0.1:7890"}
    assert model_request_proxy(CLOUD, environ) is None
    assert unsupported_proxy_notice(environ) is not None
    assert unsupported_proxy_notice({**environ, "https_proxy": "http://127.0.0.1:7890"}) is None
    assert unsupported_proxy_notice({}) is None


def test_transports_are_built_only_when_a_proxy_is_chosen() -> None:
    assert openai_proxy_transport(None) is None and anthropic_proxy_transport(None) is None
    assert openai_proxy_transport(CORPORATE) is not None
    assert anthropic_proxy_transport(CORPORATE) is not None
