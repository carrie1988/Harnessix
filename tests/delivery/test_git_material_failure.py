"""材料Worker的有限失败观察合同；测试桩不替代真实Owner或原生验收。"""

from __future__ import annotations

import io
import json
from dataclasses import fields

import pytest

from harnessix.delivery.git_material_failure import (
    FAILURE_PREFIX,
    FAILURE_SCHEMA,
    MATERIAL_ERROR_CODES,
    STAGES,
    GitMaterialFailureObservation,
    decode_failure_observation,
    encode_failure_observation,
)
from harnessix.delivery.git_material_input_contracts import GitMaterialInputError

CANARY = "failure-observation-private-text-must-never-escape"


@pytest.mark.parametrize("stage", sorted(STAGES))
@pytest.mark.parametrize("code", sorted(MATERIAL_ERROR_CODES))
def test_all_source_proven_stages_and_material_codes_round_trip(stage, code):
    observation = GitMaterialFailureObservation(stage=stage)
    body = encode_failure_observation(observation, GitMaterialInputError(code))
    status, record = decode_failure_observation(b"unrelated stderr\n" + body)
    assert status == "valid" and record["schema"] == FAILURE_SCHEMA
    assert record["stage"] == stage and record["error_code"] == code
    assert record["git_popen_returned"] is False and record["git_returncode"] is None
    assert len(body) <= 2048 and CANARY.encode() not in body


@pytest.mark.parametrize("code", [-(2**31), -9, 0, 128, 2**31, 2**32 - 1])
def test_posix_and_windows_original_wait_status_is_not_normalized(code):
    observation = GitMaterialFailureObservation(
        stage="git_validate",
        git_popen_returned=True,
        git_returncode=code,
        git_stdout_complete=True,
        git_stdout_expected=False if code == 0 else None,
    )
    status, record = decode_failure_observation(
        encode_failure_observation(observation, GitMaterialInputError("git_material_git_failed"))
    )
    assert status == "valid" and type(record["git_returncode"]) is int
    assert record["git_returncode"] == code


@pytest.mark.parametrize("value", [True, -(2**31) - 1, 2**32, CANARY])
def test_bad_observation_never_serializes_arbitrary_values(value):
    observation = GitMaterialFailureObservation(git_popen_returned=True)
    observation.git_returncode = value
    body = encode_failure_observation(observation, GitMaterialInputError(CANARY))
    status, record = decode_failure_observation(body)
    assert status == "valid" and record["stage"] == "unobserved"
    assert record["error_code"] == "unclassified" and CANARY.encode() not in body


def test_scalar_subclasses_extra_fields_and_error_subclasses_cannot_escape():
    class Text(str):
        pass

    class HostileError(GitMaterialInputError):
        def __getattribute__(self, name):
            raise AssertionError(CANARY)

    observation = GitMaterialFailureObservation()
    for error in [GitMaterialInputError(Text("git_material_git_failed")), HostileError(CANARY)]:
        status, record = decode_failure_observation(encode_failure_observation(observation, error))
        assert status == "valid" and record["error_code"] == "unclassified"
    observation.extra = CANARY
    status, record = decode_failure_observation(
        encode_failure_observation(observation, ValueError(CANARY))
    )
    assert status == "valid" and record["stage"] == "unobserved"
    assert set(vars(GitMaterialFailureObservation())) == {
        field.name for field in fields(observation)
    }


def test_encoder_never_calls_exception_text_or_observation_getters():
    class Hostile:
        def __getattribute__(self, name):
            raise AssertionError(CANARY)

    class Error(ValueError):
        def __str__(self):
            raise AssertionError(CANARY)

        def __repr__(self):
            raise AssertionError(CANARY)

    status, record = decode_failure_observation(encode_failure_observation(Hostile(), Error()))
    assert status == "valid" and record["stage"] == "unobserved"
    assert record["error_code"] == "unclassified"


def _frame(record):
    return (
        FAILURE_PREFIX + json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )


@pytest.mark.parametrize(
    "change", ["extra", "missing", "stage", "schema", "code", "bool", "integer", "incoherent"]
)
def test_complete_but_invalid_frame_is_not_observed_as_valid(change):
    _, record = decode_failure_observation(
        encode_failure_observation(GitMaterialFailureObservation(), ValueError())
    )
    if change == "extra":
        record[CANARY] = CANARY
    elif change == "missing":
        del record["stage"]
    elif change == "stage":
        record["stage"] = CANARY
    elif change == "schema":
        record["schema"] = "unknown"
    elif change == "code":
        record["error_code"] = CANARY
    elif change == "bool":
        record["git_popen_returned"] = 1
    elif change == "integer":
        record["git_returncode"] = True
    else:
        record["git_stdout_expected"] = True
    assert decode_failure_observation(_frame(record)) == ("invalid", None)


@pytest.mark.parametrize(
    "suffix", [b"", b"\xff\n", b'{"stage":"entry","stage":"proof"}\n', b"{" + b"x" * 2049 + b"}\n"]
)
def test_malformed_truncated_duplicate_key_and_overlimit_frames_are_invalid(suffix):
    assert decode_failure_observation(FAILURE_PREFIX + suffix) == ("invalid", None)


def test_absence_and_multiple_frames_are_distinct_and_no_raw_text_is_returned():
    frame = encode_failure_observation(GitMaterialFailureObservation(), ValueError(CANARY))
    assert decode_failure_observation(CANARY.encode()) == ("not_observed", None)
    assert decode_failure_observation(b"x" + frame) == ("not_observed", None)
    assert decode_failure_observation(frame + frame) == ("invalid", None)
    assert decode_failure_observation(frame + CANARY.encode())[0] == "valid"


