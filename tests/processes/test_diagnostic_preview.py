"""诊断预览只优化模型往返，不改变原始流、退出结论或历史摘要。

v2（1 KiB前缀）只重建已审计的历史结果；新结果使用v3（每流最多8 KiB尾部）。
"""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from harnessix.execution.contracts import canonical_digest
from harnessix.processes.public_output import (
    MAX_PROCESS_PREVIEW_STREAM_BYTES,
    MAX_PROCESS_TAIL_PREVIEW_STREAM_BYTES,
    PublicEvalOutputSummary,
    PublicProcessOutputSummary,
    PublicProcessOutputSummaryV2,
    PublicProcessOutputSummaryV3,
)
from harnessix.processes.trusted_output import (
    build_trusted_process_output,
    parse_trusted_process_output,
    recorded_process_public_output,
    trusted_process_public_output,
)
from tests.processes.test_trusted_output import lease, observation


def document(stdout: bytes, stderr: bytes = b""):
    return build_trusted_process_output("unit-tests", lease(stdout, stderr), stdout, stderr)


@pytest.mark.parametrize("returncode", [0, 1, -15])
def test_preview_preserves_original_binary_document_and_exit_facts(returncode):
    stdout, stderr = "检查失败：保留回归测试\n".encode(), b"AssertionError\n"
    original = build_trusted_process_output(
        "unit-tests", lease(stdout, stderr, returncode=returncode), stdout, stderr
    )
    body = original.to_jsonl()
    legacy = original.summary.public_output()
    public = trusted_process_public_output(original, legacy_preview=True)

    assert trusted_process_public_output(original) == legacy
    assert public["version"] == "trusted-process-output/v2"
    assert PublicProcessOutputSummary.model_validate(legacy).returncode == returncode
    assert PublicProcessOutputSummaryV2.model_validate(public).returncode == returncode
    for name, data in [("stdout", stdout), ("stderr", stderr)]:
        assert public["diagnostic_preview"][name] == {
            "text": data.decode(),
            "size_bytes": len(data),
            "truncated": False,
        }
    assert original.to_jsonl() == body
    assert parse_trusted_process_output(body) == original
    assert original.summary.version == "trusted-process-output/v1"


@pytest.mark.parametrize("prefix", [0, 1, 2])
def test_preview_cuts_multibyte_text_without_replacement_or_overflow(prefix):
    stdout = b"x" * prefix + "中".encode() * MAX_PROCESS_PREVIEW_STREAM_BYTES
    stderr = b"y" * (MAX_PROCESS_PREVIEW_STREAM_BYTES + 1)
    public = trusted_process_public_output(document(stdout, stderr), legacy_preview=True)
    checked = PublicProcessOutputSummaryV2.model_validate(public)
    for name, data in [("stdout", stdout), ("stderr", stderr)]:
        preview = getattr(checked.diagnostic_preview, name)
        assert preview.text is not None and "\ufffd" not in preview.text
        assert 0 < preview.size_bytes <= MAX_PROCESS_PREVIEW_STREAM_BYTES
        assert data.startswith(preview.text.encode())
        assert preview.truncated
    assert checked.complete  # 归档完整与模型预览截断不是同一事实。


@pytest.mark.parametrize("unsafe", [b"\xff", b"\x00", b"\x1b", b"\x7f", "\u202e".encode()])
@pytest.mark.parametrize("prefix", [0, MAX_PROCESS_PREVIEW_STREAM_BYTES + 1])
def test_preview_does_not_hide_binary_or_control_bytes_beyond_visible_prefix(unsafe, prefix):
    data = b"a" * prefix + unsafe
    original = document(data)
    public = trusted_process_public_output(original, legacy_preview=True)
    assert public["diagnostic_preview"]["stdout"] == {
        "text": None,
        "size_bytes": 0,
        "truncated": True,
    }
    assert b"".join(chunk.data() for chunk in original.chunks) == data
    PublicProcessOutputSummaryV2.model_validate(public)


def test_empty_and_whitespace_streams_remain_truthful_text():
    public = trusted_process_public_output(document(b"", b"\t\r\n"), legacy_preview=True)
    assert public["diagnostic_preview"]["stdout"] == {
        "text": "",
        "size_bytes": 0,
        "truncated": False,
    }
    assert public["diagnostic_preview"]["stderr"]["text"] == "\t\r\n"
    PublicProcessOutputSummaryV2.model_validate(public)


def test_preview_truncation_uses_observed_not_only_archived_or_persisted_bytes():
    persisted = b"known prefix\n"
    observed = persisted + b"not persisted\n"
    terminal = lease(persisted, b"").model_copy(
        update={"stdout": observation(persisted, observed=observed, eof=False)}
    )
    original = build_trusted_process_output("unit-tests", terminal, persisted, b"")
    public = trusted_process_public_output(original, legacy_preview=True)
    assert not public["complete"]
    assert public["diagnostic_preview"]["stdout"] == {
        "text": persisted.decode(),
        "size_bytes": len(persisted),
        "truncated": True,
    }
    PublicProcessOutputSummaryV2.model_validate(public)


