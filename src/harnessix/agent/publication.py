"""公开结果保护的纯端口及独立期限；不依赖Secrets实现或Action运行时。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from time import monotonic
from typing import Protocol, cast

from pydantic import JsonValue

from harnessix.agent.cancellation import CancelToken, TurnCancelled, parent_cancel_checkpointer
from harnessix.agent.errors import KernelError
from harnessix.domain.review_text import review_text_for_protection

PUBLIC_PROTECTION_POLICY = "harnessix.public-output-protection/v1"
PUBLIC_PROTECTION_TIMEOUT = 10.0

BinaryStreamDecoder = Callable[[bytes, Callable[[], None]], tuple[bytes, ...]]


async def protect_review_jsonl(
    protection: PublicOutputProtection | None, body: bytes, cancel: CancelToken
) -> None:
    """已知完整Review重建原文再扫描，发布和重开读取共用原10秒保护期限。"""
    if protection is None:
        return

    def check(checkpoint: Callable[[], None]) -> None:
        protection.assert_public_jsonl(body, checkpoint=checkpoint)
        text = review_text_for_protection(body, checkpoint=checkpoint)
        if text is not None:
            protection.assert_public_json(text, checkpoint=checkpoint)

    await _protect(check, cancel)


class PublicOutputProtection(Protocol):
    def assert_public_json(self, value: JsonValue, *, checkpoint: Callable[[], None]) -> None: ...

    def assert_public_jsonl(self, body: bytes, *, checkpoint: Callable[[], None]) -> None: ...


class PublicBinaryOutputProtection(Protocol):
    """原JSONL和正式解码双流共享同一预算；解码器由受信Artifact层提供。"""

    def assert_public_binary_jsonl(
        self, body: bytes, decoder: BinaryStreamDecoder, *, checkpoint: Callable[[], None]
    ) -> None: ...


class PublicTextStep(Protocol):
    """一个模型步骤共享资源预算；仅返回原文安全前缀，不替换正文。"""

    def feed(self, content_id: str, text: str, *, checkpoint: Callable[[], None]) -> str: ...

    def finish(self, content_id: str, *, checkpoint: Callable[[], None]) -> str: ...

    def close(self) -> None: ...


class PublicTextOutputProtection(Protocol):
    """可选流式能力；开启公开保护的模型宿主必须提供此工厂。"""

    def begin_public_text_step(self, *, checkpoint: Callable[[], None]) -> PublicTextStep: ...


async def _protect[T](check: Callable[[Callable[[], None]], T], cancel: CancelToken) -> T:
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
            result = check(checkpoint)
            checkpoint()
            await asyncio.sleep(0)
            checkpoint()
            return result
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
            "public_output_binary_capability_missing": "public_output_binary_capability_missing",
            "public_output_stream_capability_missing": "public_output_stream_capability_missing",
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


async def protect_binary_jsonl(
    protection: PublicOutputProtection | None,
    body: bytes,
    decoder: BinaryStreamDecoder,
    cancel: CancelToken,
) -> None:
    """已知二进制输出必须显式具备解码保护能力，不能静默降级为字符串扫描。"""
    if protection is not None:

        def check(checkpoint: Callable[[], None]) -> None:
            method = getattr(protection, "assert_public_binary_jsonl", None)
            if not callable(method):
                raise KernelError(
                    "public_output_binary_capability_missing", "公开结果缺少二进制保护能力"
                )
            method(body, decoder, checkpoint=checkpoint)

        await _protect(check, cancel)


async def begin_text_step(
    protection: PublicOutputProtection | None, cancel: CancelToken
) -> PublicTextStep | None:
    """保护已开启时必须具备流式能力；工厂异常也在固定失败边界内。"""
    if protection is None:
        return None

    step: PublicTextStep | None = None

    def begin(checkpoint: Callable[[], None]) -> PublicTextStep:
        nonlocal step
        factory = getattr(protection, "begin_public_text_step", None)
        if not callable(factory):
            raise KernelError("public_output_stream_capability_missing", "公开文本缺少流式保护能力")
        step = cast(PublicTextStep, factory(checkpoint=checkpoint))
        if not all(callable(getattr(step, name, None)) for name in ("feed", "finish", "close")):
            raise KernelError("public_output_stream_capability_missing", "公开文本缺少流式保护能力")
        return step

    try:
        return await _protect(begin, cancel)
    except BaseException:
        close_text_step(step, failed=True)
        raise


async def protect_text(
    step: PublicTextStep | None, content_id: str, text: str | None, cancel: CancelToken
) -> str:
    """每次同步扫描独立期限，步骤内累计工作预算由纯端口保持。"""
    if step is None:
        return text or ""

    def check(checkpoint: Callable[[], None]) -> str:
        result = (
            step.finish(content_id, checkpoint=checkpoint)
            if text is None
            else step.feed(content_id, text, checkpoint=checkpoint)
        )
        if type(result) is not str:
            raise KernelError("public_output_protection_failed", "公开文本保护返回类型无效")
        return result

    return await _protect(check, cancel)


def close_text_step(step: PublicTextStep | None, *, failed: bool) -> None:
    """始终回收窗口；清理异常不能覆盖原失败或取消，也不能暴露宿主诊断。"""
    if step is not None:
        try:
            step.close()
        except Exception:
            if not failed:
                raise KernelError(
                    "public_output_protection_failed", "公开文本保护关闭失败"
                ) from None
