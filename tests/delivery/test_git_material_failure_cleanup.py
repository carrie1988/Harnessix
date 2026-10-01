"""原清理与短路语义的诊断回归；局部桩不证明真实平台或Owner成功。"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from harnessix.delivery import git_material_failure as failure
from harnessix.delivery import git_material_worker as worker
from harnessix.delivery.git_material_input_contracts import GitMaterialInputError

CANARY = "private-first-failure-never-serialized"


def _git_fixture(
    monkeypatch,
    *,
    code=128,
    alive=False,
    read_error=False,
    final_error=None,
    exit_error=None,
    start_error=None,
):
    calls = []

    class Output(io.BytesIO):
        def read(self, limit):
            calls.append("read")
            assert limit == 66
            if read_error:
                raise OSError(CANARY)
            return super().read(limit)

    class Process:
        stdout = Output(b"a" * 64 + b"\n")

        def __enter__(self):
            calls.append("enter")
            return self

        def __exit__(self, *args):
            calls.append("exit")
            if exit_error is not None:
                raise exit_error

        def wait(self, *, timeout=None):
            calls.append("wait" if timeout is not None else "cleanup_wait")
            if timeout is None and final_error is not None:
                raise final_error
            return code

        def poll(self):
            calls.append("poll")
            return code

        def kill(self):
            calls.append("kill")

    class Event:
        flagged = False

        def set(self):
            self.flagged = True

        def is_set(self):
            calls.append("failed")
            return self.flagged

    class Thread:
        def __init__(self, *, target, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            calls.append("start")
            if start_error is not None:
                raise start_error
            self.target()

        def join(self, *, timeout):
            calls.append("join")
            assert timeout == 1

        def is_alive(self):
            calls.append("alive")
            return alive

    def length(value):
        if type(value) is list:
            calls.append("length")
        return len(value)

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(worker.threading, "Thread", Thread)
    monkeypatch.setattr(worker.threading, "Event", Event)
    monkeypatch.setattr(worker, "_remaining", lambda *args: 1)
    monkeypatch.setattr(worker, "len", length, raising=False)
    request = SimpleNamespace(
        expiry_monotonic_ns=1,
        git_argv=("fixed-git",),
        repo_path="fixture",
        git_environment={},
        expected_oid="a" * 64,
    )
    return request, io.BytesIO(b"snapshot"), calls


@pytest.mark.parametrize("cleanup", ["finally", "context"])
def test_first_failure_is_frozen_before_cleanup_override(monkeypatch, cleanup):
    final = PermissionError(CANARY)
    request, snapshot, calls = _git_fixture(
        monkeypatch,
        final_error=final if cleanup == "finally" else None,
        exit_error=final if cleanup == "context" else None,
    )
    observation = failure.GitMaterialFailureObservation()
    with pytest.raises(PermissionError) as raised:
        worker._git(request, snapshot, observation)
    assert raised.value is final
    status, record = failure.decode_failure_observation(
        failure.encode_failure_observation(observation, raised.value)
    )
    assert status == "valid" and record["origin"] == "pre_cleanup"
    assert record["stage"] == "git_validate"
    assert record["error_code"] == "git_material_git_failed"
    assert record["handler_error_code"] == "os_error"
    assert record["git_returncode"] == 128 and record["git_stdout_complete"] is None
    assert calls.count("wait") == calls.count("cleanup_wait") == calls.count("exit") == 1
    assert CANARY not in str(record)


def test_setup_failure_before_popen_exit_retains_first_finite_category(monkeypatch):
    first, final = TypeError(CANARY), ValueError(CANARY)
    request, snapshot, calls = _git_fixture(monkeypatch, start_error=first, exit_error=final)
    observation = failure.GitMaterialFailureObservation()
    with pytest.raises(ValueError) as raised:
        worker._git(request, snapshot, observation)
    assert raised.value is final
    _, record = failure.decode_failure_observation(
        failure.encode_failure_observation(observation, final)
    )
    assert record["origin"] == "pre_cleanup" and record["stage"] == "git_launch"
    assert record["error_code"] == "type_error" and record["handler_error_code"] == "value_error"
    assert "poll" not in calls and "cleanup_wait" not in calls and calls.count("exit") == 1


@pytest.mark.parametrize(
    ("alive", "read_error", "code", "checks", "complete"),
    [
        (True, False, 128, ["alive"], False),
        (False, True, 128, ["alive", "failed"], False),
        (False, False, 128, ["alive", "failed"], None),
        (False, False, 0, ["alive", "failed", "length"], True),
    ],
)
def test_original_predicate_order_and_short_circuit_are_preserved(
    monkeypatch, alive, read_error, code, checks, complete
):
    request, snapshot, calls = _git_fixture(
        monkeypatch, code=code, alive=alive, read_error=read_error
    )
    observation = failure.GitMaterialFailureObservation()
    if code == 0:
        assert worker._git(request, snapshot, observation) == request.expected_oid
    else:
        with pytest.raises(GitMaterialInputError) as raised:
            worker._git(request, snapshot, observation)
        assert raised.value.code == "git_material_git_failed"
    assert [call for call in calls if call in {"alive", "failed", "length"}] == checks
    assert observation.git_stdout_complete is complete
    assert observation.git_stdout_expected is (True if code == 0 else None)


def test_resource_cleanup_does_not_replace_frozen_business_failure(monkeypatch):
    from contextlib import ExitStack

    first, final = GitMaterialInputError("git_material_private_invalid"), ValueError(CANARY)
    calls = []
    resources = ExitStack()

    def close():
        calls.append("close")
        raise final

    resources.callback(close)
    request = SimpleNamespace(nonce="b" * 64, expiry_monotonic_ns=1, implementation_digest="a" * 64)
    monkeypatch.setattr(worker, "_remaining", lambda *args: 1)
    monkeypatch.setattr(worker, "decode_manifest", lambda *args: request)
    monkeypatch.setattr(worker, "manifest_sha256", lambda *args: "c" * 64)
    monkeypatch.setattr(worker, "implementation_digest", lambda: "a" * 64)
    monkeypatch.setattr(worker, "_Windows", lambda: None)
    monkeypatch.setattr(worker, "_Resources", lambda: resources)

    def command(*args):
        calls.append("command")
        raise first

    monkeypatch.setattr(worker, "_command", command)
    observation = failure.GitMaterialFailureObservation()
    with pytest.raises(ValueError) as raised:
        worker.run_worker(
            b"manifest",
            expected_nonce="b" * 64,
            expected_manifest_sha256="c" * 64,
            expiry_monotonic_ns=1,
            observation=observation,
        )
    assert raised.value is final and calls == ["command", "close"]
    _, record = failure.decode_failure_observation(
        failure.encode_failure_observation(observation, final)
    )
    assert record["stage"] == "command" and record["origin"] == "pre_cleanup"
    assert record["error_code"] == first.code and record["handler_error_code"] == "value_error"


def test_encoder_failure_keeps_original_main_marker_and_exit(monkeypatch):
    from tests.delivery.test_git_material_failure import _main_streams

    streams = _main_streams(monkeypatch, worker)
    calls = []

    def execute(*args, **kwargs):
        calls.append("worker")
        raise GitMaterialInputError("git_material_private_invalid")

    def encode(*args):
        calls.append("encode")
        raise ValueError(CANARY)

    monkeypatch.setattr(worker, "run_worker", execute)
    monkeypatch.setattr(worker, "encode_failure_observation", encode)
    assert (
        worker.main(
            ["--manifest-sha256", "a" * 64, "--nonce", "b" * 64, "--expiry-monotonic-ns", "1"]
        )
        == 2
    )
    assert calls == ["worker", "encode"]
    assert streams[1].getvalue() == b""
    assert streams[2].getvalue() == b"git_material_worker_failed\n"


@pytest.mark.parametrize("cleanup", ["finally", "context"])
def test_cleanup_only_failure_has_cleanup_stage_not_git_success(monkeypatch, cleanup):
    final = OSError(CANARY)
    request, snapshot, calls = _git_fixture(
        monkeypatch,
        code=0,
        final_error=final if cleanup == "finally" else None,
        exit_error=final if cleanup == "context" else None,
    )
    observation = failure.GitMaterialFailureObservation()
    with pytest.raises(OSError) as raised:
        worker._git(request, snapshot, observation)
    assert raised.value is final and calls.count("exit") == 1
    _, record = failure.decode_failure_observation(
        failure.encode_failure_observation(observation, final)
    )
    assert record["stage"] == "git_cleanup" and record["error_code"] == "os_error"
    assert record["origin"] == ("pre_cleanup" if cleanup == "finally" else "handler")
    assert record["git_stdout_expected"] is True


def test_first_finite_snapshot_is_immutable_without_retaining_exception():
    observation = failure.GitMaterialFailureObservation(stage="snapshot")
    failure.freeze_failure_observation(
        observation, GitMaterialInputError("git_material_body_changed")
    )
    first = observation._first_frame
    assert type(first) is bytes and len(first) < 2048
    observation.stage = "resources_close"
    failure.freeze_failure_observation(observation, TypeError(CANARY))
    assert observation._first_frame is first
    _, record = failure.decode_failure_observation(
        failure.encode_failure_observation(observation, ValueError(CANARY))
    )
    assert record["stage"] == "snapshot" and record["error_code"] == "git_material_body_changed"
    assert record["handler_error_code"] == "value_error"
    assert not any(isinstance(value, BaseException) for value in vars(observation).values())


@pytest.mark.parametrize("frame", [CANARY.encode(), CANARY, b"x" * 2049])
def test_untrusted_frozen_bytes_never_escape(frame):
    observation = failure.GitMaterialFailureObservation()
    observation._first_frame = frame
    body = failure.encode_failure_observation(observation, ValueError(CANARY))
    _, record = failure.decode_failure_observation(body)
    assert record["origin"] == "handler" and record["stage"] == "unobserved"
    assert CANARY.encode() not in body


def test_freeze_projection_error_preserves_original_exception(monkeypatch):
    original = GitMaterialInputError("git_material_body_changed")
    observation = failure.GitMaterialFailureObservation(stage="snapshot")

    def encode(*args):
        raise MemoryError(CANARY)

    monkeypatch.setattr(failure, "_encode", encode)
    with pytest.raises(GitMaterialInputError) as raised:
        with failure.observe_failure_before_cleanup(observation, "resources_close"):
            raise original
    assert raised.value is original and observation._first_frame is None
    assert observation.stage == "resources_close"


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(7), RuntimeError(CANARY)])
def test_scope_does_not_enlarge_original_business_catch_set(error):
    observation = failure.GitMaterialFailureObservation()
    with pytest.raises(type(error)) as raised:
        with failure.observe_failure_before_cleanup(observation, "resources_close"):
            raise error
    assert raised.value is error and observation._first_frame is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", "other"),
        ("origin", 1),
        ("handler_error_code", CANARY),
        ("handler_error_code", 1),
        ("handler_error_code", "type_error"),
    ],
)
def test_extended_frame_fields_are_exact_and_consistent(field, value):
    import json

    _, record = failure.decode_failure_observation(
        failure.encode_failure_observation(failure.GitMaterialFailureObservation(), ValueError())
    )
    record[field] = value
    body = (
        failure.FAILURE_PREFIX
        + json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    assert failure.decode_failure_observation(body) == ("invalid", None)
