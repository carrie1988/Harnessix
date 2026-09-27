"""默认产品持有稳定认证Binding；线程加载取消后仍结算材料并清零。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.product_config.session_key_codec import OwnedSessionKey
from harnessix.product_config.session_key_store import load_session_key
from harnessix.session.publication_seal import PublicationScope
from harnessix.session.store_publication import SessionPublicationBinding

KEY_LOAD_TIMEOUT_SECONDS = 5.0


async def _load_owned(root: Path) -> OwnedSessionKey:
    task = asyncio.create_task(asyncio.to_thread(load_session_key, root))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # 受托线程不可强杀；必须结算唯一任务，不能弃置其返回密钥或重新启动加载。
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled() and task.exception() is None:
            task.result().close()
        raise


@asynccontextmanager
async def open_product_session_binding(
    root: Path, protection: PublicationScope
) -> AsyncIterator[SessionPublicationBinding]:
    try:
        async with asyncio.timeout(KEY_LOAD_TIMEOUT_SECONDS):
            material = await _load_owned(root)
    except TimeoutError:
        raise KernelError("publication_key_timeout", "Session密钥加载超时") from None
    binding: SessionPublicationBinding | None = None
    try:
        binding = SessionPublicationBinding(
            material.store_id, material.key_id, material.key, protection
        )
        yield binding
    finally:
        if binding is not None:
            binding.close()
        material.close()
