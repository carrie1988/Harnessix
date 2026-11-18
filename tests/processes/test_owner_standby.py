"""待命Owner（ADR 0168 D6）：第二条命令起把Start帧交给已启动的Owner，一个待命Owner只用一次。

任何异常（启动失败、交接前退出、实现摘要不符、交接时管道断开）都退回现场启动，且此后不再准备待命Owner；
命令的配置只在Start帧里，Lease、回执与取消的规则与现场启动相同。
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.processes import owner_standby, posix_owner
from harnessix.processes import supervisor as process_module
from harnessix.processes.owner_protocol import ProcessOwnerStart, protected_owner_start
from harnessix.processes.owner_standby import (
    OwnerLaunch,
    OwnerStandby,
    deliver_owner_start,
    spawn_standby_owner,
)
from harnessix.processes.supervision_contracts import ProcessSpec
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervisor import PosixProcessSupervisor, SupervisedProcess
from tests.processes.helpers import owner_imports_this_checkout
from tests.processes.test_supervisor import _plan

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX Process owner语义")

_WAIT_SECONDS = 20


@pytest.fixture(autouse=True)
def _owners_import_this_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    owner_imports_this_checkout(monkeypatch)


def _python(code: str, **options: object) -> ProcessSpec:
    return build_process_spec(
        invocation="argv",
        argv=(sys.executable, "-I", "-c", code),
        output_bytes=4096,
        **options,  # type: ignore[arg-type]
    )


def _echo(text: str) -> ProcessSpec:
    return _python(f"print({text!r})")


async def _run(supervisor: PosixProcessSupervisor, workspace: Path, text: str) -> bytes:
    spec = _echo(text)
    plan = _plan(workspace, spec, supervisor)
    lease = await supervisor.run(
        plan, spec, supervisor.capability, workspace=workspace, environment={}
    )
    assert (lease.state, lease.returncode, lease.stop_reason) == ("exited", 0, "exited")
    return await supervisor.output(spec.process_id, "stdout")


def _count_spawns_on_the_spot(
    supervisor: PosixProcessSupervisor, monkeypatch: pytest.MonkeyPatch
) -> list[int]:
    started: list[int] = []
    original = supervisor._spawn_owner

    def counted(read_fd: int, run_directory: Path) -> subprocess.Popen[bytes]:
        owner = original(read_fd, run_directory)
        started.append(owner.pid)
        return owner

    monkeypatch.setattr(supervisor, "_spawn_owner", counted)
    return started


def _standby_of(supervisor: PosixProcessSupervisor) -> OwnerStandby:
    assert supervisor._standby is not None
    return supervisor._standby


async def _prepared(supervisor: PosixProcessSupervisor) -> OwnerLaunch:
    (pending,) = _standby_of(supervisor)._pending
    return (await asyncio.wrap_future(pending)).launch


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


async def _eventually(condition: Callable[[], bool]) -> None:
    for _ in range(_WAIT_SECONDS * 50):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("条件未在时限内成立")


async def test_only_the_first_command_starts_an_owner_on_the_spot(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state", standby_owner=True) as supervisor:
        on_the_spot = _count_spawns_on_the_spot(supervisor, monkeypatch)
        # 构造时不启动任何进程；第一条命令交出Start帧之后才准备下一个。
        assert not _standby_of(supervisor)._pending

        assert await _run(supervisor, workspace, "one") == b"one\n"
        first = await _prepared(supervisor)
        assert await _run(supervisor, workspace, "two") == b"two\n"
        second = await _prepared(supervisor)
        assert await _run(supervisor, workspace, "three") == b"three\n"
        last = await _prepared(supervisor)

        # 每条命令一个Owner，用过的待命Owner随命令结束而退出；任何时刻至多一个待命Owner。
        assert len(on_the_spot) == 1
        assert len({on_the_spot[0], first.owner.pid, second.owner.pid, last.owner.pid}) == 4
        assert first.owner.returncode == 0 and second.owner.returncode == 0
        assert last.owner.poll() is None and len(_standby_of(supervisor)._pending) == 1
        assert not _standby_of(supervisor).retired
    # 关闭Supervisor终止并回收尚未用到的待命Owner；它没有留下运行目录。
    assert last.owner.returncode is not None
    assert len(list((tmp_path / "state" / "runs").iterdir())) == 3


async def test_each_command_gets_only_its_own_configuration_from_a_standby_owner(
    tmp_path, monkeypatch
) -> None:
    first_workspace, second_workspace = tmp_path / "first", tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    report = (
        "import os; print([k for k in ('FIRST_ONLY', 'SECOND_ONLY') if k in os.environ]); "
        "print(os.getcwd())"
    )
    async with PosixProcessSupervisor(tmp_path / "state", standby_owner=True) as supervisor:
        on_the_spot = _count_spawns_on_the_spot(supervisor, monkeypatch)
        first = _python(report)
        first_lease = await supervisor.run(
            _plan(first_workspace, first, supervisor, environment={"FIRST_ONLY": "1"}),
            first,
            supervisor.capability,
            workspace=first_workspace,
            environment={"FIRST_ONLY": "1"},
        )
        standby = await _prepared(supervisor)
        # 第二条命令换了工作区、环境、终端模式与期限；这些只在它自己的Start帧里。
        second = _python(report, terminal="pty", timeout_seconds=7)
        second_lease = await supervisor.run(
            _plan(second_workspace, second, supervisor, environment={"SECOND_ONLY": "2"}),
            second,
            supervisor.capability,
            workspace=second_workspace,
            environment={"SECOND_ONLY": "2"},
        )
        first_output = await supervisor.output(first.process_id, "stdout")
        second_output = await supervisor.output(second.process_id, "stdout")

    assert len(on_the_spot) == 1 and standby.owner.returncode == 0
    assert first_output.decode().splitlines() == [
        "['FIRST_ONLY']",
        str(first_workspace.resolve()),
    ]
    assert second_output.decode().replace("\r\n", "\n").splitlines() == [
        "['SECOND_ONLY']",
        str(second_workspace.resolve()),
    ]
    assert (first_lease.state, second_lease.state) == ("exited", "exited")
    # Owner身份与令牌按命令生成，待命Owner不带任何一条命令的身份。
    assert first_lease.owner_identity != second_lease.owner_identity
    assert first_lease.owner_token != second_lease.owner_token
    # 期限也来自各自的Start帧：默认300秒对7秒。
    assert first_lease.deadline - second_lease.deadline > timedelta(seconds=250)


async def test_a_standby_owner_that_died_before_use_is_never_used_again(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state", standby_owner=True) as supervisor:
        on_the_spot = _count_spawns_on_the_spot(supervisor, monkeypatch)
        await _run(supervisor, workspace, "one")
        standby = await _prepared(supervisor)
        standby.owner.send_signal(signal.SIGKILL)
        await asyncio.to_thread(standby.owner.wait, _WAIT_SECONDS)

        assert await _run(supervisor, workspace, "two") == b"two\n"
        assert await _run(supervisor, workspace, "three") == b"three\n"
        # 交接前已退出：本条现场启动，之后也一直现场启动，恢复为原来的行为。
        assert len(on_the_spot) == 3
        assert _standby_of(supervisor).retired and not _standby_of(supervisor)._pending


async def test_a_standby_owner_for_another_owner_implementation_is_discarded(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state", standby_owner=True) as supervisor:
        on_the_spot = _count_spawns_on_the_spot(supervisor, monkeypatch)
        with monkeypatch.context() as patch:
            # 待命Owner启动时磁盘上的Owner实现与Supervisor冻结的能力不同（例如升级到一半）。
            patch.setattr(supervisor, "_probe_capability", lambda: SimpleNamespace(digest="0" * 64))
            _standby_of(supervisor).prepare()
            stale = await _prepared(supervisor)
        assert stale.owner.poll() is None

        assert await _run(supervisor, workspace, "one") == b"one\n"
        assert len(on_the_spot) == 1 and stale.owner.returncode is not None
        assert _standby_of(supervisor).retired


async def test_a_failed_standby_spawn_falls_back_and_stops_preparing(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def no_more_processes(*_args: object) -> OwnerLaunch:
        raise BlockingIOError("fork-canary")

    monkeypatch.setattr(process_module, "spawn_standby_owner", no_more_processes)
    async with PosixProcessSupervisor(tmp_path / "state", standby_owner=True) as supervisor:
        on_the_spot = _count_spawns_on_the_spot(supervisor, monkeypatch)
        assert await _run(supervisor, workspace, "one") == b"one\n"
        assert await _run(supervisor, workspace, "two") == b"two\n"
        assert await _run(supervisor, workspace, "three") == b"three\n"
    assert len(on_the_spot) == 3 and _standby_of(supervisor).retired


async def test_concurrent_starts_never_share_one_standby_owner(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state", standby_owner=True) as supervisor:
        on_the_spot = _count_spawns_on_the_spot(supervisor, monkeypatch)
        await _run(supervisor, workspace, "warm")
        standby = await _prepared(supervisor)
        outputs = await asyncio.gather(*(_run(supervisor, workspace, text) for text in "ab"))
    assert outputs == [b"a\n", b"b\n"]
    # 两条并发命令只有一条拿到待命Owner，另一条现场启动。
    assert len(on_the_spot) == 2 and standby.owner.returncode == 0


async def test_cancelling_a_command_on_a_standby_owner_stops_its_process_tree(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "child.pid"
    code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)']); "
        "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(30)"
    )
    async with PosixProcessSupervisor(
        tmp_path / "state", standby_owner=True, terminate_grace_seconds=0.2
    ) as supervisor:
        on_the_spot = _count_spawns_on_the_spot(supervisor, monkeypatch)
        await _run(supervisor, workspace, "warm")
        standby = await _prepared(supervisor)
        spec = build_process_spec(
            invocation="argv", argv=(sys.executable, "-I", "-c", code, str(marker))
        )
        token = CancelToken()
        handle = await supervisor.start(
            _plan(workspace, spec, supervisor),
            spec,
            supervisor.capability,
            workspace=workspace,
            environment={},
        )
        await _eventually(lambda: marker.exists() and bool(marker.read_text()))
        token.cancel()
        lease = await handle.wait(token)
    assert len(on_the_spot) == 1 and standby.owner.returncode == 0
    assert (lease.state, lease.stop_reason) == ("exited", "cancelled")
    await _eventually(lambda: _gone(int(marker.read_text())))


async def test_closing_the_supervisor_stops_a_command_on_a_standby_owner_and_the_next_standby(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor = PosixProcessSupervisor(
        tmp_path / "state", standby_owner=True, terminate_grace_seconds=0.2
    )
    on_the_spot = _count_spawns_on_the_spot(supervisor, monkeypatch)
    await _run(supervisor, workspace, "warm")
    used = await _prepared(supervisor)
    spec = _python("import time; time.sleep(30)")
    handle = await supervisor.start(
        _plan(workspace, spec, supervisor),
        spec,
        supervisor.capability,
        workspace=workspace,
        environment={},
    )
    following = await _prepared(supervisor)
    await supervisor.aclose()

    assert len(on_the_spot) == 1
    assert (handle.lease.state, handle.lease.stop_reason) == ("exited", "closed")
    assert used.owner.returncode == 0 and following.owner.returncode is not None
    assert _standby_of(supervisor).retired and not _standby_of(supervisor)._pending
    _standby_of(supervisor).prepare()
    assert not _standby_of(supervisor)._pending


async def test_handoff_failure_other_than_a_gone_owner_settles_as_launch_failed(
    tmp_path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    async with PosixProcessSupervisor(tmp_path / "state", standby_owner=True) as supervisor:
        on_the_spot = _count_spawns_on_the_spot(supervisor, monkeypatch)
        await _run(supervisor, workspace, "one")
        standby = await _prepared(supervisor)

        def failing_write(_descriptor: int, _body: bytes) -> None:
            raise RuntimeError("write-canary")

        spec = _echo("two")
        plan = _plan(workspace, spec, supervisor)
        with monkeypatch.context() as patch:
            patch.setattr(SupervisedProcess, "_write_all", staticmethod(failing_write))
            with pytest.raises(KernelError) as caught:
                await supervisor.start(
                    plan, spec, supervisor.capability, workspace=workspace, environment={}
                )
        # 与现场启动同一条规则：普通异常结算为launch_failed，用到的Owner已终止并回收，不重放。
        assert caught.value.code == "process_launch_failed" and len(on_the_spot) == 1
        lease = supervisor.status(spec.process_id)
        assert (lease.state, lease.sequence, lease.stop_reason) == ("failed", 2, "launch_failed")
        assert standby.owner.returncode is not None and _standby_of(supervisor).retired
        with pytest.raises(KernelError) as duplicate:
            await supervisor.start(
                plan, spec, supervisor.capability, workspace=workspace, environment={}
            )
        assert duplicate.value.code == "process_already_exists"


class _GoneOwner:
    """交接前一刻退出的待命Owner：存活检查已通过，控制管道的读端却已关闭。"""

    def __init__(self) -> None:
        self.killed = self.waited = False

    def poll(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        self.waited = True
        return 2


async def test_broken_pipe_at_handoff_falls_back_without_sending_the_frame_twice() -> None:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    gone = _GoneOwner()
    standby = OwnerStandby(lambda: OwnerLaunch(gone, write_fd), lambda: "digest")  # type: ignore[arg-type]
    standby.prepare()
    delivered: list[bytes] = []
    spawned: list[int] = []

    def spawn(owner_read_fd: int) -> subprocess.Popen[bytes]:
        spawned.append(owner_read_fd)
        return subprocess.Popen(
            (sys.executable, "-I", "-c", "import os,sys; os.read(int(sys.argv[1]), 65536)")
            + (str(owner_read_fd),),
            pass_fds=(owner_read_fd,),
        )

    def write(descriptor: int, body: bytes) -> None:
        SupervisedProcess._write_all(descriptor, body)
        delivered.append(body)

    launch = await deliver_owner_start(standby, "digest", spawn, write, b"start-frame\n")
    try:
        # 写给待命Owner失败时它一个字节也没有收到；帧只完整交给了现场启动的Owner一次。
        assert delivered == [b"start-frame\n"] and len(spawned) == 1
        assert gone.killed and gone.waited and launch.owner is not gone
        assert standby.retired
    finally:
        os.close(launch.control_fd)
        await asyncio.to_thread(launch.owner.wait, _WAIT_SECONDS)


async def test_standby_owner_exits_by_itself_when_the_host_side_of_the_pipe_is_gone(
    tmp_path,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir(mode=0o700)
    launch = await asyncio.to_thread(spawn_standby_owner, tmp_path, runs)
    assert launch.owner.poll() is None
    os.close(launch.control_fd)
    # 控制管道读到EOF即宿主已不在；待命Owner没有目标进程，以启动失败的退出码结束。
    assert await asyncio.to_thread(launch.owner.wait, _WAIT_SECONDS) == 2
    assert not list(runs.iterdir())


_CRASHING_HOST = """
import asyncio, sys
from pathlib import Path
from harnessix.processes import owner_standby
from harnessix.processes.supervisor import PosixProcessSupervisor

