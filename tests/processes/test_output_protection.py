"""私有Owner封套及真实OS持久前脱敏；模型材料不增加工作负载环境权限。"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.processes.owner_output import CapturedProcessOutput, capture_process_streams
from harnessix.processes.owner_protocol import (
    ProcessOwnerStart,
    ProcessOwnerStartV2,
    decode_owner_start,
    protected_owner_start,
)
from harnessix.processes.supervision_planner import build_process_spec
from harnessix.processes.supervisor import PosixProcessSupervisor, WindowsProcessSupervisor
from tests.agent.test_publication import CANARY, protected
from tests.processes.helpers import ready


def owner_start(root):
    return ProcessOwnerStart(
        process_id=uuid4(),
        owner_identity="1" * 64,
        owner_token="2" * 64,
        argv=("python", "-V"),
        cwd=str(root),
        environment={"VISIBLE": "benign"},
        secret_names=(),
        terminal="pipe",
        stdin="closed",
        deadline=datetime.now(UTC) + timedelta(seconds=10),
        output_bytes=4096,
        input_bytes=0,
        columns=80,
        rows=24,
    )


def test_original_v1_stays_identical_and_v2_only_carries_protection_in_envelope(tmp_path):
    request = owner_start(tmp_path)
    original = request.model_dump_json()
    assert protected_owner_start(request, None) is request
    assert decode_owner_start(original.encode()) == (request, ())
    with protected() as scope:
        packet = protected_owner_start(request, scope)
    assert isinstance(packet, ProcessOwnerStartV2)
    assert packet.start.model_dump_json() == original
    assert packet.start.environment == {"VISIBLE": "benign"}
    assert packet.start.secret_names == ()
    assert CANARY not in original and CANARY not in repr(packet)
    assert decode_owner_start(packet.model_dump_json().encode()) == (request, (CANARY.encode(),))
    with pytest.raises(ValidationError):
        ProcessOwnerStart.model_validate_json(packet.model_dump_json())
    assert "output_redaction_base64" not in ProcessOwnerStart.model_json_schema()["properties"]


@pytest.mark.parametrize(
    "values",
    [
        [],
        (bytearray(b"safe"),),
        (b"abc",),
        (b"abc\0def",),
        (b"x" * (65536 + 1),),
        tuple([b"safe"] * 33),
    ],
)
def test_invalid_protection_pool_is_fixed_preflight_failure(tmp_path, values):
    class Invalid:
        def output_redaction_values(self):
            return values

    with pytest.raises(KernelError) as caught:
        protected_owner_start(owner_start(tmp_path), Invalid())
    assert caught.value.code == "process_output_protection_unavailable"
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize("case", ["closed", "diagnostic", "frame", "combined_patterns"])
def test_unavailable_protection_is_not_serialized_or_hidden_in_target(tmp_path, monkeypatch, case):
    from harnessix.processes import owner_protocol

    with protected() as scope:
        request = owner_start(tmp_path)
        source = scope
        if case == "closed":
            scope.close()
        elif case == "diagnostic":

            class Broken:
                def output_redaction_values(self):
                    raise ValueError(CANARY)

            source = Broken()
        elif case == "frame":
            monkeypatch.setattr(owner_protocol, "MAX_OWNER_CONTROL_FRAME_BYTES", 10)
        else:

            def fail(_):
                raise KernelError("secret_redaction_unsafe", CANARY)

            monkeypatch.setattr(owner_protocol, "secret_patterns", fail)
        with pytest.raises(KernelError) as caught:
            protected_owner_start(request, source)
        assert caught.value.code == "process_output_protection_unavailable"
        assert CANARY not in str(caught.value)
        assert request.environment == {"VISIBLE": "benign"}


@pytest.mark.parametrize(
    "encoded",
    [
        "YWJjZA",
        "YWJjZA==\n",
        "YWJjZB==",
        base64.b64encode(b"abc").decode(),
        base64.b64encode(b"abc\0def").decode(),
    ],
)
def test_v2_owner_rejects_noncanonical_or_unsafe_encoded_values(tmp_path, encoded):
    with pytest.raises(ValidationError):
        ProcessOwnerStartV2(start=owner_start(tmp_path), output_redaction_base64=(encoded,))


def test_original_declared_secret_and_model_protection_share_stream_redactor(tmp_path):
    request = owner_start(tmp_path).model_copy(
        update={"environment": {"INJECTED": "target-secret"}, "secret_names": ("INJECTED",)}
    )
    request = ProcessOwnerStart.model_validate_json(request.model_dump_json())
    outputs = capture_process_streams(tmp_path, request, (CANARY.encode(),))
    try:
        for output in outputs:
            for byte in b"\xff" + CANARY.encode() + b" target-secret\0":
                output.feed(bytes([byte]), 4096)
            output.finish(4096, eof=True)
            output.sync()
            expected = b"\xff[REDACTED] [REDACTED]\0"
            assert output.observation().persisted_sha256 == hashlib.sha256(expected).hexdigest()
        assert (tmp_path / "stdout.bin").read_bytes() == expected
        assert (tmp_path / "stderr.bin").read_bytes() == expected
    finally:
        for output in outputs:
            output.close()


def test_redactor_validation_happens_before_file_creation(tmp_path):
    with pytest.raises(KernelError):
        CapturedProcessOutput(tmp_path / "stdout.bin", (b"abc",))
    assert not (tmp_path / "stdout.bin").exists()


def test_second_capture_failure_closes_first_fd(tmp_path, monkeypatch):
    from harnessix.processes import owner_output

    original = owner_output.CapturedProcessOutput
    opened = []

    def capture(path, values):
        if opened:
            raise OSError("第二流创建失败")
        result = original(path, values)
        opened.append(result)
        return result

    monkeypatch.setattr(owner_output, "CapturedProcessOutput", capture)
    with pytest.raises(OSError, match="第二流"):
        capture_process_streams(tmp_path, owner_start(tmp_path), (CANARY.encode(),))
    assert opened[0]._fd == -1


def native_plan(root, spec, supervisor):
    if os.name == "nt":
        from tests.processes.test_windows_supervisor import _plan
    else:
        from tests.processes.test_supervisor import _plan
    return _plan(root, spec, supervisor)


@pytest.mark.parametrize("stop", ["exited", "timeout", "cancelled"])
async def test_real_owner_redacts_original_model_value_before_binary_persistence(tmp_path, stop):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    # 工作负载从批准的stdin接收合成测试值；argv、环境及计划不含保护值。
    code = (
        "import os,sys,time; data=sys.stdin.buffer.readline().rstrip(b'\\n'); "
        "assert 'MODEL_KEY' not in os.environ; "
        "os.write(1,b'\\xffx'); os.write(2,b'\\x00y'); "
        "[(os.write(1,bytes([byte])),os.write(2,bytes([byte])),time.sleep(.005)) "
        "for byte in data]; "
        "os.write(1,b'\\n'); os.write(2,b'\\n'); "
        "open(sys.argv[1],'w').write(str(os.getpid())); "
        + ("time.sleep(30)" if stop != "exited" else "")
    )
    with protected() as scope:
        async with supervisor_type(
            state, output_redaction=scope, terminate_grace_seconds=0.05
        ) as supervisor:
            spec = build_process_spec(
                invocation="argv",
                argv=(sys.executable, "-I", "-c", code, str(workspace / "ready")),
                stdin="pipe",
                input_bytes=128,
                output_bytes=4096,
                timeout_seconds=1.5 if stop == "timeout" else 10.0,
            )
            plan = native_plan(workspace, spec, supervisor)
            assert not plan.secrets and CANARY not in plan.model_dump_json()
            handle = await supervisor.start(
                plan, spec, supervisor.capability, workspace=workspace, environment={}
            )
            await handle.send_stdin(CANARY.encode() + b"\n")
            await handle.close_stdin()
            if stop == "cancelled":
                # 用目标进程的实际就绪标记同步，取消后Owner仍需排空隐藏尾窗。
                await ready(workspace / "ready")
                await handle.stop("cancelled")
            result = await handle.wait()
            stdout = await handle.output("stdout")
            stderr = await handle.output("stderr")
            assert result.stop_reason == stop and result.state == "exited"
            assert stdout == b"\xffx[REDACTED]\n"
            assert stderr == b"\x00y[REDACTED]\n"
            assert result.stdout.persisted_sha256 == hashlib.sha256(stdout).hexdigest()
            assert result.stderr.persisted_sha256 == hashlib.sha256(stderr).hexdigest()
            assert CANARY not in result.model_dump_json()
    for path in state.rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes()


async def test_closed_scope_fails_before_lease_and_owner_creation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    supervisor_type = WindowsProcessSupervisor if os.name == "nt" else PosixProcessSupervisor
    with protected() as scope:
        async with supervisor_type(tmp_path / "state", output_redaction=scope) as supervisor:
            spec = build_process_spec(invocation="argv", argv=(sys.executable, "-V"))
            plan = native_plan(workspace, spec, supervisor)
            scope.close()

            def forbidden(*args, **kwargs):
                raise AssertionError("保护失败不能创建Lease或启动Owner")

            monkeypatch.setattr(supervisor._store, "create", forbidden)
            monkeypatch.setattr(supervisor, "_spawn_owner", forbidden)
            with pytest.raises(KernelError) as caught:
                await supervisor.start(
                    plan, spec, supervisor.capability, workspace=workspace, environment={}
                )
            assert caught.value.code == "process_output_protection_unavailable"
            assert not list(supervisor._runs.iterdir())


def test_encoded_pool_rejects_before_base64_allocation(tmp_path, monkeypatch):
    def forbidden(_self):
        raise AssertionError("超大编码池不应先执行解码")

    monkeypatch.setattr(ProcessOwnerStartV2, "output_values", forbidden)
    with pytest.raises(ValidationError, match="编码池"):
        ProcessOwnerStartV2(start=owner_start(tmp_path), output_redaction_base64=("a" * 65536,) * 2)
