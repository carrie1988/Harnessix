"""停止命令时，已退出但未回收的组长不能被当成清理失败（macOS对僵尸进程组的killpg返回EPERM）。

2026-11-10 CI macOS连续两轮：test_output_stop_threshold_closes_pipes_without_claiming_eof得到
cleanup_failed。子进程收到SIGTERM后已退出，事件循环poll之前升级到SIGKILL：macOS对只剩僵尸的
进程组，killpg探测与SIGKILL都返回EPERM，_signal_group判为失败。Linux此时killpg成功，测试照常通过。
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX进程组语义")

if os.name == "posix":
    from harnessix.processes.posix_owner import _Owner


def _zombie_leader() -> subprocess.Popen[bytes]:
    """独立进程组里已退出、尚未被wait回收的组长。"""

    read_end, write_end = os.pipe()
    process = subprocess.Popen(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        start_new_session=True,
        pass_fds=(write_end,),
    )
    os.close(write_end)
    os.killpg(process.pid, signal.SIGTERM)
    # 子进程退出时关闭继承的写端，读端随即EOF；不wait，组长保持僵尸。
    ready, _, _ = select.select([read_end], [], [], 10)
    assert ready and os.read(read_end, 1) == b"", "子进程未按时退出"
    os.close(read_end)
    time.sleep(0.2)
    return process


def _owner(process: subprocess.Popen[bytes]) -> _Owner:
    owner = object.__new__(_Owner)
    owner.request = SimpleNamespace(terminate_grace_seconds=0)
    owner.process = process
    owner.stop_reason = None
    owner.stop_started = None
    owner.kill_sent = False
    owner.io_failed = False
    return owner


def test_escalating_after_the_leader_exited_is_not_a_cleanup_failure() -> None:
    process = _zombie_leader()
    owner = _owner(process)
    owner.stop_reason = "output_limit"
    owner.stop_started = time.monotonic() - 1

    owner._escalate_if_needed()

    assert not owner.io_failed
    assert process.returncode == -signal.SIGTERM


def test_stopping_a_command_that_already_exited_is_not_a_cleanup_failure() -> None:
    process = _zombie_leader()
    owner = _owner(process)

    owner._request_stop("output_limit")

    assert owner.stop_reason == "output_limit" and not owner.io_failed
    assert process.returncode == -signal.SIGTERM
