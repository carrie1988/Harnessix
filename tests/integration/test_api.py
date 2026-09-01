from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from harnessix.api import create_app
from harnessix.bootstrap import build_service
from harnessix.domain.models import ActionStatus
from harnessix.runtime import ActionService
from harnessix.settings import Settings
from harnessix.worker import ActionWorker
from tests.helpers import action_request


async def test_http_api_executes_echo(service: ActionService) -> None:
    app = create_app(service=service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            request = action_request("system.echo", {"message": "api"})
            response = await client.post(
                "/v1/actions", json=request.model_dump(mode="json", exclude_none=True)
            )
            events = await client.get(f"/v1/actions/{request.action_id}/events")
            tools = await client.get("/v1/tools")

    assert response.status_code == 200
    assert response.json()["status"] == ActionStatus.SUCCEEDED
    assert len(events.json()["events"]) >= 6
    assert {item["name"] for item in tools.json()["tools"]} == {
        "demo.issue.create",
        "system.echo",
    }


async def test_http_api_returns_structured_conflict(service: ActionService) -> None:
    app = create_app(service=service)
    first = action_request("demo.issue.create", {"title": "A"}, idempotency_key="api:conflict")
    second = action_request("demo.issue.create", {"title": "B"}, idempotency_key="api:conflict")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/v1/actions", json=first.model_dump(mode="json"))
            response = await client.post("/v1/actions", json=second.model_dump(mode="json"))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"


async def test_queued_http_api_returns_202_and_worker_completes(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "queued.db",
        demo_database_path=tmp_path / "demo-external.db",
        execution_mode="queued",
        lease_seconds=2,
        worker_heartbeat_seconds=0.2,
    )
    api_service = build_service(settings)
    app = create_app(service=api_service)
    request = action_request("system.echo", {"message": "api-queue"})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/actions", json=request.model_dump(mode="json", exclude_none=True)
            )

    worker_service = build_service(settings, worker_id="separate-worker")
    await worker_service.initialize()
    try:
        completed = await ActionWorker(
            worker_service,
            heartbeat_seconds=0.2,
        ).run_once()
    finally:
        await worker_service.close()

    assert response.status_code == 202
    assert response.json()["status"] == ActionStatus.READY
    assert completed is not None
    assert completed.status is ActionStatus.SUCCEEDED


async def test_readiness_checks_journal(
    service: ActionService, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(service=service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            healthy = await client.get("/readyz")

            async def unavailable() -> bool:
                return False

            monkeypatch.setattr(service.journal, "ping", unavailable)
            unhealthy = await client.get("/readyz")

    assert healthy.status_code == 200
    assert healthy.json() == {"status": "ready"}
    assert unhealthy.status_code == 503
    assert unhealthy.json()["reason"] == "journal_unavailable"
