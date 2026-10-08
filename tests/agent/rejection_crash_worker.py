"""独立进程在拒绝批次提交点硬退出，不模拟恢复或执行效果。"""

from __future__ import annotations

import asyncio
import os
import sys
from uuid import UUID

from harnessix.agent.runtime import AgentRuntime
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallRejected
from harnessix.session.sqlite import SQLiteSessionStore


async def main():
    path, thread_id, point = sys.argv[1:]
    armed = False

    def crash(name):
        if armed and name == point:
            os._exit(77)

    class Provider:
        async def stream(self, request, token):
            nonlocal armed
            yield ResponseStarted(response_id="rejection-response")
            yield ToolCallRejected(call_id="rejected", argument_chars=2)
            if point == "before-terminal":
                os._exit(77)
            yield ResponseCompleted(finish_reason="tool_calls")
            # 原Usage独立提交已完成；只在调用组批次边界触发Session故障。
            armed = True

    async with AgentRuntime(
        SQLiteSessionStore(path, fault=crash), Provider(), fault=crash
    ) as runtime:
        await runtime.run_turn(UUID(thread_id), "拒绝提交恢复验证", request_id="crash")
    raise AssertionError("硬退出故障点未触发")


if __name__ == "__main__":
    asyncio.run(main())
