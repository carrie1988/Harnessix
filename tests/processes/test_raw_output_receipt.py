"""原始观察只保留认证元数据；脱敏文件与原v1字节合同独立保持。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.processes import owner_output, owner_receipt
from harnessix.processes.owner_output import CapturedProcessOutput
from harnessix.processes.owner_receipt import (
    MAX_OWNER_RECEIPT_BYTES,
    MAX_RAW_PROCESS_OUTPUT_BYTES,
    OwnerReceipt,
    ProcessOwnerReceipt,
    ProcessOwnerReceiptV2,
    RawProcessOutputObservation,
    parse_owner_receipt,
    read_owner_receipt,
    sign_owner_receipt,
    verify_owner_receipt,
    write_owner_receipt,
)
from harnessix.processes.supervision_contracts import empty_process_output

EMPTY_SHA = hashlib.sha256(b"").hexdigest()
TOKEN = "d" * 64
IDENTITY = "e" * 64
PROCESS_ID = UUID("00000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 9, 30, tzinfo=UTC)
V1_MAC = "94fac5401f3abd1e1086427160971c7676571938f050680901b010442c2a6403"
V1_FIELDS = (
    "spec_version",
    "process_id",
    "owner_identity",
    "state",
    "sequence",
    "pid",
    "started_at",
    "finished_at",
    "returncode",
    "stop_reason",
    "stdout",
    "stderr",
    "mac",
)


def _raw(body: bytes = b"", *, eof: bool = False) -> RawProcessOutputObservation:
    return RawProcessOutputObservation(
        observed_bytes=len(body), sha256=hashlib.sha256(body).hexdigest(), eof=eof
    )


def _receipt(*, v2: bool = True) -> OwnerReceipt:
    return sign_owner_receipt(
        process_id=PROCESS_ID,
        owner_identity=IDENTITY,
        state="running",
        sequence=1,
        owner_token=TOKEN,
        pid=123,
        started_at=NOW,
        stdout=empty_process_output(),
        stderr=empty_process_output(),
        raw_stdout=_raw(b"stdout") if v2 else None,
        raw_stderr=_raw(b"stderr") if v2 else None,
    )


def _verify(receipt: OwnerReceipt) -> OwnerReceipt:
    return verify_owner_receipt(
        receipt, owner_token=TOKEN, process_id=PROCESS_ID, owner_identity=IDENTITY
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_bytes": -1},
        {"observed_bytes": MAX_RAW_PROCESS_OUTPUT_BYTES + 1},
        {"observed_bytes": True},
        {"observed_bytes": 1.0},
        {"observed_bytes": "1"},
        {"sha256": "f" * 63},
        {"sha256": "f" * 65},
        {"sha256": "F" * 64},
        {"sha256": "g" * 64},
        {"sha256": "f" * 64 + "\n"},
        {"sha256": b"f" * 64},
        {"observed_bytes": 0, "sha256": "f" * 64},
        {"eof": 1},
        {"eof": "true"},
        {"eof": None},
        {"data": "raw正文禁止进入模型"},
        {"persisted_bytes": 0},
    ],
)
def test_raw_observation_rejects_non_strict_or_unbounded_fields(changes):
    with pytest.raises(ValidationError):
        RawProcessOutputObservation.model_validate({**_raw(b"x").model_dump(), **changes})


@pytest.mark.parametrize("field", ["observed_bytes", "sha256", "eof"])
def test_raw_observation_requires_every_field(field):
    payload = _raw().model_dump()
    del payload[field]
    with pytest.raises(ValidationError):
        RawProcessOutputObservation.model_validate(payload)


def test_raw_observation_boundary_and_empty_eof_are_explicit():
    assert _raw().sha256 == _raw(eof=True).sha256 == EMPTY_SHA
    assert _raw().eof is False and _raw(eof=True).eof is True
    maximum = RawProcessOutputObservation(
        observed_bytes=MAX_RAW_PROCESS_OUTPUT_BYTES, sha256="f" * 64, eof=False
    )
    assert maximum.observed_bytes == 2**63 - 1
    assert tuple(RawProcessOutputObservation.model_fields) == ("observed_bytes", "sha256", "eof")
    with pytest.raises(ValidationError):
        maximum.observed_bytes = 1


@pytest.mark.parametrize("eof", [False, True])
def test_empty_capture_finishes_without_double_counting(tmp_path, eof):
    stream = CapturedProcessOutput(tmp_path / "stdout.bin", ())
    try:
        assert stream.raw_observed == 0 and stream.raw_observation() == _raw()
        assert stream.feed(b"", 0) == 0
        assert stream.finish(0, eof=eof) == 0
        snapshot = stream.raw_observation()
        assert snapshot == _raw(eof=eof)
        assert stream.finish(100, eof=not eof) == 0
        assert stream.raw_observation() == snapshot
        assert stream.observation() == empty_process_output(eof=eof)
        assert (tmp_path / "stdout.bin").read_bytes() == b""
    finally:
        stream.close()


@pytest.mark.parametrize(
    "secret",
    [b"RAW-SYNTHETIC-SECRET-ONLY-FOR-TEST", b"safe"],
    ids=["long-protected-value", "short-protected-value"],
)
@pytest.mark.parametrize("budget", [0, 1, 7, 1024])
@pytest.mark.parametrize("eof", [False, True])
def test_dual_raw_streams_ignore_redaction_length_and_persisted_truncation(
    tmp_path, secret, budget, eof
):
    streams = [
        CapturedProcessOutput(tmp_path / f"{name}.bin", (secret,)) for name in ("stdout", "stderr")
    ]
    bodies = (b"\xff\x00" + secret + b"\r\n\x1a", secret + b"\xfe\n")
    safe_bodies = (b"\xff\x00[REDACTED]\r\n\x1a", b"[REDACTED]\xfe\n")
    remaining = budget
    try:
        for stream, body, safe in zip(streams, bodies, safe_bodies, strict=True):
            for offset, byte in enumerate(body, 1):
                before = stream.persisted
                stream.feed(bytes([byte]), remaining)
                remaining -= stream.persisted - before
                assert stream.raw_observation() == _raw(body[:offset])
                assert stream.raw_observed == offset
            before = stream.persisted
            stream.finish(remaining, eof=eof)
            remaining -= stream.persisted - before
            stream.sync()
            assert stream.raw_observation() == _raw(body, eof=eof)
            observation = stream.observation()
            assert observation.observed_bytes == len(safe)
            assert observation.sha256 == hashlib.sha256(safe).hexdigest()
            assert observation.eof is eof
            if len(secret) == 4:
                assert stream.raw_observed < observation.observed_bytes
            else:
                assert stream.raw_observed > observation.observed_bytes
        stdout, stderr = streams
        receipt = sign_owner_receipt(
            process_id=PROCESS_ID,
            owner_identity=IDENTITY,
            state="running",
            sequence=1,
            owner_token=TOKEN,
            pid=123,
            started_at=NOW,
            stdout=stdout.observation(),
            stderr=stderr.observation(),
            raw_stdout=stdout.raw_observation(),
            raw_stderr=stderr.raw_observation(),
        )
        write_owner_receipt(tmp_path / "receipt.json", receipt)
        assert (
            read_owner_receipt(
                tmp_path / "receipt.json",
                owner_token=TOKEN,
                process_id=PROCESS_ID,
                owner_identity=IDENTITY,
            )
            == receipt
        )
        assert sum(stream.persisted for stream in streams) == min(
            budget, sum(map(len, safe_bodies))
        )
        for name, stream, safe in zip(("stdout", "stderr"), streams, safe_bodies, strict=True):
            persisted = (tmp_path / f"{name}.bin").read_bytes()
            observation = stream.observation()
            assert persisted == safe[: stream.persisted]
            assert observation.persisted_sha256 == hashlib.sha256(persisted).hexdigest()
            assert observation.truncated is (len(persisted) < len(safe))
        for path in tmp_path.iterdir():
            assert secret not in path.read_bytes()
            if os.name == "posix":
                assert path.stat().st_mode & 0o777 == 0o600
        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "receipt.json",
            "stderr.bin",
            "stdout.bin",
        ]
        assert {name for name in vars(stdout) if name.startswith("_raw")} == {
            "_raw_digest",
            "_raw_observed",
        }
    finally:
        for stream in streams:
            stream.close()


def test_raw_measurement_precedes_redactor_and_includes_hidden_tail(tmp_path, monkeypatch):
    stream = CapturedProcessOutput(tmp_path / "stdout.bin", (b"safe",))
    original = stream._redactor.feed

    def checked_feed(body):
        assert stream.raw_observation() == _raw(body)
        return original(body)

    monkeypatch.setattr(stream._redactor, "feed", checked_feed)
    try:
        assert stream.feed(b"safe", 100) == 0
        assert stream.observed == 0 and stream.raw_observed == 4
        assert stream.finish(100, eof=True) == 10
        assert stream.raw_observation() == _raw(b"safe", eof=True)
        assert stream.observation().observed_bytes == 10
    finally:
        stream.close()


@pytest.mark.parametrize("body", [bytearray(b"x"), memoryview(b"x"), "x", None])
def test_rejected_feed_does_not_pollute_raw_observation(tmp_path, body):
    stream = CapturedProcessOutput(tmp_path / "stdout.bin", ())
    try:
        with pytest.raises(KernelError) as caught:
            stream.feed(body, 100)
        assert caught.value.code == "secret_redaction_failed"
        assert stream.raw_observation() == _raw()
        stream.finish(100, eof=True)
        with pytest.raises(KernelError) as caught:
            stream.feed(b"rejected", 100)
        assert caught.value.code == "secret_redaction_closed"
        assert stream.raw_observation() == _raw(eof=True)
    finally:
        stream.close()


def test_raw_overflow_rejects_before_redactor_and_preserves_accepted_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(owner_output, "MAX_RAW_PROCESS_OUTPUT_BYTES", 3)
    stream = CapturedProcessOutput(tmp_path / "stdout.bin", ())
    try:
        stream.feed(b"12", 10)
        with pytest.raises(KernelError) as caught:
            stream.feed(b"34", 10)
        assert caught.value.code == "process_output_limit_exceeded"
        assert stream.raw_observation() == _raw(b"12")
        stream.feed(b"3", 10)
        stream.finish(10, eof=True)
        assert stream.raw_observation() == _raw(b"123", eof=True)
        assert (tmp_path / "stdout.bin").read_bytes() == b"123"
    finally:
        stream.close()


def test_v1_exact_fields_json_canonical_payload_and_mac_remain_unchanged(tmp_path):
    observation = {
        "observed_bytes": 0,
        "persisted_bytes": 0,
        "sha256": EMPTY_SHA,
        "persisted_sha256": EMPTY_SHA,
        "truncated": False,
        "eof": False,
    }
    expected = {
        "spec_version": "harnessix.process-owner-receipt/v1",
        "process_id": str(PROCESS_ID),
        "owner_identity": IDENTITY,
        "state": "running",
        "sequence": 1,
        "pid": 123,
        "started_at": "2026-09-30T00:00:00Z",
        "finished_at": None,
        "returncode": None,
        "stop_reason": None,
        "stdout": observation,
        "stderr": observation,
        "mac": V1_MAC,
    }
    receipt = _receipt(v2=False)
    assert type(receipt) is ProcessOwnerReceipt
    assert tuple(ProcessOwnerReceipt.model_fields) == V1_FIELDS
    golden = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    assert receipt.model_dump_json() == golden
    payload = json.dumps(
        {key: value for key, value in expected.items() if key != "mac"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    assert owner_receipt._canonical_payload(receipt) == payload
    assert receipt.mac == hmac.new(bytes.fromhex(TOKEN), payload, hashlib.sha256).hexdigest()
    assert receipt.mac == V1_MAC
    assert ProcessOwnerReceipt.model_validate_json(golden) == receipt
    assert parse_owner_receipt(golden) == parse_owner_receipt(golden.encode()) == receipt
    assert type(_verify(receipt)) is ProcessOwnerReceipt
    write_owner_receipt(tmp_path / "receipt.json", receipt)
    assert (tmp_path / "receipt.json").read_bytes() == golden.encode()


@pytest.mark.parametrize("v2", [False, True])
def test_versioned_reader_round_trip_with_short_file_reads(tmp_path, monkeypatch, v2):
    receipt = _receipt(v2=v2)
    expected_type = ProcessOwnerReceiptV2 if v2 else ProcessOwnerReceipt
    assert type(receipt) is expected_type
    path = tmp_path / "receipt.json"
    write_owner_receipt(path, receipt)
    original = os.read
    monkeypatch.setattr(os, "read", lambda fd, size: original(fd, min(size, 7)))
    checked = read_owner_receipt(
        path, owner_token=TOKEN, process_id=PROCESS_ID, owner_identity=IDENTITY
    )
    assert checked == receipt and type(checked) is expected_type
    assert path.read_bytes() == receipt.model_dump_json().encode()


@pytest.mark.parametrize("state", ["running", "exited", "failed", "unknown"])
def test_v2_empty_dual_streams_keep_original_lifecycle_validation(tmp_path, state):
    running = state == "running"
    started = state in {"running", "exited"}
    reason = {
        "running": None,
        "exited": "exited",
        "failed": "launch_failed",
        "unknown": "cleanup_failed",
    }[state]
    receipt = sign_owner_receipt(
        process_id=PROCESS_ID,
        owner_identity=IDENTITY,
        state=state,
        sequence=1,
        owner_token=TOKEN,
        pid=123 if started else None,
        started_at=NOW if started else None,
        finished_at=None if running else NOW,
        returncode=0 if state == "exited" else None,
        stop_reason=reason,
        stdout=empty_process_output(eof=not running),
        stderr=empty_process_output(eof=not running),
        raw_stdout=_raw(eof=not running),
        raw_stderr=_raw(eof=not running),
    )
    assert type(receipt) is ProcessOwnerReceiptV2
    assert receipt.raw_stdout.sha256 == receipt.raw_stderr.sha256 == EMPTY_SHA
    path = tmp_path / "receipt.json"
    write_owner_receipt(path, receipt)
    assert read_owner_receipt(path, owner_token=TOKEN, process_id=PROCESS_ID) == receipt
    invalid = receipt.model_copy(update={"returncode": 1} if running else {"finished_at": None})
    with pytest.raises(KernelError) as caught:
        _verify(invalid)
    assert caught.value.code == "process_owner_receipt_invalid"


def test_old_v1_model_reader_rejects_v2_and_raw_fields_are_not_in_v1_schema():
    with pytest.raises(ValidationError):
        ProcessOwnerReceipt.model_validate_json(_receipt().model_dump_json())
    assert "raw_stdout" not in ProcessOwnerReceipt.model_json_schema()["properties"]
    assert "raw_stderr" not in ProcessOwnerReceipt.model_json_schema()["properties"]
    assert {"raw_stdout", "raw_stderr"} <= set(
        ProcessOwnerReceiptV2.model_json_schema()["required"]
    )


@pytest.mark.parametrize("stream", ["raw_stdout", "raw_stderr", "both"])
@pytest.mark.parametrize("operation", ["missing", "null"])
def test_v2_never_fills_missing_or_null_raw_from_redacted(stream, operation):
    payload = _receipt().model_dump(mode="json")
    for field in ("raw_stdout", "raw_stderr") if stream == "both" else (stream,):
        if operation == "missing":
            del payload[field]
        else:
            payload[field] = None
    with pytest.raises(ValidationError):
        parse_owner_receipt(json.dumps(payload))


@pytest.mark.parametrize("stream", ["raw_stdout", "raw_stderr"])
def test_signer_rejects_partial_raw_pairs(stream):
    with pytest.raises(KernelError) as caught:
        sign_owner_receipt(
            process_id=PROCESS_ID,
            owner_identity=IDENTITY,
            state="running",
            sequence=1,
            owner_token=TOKEN,
            pid=123,
            started_at=NOW,
            stdout=empty_process_output(),
            stderr=empty_process_output(),
            **{stream: _raw()},
        )
    assert caught.value.code == "process_owner_receipt_invalid"


@pytest.mark.parametrize("stream", ["raw_stdout", "raw_stderr"])
@pytest.mark.parametrize("field", ["observed_bytes", "sha256", "eof"])
def test_original_mac_covers_each_raw_field(tmp_path, stream, field):
    receipt = _receipt()
    raw = getattr(receipt, stream)
    value = {"observed_bytes": raw.observed_bytes + 1, "sha256": "f" * 64, "eof": True}[field]
    forged = receipt.model_copy(update={stream: raw.model_copy(update={field: value})})
    # 篡改后的合同仍合法，因此失败必须来自原MAC，而非形状校验。
    parsed = parse_owner_receipt(forged.model_dump_json())
    assert owner_receipt.owner_receipt_mac(parsed, TOKEN) != receipt.mac
    with pytest.raises(KernelError) as caught:
        _verify(forged)
    assert caught.value.code == "process_owner_receipt_invalid"
    path = tmp_path / "receipt.json"
    write_owner_receipt(path, forged)
    with pytest.raises(KernelError) as caught:
        read_owner_receipt(path, owner_token=TOKEN, process_id=PROCESS_ID)
    assert caught.value.code == "process_owner_receipt_invalid"


@pytest.mark.parametrize(
    "changes", [{"observed_bytes": -1}, {"observed_bytes": True}, {"sha256": "f" * 63}, {"eof": 1}]
)
def test_signer_revalidates_copied_raw_instances(changes):
    with pytest.raises(ValidationError):
        sign_owner_receipt(
            process_id=PROCESS_ID,
            owner_identity=IDENTITY,
            state="running",
            sequence=1,
            owner_token=TOKEN,
            pid=123,
            started_at=NOW,
            stdout=empty_process_output(),
            stderr=empty_process_output(),
            raw_stdout=_raw(b"x").model_copy(update=changes),
            raw_stderr=_raw(),
        )


@pytest.mark.parametrize("v2", [False, True])
def test_version_upgrade_or_downgrade_cannot_reuse_original_mac(v2):
    receipt = _receipt(v2=v2)
    payload = receipt.model_dump(mode="json")
    if v2:
        payload["spec_version"] = "harnessix.process-owner-receipt/v1"
        del payload["raw_stdout"]
        del payload["raw_stderr"]
    else:
        payload["spec_version"] = "harnessix.process-owner-receipt/v2"
        payload["raw_stdout"] = _raw().model_dump()
        payload["raw_stderr"] = _raw().model_dump()
    forged = parse_owner_receipt(json.dumps(payload))
    assert owner_receipt.owner_receipt_mac(forged, TOKEN) != receipt.mac
    with pytest.raises(KernelError) as caught:
        _verify(forged)
    assert caught.value.code == "process_owner_receipt_invalid"


@pytest.mark.parametrize("version", [None, "harnessix.process-owner-receipt/v3", 2])
def test_explicit_parser_rejects_missing_or_unknown_versions(version):
    payload = _receipt(v2=False).model_dump(mode="json")
    if version is None:
        del payload["spec_version"]
    else:
        payload["spec_version"] = version
    with pytest.raises(ValueError):
        parse_owner_receipt(json.dumps(payload))


def test_version_only_tamper_with_retained_raw_is_rejected():
    forged = _receipt().model_copy(update={"spec_version": "harnessix.process-owner-receipt/v1"})
    with pytest.raises(KernelError) as caught:
        _verify(forged)
    assert caught.value.code == "process_owner_receipt_invalid"


@pytest.mark.parametrize(
    "body",
    [b"", b"{}", b"[]", b"null", b"{", b"\xff", b" " * (MAX_OWNER_RECEIPT_BYTES + 1)],
    ids=["empty", "missing-fields", "array", "null", "malformed", "non-utf8", "oversized"],
)
def test_parser_rejects_invalid_or_oversized_body(body):
    with pytest.raises(ValueError):
        parse_owner_receipt(body)


@pytest.mark.parametrize("v2", [False, True])
@pytest.mark.parametrize("binding", ["token", "process", "owner"])
def test_each_version_rejects_wrong_binding(tmp_path, v2, binding):
    receipt = _receipt(v2=v2)
    path = tmp_path / "receipt.json"
    write_owner_receipt(path, receipt)
    with pytest.raises(KernelError) as caught:
        read_owner_receipt(
            path,
            owner_token="f" * 64 if binding == "token" else TOKEN,
            process_id=uuid4() if binding == "process" else PROCESS_ID,
            owner_identity="f" * 64 if binding == "owner" else IDENTITY,
        )
    assert caught.value.code == "process_owner_receipt_invalid"


@pytest.mark.parametrize("token", ["g" * 64, "d" * 62])
def test_v2_retains_original_invalid_token_error(token):
    with pytest.raises(KernelError) as caught:
        verify_owner_receipt(_receipt(), owner_token=token, process_id=PROCESS_ID)
    assert caught.value.code == "process_owner_token_invalid"


@pytest.mark.parametrize("malformed_type", [False, True])
def test_write_and_verify_revalidate_copied_invalid_raw_without_creating_files(
    tmp_path, malformed_type
):
    receipt = _receipt()
    forged = receipt.model_copy(
        update={
            "raw_stdout": object()
            if malformed_type
            else receipt.raw_stdout.model_copy(update={"observed_bytes": -1})
        }
    )
    for operation in (
        lambda: _verify(forged),
        lambda: write_owner_receipt(tmp_path / "receipt.json", forged),
    ):
        with pytest.raises(KernelError) as caught:
            operation()
        assert caught.value.code == "process_owner_receipt_invalid"
    assert not list(tmp_path.iterdir())
