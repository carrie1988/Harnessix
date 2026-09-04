import asyncio
import subprocess
import sys

from harnessix.domain.models import ActionStatus
from tests.processes.helpers import cleanup, ready, running

from .test_action_executor import factory, service


async def test_hard_exit_recovers_unknown_without_pid_kill_or_replay(tmp_path):
    database = tmp_path / "effects.db"
    marker = tmp_path / "owned-child"
    action_marker = tmp_path / "action-id"
    pid = None
    value = None
    try:
        worker = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "tests.processes.action_crash_worker",
                str(database),
                str(tmp_path),
                str(marker),
                str(action_marker),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert worker.returncode == 84, worker.stderr
        pid = await ready(marker)
        assert await asyncio.to_thread(running, pid)
        await asyncio.sleep(1.1)
        value = await service(tmp_path, factory(tmp_path), lease_seconds=1)
        stats = await value.journal.operational_stats()
        assert stats.unknown_count == 1
        # 重开与对账均不运行factory命令，也不按持久PID终止仍存活的进程。
        snapshot = await value.get(action_marker.read_text())
        assert snapshot.status is ActionStatus.UNKNOWN
        reconciled = await value.reconcile(snapshot.request.action_id)
        assert reconciled.status is ActionStatus.MANUAL_INTERVENTION
        assert await asyncio.to_thread(running, pid)
        assert marker.read_text() == str(pid)
    finally:
        if value is not None:
            await value.close()
        if pid is None and marker.exists():
            pid = int(marker.read_text())
        await cleanup(pid, group=True)
