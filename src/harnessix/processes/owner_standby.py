"""待命的POSIX Process Owner（ADR 0168 D6）：提前完成解释器启动与导入，下一条命令把Start帧交给它。

职责：启动Owner进程；至多保有一个尚未收到Start帧的待命Owner及其控制管道写端；把Start帧交给待命Owner，
没有可用的待命Owner时现场启动一个。
非职责：不持有Lease，不决定命令能否执行，也不带任何一条命令的配置：argv、cwd、环境、凭据、沙箱前缀、
期限与输出保护都只在Start帧里。待命Owner读到完整的Start帧之前不创建目标进程，所以“Lease、运行目录、
Start帧、目标”的顺序不变，提前的只是Owner进程本身。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import weakref
from collections.abc import Callable, Iterable
from concurrent.futures import Future, wait
from dataclasses import dataclass
from pathlib import Path

_OWNER_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


@dataclass(frozen=True, slots=True)
class OwnerLaunch:
    """一个Owner进程和只属于Supervisor的控制管道写端。"""

    owner: subprocess.Popen[bytes]
    control_fd: int


@dataclass(frozen=True, slots=True)
class _Standby:
    """待命Owner及其启动前测得的Owner实现摘要。"""

    launch: OwnerLaunch
    implementation: str


def spawn_posix_owner(state_root: Path, read_fd: int, *location: str) -> subprocess.Popen[bytes]:
    """启动一个POSIX Owner；location是`--run-directory`或`--runs-directory`加对应的目录。"""
    return subprocess.Popen(
        (
            sys.executable,
            "-m",
            "harnessix.processes.posix_owner",
            "--control-fd",
            str(read_fd),
            *location,
        ),
        cwd=state_root,
        env=_OWNER_ENVIRONMENT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(read_fd,),
        start_new_session=True,
    )


def spawn_standby_owner(state_root: Path, runs: Path) -> OwnerLaunch:
    """创建控制管道并启动一个等待Start帧的Owner；它的运行目录由Start帧里的Process ID决定。"""
    read_fd, write_fd = os.pipe()
    try:
        owner = spawn_posix_owner(state_root, read_fd, "--runs-directory", str(runs))
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
    return OwnerLaunch(owner, write_fd)


def _discard(launch: OwnerLaunch) -> None:
    """待命Owner没有目标进程，可以直接终止：关闭控制写端，杀死并回收它。"""
    try:
        os.close(launch.control_fd)
    except OSError:
        pass
    launch.owner.kill()
    launch.owner.wait()


def _prepared(pending: Future[_Standby]) -> _Standby | None:
    if pending.cancelled() or pending.exception() is not None:
        return None
    return pending.result()


def _discard_prepared(pending: Future[_Standby]) -> None:
    standby = _prepared(pending)
    if standby is not None:
        _discard(standby.launch)


def _discard_all(pending: Iterable[Future[_Standby]]) -> None:
    for future in wait(tuple(pending)).done:
        _discard_prepared(future)


def _release_control(pending: Future[_Standby]) -> None:
    standby = _prepared(pending)
    if standby is not None:
        try:
            os.close(standby.launch.control_fd)
        except OSError:
            pass


def _abandon(pending: list[Future[_Standby]]) -> None:
    """持有者未经aclose就被回收：关闭控制写端，待命Owner读到EOF后自行退出。"""
    for future in pending:
        future.add_done_callback(_release_control)


def _run_spawn(
    spawn: Callable[[], OwnerLaunch],
    implementation: Callable[[], str],
    pending: Future[_Standby],
) -> None:
    if not pending.set_running_or_notify_cancel():
        return
    try:
        digest = implementation()
        pending.set_result(_Standby(spawn(), digest))
    except BaseException as error:
        pending.set_exception(error)


class OwnerStandby:
    """至多一个待命Owner；任何一次不可用之后，本实例不再准备，余下的命令都现场启动。

    spawn在后台线程里启动Owner并返回它与控制写端；implementation给出启动前的Owner实现摘要，
    交接时与Supervisor冻结的能力摘要不同的待命Owner被丢弃。
    """

    def __init__(self, spawn: Callable[[], OwnerLaunch], implementation: Callable[[], str]) -> None:
        self._spawn = spawn
        self._implementation = implementation
        self._pending: list[Future[_Standby]] = []
        self._retired = False
        weakref.finalize(self, _abandon, self._pending)

    @property
    def retired(self) -> bool:
        """已关闭，或者某个待命Owner不可用过；此后prepare不再启动新的待命Owner。"""
        return self._retired

    def prepare(self) -> None:
        """没有待命Owner时在后台启动一个；不等它完成，也不占用事件循环。"""
        if self._retired or self._pending:
            return
        pending: Future[_Standby] = Future()
        self._pending.append(pending)
        threading.Thread(
            target=_run_spawn,
            args=(self._spawn, self._implementation, pending),
            name="harnessix-owner-standby",
            daemon=True,
        ).start()

    async def take(self, implementation: str) -> OwnerLaunch | None:
        """交出待命Owner；没有、启动失败、已经退出或实现摘要不符时返回None，由调用方现场启动。"""
        if not self._pending:
            return None
        # 先取走再等待：并发的两次启动不会拿到同一个Owner。
        pending = self._pending.pop()
        try:
            standby = await asyncio.wrap_future(pending)
        except asyncio.CancelledError:
            # 被取消的只是这次等待；这个待命Owner不再交给别的命令，启动完成后即回收。
            pending.add_done_callback(_discard_prepared)
            raise
        except Exception:
            self._retired = True
            return None
        if standby.implementation != implementation or standby.launch.owner.poll() is not None:
            await self.retire(standby.launch)
            return None
        return standby.launch

    async def retire(self, launch: OwnerLaunch) -> None:
        """回收一个交接前就不可用的待命Owner，本实例此后不再准备新的。"""
        self._retired = True
        await asyncio.to_thread(_discard, launch)

    async def aclose(self) -> None:
        """终止并回收尚未交出的待命Owner；正在启动的先等它启动完。"""
        self._retired = True
        pending = tuple(self._pending)
        self._pending.clear()
        if pending:
            await asyncio.to_thread(_discard_all, pending)


async def _hand_over(
    standby: OwnerStandby,
    launch: OwnerLaunch,
    write: Callable[[int, bytes], None],
    body: bytes,
) -> bool:
    """把Start帧写给待命Owner；它在交接前已退出（管道断开）时回收它并返回False。"""
    try:
        await asyncio.to_thread(write, launch.control_fd, body)
    except BrokenPipeError:
        # 帧没有写完：待命Owner读不到以换行结尾的完整Start帧，目标一定没有启动，
        # 所以改为现场启动不构成重放。
        await standby.retire(launch)
        return False
    except Exception:
        await standby.retire(launch)
        raise
    return True


async def _start_on_the_spot(
    spawn: Callable[[int], subprocess.Popen[bytes]],
    write: Callable[[int, bytes], None],
    body: bytes,
) -> OwnerLaunch:
    read_fd = write_fd = -1
    owner: subprocess.Popen[bytes] | None = None
    try:
        read_fd, write_fd = os.pipe()
        owner = await asyncio.to_thread(spawn, read_fd)
        os.close(read_fd)
        read_fd = -1
        await asyncio.to_thread(write, write_fd, body)
    except Exception:
        if owner is not None:
            owner.kill()
            await asyncio.to_thread(owner.wait)
        for descriptor in (read_fd, write_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise
    return OwnerLaunch(owner, write_fd)


async def deliver_owner_start(
    standby: OwnerStandby | None,
    implementation: str,
    spawn: Callable[[int], subprocess.Popen[bytes]],
    write: Callable[[int, bytes], None],
    body: bytes,
) -> OwnerLaunch:
    """把Start帧交给一个Owner并返回它：优先用待命Owner，没有可用的时用spawn现场启动。

    普通异常时已终止并回收本次用到的Owner、关闭本次的管道，再原样抛出。等待被取消不能证明后台
    的启动或写入已经结束，这时不做回收，由调用方按原规则保留Lease状态。
    """
    if standby is not None:
        launch = await standby.take(implementation)
        if launch is not None and await _hand_over(standby, launch, write, body):
            return launch
    return await _start_on_the_spot(spawn, write, body)