owner_standby._OWNER_ENVIRONMENT = {**owner_standby._OWNER_ENVIRONMENT, "PYTHONPATH": sys.argv[2]}

async def main():
    supervisor = PosixProcessSupervisor(Path(sys.argv[1]), standby_owner=True)
    supervisor._standby.prepare()
    (pending,) = supervisor._standby._pending
    launch = (await asyncio.wrap_future(pending)).launch
    print(launch.owner.pid, flush=True)
    await asyncio.sleep(60)

asyncio.run(main())
"""


async def test_a_standby_owner_does_not_outlive_a_host_killed_without_cleanup(tmp_path) -> None:
    source = owner_standby._OWNER_ENVIRONMENT["PYTHONPATH"]
    host = await asyncio.to_thread(
        subprocess.Popen,
        (sys.executable, "-c", _CRASHING_HOST, str(tmp_path / "state"), source),
        env={**os.environ, "PYTHONPATH": source},
        stdout=subprocess.PIPE,
    )
    try:
        assert host.stdout is not None
        line = await asyncio.wait_for(asyncio.to_thread(host.stdout.readline), _WAIT_SECONDS)
        standby_pid = int(line)
        assert not _gone(standby_pid)
    finally:
        host.kill()
        host.wait()
    # 宿主被强杀时内核关闭控制写端；待命Owner读到EOF后自行退出，不会留下等待中的Owner。
    await _eventually(lambda: _gone(standby_pid))


async def test_a_cancelled_take_discards_the_standby_owner_instead_of_reusing_it(tmp_path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir(mode=0o700)
    entered, release = threading.Event(), threading.Event()
    spawned: list[OwnerLaunch] = []

    def slow_spawn() -> OwnerLaunch:
        entered.set()
        assert release.wait(_WAIT_SECONDS)
        spawned.append(spawn_standby_owner(tmp_path, runs))
        return spawned[-1]

    standby = OwnerStandby(slow_spawn, lambda: "digest")
    standby.prepare()
    standby.prepare()  # 已有一个在启动：不重复启动
    assert len(standby._pending) == 1
    assert await asyncio.to_thread(entered.wait, _WAIT_SECONDS)
    waiting = asyncio.create_task(standby.take("digest"))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()

    await _eventually(lambda: bool(spawned) and spawned[0].owner.poll() is not None)
    # 被取消的等待没有让它退役：之后照常准备新的待命Owner。
    assert await standby.take("digest") is None and not standby.retired
    standby.prepare()
    launch = await standby.take("digest")
    assert launch is not None and launch.owner.poll() is None and len(spawned) == 2
    await standby.retire(launch)
    assert launch.owner.returncode is not None


async def test_closing_waits_for_a_standby_owner_still_starting(tmp_path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir(mode=0o700)
    release = threading.Event()
    spawned: list[OwnerLaunch] = []

    def slow_spawn() -> OwnerLaunch:
        assert release.wait(_WAIT_SECONDS)
        spawned.append(spawn_standby_owner(tmp_path, runs))
        return spawned[-1]

    standby = OwnerStandby(slow_spawn, lambda: "digest")
    standby.prepare()
    closing = asyncio.create_task(standby.aclose())
    await asyncio.sleep(0.05)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, _WAIT_SECONDS)
    assert spawned[0].owner.returncode is not None and standby.retired


async def test_an_unclosed_holder_releases_its_standby_owner(tmp_path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir(mode=0o700)
    holder = OwnerStandby(lambda: spawn_standby_owner(tmp_path, runs), lambda: "digest")
    holder.prepare()
    launch = (await asyncio.wrap_future(holder._pending[0])).launch
    # 持有者未关闭就被回收：控制写端随之关闭，待命Owner自行退出，不随宿主存活到最后。
    del holder
    assert await asyncio.to_thread(launch.owner.wait, _WAIT_SECONDS) == 2


def _start_frame(cwd: Path) -> tuple[bytes, str]:
    request = ProcessOwnerStart(
        process_id=uuid4(),
        owner_identity="a" * 64,
        owner_token="b" * 64,
        argv=("/bin/echo", "never"),
        cwd=str(cwd),
        environment={},
        terminal="pipe",
        stdin="closed",
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        output_bytes=4096,
        input_bytes=0,
        columns=80,
        rows=24,
    )
    frame = protected_owner_start(request, None).model_dump_json(warnings="error")
    return frame.encode() + b"\n", str(request.process_id)


@pytest.mark.parametrize("problem", ["missing", "symlink", "group-readable"])
def test_standby_owner_checks_the_run_directory_before_creating_anything(
    tmp_path, monkeypatch, problem: str
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir(mode=0o700)
    frame, process_id = _start_frame(tmp_path)
    directory = runs / process_id
    if problem == "symlink":
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        directory.symlink_to(tmp_path / "elsewhere")
    elif problem == "group-readable":
        directory.mkdir(mode=0o750)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("运行目录未通过检查时不得创建Owner或目标进程")

    monkeypatch.setattr(posix_owner, "_Owner", forbidden)
    read_fd, write_fd = os.pipe()
    try:
        # 必须恰好给出一种定位方式；不满足时不读取Start帧。
        assert posix_owner._run(read_fd, None, None) == 2
        assert posix_owner._run(read_fd, str(directory), str(runs)) == 2
        os.write(write_fd, frame)
        assert posix_owner._run(read_fd, None, str(runs)) == 2
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert not list(tmp_path.glob("elsewhere/*"))


def test_standby_owner_runs_the_command_in_the_directory_named_by_the_start_frame(
    tmp_path,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir(mode=0o700)
    frame, process_id = _start_frame(tmp_path)
    (runs / process_id).mkdir(mode=0o700)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, frame)
    try:
        assert posix_owner._run(read_fd, None, str(runs)) == 0
    finally:
        os.close(write_fd)
    assert (runs / process_id / "stdout.bin").read_bytes() == b"never\n"
    assert (runs / process_id / "receipt.json").is_file()
