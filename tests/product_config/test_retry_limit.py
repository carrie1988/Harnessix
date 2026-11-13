"""配置的自动重试次数限制一次模型请求的总尝试数（Beta Issue #2）。

0.10.0b24上的实测：`--service-failure-retries 2`之后，端点连续返回HTTP 503时仍发出6次请求。
服务端明确拒绝的请求确定没有计费，不受“用量未知”的限制，实际次数来自Profile的`max_attempts`（向导写6）。
现在配置了service_failure_retries时，一次请求最多尝试N+1次；没有配置时沿用Profile。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.models.contracts import ResponseFailed
from harnessix.product_config import runtime as product_runtime
from harnessix.product_config.codec import ProductConfigSnapshot, load_product_config
from harnessix.product_config.contracts import ProductConfigV2
from harnessix.product_config.runtime import (
    build_provider_bundle,
    environment_secret_provider,
    select_profile,
)
from tests.contracts.provider import model_request
from tests.product_config.conftest import write_config

_SECRETS = {"PRIMARY_API_KEY": "primary-secret-0123456789", "BACKUP_API_KEY": "backup-secret-0123"}


def _configured(config: ProductConfigV2, retries: int | None, max_attempts: int) -> ProductConfigV2:
    profiles = tuple(
        profile.model_copy(update={"max_attempts": max_attempts, "retry_delay_seconds": 0.0})
        for profile in config.profiles
    )
    return config.model_copy(update={"service_failure_retries": retries, "profiles": profiles})


async def _bundle(tmp_path: Path, config: ProductConfigV2, factory):  # type: ignore[no-untyped-def]
    snapshot = load_product_config(write_config(tmp_path / "config.json", config))
    assert isinstance(snapshot, ProductConfigSnapshot)
    return await build_provider_bundle(
        snapshot,
        select_profile(snapshot, None),
        environment_secret_provider(config, environment=_SECRETS),
        audit=None,
        factory=factory,
    )


@pytest.mark.parametrize(
    ("retries", "max_attempts", "expected"),
    [
        (None, 6, 6),  # 未配置：沿用Profile（向导写6）
        (None, 4, 4),  # 未配置的旧配置：沿用原来的4次
        (2, 6, 3),  # Issue #2：配置2次重试，最多3次尝试
        (0, 6, 1),  # 0：不自动重试
        (5, 4, 6),  # 旧配置显式要5次重试：按配置的次数，而不是Profile的4次
        (20, 6, 6),  # 一次请求最多6次尝试，更大的值按5次重试
    ],
)
async def test_each_profile_gets_the_configured_attempt_count(
    tmp_path: Path, config: ProductConfigV2, retries: int | None, max_attempts: int, expected: int
) -> None:
    seen: dict[str, int] = {}

    def factory(_definition, profile, _key):  # type: ignore[no-untyped-def]
        seen[profile.profile_id] = profile.max_attempts
        return object()

    bundle = await _bundle(tmp_path, _configured(config, retries, max_attempts), factory)
    # 配置的回退模型是另一个Profile，同样按这个次数尝试。
    assert seen == {"primary": expected, "backup": expected}
    assert [candidate.profile.max_attempts for candidate in bundle.candidates] == [max_attempts] * 2


@pytest.mark.parametrize(("retries", "requests"), [(2, 3), (0, 1), (None, 6)])
async def test_a_service_that_keeps_returning_503_gets_n_plus_one_requests(
    tmp_path: Path,
    config: ProductConfigV2,
    monkeypatch: pytest.MonkeyPatch,
    retries: int | None,
    requests: int,
) -> None:
    """真实的OpenAI Chat适配器与产品装配，只把网络换成一直返回503的服务（与Issue #2的夹具相同）。"""
    sent: list[httpx.Request] = []

    def unavailable(outgoing: httpx.Request) -> httpx.Response:
        sent.append(outgoing)
        return httpx.Response(
            503,
            headers={"content-type": "application/json", "retry-after": "0"},
            json={"error": {"message": "fixture temporary failure", "type": "server_error"}},
        )

    monkeypatch.setattr(
        product_runtime, "openai_proxy_transport", lambda _proxy: httpx.MockTransport(unavailable)
    )
    single = _configured(config, retries, 6)
    primary = next(profile for profile in single.profiles if profile.profile_id == "primary")
    single = single.model_copy(
        update={
            "profiles": (*single.profiles[:1], primary.model_copy(update={"fallback_profiles": ()}))
        }
    )
    bundle = await _bundle(tmp_path, single, product_runtime.default_provider_factory)
    async with bundle:
        events = [event async for event in bundle.stream(model_request(), CancelToken())]
    assert len(sent) == requests
    assert isinstance(events[-1], ResponseFailed) and events[-1].code == "provider_internal"