def _main_streams(monkeypatch, worker):
    from types import SimpleNamespace

    streams = [io.BytesIO(b"manifest"), io.BytesIO(), io.BytesIO()]
    for number, name in enumerate(("stdin", "stdout", "stderr")):
        monkeypatch.setattr(
            worker.sys, name, SimpleNamespace(buffer=streams[number], fileno=lambda n=number: n)
        )
    if worker.os.name == "nt":
        import msvcrt

        monkeypatch.setattr(msvcrt, "setmode", lambda *args: 0)
    monkeypatch.setattr(worker, "_remaining", lambda *args: 1)
    return streams


def test_original_main_failure_preserves_marker_exit_and_single_call(monkeypatch):
    from harnessix.delivery import git_material_worker as worker

    streams = _main_streams(monkeypatch, worker)
    calls = []

    def fail(payload, **kwargs):
        calls.append((payload, kwargs))
        kwargs["observation"].stage = "snapshot"
        raise GitMaterialInputError("git_material_private_invalid")

    monkeypatch.setattr(worker, "run_worker", fail)
    assert (
        worker.main(
            ["--manifest-sha256", "a" * 64, "--nonce", "b" * 64, "--expiry-monotonic-ns", "1"]
        )
        == 2
    )
    assert len(calls) == 1 and streams[1].getvalue() == b""
    body = streams[2].getvalue()
    assert body.startswith(b"git_material_worker_failed\n")
    status, record = decode_failure_observation(body)
    assert status == "valid" and record["stage"] == "snapshot"
    assert record["error_code"] == "git_material_private_invalid"


def test_original_main_success_writes_only_original_proof(monkeypatch):
    from harnessix.delivery import git_material_worker as worker

    streams = _main_streams(monkeypatch, worker)
    monkeypatch.setattr(worker, "run_worker", lambda *args, **kwargs: object())
    monkeypatch.setattr(worker, "encode_proof", lambda *args: b"original-proof")
    assert (
        worker.main(
            ["--manifest-sha256", "a" * 64, "--nonce", "b" * 64, "--expiry-monotonic-ns", "1"]
        )
        == 0
    )
    assert streams[1].getvalue() == b"original-proof" and streams[2].getvalue() == b""


@pytest.mark.parametrize("code", [0, 128])
@pytest.mark.parametrize("output_kind", ["exact", "mismatch", "overlimit", "read_error"])
def test_git_observation_reuses_original_calls_and_never_decides_business(
    monkeypatch, code, output_kind
):
    from types import SimpleNamespace

    from harnessix.delivery import git_material_worker as worker

    calls = []
    expected = b"a" * 64 + b"\n"

    class Output(io.BytesIO):
        def read(self, limit):
            calls.append(("read", limit))
            if output_kind == "read_error":
                raise OSError(CANARY)
            return super().read(limit)

    body = {
        "exact": expected,
        "mismatch": b"b" * 64 + b"\n",
        "overlimit": expected + b"x",
        "read_error": b"",
    }[output_kind]

    class Process:
        stdout = Output(body)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append(("exit",))

        def wait(self, *args, **kwargs):
            calls.append(("wait", bool(kwargs)))
            return code

        def poll(self):
            calls.append(("poll",))
            return code

        def kill(self):
            calls.append(("kill",))

    captured = []

    def create(argv, **kwargs):
        captured.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(worker.subprocess, "Popen", create)
    monkeypatch.setattr(worker, "_remaining", lambda *args: 1)
    request = SimpleNamespace(
        expiry_monotonic_ns=1,
        git_argv=("fixed-git",),
        repo_path="fixture",
        git_environment={"LANG": "C"},
        expected_oid="a" * 64,
    )
    snapshot = io.BytesIO(b"full-snapshot")
    observation = GitMaterialFailureObservation()
    if code == 0 and output_kind == "exact":
        assert worker._git(request, snapshot, observation) == request.expected_oid
    else:
        with pytest.raises(GitMaterialInputError) as raised:
            worker._git(request, snapshot, observation)
        assert raised.value.code == "git_material_git_failed"
    assert len(captured) == 1 and captured[0][1]["stdin"] is snapshot
    assert captured[0][1]["shell"] is False and captured[0][1]["close_fds"] is True
    assert calls.count(("read", 66)) == 1
    assert calls.count(("wait", True)) == calls.count(("wait", False)) == 1
    assert observation.git_popen_returned is True and observation.git_returncode == code
    assert observation.git_stdout_complete is (
        False if output_kind in {"read_error", "overlimit"} else True if code == 0 else None
    )
    assert observation.git_stdout_expected is (
        output_kind == "exact" if code == 0 and observation.git_stdout_complete else None
    )


@pytest.mark.parametrize("sink_failure", ["write", "flush"])
def test_original_failure_sink_errors_do_not_retry_or_reexecute(monkeypatch, sink_failure):
    from types import SimpleNamespace

    from harnessix.delivery import git_material_worker as worker

    streams = _main_streams(monkeypatch, worker)
    calls = []

    class Sink:
        def write(self, body):
            calls.append("write")
            if sink_failure == "write":
                raise OSError(CANARY)

        def flush(self):
            calls.append("flush")
            raise OSError(CANARY)

    monkeypatch.setattr(worker.sys, "stderr", SimpleNamespace(buffer=Sink(), fileno=lambda: 2))

    def failure(*args, **kwargs):
        calls.append("worker")
        raise GitMaterialInputError(CANARY)

    monkeypatch.setattr(worker, "run_worker", failure)
    assert (
        worker.main(
            ["--manifest-sha256", "a" * 64, "--nonce", "b" * 64, "--expiry-monotonic-ns", "1"]
        )
        == 2
    )
    assert calls == (
        ["worker", "write"] if sink_failure == "write" else ["worker", "write", "flush"]
    )
    assert streams[1].getvalue() == b""
