"""固定 Git 错误分支的纯字节侧车契约；不代表原生根因或 Owner 验收。"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from tests.governance import test_git_minimum_commit_probe as legacy_tests
from tests.product_config import git_minimum_commit_probe as probe_module

OLD_FIELDS = {
    "worker_failure_literal",
    "git_temp_create_prefix",
    "git_object_db_permission_prefix",
    "git_malformed_object_literal",
}
# 对应固定 tag 的 read/short-read、NULL vpath 包装、loose write/close 错误点。
BRANCHES = {
    "READ_ERROR": (b"error: read error while indexing <unknown>: ", False),
    "SHORT_READ": (b"error: short read while indexing <unknown>", True),
    "HASH_FD": (b"fatal: Unable to add (null) to database", True),
    "LOOSE_WRITE": (b"fatal: unable to write loose object file: ", False),
    "LOOSE_CLOSE": (b"fatal: error when closing loose object file: ", False),
}
BOUND = 1024 * 1024
CANARY = b"opaque-errno-not-a-path-secret-body-or-author\xff"


def _line(name: str) -> bytes:
    literal, exact = BRANCHES[name]
    return literal if exact else literal + CANARY


def _expected(*active: str) -> dict[str, bool]:
    return {name: name in active for name in OLD_FIELDS | BRANCHES.keys()}


def test_v4_retains_four_fields_and_adds_exactly_five_boolean_templates():
    assert set(probe_module._STDERR_LITERALS) == OLD_FIELDS | BRANCHES.keys()
    assert {name: probe_module._STDERR_LITERALS[name] for name in BRANCHES} == BRANCHES
    assert probe_module.MAX_STDERR_SIGNAL_BYTES == BOUND
    record = json.loads(probe_module.Probe("A").render().removeprefix(probe_module.PREFIX))
    assert record["schema"] == "harnessix.minimum-commit-probe/v4"


@pytest.mark.parametrize("name", BRANCHES)
@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_complete_branch_lines_project_only_fixed_boolean(name, ending):
    body = b"\xff noise\n" + _line(name) + ending + b"unfinished noise"
    result = probe_module._stderr_signals(body)
    assert result == _expected(name)
    assert all(type(value) is bool for value in result.values())
    assert CANARY.decode("ascii", errors="ignore") not in json.dumps(result)


@pytest.mark.parametrize("name", BRANCHES)
@pytest.mark.parametrize("case", ["hidden", "uppercase", "truncated", "no_lf", "bare_cr"])
def test_hidden_changed_or_incomplete_templates_never_match(name, case):
    literal, _ = BRANCHES[name]
    line = _line(name)
    body = {
        "hidden": b"noise " + line + b"\n",
        "uppercase": line.upper() + b"\n",
        "truncated": literal[:-1] + b"\n",
        "no_lf": line,
        "bare_cr": line + b"\r",
    }[case]
    assert probe_module._stderr_signals(body) == _expected()


@pytest.mark.parametrize("name", [name for name, (_, exact) in BRANCHES.items() if exact])
@pytest.mark.parametrize("extra", [b" extra", b"\r extra", b"\x00", b"\r\r"])
def test_exact_branches_reject_any_non_line_ending_suffix(name, extra):
    assert probe_module._stderr_signals(_line(name) + extra + b"\n") == _expected()


@pytest.mark.parametrize("name", [name for name, (_, exact) in BRANCHES.items() if not exact])
@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
def test_errno_templates_require_nonempty_suffix_without_extracting_it(name, ending):
    literal, _ = BRANCHES[name]
    assert probe_module._stderr_signals(literal + ending) == _expected()
    assert probe_module._stderr_signals(literal + b"\xff" + ending) == _expected(name)


@pytest.mark.parametrize(
    "body",
    [
        b"error: read error while indexing <stdin>: denied\n",
        b"error: short read while indexing stdin\n",
        b"fatal: Unable to add <stdin> to database\n",
        b"fatal: Unable to add named-file to database\n",
        b"fatal: unable to write loose object file\n",
        b"fatal: error when closing loose object file\n",
        b"Trace: fatal: Unable to add (null) to database\n",
        b"fatal: Unable to add (null) to database\xff\n",
    ],
)
def test_other_path_labels_or_free_text_never_expand_fixed_branch_authority(body):
    assert probe_module._stderr_signals(body) == _expected()


@pytest.mark.parametrize("name", BRANCHES)
def test_duplicate_lines_are_presence_not_request_or_error_counts(name):
    body = (_line(name) + b"\r\n") * 20
    assert probe_module._stderr_signals(body) == _expected(name)


def test_all_nine_fields_are_fixed_and_low_sensitivity_with_large_noise():
    old_lines = (
        b"git_material_worker_failed\n"
        b"error: unable to create temporary file: opaque\n"
        b"error: insufficient permission for adding an object to repository database opaque\n"
        b"fatal: refusing to create malformed object\n"
    )
    body = b"noise\xff\n" * 50000 + old_lines
    body += b"\n".join(_line(name) for name in BRANCHES) + b"\n"
    result = probe_module._stderr_signals(body)
    assert result == dict.fromkeys(OLD_FIELDS | BRANCHES.keys(), True)
    assert len(json.dumps(result)) < 400
    assert "opaque" not in json.dumps(result)


@pytest.mark.parametrize("name", BRANCHES)
def test_signal_after_large_unterminated_noise_is_not_a_new_line(name):
    body = b"noise" * 120000 + _line(name) + b"\n"
    assert probe_module._stderr_signals(body) == _expected()


@pytest.mark.parametrize("name", BRANCHES)
def test_last_complete_line_at_raw_bound_is_observed_but_oversize_is_not(name):
    line = _line(name) + b"\n"
    body = b"x" * (BOUND - len(line) - 1) + b"\n" + line
    assert len(body) == BOUND
    assert probe_module._stderr_signals(body) == _expected(name)
    assert probe_module._stderr_signals(body + b"\n") == _expected()


def test_projection_has_no_decoding_io_regex_or_free_message_conversion():
    tree = ast.parse(inspect.getsource(probe_module._stderr_signals))
    attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert attributes == {"find", "startswith", "items"}
    assert not any(isinstance(node, (ast.Await, ast.AsyncWith)) for node in ast.walk(tree))
    names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert names <= {"len"}


def _post(monkeypatch, boundary="none"):
    # 仅用桩核验原次数/顺序和真实 raw 守卫，不伪造 MAC 或 Windows 通过。
    stderr = b"\n".join(_line(name) for name in BRANCHES) + b"\n"
    probe, operation, calls = legacy_tests._signal_post_operation(monkeypatch, boundary, stderr)
    return probe, operation, calls, stderr


@pytest.mark.parametrize(
    "boundary", ["none", "receipt", "stdout_raw", "stderr_raw", "protection", "projection"]
)
async def test_all_branches_remain_after_original_guards_with_one_pair_of_raw_reads(
    monkeypatch, boundary
):
    from harnessix.delivery import git_material_failure

    probe, operation, calls, stderr = _post(monkeypatch, boundary)
    decoded = []
    original_decoder = git_material_failure.decode_failure_observation

    def decoder(body):
        assert body is stderr
        decoded.append(True)
        return original_decoder(body)

    monkeypatch.setattr(git_material_failure, "decode_failure_observation", decoder)
    lease = operation.handle.lease
    await probe.post_settlement()
    await probe.post_settlement()
    assert calls.count("receipt") == 1
    assert calls.count("stdout") == calls.count("stderr") == (boundary != "receipt")
    assert calls.count("raw") == {"receipt": 0, "stdout_raw": 1}.get(boundary, 2)
    assert calls.count("protection") == (boundary in {"none", "protection", "projection"})
    assert calls.count("signals") == (boundary in {"none", "projection"})
    assert len(decoded) == (boundary == "none")
    assert operation.handle.lease is lease and lease.returncode == 2
    assert operation.data["original_operation_returned"] is False
    assert probe.incomplete is (boundary != "none")
    if boundary == "none":
        assert calls == ["receipt", "stdout", "stderr", "raw", "raw", "protection", "signals"]
        assert operation.data["post_stderr_signals"] == _expected(*BRANCHES)
        assert operation.data["post_status"] == "raw_verified_only"
        assert operation.data["post_proof"] == {"status": "absent"}
    else:
        assert "post_stderr_signals" not in operation.data
    assert "opaque" not in probe.render()


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("corruption", ["length", "digest", "eof", "limit"])
async def test_raw_integrity_failure_never_reaches_new_projection(monkeypatch, stream, corruption):
    probe, operation, calls, _ = _post(monkeypatch)
    original_receipt = operation.handle._terminal_owner_receipt

    async def corrupt_receipt():
        receipt = await original_receipt()
        raw = getattr(receipt, f"raw_{stream}")
        if corruption == "length":
            raw.observed_bytes += 1
        elif corruption == "digest":
            raw.sha256 = "0" * 64
        elif corruption == "eof":
            raw.eof = False
        else:
            raw.observed_bytes = BOUND + 1
        return receipt

    operation.handle._terminal_owner_receipt = corrupt_receipt
    await probe.post_settlement()
    await probe.post_settlement()
    assert calls.count("receipt") == calls.count("stdout") == calls.count("stderr") == 1
    assert calls.count("raw") == (1 if stream == "stdout" else 2)
    assert "protection" not in calls and "signals" not in calls
    assert "post_stderr_signals" not in operation.data
    assert probe.incomplete and operation.data["original_operation_returned"] is False


@pytest.mark.parametrize("boundary", ["signals", "decoder"])
async def test_projection_callback_or_decoder_cancellation_preserves_identity_and_no_replay(
    monkeypatch, boundary
):
    from harnessix.delivery import git_material_failure

    probe, operation, calls, stderr = _post(monkeypatch)
    error = asyncio.CancelledError("fake cancellation")
    cancelled = []

    def cancel(body):
        assert body is stderr
        cancelled.append(True)
        raise error

    if boundary == "signals":
        monkeypatch.setattr(probe_module, "_stderr_signals", cancel)
    else:
        monkeypatch.setattr(git_material_failure, "decode_failure_observation", cancel)
    with pytest.raises(asyncio.CancelledError) as raised:
        await probe.post_settlement()
    assert raised.value is error
    await probe.post_settlement()
    assert cancelled == [True]
    assert calls.count("receipt") == calls.count("stdout") == calls.count("stderr") == 1
    assert calls.count("raw") == 2 and calls.count("protection") == 1
    assert operation.data["original_operation_returned"] is False
    assert "post_worker_failure_status" not in operation.data


@pytest.mark.parametrize("kind", ["unknown", "turn_cancelled", "task_cancelled"])
async def test_original_unknown_or_cancellation_is_not_replaced_by_branch_diagnostics(
    monkeypatch, kind
):
    probe, operation, calls, _ = _post(monkeypatch)
    error = {
        "unknown": KernelError("git_material_effect_unknown", "fake original unknown"),
        "turn_cancelled": TurnCancelled("fake original turn cancellation"),
        "task_cancelled": asyncio.CancelledError("fake original task cancellation"),
    }[kind]
    business_calls = []

    async def original():
        business_calls.append(True)
        raise error

    token = probe_module._ACTIVE.set(operation)
    try:
        with pytest.raises(type(error)) as raised:
            await probe_module._async_wrapper(original, "operation")()
        assert raised.value is error
    finally:
        probe_module._ACTIVE.reset(token)
    await probe.post_settlement()
    await probe.post_settlement()
    assert business_calls == [True] and probe_module._ACTIVE.get() is None
    assert calls.count("receipt") == calls.count("stdout") == calls.count("stderr") == 1
    assert operation.data["original_operation_returned"] is False
    assert operation.data["post_stderr_signals"] == _expected(*BRANCHES)


async def test_decoder_fault_stays_diagnostic_and_never_replays_or_exposes_payload(monkeypatch):
    from harnessix.delivery import git_material_failure

    probe, operation, calls, stderr = _post(monkeypatch)

    def broken_decoder(body):
        assert body is stderr
        calls.append("decoder")
        raise ValueError("opaque-not-for-record")

    monkeypatch.setattr(git_material_failure, "decode_failure_observation", broken_decoder)
    await probe.post_settlement()
    await probe.post_settlement()
    assert calls.count("decoder") == 1
    assert calls.count("receipt") == calls.count("stdout") == calls.count("stderr") == 1
    assert probe.incomplete and "post_worker_failure_status" not in operation.data
    assert operation.data["original_operation_returned"] is False
    assert "opaque" not in probe.render()
