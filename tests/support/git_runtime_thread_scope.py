"""真实 Git SDK 测试的 Runtime Thread 持锁窗口，嵌套连接仅复用原 Task 的锁。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime


@asynccontextmanager
async def git_runtime_thread_scope(runtime: AgentRuntime, thread_id: UUID) -> AsyncIterator[None]:
    """复用已验证属于当前 Task 的锁；未持锁时沿原 Runtime 入口获取。"""
    try:
        runtime._require_thread_lock(thread_id)
    except KernelError as error:
        if error.code != "runtime_thread_lock_unowned":
            raise
        async with runtime._lock(thread_id):
            yield
    else:
        # 嵌套故障注入连接保持同一 Task 持锁，不重入获取或释放外层锁。
        yield
