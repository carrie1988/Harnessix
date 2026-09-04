import asyncio
import subprocess
import sys

from tests.processes.helpers import cleanup, ready, running


async def test_host_hard_exit_does_not_falsely_claim_process_group_containment(tmp_path):
    marker = tmp_path / "owned-child"
    pid = None
    try:
        child = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "tests.processes.crash_worker", str(marker)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert child.returncode == 84, child.stderr
        pid = await ready(marker)
        # 这是已知能力缺口的真退出验证，不是已实现跨重启恢复的验收。
        assert await asyncio.to_thread(running, pid)
    finally:
        if pid is None and marker.exists():
            pid = int(marker.read_text())
        await cleanup(pid, group=True)
