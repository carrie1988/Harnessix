from __future__ import annotations

import httpx

from harnessix.api import create_app
from harnessix.domain.models import ActionStatus
from harnessix.runtime import ActionService
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
