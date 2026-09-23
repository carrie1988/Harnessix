"""完整产品重启Soak子进程：真实组合根与私有硬退出门闩。"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harnessix.agent.errors import KernelError
from harnessix.product_config.server import run_product_stdio
from scripts.soak_restart_proof import SoakRestartChildResult
from scripts.soak_rss import read_peak_rss


def _write_private(path: Path, body: bytes) -> None:
    """私有夹具文件只写一次并刷盘；不输出状态路径或异常正文。"""

    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if os.write(descriptor, body) != len(body):
            raise OSError("夹具文件写入不完整")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def _crash_gate(gate: Path) -> None:
    """门闩仅属于Soak子进程，绝不进入生产Agent Protocol。"""

    while not (gate / "crash.request").is_file():  # noqa: ASYNC110, ASYNC240
        await asyncio.sleep(0.005)
    _write_private(gate / "crash.ack", b"ACK\n")
    os._exit(97)


async def _serve(config: Path, workspace: Path, state: Path, gate: Path) -> None:
    # 只允许离线假凭据；用户环境中的同名变量即使存在也会被覆盖。
    os.environ["HARNESSIX_SOAK_KEY"] = "soak-fixture-not-a-real-key"
    watcher = asyncio.create_task(_crash_gate(gate), name="harnessix-soak-restart-gate")
    try:
        await run_product_stdio(
            config_path=config,
            profile_id=None,
            workspace=workspace,
            state_directory=state,
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout.buffer,
        )
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    result = SoakRestartChildResult(
        spec_version="harnessix.soak-restart-child/v1",
        rss=read_peak_rss(),
    )
    _write_private(gate / "child-result.json", (result.model_dump_json() + "\n").encode())


def main(argv: list[str]) -> None:
    """传入配置、Workspace、持久State和私有门闩目录。"""

    if len(argv) != 4:
        raise SystemExit(2)
    config, workspace, state, gate = (Path(value) for value in argv)
    try:
        asyncio.run(_serve(config, workspace, state, gate))
    except Exception as error:
        code = error.code if isinstance(error, KernelError) else "unknown"
        if re.fullmatch(r"[a-z0-9_]{1,64}", code) is None:
            code = "unknown"
        _write_private(gate / "child-failure.txt", f"{type(error).__name__}:{code}\n".encode())
        raise SystemExit(1) from None


if __name__ == "__main__":
    main(sys.argv[1:])
