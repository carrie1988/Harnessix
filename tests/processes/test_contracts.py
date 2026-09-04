import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.processes.capture import CaptureProtocol
from harnessix.processes.contracts import (
    ProcessLimits,
    ProcessRequest,
    ProcessResult,
    ProcessStream,
)
from harnessix.processes.runtime import HostProcessRuntime
from tests.processes.helpers import request, runtime


@pytest.mark.parametrize(
    "change",
    [
        {"program": "../python"},
        {"program": ""},
        {"program": "x" * 65},
        {"arguments": ["not-a-tuple"]},
        {"arguments": ("nul\0",)},
        {"arguments": ("中" * 21846,)},
        {"arguments": ("x",) * 129},
        {"arguments": (1,)},
        {"timeout_seconds": 0.0},
        {"timeout_seconds": -1.0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": True},
        {"shell": True},
        {"cwd": "/tmp"},
        {"env": {}},
    ],
)
def test_request_fails_closed(change):
    with pytest.raises(ValidationError):
        ProcessRequest(**{"program": "python", **change})


def test_arguments_are_byte_bounded_and_not_exposed_by_repr():
    value = ProcessRequest(program="python", arguments=("中" * 21845,))
    assert ProcessRequest.model_validate_json(value.model_dump_json()) == value
    assert "中" not in repr(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("stdout_bytes", -1),
        ("stdout_bytes", True),
        ("stderr_bytes", 1048577),
        ("stop_output_bytes", 0),
        ("stop_output_bytes", 67108865),
        ("terminate_grace_seconds", -0.1),
        ("terminate_grace_seconds", 6.0),
        ("pipe_drain_seconds", 0.0),
        ("max_timeout_seconds", float("inf")),
    ],
)
def test_limits_are_finite_and_bounded(field, value):
    with pytest.raises(ValidationError):
        ProcessLimits(**{field: value})


@pytest.mark.parametrize(
    "name",
    [
        "HOME",
        "SSH_AUTH_SOCK",
        "OPENAI_API_KEY",
        "PYTHONPATH",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
    ],
)
def test_secret_or_loader_environment_keys_are_denied(tmp_path, name):
    with pytest.raises(KernelError) as error:
        runtime(tmp_path, environment={name: "fixture-canary"})
    assert error.value.code == "process_environment_denied"
    assert "canary" not in str(error.value)


@pytest.mark.parametrize("value", [None, 1, "a\0b", "\ud800", "a" * 8192])
def test_environment_values_fail_closed(tmp_path, value):
    with pytest.raises(KernelError) as error:
        runtime(tmp_path, environment={"LANG": value})
    assert error.value.code == "process_environment_denied"


@pytest.mark.parametrize(
    "kind",
    [
        "relative_cwd",
        "missing_cwd",
        "relative_executable",
        "unknown_program",
        "non_executable",
        "directory",
        "empty_table",
    ],
)
def test_invalid_host_binding(tmp_path, kind):
    cwd, programs = tmp_path, {"python": sys.executable}
    if kind == "relative_cwd":
        cwd = Path("relative")
    elif kind == "missing_cwd":
        cwd = tmp_path / "missing"
    elif kind == "relative_executable":
        programs = {"python": "python"}
    elif kind == "unknown_program":
        programs = {"../bad": sys.executable}
    elif kind == "empty_table":
        programs = {}
    elif kind == "directory":
        programs = {"python": tmp_path}
    else:
        path = tmp_path / "no-exec"
        path.write_text("fixture")
        path.chmod(0o600)
        programs = {"python": path}
    with pytest.raises(KernelError) as error:
        HostProcessRuntime(cwd, programs)
    assert error.value.code == "process_binding_invalid"


@pytest.mark.parametrize("kind", ["program", "budget", "forged"])
async def test_admission_does_not_launch_on_invalid_request(tmp_path, kind):
    async with runtime(tmp_path, limits=ProcessLimits(max_timeout_seconds=1.0)) as host:
        value = request("raise AssertionError('must not launch')", timeout=0.5)
        if kind == "program":
            value = value.model_copy(update={"program": "unregistered"})
        elif kind == "budget":
            value = value.model_copy(update={"timeout_seconds": 2.0})
        else:
            value = value.model_copy(update={"arguments": ("nul\0",)})
        with pytest.raises(KernelError) as error:
            await host.run(value, CancelToken())
        assert (
            error.value.code
            == {
                "program": "process_program_denied",
                "budget": "process_budget_exceeded",
                "forged": "process_invalid_arguments",
            }[kind]
        )


def stream(**change):
    data = b"abc"
    return ProcessStream(
        **{
            "data_base64": base64.b64encode(data).decode(),
            "captured_bytes": 3,
            "observed_bytes": 3,
            "observed_sha256": hashlib.sha256(data).hexdigest(),
            "truncated": False,
            "eof": True,
            **change,
        }
    )


@pytest.mark.parametrize(
    "change",
    [
        {"data_base64": "!!!"},
        {"data_base64": "YQ==\n"},
        {"captured_bytes": 2},
        {"observed_bytes": 2},
        {"observed_sha256": "0" * 64},
        {"truncated": True},
        {"observed_bytes": 4},
        {"eof": "yes"},
    ],
)
def test_stream_metadata_cannot_claim_wrong_capture(change):
    with pytest.raises(ValidationError):
        stream(**change)


async def test_pipe_error_and_forced_close_never_claim_natural_eof():
    reasons = []
    capture = CaptureProtocol(ProcessLimits(stdout_bytes=2), reasons.append)
    capture.pipe_data_received(1, b"abcd")
    capture.pipe_connection_lost(1, OSError("raw-canary"))
    result = capture.streams[1].result()
    assert result.data() == b"ab" and not result.eof and result.truncated
    assert reasons == ["io_error"] and "raw-canary" not in result.model_dump_json()


@pytest.mark.parametrize(
    "name,model",
    [
        ("request", ProcessRequest),
        ("limits", ProcessLimits),
        ("stream", ProcessStream),
        ("result", ProcessResult),
    ],
)
def test_generated_process_schemas(name, model):
    path = Path(__file__).parents[2] / "spec" / f"process-{name}-v1.schema.json"
    assert json.loads(path.read_text()) == model.model_json_schema()
