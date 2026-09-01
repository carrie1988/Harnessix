from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from harnessix.bootstrap import build_service
from harnessix.domain.models import ActionStatus, TraceContext
from harnessix.runtime import action_fingerprint
from harnessix.settings import Settings
from harnessix.worker import ActionWorker
from tests.helpers import action_request

POSTGRES_URL = os.getenv("HARNESSIX_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    POSTGRES_URL is None,
    reason="未配置 HARNESSIX_TEST_POSTGRES_URL",
)


async def test_postgres_workers_claim_action_without_duplication(tmp_path: Path) -> None:
    assert POSTGRES_URL is not None
    settings = Settings(
        database_url=POSTGRES_URL,
        demo_database_path=tmp_path / "demo-external.db",
        execution_mode="queued",
        lease_seconds=2,
        worker_heartbeat_seconds=0.2,
    )
    first_service = build_service(settings, worker_id="postgres-worker-a")
    second_service = build_service(settings, worker_id="postgres-worker-b")
    await asyncio.gather(first_service.initialize(), second_service.initialize())
    try:
        request = action_request("system.echo", {"message": "postgres-queue"})
        ready = await first_service.submit(request)

        first_result, second_result = await asyncio.gather(
            ActionWorker(first_service, heartbeat_seconds=0.2).run_once(),
            ActionWorker(second_service, heartbeat_seconds=0.2).run_once(),
        )
        completed = [result for result in (first_result, second_result) if result is not None]
        stored = await first_service.get(request.action_id)
        events = await first_service.events(request.action_id)

        assert ready.status is ActionStatus.READY
        assert len(completed) == 1
        assert completed[0].status is ActionStatus.SUCCEEDED
        assert stored.status is ActionStatus.SUCCEEDED
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert sum(event.event_type == "execution_leased" for event in events) == 1

        traced_request = action_request("system.echo", {"message": "postgres-trace"})
        trace_context = TraceContext(
            traceparent="00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"
        )
        traced, created = await first_service.journal.create_action(
            traced_request,
            first_service.registry.get(traced_request.tool).descriptor(),
            action_fingerprint(traced_request),
            trace_context,
        )
        assert created is True
        assert traced.trace_context == trace_context
    finally:
        await asyncio.gather(first_service.close(), second_service.close())
