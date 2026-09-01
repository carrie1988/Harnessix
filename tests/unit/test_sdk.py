from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from harnessix import HarnessixAsyncClient
from harnessix.domain.models import ActionStatus, EffectClass, RiskLevel
from tests.helpers import action_request


async def test_async_sdk_preserves_action_contract() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        now = datetime.now(UTC).isoformat()
        return httpx.Response(
            200,
            json={
                "request": observed,
                "request_fingerprint": "fingerprint",
                "tool": {
                    "name": "system.echo",
                    "version": "1.0.0",
                    "description": "echo",
                    "input_schema": {},
                    "effect_class": EffectClass.READ_ONLY,
                    "risk_level": RiskLevel.LOW,
                    "requires_idempotency": False,
                    "requires_approval": False,
                    "supports_reconciliation": False,
                },
                "status": ActionStatus.SUCCEEDED,
                "created_at": now,
                "updated_at": now,
                "version": 1,
            },
        )

    client = HarnessixAsyncClient(transport=httpx.MockTransport(handler))
    request = action_request("system.echo", {"message": "SDK"})
    try:
        snapshot = await client.submit(request)
    finally:
        await client.close()

    assert observed["spec_version"] == "harnessix.action/v1"
    assert observed["action_id"] == str(request.action_id)
    assert snapshot.status is ActionStatus.SUCCEEDED
