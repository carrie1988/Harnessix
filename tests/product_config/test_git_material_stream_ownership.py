"""真实 io.open 审计下的合成快照资源回归；不代表 Owner、SDK 或 Windows 证明。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.delivery.git_material_input_contracts import encode_manifest
from harnessix.product_config.git_material_process import stage_material
from tests.product_config import test_git_material_input as input_tests

make_process = input_tests.make_process

pytestmark = pytest.mark.skipif(os.name != "posix", reason="需要真实 POSIX FD 和匿名快照")

# 基础解释器隔离审计钩子；导入仅绑定当前仓库 src，不加载其他安装版本。
_SNAPSHOT_PROGRAM = r"""
import errno
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from harnessix.delivery.git_material_input_contracts import decode_manifest
from harnessix.delivery.git_material_native import _Resources, _snapshot


def closed(descriptor):
    try:
        os.fstat(descriptor)
    except OSError as error:
        return error.errno == errno.EBADF
    return False


def main():
    request = decode_manifest(sys.stdin.buffer.read(65537))
    root = Path(request.stage_root)
    failure = sys.argv[2]
    body = Path(request.body_path).read_bytes() if failure == "none" else None
    captured = []
    process_attempts = 0
    injected = {
        "PermissionError": PermissionError("synthetic-io-open"),
        "KeyboardInterrupt": KeyboardInterrupt("synthetic-io-open"),
        "none": None,
    }[failure]

    def audit(event, arguments):
        nonlocal process_attempts
        if event in {
            "subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty"
        } or event.startswith("os.exec"):
            process_attempts += 1
        if event == "open" and type(arguments[0]) is int:
            captured.append(arguments[0])
            if injected is not None:
                raise injected

    sys.addaudithook(audit)
    before = len(os.listdir("/dev/fd"))
    if injected is not None:
        preserved = True
        all_closed = True
        remaining = 0
        iterations = 6
        for _ in range(iterations):
            start = len(captured)
            try:
                with _Resources() as resources:
                    _snapshot(request, resources, None)
            except BaseException as error:
                preserved = preserved and error is injected
            else:
                preserved = False
            iteration_closed = len(captured) == start + 1 and all(
                closed(fd) for fd in captured[start:]
            )
            all_closed = all_closed and iteration_closed
            remaining += len(tuple(root.glob("snapshot-*.bin")))
        return {
            "iterations": iterations,
            "audit_open_count": len(captured),
            "exceptions_preserved": preserved,
            "captured_fds_closed": all_closed,
            "snapshot_names_remaining": remaining,
            "process_attempts": process_attempts,
            "fd_growth": len(os.listdir("/dev/fd")) - before,
        }

    with _Resources() as resources:
        stream = _snapshot(request, resources, None)
        descriptor = stream.fileno()
        offset_zero = stream.tell() == 0 and os.lseek(descriptor, 0, os.SEEK_CUR) == 0
        complete = stream.read() == body and stream.read() == b""
        try:
            os.write(descriptor, b"x")
        except OSError as error:
            readonly = error.errno == errno.EBADF
        else:
            readonly = False
        remaining = len(tuple(root.glob("snapshot-*.bin")))
        stream.close()
        try:
            os.fstat(descriptor)
        except OSError:
            borrowed_fd_open = False
        else:
            borrowed_fd_open = True
    return {
        "audit_open_count": len(captured),
        "stream_fd_audited": captured == [descriptor],
        "offset_zero": offset_zero,
        "complete_bytes": complete,
        "readonly_write_rejected": readonly,
        "stream_closed": stream.closed,
        "fd_survives_stream_close": borrowed_fd_open,
        "captured_fds_closed": closed(descriptor),
        "snapshot_names_remaining": remaining + len(tuple(root.glob("snapshot-*.bin"))),
        "process_attempts": process_attempts,
        "fd_growth": len(os.listdir("/dev/fd")) - before,
    }


try:
    result = main()
except BaseException as error:
    # 不输出第三方错误正文、文件路径、材料或环境。
    print(json.dumps({"child_error": type(error).__name__}, sort_keys=True))
    sys.exit(1)
print(json.dumps(result, sort_keys=True))
"""


def _observe_snapshot(prepared, failure):
    executable = Path(sys._base_executable).resolve(strict=True)
    source = Path(__file__).resolve().parents[2] / "src"
    with subprocess.Popen(
        (str(executable), "-I", "-B", "-c", _SNAPSHOT_PROGRAM, str(source), failure),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        try:
            stdout, stderr = child.communicate(encode_manifest(prepared.write.request), timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise AssertionError("快照审计子进程超时") from None
    assert child.returncode == 0, "快照审计子进程异常退出"
    assert not stderr and len(stdout) <= 2048, "子进程输出必须仅含有限标量"
    result = json.loads(stdout)
    assert type(result) is dict and all(type(value) in {bool, int} for value in result.values())
    print(json.dumps(result, sort_keys=True))
    return result


@pytest.fixture
def staged_snapshot(make_process, tmp_path, request):
    object_format = request.param
    case = make_process()
    binding = input_tests._repository(case, tmp_path, object_format)
    material = input_tests._material(b"\x00\xff\x80snapshot\r\n\x00", object_format, "blob")
    prepared = input_tests._prepare(case, binding, material)
    input_tests.process_tests._assert_not_started(case)
    staged = stage_material(prepared.write, CancelToken(), prepared.budget)
    try:
        yield prepared
    finally:
        staged.remove()
        assert not tuple(Path(prepared.write.request.stage_root).glob("snapshot-*.bin"))
        assert not (case.state / "execution-plans.db").exists()
        assert not (case.state / "process-owner").exists()


@pytest.mark.parametrize("staged_snapshot", ["sha1"], indirect=True)
@pytest.mark.parametrize("failure", ["PermissionError", "KeyboardInterrupt"])
def test_real_fdopen_audit_failure_closes_duplicate(staged_snapshot, failure):
    observed = _observe_snapshot(staged_snapshot, failure)
    assert observed == {
        "iterations": 6,
        "audit_open_count": 6,
        "exceptions_preserved": True,
        "captured_fds_closed": True,
        "snapshot_names_remaining": 0,
        "process_attempts": 0,
        "fd_growth": 0,
    }


@pytest.mark.parametrize("staged_snapshot", ["sha1", "sha256"], indirect=True)
def test_real_snapshot_stream_is_complete_readonly_and_owned(staged_snapshot):
    observed = _observe_snapshot(staged_snapshot, "none")
    assert observed == {
        "audit_open_count": 1,
        "stream_fd_audited": True,
        "offset_zero": True,
        "complete_bytes": True,
        "readonly_write_rejected": True,
        "stream_closed": True,
        "fd_survives_stream_close": True,
        "captured_fds_closed": True,
        "snapshot_names_remaining": 0,
        "process_attempts": 0,
        "fd_growth": 0,
    }
