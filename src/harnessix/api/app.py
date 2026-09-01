from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from harnessix.bootstrap import build_service
from harnessix.domain.errors import HarnessixError
from harnessix.domain.models import (
    ActionEvent,
    ActionRequest,
    ActionSnapshot,
    ActionStatus,
    ApprovalDecision,
    ToolDescriptor,
)
from harnessix.runtime import ActionService
from harnessix.settings import Settings


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


class ToolListResponse(BaseModel):
    tools: list[ToolDescriptor]


class EventListResponse(BaseModel):
    events: list[ActionEvent]


def _service(request: Request) -> ActionService:
    service = request.app.state.action_service
    assert isinstance(service, ActionService)
    return service


def _apply_action_status(response: Response, snapshot: ActionSnapshot) -> None:
    if snapshot.status in {
        ActionStatus.PENDING_APPROVAL,
        ActionStatus.READY,
        ActionStatus.LEASED,
        ActionStatus.RUNNING,
        ActionStatus.UNKNOWN,
        ActionStatus.RECONCILING,
    }:
        response.status_code = 202


def create_app(
    settings: Settings | None = None, *, service: ActionService | None = None
) -> FastAPI:
    resolved_settings = settings or Settings.from_environment()
    resolved_service = service or build_service(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await resolved_service.initialize()
        app.state.action_service = resolved_service
        try:
            yield
        finally:
            await resolved_service.close()

    app = FastAPI(
        title="Harnessix Action API",
        version="0.1.0",
        description="跨 Agent 框架的副作用安全执行与治理平面。",
        lifespan=lifespan,
    )

    @app.exception_handler(HarnessixError)
    async def handle_harnessix_error(_: Request, error: HarnessixError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": error.code, "message": error.message}},
        )

    @app.get("/healthz", tags=["系统"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/tools", response_model=ToolListResponse, tags=["工具"])
    async def list_tools(request: Request) -> ToolListResponse:
        return ToolListResponse(tools=_service(request).tools())

    @app.post(
        "/v1/actions",
        response_model=ActionSnapshot,
        responses={202: {"model": ActionSnapshot}, 409: {"model": ErrorResponse}},
        tags=["Action"],
    )
    async def submit_action(
        action_request: ActionRequest, request: Request, response: Response
    ) -> ActionSnapshot:
        snapshot = await _service(request).submit(action_request)
        _apply_action_status(response, snapshot)
        return snapshot

    @app.get(
        "/v1/actions/{action_id}",
        response_model=ActionSnapshot,
        responses={404: {"model": ErrorResponse}},
        tags=["Action"],
    )
    async def get_action(action_id: UUID, request: Request) -> ActionSnapshot:
        return await _service(request).get(action_id)

    @app.get(
        "/v1/actions/{action_id}/events",
        response_model=EventListResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["Action"],
    )
    async def list_action_events(action_id: UUID, request: Request) -> EventListResponse:
        return EventListResponse(events=await _service(request).events(action_id))

    @app.post(
        "/v1/actions/{action_id}/approval",
        response_model=ActionSnapshot,
        responses={202: {"model": ActionSnapshot}, 409: {"model": ErrorResponse}},
        tags=["审批"],
    )
    async def decide_approval(
        action_id: UUID,
        decision: ApprovalDecision,
        request: Request,
        response: Response,
    ) -> ActionSnapshot:
        snapshot = await _service(request).decide_approval(action_id, decision)
        _apply_action_status(response, snapshot)
        return snapshot

    @app.post(
        "/v1/actions/{action_id}/reconcile",
        response_model=ActionSnapshot,
        responses={202: {"model": ActionSnapshot}, 409: {"model": ErrorResponse}},
        tags=["对账"],
    )
    async def reconcile_action(
        action_id: UUID, request: Request, response: Response
    ) -> ActionSnapshot:
        snapshot = await _service(request).reconcile(action_id)
        _apply_action_status(response, snapshot)
        return snapshot

    return app


app: Any = create_app()
