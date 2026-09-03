"""捕获真实 asyncio 超时上下文，在明确执行阶段推进期限；不修改全局时钟。"""

import asyncio
from types import SimpleNamespace


def capture_deadlines(monkeypatch, module):
    deadlines = []

    def timeout(delay):
        context = asyncio.timeout(delay)
        deadlines.append(context)
        return context

    # 仅替换待测模块的引用；SDK、其他模块和测试自身仍使用原始 asyncio。
    monkeypatch.setattr(
        module, "asyncio", SimpleNamespace(**(vars(asyncio) | {"timeout": timeout}))
    )
    return deadlines
