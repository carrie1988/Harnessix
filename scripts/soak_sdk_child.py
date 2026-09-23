"""SDK容量Soak使用的真实Agent Protocol stdio子进程夹具。"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

# 夹具通过绝对路径由Transport启动；只把自身仓库根加入模块搜索路径。
if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harnessix.agent.errors import KernelError
from harnessix.agent.runtime import AgentRuntime
from harnessix.app_server.server import AgentProtocolServer
from harnessix.app_server.service import AgentApplicationService
from harnessix.app_server.stdio import run_stdio
from harnessix.protocol.contracts import ProtocolLimits, ThreadListParams, ThreadListResult
from harnessix.protocol.requests import SQLiteProtocolRequestStore
from harnessix.session.sqlite import SQLiteSessionStore
from scripts.soak_provider import SoakProvider
from scripts.soak_rss import read_peak_rss
from scripts.soak_sdk_proof import SoakSdkChildResult


async def _wait_marker(path: Path) -> None:
    """跨进程夹具只等待私有标记；超时让场景失败而不是无限悬挂。"""

    async with asyncio.timeout(30):
        # 跨进程门闩只属于固定Soak夹具；生产协议没有轮询文件的控制面。
        while not path.is_file():  # noqa: ASYNC110, ASYNC240
            await asyncio.sleep(0.005)


class GatedSdkService(AgentApplicationService):
    """在真实列表操作前设置门闩，保留Server和Store的正式执行链。"""

    def __init__(
        self,
        runtime: AgentRuntime,
        store: SQLiteSessionStore,
        requests: SQLiteProtocolRequestStore,
        *,
        gate_root: Path,
        pending_limit: int,
    ) -> None:
        super().__init__(runtime, store, requests)
        self.gate_root = gate_root
        self.pending_limit = pending_limit
        self.entered = 0

    async def list_threads(self, params: ThreadListParams) -> ThreadListResult:
        if params.limit == 50:
            return await super().list_threads(params)
        if params.limit not in (1, 2, 3):
            raise ValueError("SDK夹具只允许固定列表负载")
        round_index, position = divmod(self.entered, self.pending_limit + 1)
        self.entered += 1
        if position < self.pending_limit:
            if params.limit == 3:
                raise ValueError("溢出请求在容量释放前进入Server")
            if position == self.pending_limit - 1:
                (self.gate_root / f"ready-{round_index}").touch()
            gate = "release-cancelled" if params.limit == 2 else "release-rest"
        else:
            if params.limit != 3:
                raise ValueError("SDK夹具溢出请求身份不匹配")
            (self.gate_root / f"overflow-{round_index}").touch()
            gate = "release-rest"
        await _wait_marker(self.gate_root / f"{gate}-{round_index}")
        return await super().list_threads(params)


async def _serve(database: Path, gate_root: Path, pending_limit: int) -> None:
    store = SQLiteSessionStore(database)
    provider = SoakProvider()
    async with AgentRuntime(store, provider) as runtime:
        service = GatedSdkService(
            runtime,
            store,
            SQLiteProtocolRequestStore(store.path),
            gate_root=gate_root,
            pending_limit=pending_limit,
        )
        await run_stdio(
            AgentProtocolServer(service, limits=ProtocolLimits(max_pending_requests=pending_limit)),
            sys.stdin.buffer,
            sys.stdout.buffer,
        )
    result = SoakSdkChildResult(
        spec_version="harnessix.soak-sdk-child/v1",
        rss=read_peak_rss(),
        provider_request_count=provider.request_count,
    )
    (gate_root / "child-result.json").write_text(result.model_dump_json() + "\n", encoding="utf-8")


def _run_with_failure_marker(database: Path, gate_root: Path, pending_limit: int) -> None:
    """失败时只在私有夹具目录写异常类型与稳定错误码，不保留异常正文。"""

    try:
        asyncio.run(_serve(database, gate_root, pending_limit))
    except Exception as error:
        code = error.code if isinstance(error, KernelError) else "unknown"
        if re.fullmatch(r"[a-z0-9_]{1,64}", code) is None:
            code = "unknown"
        (gate_root / "child-failure.txt").write_text(
            f"{type(error).__name__}:{code}\n", encoding="ascii"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    _run_with_failure_marker(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]))
