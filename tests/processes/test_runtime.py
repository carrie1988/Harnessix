import asyncio
import hashlib
import json
import os
import signal
import sys

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.processes.contracts import ProcessLimits, ProcessRequest, ProcessResult
from tests.processes.helpers import cleanup, ready, request, runtime, stopped


@pytest.mark.parametrize("code", [0, 7, 127])
async def test_exit_stdin_eof_and_independent_binary_streams(tmp_path, code):
    async with runtime(tmp_path) as host:
        result = await host.run(
            request(
                "import os,sys; assert sys.stdin.buffer.read()==b''; "
                "os.write(1,bytes(range(256))); os.write(2,'中文'.encode()); "
                "sys.exit(int(sys.argv[1]))",
                str(code),
            ),
            CancelToken(),
        )
        assert result.stop_reason == "exited" and result.returncode == code
        assert result.stdout.data() == bytes(range(256)) and result.stderr.text() == "中文"
        assert result.stdout.eof and result.stderr.eof
        assert not result.stdout.truncated
        assert ProcessResult.model_validate_json(result.model_dump_json()) == result
        with pytest.raises(UnicodeDecodeError):
            result.stdout.text()
        with pytest.raises(ChildProcessError):
            await asyncio.to_thread(os.waitpid, result.pid, os.WNOHANG)


@pytest.mark.parametrize("capture", [0, 1, 1024, 24576])
async def test_large_dual_stream_drained_without_unbounded_capture(tmp_path, capture):
    size = 2 * 1024 * 1024
    async with runtime(
        tmp_path, limits=ProcessLimits(stdout_bytes=capture, stderr_bytes=capture)
    ) as host:
        result = await host.run(
            request(
                "import os,threading; n=int(__import__('sys').argv[1]); "
                "t=threading.Thread(target=lambda:os.write(1,b'a'*n)); t.start(); "
                "os.write(2,b'b'*n); t.join()",
                str(size),
            ),
            CancelToken(),
        )
    assert result.returncode == 0 and result.stop_reason == "exited"
    for stream, byte in ((result.stdout, b"a"), (result.stderr, b"b")):
        assert stream.data() == byte * capture and stream.truncated and stream.eof
        assert stream.observed_bytes == size
        assert stream.observed_sha256 == hashlib.sha256(byte * size).hexdigest()


async def test_output_stop_threshold_closes_pipes_without_claiming_eof(tmp_path):
    async with runtime(
        tmp_path, limits=ProcessLimits(stop_output_bytes=32768, stdout_bytes=128)
    ) as host:
        result = await host.run(
            request("import os\nwhile True: os.write(1,b'x'*8192)"), CancelToken()
        )
    assert result.stop_reason == "output_limit"
    assert result.stdout.captured_bytes == 128 and result.stdout.observed_bytes >= 32768
    assert not result.stdout.eof and result.stdout.truncated
    assert result.termination in {"none", "term", "kill"}
    await stopped(result.pid)


@pytest.mark.parametrize("size", [2, 3, 4])
async def test_utf8_prefix_is_not_silently_replaced(tmp_path, size):
    async with runtime(tmp_path, limits=ProcessLimits(stdout_bytes=size)) as host:
        result = await host.run(
            request(
                "import os,time; b='中X'.encode(); os.write(1,b[:1]); "
                "time.sleep(.02); os.write(1,b[1:])"
            ),
            CancelToken(),
        )
    assert result.stdout.data() == "中X".encode()[:size]
    if size == 2:
        with pytest.raises(UnicodeDecodeError):
            result.stdout.text()
    else:
        assert result.stdout.text() == ("中" if size == 3 else "中X")


async def test_argv_is_not_shell_and_environment_is_not_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESSIX_PROCESS_SECRET_CANARY", "must-not-reach-child")
    monkeypatch.setenv("PYTHONPATH", "untrusted-fixture-path")
    payload = "$(touch injected); * | cat > injected"
    async with runtime(tmp_path, environment={"NO_COLOR": "1"}) as host:
        result = await host.run(
            request(
                "import os,sys,json; print(json.dumps([os.getcwd(),sys.argv[1],dict(os.environ)]))",
                payload,
            ),
            CancelToken(),
        )
    cwd, arg, env = json.loads(result.stdout.text())
    assert cwd == str(tmp_path.resolve()) and arg == payload
    assert set(env) <= {"NO_COLOR", "LC_CTYPE"} and env["NO_COLOR"] == "1"
    assert not (tmp_path / "injected").exists()
    assert "must-not-reach-child" not in result.model_dump_json()