@pytest.mark.parametrize(
    "case", ["missing", "extra", "size", "bytes", "oversize", "truncated", "binary", "control"]
)
def test_public_v2_contract_rejects_forged_or_unbounded_previews(case):
    public = copy.deepcopy(trusted_process_public_output(document(b"ok\n"), legacy_preview=True))
    preview = public["diagnostic_preview"]["stdout"]
    if case == "missing":
        del public["diagnostic_preview"]
    elif case == "extra":
        preview["path"] = "private"
    elif case == "size":
        preview["size_bytes"] = 4
    elif case == "bytes":
        preview.update(text="中", size_bytes=1)
    elif case == "oversize":
        preview.update(text="x" * 1025, size_bytes=1025)
    elif case == "truncated":
        preview["truncated"] = True
    elif case == "binary":
        preview["text"] = None
    else:
        preview.update(text="\x00\n\r", size_bytes=3)
    with pytest.raises(ValidationError):
        PublicProcessOutputSummaryV2.model_validate(public)


def test_v1_and_eval_shapes_are_closed_and_never_silently_upgraded():
    original = document(b"ok\n")
    legacy = trusted_process_public_output(original)
    v2 = trusted_process_public_output(original, legacy_preview=True)
    evaluation = trusted_process_public_output(original, include_passed=True)
    PublicProcessOutputSummary.model_validate(legacy)
    PublicEvalOutputSummary.model_validate(evaluation)
    with pytest.raises(ValidationError):
        PublicProcessOutputSummary.model_validate(v2)
    with pytest.raises(ValidationError):
        PublicEvalOutputSummary.model_validate({**v2, "passed": True})
    for flag in ("include_preview", "legacy_preview"):
        with pytest.raises(ValueError, match="Eval v1"):
            trusted_process_public_output(original, include_passed=True, **{flag: True})
    with pytest.raises(ValueError, match="一个预览版本"):
        trusted_process_public_output(original, include_preview=True, legacy_preview=True)
    assert "diagnostic_preview" not in legacy
    assert len(json.dumps(v2, ensure_ascii=False).encode()) < 4096


TAIL = MAX_PROCESS_TAIL_PREVIEW_STREAM_BYTES


def tail_preview(stdout: bytes, stderr: bytes = b"") -> PublicProcessOutputSummaryV3:
    public = trusted_process_public_output(document(stdout, stderr), include_preview=True)
    assert public["version"] == "trusted-process-output/v3"
    return PublicProcessOutputSummaryV3.model_validate(public)


def test_v3_keeps_the_end_of_a_long_stream_where_the_traceback_is() -> None:
    """v2只给前1 KiB，pytest失败摘要与Traceback末行常在其后；v3给每流最后8 KiB。"""
    head = b"".join(f"collected line {i}\n".encode() for i in range(2000))
    traceback = b"Traceback (most recent call last):\n  ...\nAssertionError: expected 2\n"
    summary = b"FAILED tests/test_x.py::test_y - AssertionError\n1 failed, 99 passed\n"
    stdout = head + traceback + summary
    checked = tail_preview(stdout, b"E   assert 1 == 2\n")
    preview = checked.diagnostic_preview.stdout
    assert preview.size_bytes == TAIL and preview.truncated
    assert preview.offset == len(stdout) - TAIL
    assert preview.text is not None and stdout.endswith(preview.text.encode())
    assert preview.text.endswith(traceback.decode() + summary.decode())
    assert stdout[preview.offset :] == preview.text.encode()
    stderr = checked.diagnostic_preview.stderr
    assert (stderr.text, stderr.offset, stderr.size_bytes, stderr.truncated) == (
        "E   assert 1 == 2\n",
        0,
        18,
        False,
    )
    assert checked.complete


@pytest.mark.parametrize("size", [0, 1, TAIL - 1, TAIL])
def test_v3_short_streams_are_shown_whole_from_offset_zero(size: int) -> None:
    preview = tail_preview(b"a" * size).diagnostic_preview.stdout
    assert (preview.text, preview.offset, preview.size_bytes, preview.truncated) == (
        "a" * size,
        0,
        size,
        False,
    )


@pytest.mark.parametrize("prefix", [0, 1, 2])
def test_v3_tail_starts_on_a_complete_utf8_character(prefix: int) -> None:
    stdout = "中".encode() * TAIL + b"x" * prefix
    preview = tail_preview(stdout).diagnostic_preview.stdout
    assert preview.text is not None and "\ufffd" not in preview.text
    assert TAIL - 2 <= preview.size_bytes <= TAIL
    assert stdout[preview.offset :] == preview.text.encode()
    assert preview.truncated


