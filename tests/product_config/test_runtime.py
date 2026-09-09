from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import AgentFailure, KernelError
from harnessix.agent.models import Budget, Usage
from harnessix.agent.usage import (
    ModelAttemptFinished,
    ModelAttemptStarted,
    ModelUsageObserved,
    UsageObservation,
)
from harnessix.models.contracts import (
    ModelRequest,
    ProviderEvent,
    ResponseCompleted,
    ResponseFailed,
    ResponseStarted,
    TextDelta,
    ToolCallCompleted,
)
from harnessix.product_config.codec import load_product_config
from harnessix.product_config.contracts import (
    ProductConfigSnapshot,
    ProductConfigV2,
    ProfileSelection,
    profile_selection_digest,
)
from harnessix.product_config.runtime import (
    build_provider_bundle,
    diagnose_configuration,
    environment_secret_provider,
    select_profile,
)
from harnessix.product_config.store import SQLiteProductConfigStore
from tests.product_config.conftest import write_config


class EventsProvider:
    def __init__(self, events: tuple[ProviderEvent, ...]) -> None:
        self.events = events
        self.requests = 0
        self.closed = False

    async def stream(
        self, request: ModelRequest, cancel: CancelToken
    ) -> AsyncGenerator[ProviderEvent, None]:
        self.requests += 1
        for event in self.events:
            cancel.checkpoint()
            yield event

    async def aclose(self) -> None:
        self.closed = True


class FailingCloseProvider(EventsProvider):
    async def aclose(self) -> None:
        self.closed = True
        raise RuntimeError("close failed")


def request() -> ModelRequest:
    return ModelRequest(
        thread_id=uuid4(),
        turn_id=uuid4(),
        step=1,
        history=(),
        tools=(),
        budget=Budget(),
    )


def failed_events() -> tuple[ProviderEvent, ...]:
    attempt = uuid4()
    return (
        ModelAttemptStarted(
            attempt_id=attempt,
            step=1,
            index=1,
            provider="local",
            requested_model="primary-model",
        ),
        ModelUsageObserved(
            attempt_id=attempt,
            usage=UsageObservation(completeness="partial", input_tokens=1),
            actual_model="primary-model",
        ),
        ModelAttemptFinished(
            attempt_id=attempt,
            outcome="failed",
            error=AgentFailure(code="provider_transport", message="fixture", retryable=True),
        ),
        ResponseFailed(code="transport", retryable=True),
    )


def completed_events() -> tuple[ProviderEvent, ...]:
    attempt = uuid4()
    return (
        ModelAttemptStarted(
            attempt_id=attempt,
            step=1,
            index=1,
            provider="local",
            requested_model="backup-model",
        ),
        ModelUsageObserved(
            attempt_id=attempt,
            usage=UsageObservation(completeness="complete", input_tokens=2, output_tokens=3),
            actual_model="backup-model",
            response_id="response-2",
        ),
        ModelAttemptFinished(attempt_id=attempt, outcome="completed"),
        ResponseStarted(response_id="response-2"),
        ResponseCompleted(usage=Usage(input_tokens=2, output_tokens=3)),
    )


async def _bundle(
    tmp_path: Path,
    config: ProductConfigV2,
    providers: list[EventsProvider],
):
    path = write_config(tmp_path / "config.json", config)
    snapshot = load_product_config(path)
    assert isinstance(snapshot, ProductConfigSnapshot)
    store = SQLiteProductConfigStore(tmp_path / "config.db")
    store.save_snapshot(snapshot)
    store.activate(snapshot, "primary", expected_active_sha256=None)
    selected = select_profile(snapshot, None)
    position = 0

    def factory(_definition, _profile, key: str):
        nonlocal position
        assert key == ("primary-secret" if position == 0 else "backup-secret")
        provider = providers[position]
        position += 1
        return provider

    bundle = await build_provider_bundle(
        snapshot,
        selected,
        environment_secret_provider(
            config,
            environment={
                "PRIMARY_API_KEY": "primary-secret",
                "BACKUP_API_KEY": "backup-secret",
            },
        ),
        audit=store,
        factory=factory,
    )
    return snapshot, store, bundle