async def test_extra_inheritable_fd_is_closed(tmp_path):
    path = tmp_path / "private"
    path.write_text("private-canary")
    with path.open() as handle:
        fd = handle.fileno()
        os.set_inheritable(fd, True)
        async with runtime(tmp_path) as host:
            result = await host.run(
                request(
                    "import os,sys\ntry: os.fstat(int(sys.argv[1]))\n"
                    "except OSError: print('closed')\nelse: raise AssertionError('fd inherited')",
                    str(fd),
                ),
                CancelToken(),
            )
    assert result.returncode == 0 and result.stdout.text().strip() == "closed"


@pytest.mark.parametrize("ignore", [False, True])
async def test_timeout_escalates_and_reaps(tmp_path, ignore):
    async with runtime(tmp_path, limits=ProcessLimits(terminate_grace_seconds=0.05)) as host:
        result = await host.run(
            request(
                "import signal,time; "
                + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore else "")
                + "time.sleep(10)",
                timeout=0.2,
            ),
            CancelToken(),
        )
    assert result.stop_reason == "timeout" and result.returncode == -(
        signal.SIGKILL if ignore else signal.SIGTERM
    )
    assert result.termination == ("kill" if ignore else "term")
    assert result.elapsed_seconds < 3
    await stopped(result.pid)


@pytest.mark.parametrize("how", ["token", "task", "repeated_task", "close", "timeout"])
async def test_active_cancellation_drains_before_return(tmp_path, how):
    marker = tmp_path / "started"
    token = CancelToken()
    host = runtime(tmp_path, limits=ProcessLimits(terminate_grace_seconds=0.05))
    job = request(
        "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(10)",
        str(marker),
    )
    task = asyncio.create_task(host.run(job, token))
    pid = None
    try:
        pid = await ready(marker)
        if how == "token":
            token.cancel()
            result = await task
            assert result.stop_reason == "cancelled"
        elif how == "close":
            await host.aclose()
            assert (await task).stop_reason == "closed"
            await host.aclose()
        elif how == "timeout":
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, 0.01)
        else:
            task.cancel()
            if how == "repeated_task":
                await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.01)
                task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await stopped(pid)
        with pytest.raises(ChildProcessError):
            await asyncio.to_thread(os.waitpid, pid, os.WNOHANG)
    finally:
        await host.aclose()
        await asyncio.gather(task, return_exceptions=True)
        await cleanup(pid)


async def test_precancel_busy_and_reuse_are_explicit(tmp_path):
    token = CancelToken()
    token.cancel()
    marker = tmp_path / "started"
    async with runtime(tmp_path) as host:
        with pytest.raises(TurnCancelled):
            await host.run(request("raise AssertionError('must not run')"), token)
        task = asyncio.create_task(
            host.run(
                request(
                    "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); "
                    "time.sleep(5)",
                    str(marker),
                ),
                CancelToken(),
            )
        )
        try:
            await ready(marker)
            with pytest.raises(KernelError) as error:
                await host.run(request("raise AssertionError('busy')"), CancelToken())
            assert error.value.code == "process_busy"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert (
            await host.run(request("print('reused')"), CancelToken())
        ).stdout.text() == "reused\n"
    with pytest.raises(KernelError) as error:
        await host.run(request("pass"), CancelToken())
    assert error.value.code == "process_closed"


@pytest.mark.parametrize("change", ["cwd", "executable"])
async def test_binding_change_rejected_before_spawn(tmp_path, change):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    executable = tmp_path / "runner"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    from harnessix.processes.runtime import HostProcessRuntime

    async with HostProcessRuntime(cwd, {"fixture": executable}) as host:
        if change == "cwd":
            cwd.rename(tmp_path / "old")
            cwd.mkdir()
        else:
            executable.write_text("#!/bin/sh\nexit 9\n")
        with pytest.raises(KernelError) as error:
            await host.run(ProcessRequest(program="fixture"), CancelToken())
        assert error.value.code == "process_binding_changed"


async def test_spawn_error_is_sanitized_and_does_not_poison_next_run(tmp_path):
    from harnessix.processes.runtime import HostProcessRuntime

    bad = tmp_path / "invalid-executable-canary"
    bad.write_bytes(b"no executable header")
    bad.chmod(0o700)
    async with HostProcessRuntime(tmp_path, {"bad": bad, "python": sys.executable}) as host:
        with pytest.raises(KernelError) as error:
            await host.run(ProcessRequest(program="bad"), CancelToken())
        assert error.value.code == "process_launch_failed" and "canary" not in str(error.value)
        assert (await host.run(request("pass"), CancelToken())).returncode == 0
