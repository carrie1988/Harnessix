"""公开结果保护的纯端口及独立期限；不依赖Secrets实现或Action运行时。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import monotonic
from typing import Protocol

from pydantic import JsonValue

from harnessix.agent.cancellation import CancelToken, TurnCancelled, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError

PUBLIC_PROTECTION_POLICY = "harnessix.public-output-protection/v1"
PUBLIC_PROTECTION_TIMEOUT = 10.0


class PublicOutputProtection(Protocol):
    def assert_public_json(self, value: JsonValue, *, checkpoint: Callable[[], None]) -> None: ...

    def assert_public_jsonl(self, body: bytes, *, checkpoint: Callable[[], None]) -> None: ...


async def _protect(check: Callable[[Callable[[], None]], None], cancel: CancelToken) -> None:
    deadline = monotonic() + PUBLIC_PROTECTION_TIMEOUT

    def check_budget() -> None:
        cancel.checkpoint()
        if monotonic() >= deadline:
            raise KernelError("public_output_timeout", "公开结果保护超时")

    checkpoint = parent_cancel_checkpointer(check_budget)
    try:
        async with asyncio.timeout(PUBLIC_PROTECTION_TIMEOUT) as timer:
            await asyncio.sleep(0)
            checkpoint()
            check(checkpoint)
            checkpoint()
            await asyncio.sleep(0)
            checkpoint()
    except TurnCancelled:
        raise
    except TimeoutError:
        code = "public_output_timeout" if timer.expired() else "public_output_protection_failed"
        raise KernelError(code, "公开结果未通过保护校验") from None
    except KernelError as error:
        code = {
            "trusted_action_secret_leak": "public_output_secret_leak",
            "trusted_action_secret_unavailable": "public_output_secret_unavailable",
            "trusted_action_output_limit": "public_output_limit",
            "public_output_timeout": "public_output_timeout",
        }.get(error.code, "public_output_protection_failed")
        raise KernelError(code, "公开结果未通过保护校验") from None
    except Exception:
        raise KernelError("public_output_protection_failed", "公开结果未通过保护校验") from None


async def protect_json(
    protection: PublicOutputProtection | None, value: JsonValue, cancel: CancelToken
) -> None:
    if protection is not None:
        await _protect(
            lambda checkpoint: protection.assert_public_json(value, checkpoint=checkpoint), cancel
        )


async def protect_jsonl(
    protection: PublicOutputProtection | None, body: bytes, cancel: CancelToken
) -> None:
    if protection is not None:
        await _protect(
            lambda checkpoint: protection.assert_public_jsonl(body, checkpoint=checkpoint), cancel
        )