@pytest.mark.parametrize("unsafe", [b"\xff", b"\x00", b"\x1b", b"\x7f", "\u202e".encode()])
@pytest.mark.parametrize("where", ["before-tail", "inside-tail"])
def test_v3_does_not_hide_binary_or_control_bytes_outside_the_visible_tail(unsafe, where):
    data = unsafe + b"a" * (TAIL + 1) if where == "before-tail" else b"a" * 10 + unsafe
    preview = tail_preview(data).diagnostic_preview.stdout
    assert (preview.text, preview.offset, preview.size_bytes, preview.truncated) == (
        None,
        0,
        0,
        True,
    )


def test_v3_truncation_uses_observed_bytes_and_offset_stays_inside_persisted() -> None:
    persisted = b"known prefix\n"
    terminal = lease(persisted, b"").model_copy(
        update={"stdout": observation(persisted, observed=persisted + b"lost\n", eof=False)}
    )
    original = build_trusted_process_output("unit-tests", terminal, persisted, b"")
    public = trusted_process_public_output(original, include_preview=True)
    checked = PublicProcessOutputSummaryV3.model_validate(public)
    assert not checked.complete
    preview = checked.diagnostic_preview.stdout
    assert (preview.text, preview.offset, preview.truncated) == (persisted.decode(), 0, True)


def test_v3_worst_case_result_stays_inside_the_model_view_budget() -> None:
    """两流满8 KiB且全为需JSON转义的字符时，仍远低于默认64 KiB单结果视图上限。"""
    public = trusted_process_public_output(
        document(b'"' * (3 * TAIL), b"\\" * (3 * TAIL)), include_preview=True
    )
    PublicProcessOutputSummaryV3.model_validate(public)
    encoded = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert 4 * TAIL < len(encoded.encode()) < 4 * TAIL + 2048 < 64 * 1024


@pytest.mark.parametrize(
    "case",
    [
        "missing-offset",
        "offset-beyond-persisted",
        "oversize",
        "size",
        "truncated",
        "null-with-offset",
        "control",
        "extra",
        "v2-shape",
    ],
)
def test_v3_contract_rejects_forged_or_unbounded_previews(case: str) -> None:
    public = copy.deepcopy(trusted_process_public_output(document(b"ok\n"), include_preview=True))
    preview = public["diagnostic_preview"]["stdout"]
    if case == "missing-offset":
        del preview["offset"]
    elif case == "offset-beyond-persisted":
        preview["offset"] = 1
    elif case == "oversize":
        preview.update(text="x" * (TAIL + 1), size_bytes=TAIL + 1)
    elif case == "size":
        preview["size_bytes"] = 4
    elif case == "truncated":
        preview["truncated"] = True
    elif case == "null-with-offset":
        preview.update(text=None, size_bytes=0, offset=1)
    elif case == "control":
        preview.update(text="\x00\n\r", size_bytes=3)
    elif case == "extra":
        preview["path"] = "private"
    else:
        for stream in public["diagnostic_preview"].values():
            del stream["offset"]
        public["version"] = "trusted-process-output/v2"
        PublicProcessOutputSummaryV2.model_validate(public)
        public["version"] = "trusted-process-output/v3"
    with pytest.raises(ValidationError):
        PublicProcessOutputSummaryV3.model_validate(public)


def test_v2_and_v3_are_distinct_closed_versions() -> None:
    original = document(b"ok\n")
    v2 = trusted_process_public_output(original, legacy_preview=True)
    v3 = trusted_process_public_output(original, include_preview=True)
    with pytest.raises(ValidationError):
        PublicProcessOutputSummaryV2.model_validate(v3)
    with pytest.raises(ValidationError):
        PublicProcessOutputSummaryV3.model_validate(v2)
    with pytest.raises(ValidationError):
        PublicProcessOutputSummary.model_validate(v3)


@pytest.mark.parametrize("recorded", ["v3", "v2", "v1", "other"])
def test_recorded_output_is_chosen_by_the_audited_hash_without_upgrading_history(recorded):
    original = document(b"x" * (2 * TAIL), b"diagnostic\n")
    current = trusted_process_public_output(original, include_preview=True)
    shapes = {
        "v3": current,
        "v2": trusted_process_public_output(original, legacy_preview=True),
        "v1": trusted_process_public_output(original),
        "other": trusted_process_public_output(document(b"other\n")),
    }
    found = recorded_process_public_output(original, current, canonical_digest(shapes[recorded]))
    assert found == (None if recorded == "other" else shapes[recorded])


def test_eval_shape_has_no_historical_preview_candidates() -> None:
    original = document(b"ok\n")
    evaluation = trusted_process_public_output(original, include_passed=True)
    v1 = trusted_process_public_output(original)
    assert recorded_process_public_output(original, evaluation, canonical_digest(v1)) is None
    assert (
        recorded_process_public_output(original, evaluation, canonical_digest(evaluation))
        == evaluation
    )
