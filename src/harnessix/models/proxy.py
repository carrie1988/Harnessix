"""产品模型请求的代理选择（ADR 0014修订，2026-11-10用户决定按主流工具做法）。

顺序：显式配置的代理 → `HTTPS_PROXY`/`HTTP_PROXY`（遵守`NO_PROXY`）→ 直连。
本机回环与已声明的内网自托管端点总是直连。SOCKS等其他协议的代理暂不支持，按直连处理并由诊断提示。
代理地址可能含账号密码，只在内存里使用，不写入配置、事件或日志。
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx
import httpx2

from harnessix.models.config import LOOPBACK_HOSTS

_SUPPORTED_SCHEMES = frozenset({"http", "https"})


def model_request_proxy(
    base_url: str,
    environ: Mapping[str, str],
    *,
    configured: str | None = None,
    direct: bool = False,
) -> str | None:
    """返回本次模型请求应使用的代理地址；None表示直连。"""

    if configured is not None:
        return configured
    endpoint = urlsplit(base_url)
    if direct or endpoint.hostname is None or endpoint.hostname in LOOPBACK_HOSTS:
        return None
    candidate = _environment_value(environ, "https" if endpoint.scheme == "https" else "http")
    if candidate is None or urlsplit(candidate).scheme not in _SUPPORTED_SCHEMES:
        return None
    if _bypassed(endpoint.hostname, environ.get("NO_PROXY", environ.get("no_proxy", ""))):
        return None
    return candidate


def openai_proxy_transport(proxy: str | None) -> httpx.AsyncHTTPTransport | None:
    """需要代理时构造不读环境的Transport交给Provider；直连时返回None，沿用Provider默认。"""

    return None if proxy is None else httpx.AsyncHTTPTransport(trust_env=False, proxy=proxy)


def anthropic_proxy_transport(proxy: str | None) -> httpx2.AsyncHTTPTransport | None:
    return None if proxy is None else httpx2.AsyncHTTPTransport(trust_env=False, proxy=proxy)


def unsupported_proxy_notice(environ: Mapping[str, str]) -> str | None:
    """只设置了SOCKS等不支持的代理时给出提示，避免用户以为请求已经走了代理。"""

    values = [
        environ.get(name, "") for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
    ]
    schemes = {urlsplit(value).scheme for value in values if value}
    if schemes and not schemes & _SUPPORTED_SCHEMES:
        return "检测到 SOCKS 等代理，Harnessix 只支持 HTTP 代理；请设置 HTTPS_PROXY=http://…"
    return None


def _environment_value(environ: Mapping[str, str], scheme: str) -> str | None:
    upper = f"{scheme.upper()}_PROXY"
    value = environ.get(upper) or environ.get(upper.lower())
    return value.strip() if value and value.strip() else None


def _bypassed(host: str, no_proxy: str) -> bool:
    """`*`表示全部直连；其余按域名后缀匹配（`.example.com`与`example.com`等价），忽略端口。"""

    for entry in (item.strip().lower() for item in no_proxy.split(",")):
        name = entry.rsplit(":", 1)[0].lstrip(".") if entry.count(":") == 1 else entry.lstrip(".")
        if entry == "*" or (name and (host == name or host.endswith("." + name))):
            return True
    return False
