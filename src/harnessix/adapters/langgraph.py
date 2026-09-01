from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from harnessix.domain.models import (
    ActionContext,
    ActionRequest,
    ActionSnapshot,
    EffectClass,
    Principal,
    SecretRef,
)
from harnessix.sdk.client import HarnessixAsyncClient, HarnessixClient

IdempotencyKeyFactory = Callable[[dict[str, Any]], str | None]


class SyncActionClient(Protocol):
    def submit(self, request: ActionRequest) -> ActionSnapshot: ...


class AsyncActionClient(Protocol):
    async def submit(self, request: ActionRequest) -> ActionSnapshot: ...


@dataclass(frozen=True, slots=True)
class HarnessixToolContext:
    principal: Principal
    action_context: ActionContext
    secret_refs: tuple[SecretRef, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


def create_harnessix_tool(
    *,
    action_name: str,
    description: str,
    args_schema: type[BaseModel],
    context: HarnessixToolContext,
    client: SyncActionClient | HarnessixClient | None = None,
    async_client: AsyncActionClient | HarnessixAsyncClient | None = None,
    tool_name: str | None = None,
    effect_hint: EffectClass | None = None,
    idempotency_key: IdempotencyKeyFactory | None = None,
) -> BaseTool:
    """把一个 Harnessix Action 暴露为标准 LangGraph/LangChain 工具。"""
    if client is None and async_client is None:
        raise ValueError("client 和 async_client 至少提供一个")

    def build_request(arguments: dict[str, Any]) -> ActionRequest:
        return ActionRequest(
            tool=action_name,
            arguments=arguments,
            principal=context.principal,
            context=context.action_context,
            effect_hint=effect_hint,
            idempotency_key=idempotency_key(arguments) if idempotency_key else None,
            secret_refs=context.secret_refs,
            metadata={"adapter": "langgraph", **context.metadata},
        )

    def invoke(**arguments: Any) -> str:
        if client is None:
            raise RuntimeError("同步调用需要同步 Harnessix Client")
        return client.submit(build_request(arguments)).model_dump_json(exclude_none=True)

    async def ainvoke(**arguments: Any) -> str:
        if async_client is None:
            raise RuntimeError("异步调用需要异步 Harnessix Client")
        snapshot = await async_client.submit(build_request(arguments))
        return snapshot.model_dump_json(exclude_none=True)

    return StructuredTool.from_function(
        func=invoke if client is not None else None,
        coroutine=ainvoke if async_client is not None else None,
        name=tool_name or _safe_tool_name(action_name),
        description=description,
        args_schema=args_schema,
    )


def _safe_tool_name(action_name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", action_name).strip("_")
    if not name:
        raise ValueError("action_name 必须包含字母或数字")
    if name[0].isdigit():
        name = f"action_{name}"
    return name
