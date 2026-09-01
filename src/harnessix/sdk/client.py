from __future__ import annotations

from typing import Any, Self
from uuid import UUID

import httpx

from harnessix.domain.models import (
    ActionEvent,
    ActionRequest,
    ActionSnapshot,
    ApprovalDecision,
    ToolDescriptor,
)


class HarnessixAPIError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class HarnessixClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8787",
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def submit(self, request: ActionRequest) -> ActionSnapshot:
        response = self._client.post(
            "/v1/actions", json=request.model_dump(mode="json", exclude_none=True)
        )
        return _snapshot(response)

    def get(self, action_id: UUID | str) -> ActionSnapshot:
        return _snapshot(self._client.get(f"/v1/actions/{action_id}"))

    def decide_approval(self, action_id: UUID | str, decision: ApprovalDecision) -> ActionSnapshot:
        response = self._client.post(
            f"/v1/actions/{action_id}/approval",
            json=decision.model_dump(mode="json", exclude_none=True),
        )
        return _snapshot(response)

    def reconcile(self, action_id: UUID | str) -> ActionSnapshot:
        return _snapshot(self._client.post(f"/v1/actions/{action_id}/reconcile"))

    def events(self, action_id: UUID | str) -> list[ActionEvent]:
        response = self._client.get(f"/v1/actions/{action_id}/events")
        _raise_for_error(response)
        return [ActionEvent.model_validate(item) for item in response.json()["events"]]

    def tools(self) -> list[ToolDescriptor]:
        response = self._client.get("/v1/tools")
        _raise_for_error(response)
        return [ToolDescriptor.model_validate(item) for item in response.json()["tools"]]


class HarnessixAsyncClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8787",
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def submit(self, request: ActionRequest) -> ActionSnapshot:
        response = await self._client.post(
            "/v1/actions", json=request.model_dump(mode="json", exclude_none=True)
        )
        return _snapshot(response)

    async def get(self, action_id: UUID | str) -> ActionSnapshot:
        return _snapshot(await self._client.get(f"/v1/actions/{action_id}"))

    async def decide_approval(
        self, action_id: UUID | str, decision: ApprovalDecision
    ) -> ActionSnapshot:
        response = await self._client.post(
            f"/v1/actions/{action_id}/approval",
            json=decision.model_dump(mode="json", exclude_none=True),
        )
        return _snapshot(response)

    async def reconcile(self, action_id: UUID | str) -> ActionSnapshot:
        return _snapshot(await self._client.post(f"/v1/actions/{action_id}/reconcile"))

    async def events(self, action_id: UUID | str) -> list[ActionEvent]:
        response = await self._client.get(f"/v1/actions/{action_id}/events")
        _raise_for_error(response)
        return [ActionEvent.model_validate(item) for item in response.json()["events"]]

    async def tools(self) -> list[ToolDescriptor]:
        response = await self._client.get("/v1/tools")
        _raise_for_error(response)
        return [ToolDescriptor.model_validate(item) for item in response.json()["tools"]]


def _snapshot(response: httpx.Response) -> ActionSnapshot:
    _raise_for_error(response)
    return ActionSnapshot.model_validate(response.json())


def _raise_for_error(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        detail: dict[str, Any] = response.json()["error"]
        code = str(detail["code"])
        message = str(detail["message"])
    except (KeyError, TypeError, ValueError):
        code = "http_error"
        message = response.text or response.reason_phrase
    raise HarnessixAPIError(response.status_code, code, message)