async def test_zero_exposure_failure_falls_back_with_global_attempts_and_audit(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    first = EventsProvider(failed_events())
    second = EventsProvider(completed_events())
    snapshot, store, bundle = await _bundle(tmp_path, config, [first, second])
    try:
        events = [event async for event in bundle.stream(request(), CancelToken())]
        attempts = [event for event in events if isinstance(event, ModelAttemptStarted)]
        assert [(item.index, item.provider) for item in attempts] == [
            (1, "primary"),
            (2, "backup"),
        ]
        assert not any(isinstance(event, ResponseFailed) for event in events)
        assert any(isinstance(event, ModelUsageObserved) for event in events)
        assert isinstance(events[-1], ResponseCompleted)
        decisions = store.fallback_events()
        assert len(decisions) == 1
        assert decisions[0].config_sha256 == snapshot.config_sha256
        assert decisions[0].failure_code == "transport"
        assert not decisions[0].response_exposed and not decisions[0].tool_call_exposed
    finally:
        await bundle.aclose()
        store.close()
    assert first.closed and second.closed


@pytest.mark.parametrize("exposed", ["response", "text", "tool"])
async def test_never_falls_back_after_response_or_tool_exposure(
    tmp_path: Path, config: ProductConfigV2, exposed: str
) -> None:
    attempt = uuid4()
    visible: ProviderEvent
    if exposed == "response":
        visible = ResponseStarted(response_id="visible")
    elif exposed == "text":
        visible = TextDelta(content_id="text", delta="visible")
    else:
        visible = ToolCallCompleted(call_id="call", tool="read_file", arguments={})
    first = EventsProvider(
        (
            ModelAttemptStarted(
                attempt_id=attempt,
                step=1,
                index=1,
                provider="local",
                requested_model="primary-model",
            ),
            visible,
            ModelAttemptFinished(
                attempt_id=attempt,
                outcome="failed",
                error=AgentFailure(code="provider_transport", message="fixture", retryable=True),
            ),
            ResponseFailed(code="transport", retryable=True),
        )
    )
    second = EventsProvider(completed_events())
    _, store, bundle = await _bundle(tmp_path, config, [first, second])
    try:
        events = [event async for event in bundle.stream(request(), CancelToken())]
        assert isinstance(events[-1], ResponseFailed)
        assert second.requests == 0
        assert store.fallback_events() == ()
    finally:
        await bundle.aclose()
        store.close()


async def test_non_retryable_or_unaudited_failure_never_falls_back(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    first = EventsProvider(
        tuple(
            ResponseFailed(code="authentication") if isinstance(event, ResponseFailed) else event
            for event in failed_events()
        )
    )
    second = EventsProvider(completed_events())
    _, store, bundle = await _bundle(tmp_path, config, [first, second])
    try:
        events = [event async for event in bundle.stream(request(), CancelToken())]
        assert events[-1] == ResponseFailed(code="authentication")
        assert second.requests == 0 and store.fallback_events() == ()
    finally:
        await bundle.aclose()
        store.close()

    path = write_config(tmp_path / "unaudited.json", config)
    snapshot = load_product_config(path)
    assert isinstance(snapshot, ProductConfigSnapshot)
    selection = select_profile(snapshot, None)
    providers = [EventsProvider(failed_events()), EventsProvider(completed_events())]
    position = 0

    def factory(_definition, _profile, _key: str):
        nonlocal position
        provider = providers[position]
        position += 1
        return provider

    unaudited = await build_provider_bundle(
        snapshot,
        selection,
        environment_secret_provider(
            config,
            environment={
                "PRIMARY_API_KEY": "primary-secret",
                "BACKUP_API_KEY": "backup-secret",
            },
        ),
        audit=None,
        factory=factory,
    )
    try:
        events = [event async for event in unaudited.stream(request(), CancelToken())]
        assert isinstance(events[-1], ResponseFailed)
        assert providers[1].requests == 0
    finally:
        await unaudited.aclose()


async def test_audit_failure_prevents_switch(tmp_path: Path, config: ProductConfigV2) -> None:
    first = EventsProvider(failed_events())
    second = EventsProvider(completed_events())
    _, store, bundle = await _bundle(tmp_path, config, [first, second])
    store.close()
    try:
        events = [event async for event in bundle.stream(request(), CancelToken())]
        assert isinstance(events[-1], ResponseFailed)
        assert second.requests == 0
    finally:
        await bundle.aclose()
        store.close()


async def test_selection_snapshot_mismatch_fails_before_secret_resolution(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    path = write_config(tmp_path / "config.json", config)
    snapshot = load_product_config(path)
    assert isinstance(snapshot, ProductConfigSnapshot)
    selected = select_profile(snapshot, None)
    forged = ProfileSelection.model_construct(
        **selected.model_dump(
            exclude={
                "profile_chain",
                "provider_chain",
                "model_chain",
                "selection_sha256",
            }
        ),
        profile_chain=("primary",),
        provider_chain=("primary",),
        model_chain=("gpt-test",),
        selection_sha256="0" * 64,
    )
    # 重新构造合法摘要，但链仍不是配置图的正式展开。
    forged = forged.model_copy(update={"selection_sha256": profile_selection_digest(forged)})
    forged = ProfileSelection.model_validate_json(forged.model_dump_json())
    secrets = environment_secret_provider(config, environment={})
    with pytest.raises(KernelError) as error:
        diagnose_configuration(snapshot, forged, secrets)
    assert error.value.code == "product_config_selection_mismatch"


async def test_provider_construction_failure_closes_every_created_candidate(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    path = write_config(tmp_path / "config.json", config)
    snapshot = load_product_config(path)
    assert isinstance(snapshot, ProductConfigSnapshot)
    selected = select_profile(snapshot, None)
    first = FailingCloseProvider(())
    calls = 0

    def factory(_definition, _profile, _key: str):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("factory failed")
        return first

    with pytest.raises(RuntimeError, match="factory failed"):
        await build_provider_bundle(
            snapshot,
            selected,
            environment_secret_provider(
                config,
                environment={
                    "PRIMARY_API_KEY": "primary-secret",
                    "BACKUP_API_KEY": "backup-secret",
                },
            ),
            audit=None,
            factory=factory,
        )
    assert first.closed


async def test_bundle_close_attempts_every_provider_even_if_one_close_fails(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    first = EventsProvider(())
    second = FailingCloseProvider(())
    _, store, bundle = await _bundle(tmp_path, config, [first, second])
    with pytest.raises(RuntimeError, match="close failed"):
        await bundle.aclose()
    store.close()
    assert first.closed and second.closed


def test_diagnostics_are_offline_bounded_and_do_not_expose_secret(
    tmp_path: Path, config: ProductConfigV2
) -> None:
    path = write_config(tmp_path / "config.json", config)
    snapshot = load_product_config(path)
    assert isinstance(snapshot, ProductConfigSnapshot)
    selection = select_profile(snapshot, None)
    canary = "diagnostic-secret-canary"
    report = diagnose_configuration(
        snapshot,
        selection,
        environment_secret_provider(
            config,
            environment={"PRIMARY_API_KEY": canary, "BACKUP_API_KEY": "backup"},
        ),
        dependency_finder=lambda _: object(),
    )
    assert report.ready
    assert canary not in report.model_dump_json()
    failed = diagnose_configuration(
        snapshot,
        selection,
        environment_secret_provider(config, environment={}),
        dependency_finder=lambda _: None,
    )
    assert not failed.ready
    assert {item.status for item in failed.checks} == {"passed", "failed"}

    invalid = diagnose_configuration(
        snapshot,
        selection,
        environment_secret_provider(
            config,
            environment={"PRIMARY_API_KEY": "with space", "BACKUP_API_KEY": "backup"},
        ),
        dependency_finder=lambda _: object(),
    )
    assert not invalid.ready
    assert any(
        item.code == "config_provider_auth_usable" and item.status == "failed"
        for item in invalid.checks
    )
    assert any(
        item.code == "config_secret_version_available" and item.status == "passed"
        for item in invalid.checks
    )
