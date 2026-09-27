"""按持久用途核验正式二进制双流，不以Base64字面值检查授权解码结果。"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.publication import protect_binary_jsonl, protect_jsonl
from harnessix.artifacts.binary_projection import decode_process_artifact
from harnessix.artifacts.publication import ArtifactPublicationGuard
from harnessix.processes.contracts import ProcessResult, ProcessStream
from harnessix.processes.output_artifact import process_output_document
from harnessix.processes.trusted_output import build_trusted_process_output
from tests.agent.test_publication import CANARY, protected
from tests.processes.test_trusted_output import lease


def binary_document(purpose: str, stdout: bytes, stderr: bytes = b"") -> bytes:
    if purpose == "action_output":
        return build_trusted_process_output(
            "unit-tests", lease(stdout, stderr), stdout, stderr
        ).to_jsonl()

    def stream(data):
        return ProcessStream(
            data_base64=base64.b64encode(data).decode(),
            captured_bytes=len(data),
            observed_bytes=len(data),
            observed_sha256=hashlib.sha256(data).hexdigest(),
            truncated=False,
            eof=True,
        )

    document = process_output_document(
        ProcessResult(
            pid=1234,
            returncode=0,
            stop_reason="exited",
            termination="none",
            stdout=stream(stdout),
            stderr=stream(stderr),
            elapsed_seconds=0.1,
        )
    )
    assert document is not None
    return document.to_jsonl()


@pytest.mark.parametrize("purpose", ["action_output", "process_output"])
@pytest.mark.parametrize("prefix", [0, 1, 2, 12280])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_formal_output_rejects_alignment_and_same_stream_chunk_split(purpose, prefix, stream):
    payload = b"\xff" * prefix + CANARY.encode() + b"\0\n"
    body = binary_document(
        purpose, payload if stream == "stdout" else b"", payload if stream == "stderr" else b""
    )
    with protected() as scope:
        with pytest.raises(KernelError) as caught:
            await ArtifactPublicationGuard(scope).check_body(body, purpose=purpose)
        assert caught.value.code == "public_output_secret_leak"
        assert CANARY not in str(caught.value)


@pytest.mark.parametrize("purpose", ["action_output", "process_output"])
async def test_binary_streams_are_not_text_or_concatenated_to_each_other(purpose):
    split = len(CANARY) // 2
    stdout, stderr = CANARY[:split].encode() + b"\xff", CANARY[split:].encode() + b"\0"
    body = binary_document(purpose, stdout, stderr)
    with protected() as scope:
        await ArtifactPublicationGuard(scope).check_body(body, purpose=purpose)
    assert decode_process_artifact(body, purpose, lambda: None) == (stdout, stderr)


@pytest.mark.parametrize("purpose", ["action_output", "process_output"])
@pytest.mark.parametrize("mode", ["offset", "hash", "base64", "duplicate", "wrong-purpose"])
async def test_contract_tampering_is_denied_before_publication(purpose, mode):
    body = binary_document(purpose, b"benign\xff")
    if mode == "offset":
        body = body.replace(b'"offset":0', b'"offset":1')
    elif mode == "hash":
        body = body.replace(hashlib.sha256(b"benign\xff").hexdigest().encode(), b"0" * 64)
    elif mode == "base64":
        body = body.replace(base64.b64encode(b"benign\xff"), b"not=base64")
    elif mode == "duplicate":
        body = body.replace(b'"kind":"chunk"', b'"kind":"summary","kind":"chunk"')
    else:
        purpose = "process_output" if purpose == "action_output" else "action_output"
    with protected() as scope:
        with pytest.raises(KernelError) as caught:
            await ArtifactPublicationGuard(scope).check_body(body, purpose=purpose)
        assert caught.value.code == "public_output_protection_failed"


async def test_generic_json_does_not_guess_encoding_and_missing_capability_does_not_downgrade():
    body = (
        json.dumps({"data_base64": base64.b64encode(b"x" + CANARY.encode()).decode()}) + "\n"
    ).encode()
    with protected() as scope:
        await protect_jsonl(scope, body, CancelToken())

    class GenericOnly:
        def assert_public_json(self, *args, **kwargs):
            raise AssertionError("不应使用普通结果替代二进制契约")

        def assert_public_jsonl(self, *args, **kwargs):
            raise AssertionError("不应静默降级")

    with pytest.raises(KernelError) as caught:
        await ArtifactPublicationGuard(GenericOnly()).check_body(b"{}\n", purpose="action_output")
    assert caught.value.code == "public_output_binary_capability_missing"
    await ArtifactPublicationGuard(None).check_body(b"old opaque body", purpose="action_output")
    with pytest.raises(ValueError, match="用途"):
        decode_process_artifact(b"{}\n", "unknown", lambda: None)


@pytest.mark.parametrize(
    "value", [[b"safe"], (b"a", b"b", b"c"), (bytearray(b"safe"),), (b"x" * (1024 * 1024 + 1),)]
)
async def test_decoder_return_budget_is_exact_and_bounded(value):
    with protected() as scope:
        with pytest.raises(KernelError) as caught:
            await protect_binary_jsonl(scope, b"{}\n", lambda *_: value, CancelToken())
        assert caught.value.code == "public_output_limit"


@pytest.mark.parametrize("mode", ["work", "cancel", "timeout", "diagnostic", "closed"])
async def test_original_and_decoded_bytes_share_work_cancel_and_time(monkeypatch, mode):
    from harnessix.agent import publication as boundary
    from harnessix.secrets import publication

    token = CancelToken()
    clock = [boundary.monotonic()]
    monkeypatch.setattr(boundary, "monotonic", lambda: clock[0])
    called = []

    def decoder(body, checkpoint):
        called.append(body)
        if mode == "work":
            monkeypatch.setattr(publication, "MAX_SCAN_WORK", 3)
        elif mode == "cancel":
            token.cancel()
        elif mode == "timeout":
            clock[0] += 20
        elif mode == "diagnostic":
            raise ValueError(CANARY)
        checkpoint()
        return (b"safe\xff", b"")

    with protected() as scope:
        if mode == "closed":
            scope.close()
        with pytest.raises(TurnCancelled if mode == "cancel" else KernelError) as caught:
            await protect_binary_jsonl(scope, b"{}\n", decoder, token)
        if mode != "cancel":
            assert (
                caught.value.code
                == {
                    "work": "public_output_limit",
                    "timeout": "public_output_timeout",
                    "diagnostic": "public_output_protection_failed",
                    "closed": "public_output_secret_unavailable",
                }[mode]
            )
            assert CANARY not in str(caught.value)
        assert bool(called) == (mode != "closed")


async def test_decoded_scan_does_not_reset_original_work_counter(monkeypatch):
    from harnessix.secrets import publication

    observed = []
    original = publication._ScanBudget.scan_bytes

    def record(budget, encoded, patterns, **kwargs):
        observed.append((id(budget), budget.work))
        return original(budget, encoded, patterns, **kwargs)

    monkeypatch.setattr(publication._ScanBudget, "scan_bytes", record)
    with protected() as scope:
        await protect_binary_jsonl(
            scope, b'{"safe":"yes"}\n', lambda *_: (b"ok\xff", b""), CancelToken()
        )
    assert len({identity for identity, _ in observed}) == 1
    assert observed[-2][1] > observed[0][1]
