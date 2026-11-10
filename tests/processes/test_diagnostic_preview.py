"""诊断预览只优化模型往返，不改变原始流、退出结论或历史摘要。

v2（1 KiB前缀）与v3（8 KiB尾部）只重建已审计的历史结果；新结果使用v4（去除终端转义序列后
每流最多8 KiB尾部）。
"""

from __future__ import annotations

import copy
import json
import re

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
    PublicProcessOutputSummaryV4,
)
from harnessix.processes.trusted_output import (
    build_trusted_process_output,
    parse_trusted_process_output,
    recorded_process_public_output,
    trusted_process_public_output,
)
from tests.processes.test_trusted_output import lease, observation

V2 = "trusted-process-output/v2"
V3 = "trusted-process-output/v3"


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
    public = trusted_process_public_output(original, legacy_preview=V2)

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
    public = trusted_process_public_output(document(stdout, stderr), legacy_preview=V2)
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
    public = trusted_process_public_output(original, legacy_preview=V2)
    assert public["diagnostic_preview"]["stdout"] == {
        "text": None,
        "size_bytes": 0,
        "truncated": True,
    }
    assert b"".join(chunk.data() for chunk in original.chunks) == data
    PublicProcessOutputSummaryV2.model_validate(public)


def test_empty_and_whitespace_streams_remain_truthful_text():
    public = trusted_process_public_output(document(b"", b"\t\r\n"), legacy_preview=V2)
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
    public = trusted_process_public_output(original, legacy_preview=V2)
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
    public = copy.deepcopy(trusted_process_public_output(document(b"ok\n"), legacy_preview=V2))
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
    v2 = trusted_process_public_output(original, legacy_preview=V2)
    evaluation = trusted_process_public_output(original, include_passed=True)
    PublicProcessOutputSummary.model_validate(legacy)
    PublicEvalOutputSummary.model_validate(evaluation)
    with pytest.raises(ValidationError):
        PublicProcessOutputSummary.model_validate(v2)
    with pytest.raises(ValidationError):
        PublicEvalOutputSummary.model_validate({**v2, "passed": True})
    for flag, value in (("include_preview", True), ("legacy_preview", V2), ("legacy_preview", V3)):
        with pytest.raises(ValueError, match="Eval v1"):
            trusted_process_public_output(original, include_passed=True, **{flag: value})
    with pytest.raises(ValueError, match="一个预览版本"):
        trusted_process_public_output(original, include_preview=True, legacy_preview=V3)
    assert "diagnostic_preview" not in legacy
    assert len(json.dumps(v2, ensure_ascii=False).encode()) < 4096


TAIL = MAX_PROCESS_TAIL_PREVIEW_STREAM_BYTES


def tail_preview(stdout: bytes, stderr: bytes = b"") -> PublicProcessOutputSummaryV3:
    """v3只用于重建已审计历史；以下v3用例确认其形状与可见性规则保持不变。"""
    public = trusted_process_public_output(document(stdout, stderr), legacy_preview=V3)
    assert public["version"] == V3
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


@pytest.mark.parametrize("version", ["v3", "v4"])
def test_tail_truncation_uses_observed_bytes_and_offset_stays_inside_persisted(version) -> None:
    persisted = b"known prefix\n"
    terminal = lease(persisted, b"").model_copy(
        update={"stdout": observation(persisted, observed=persisted + b"lost\n", eof=False)}
    )
    original = build_trusted_process_output("unit-tests", terminal, persisted, b"")
    if version == "v3":
        public = trusted_process_public_output(original, legacy_preview=V3)
        checked = PublicProcessOutputSummaryV3.model_validate(public)
    else:
        public = trusted_process_public_output(original, include_preview=True)
        checked = PublicProcessOutputSummaryV4.model_validate(public)
    assert not checked.complete
    preview = checked.diagnostic_preview.stdout
    assert (preview.text, preview.offset, preview.truncated) == (persisted.decode(), 0, True)


@pytest.mark.parametrize("version", ["v3", "v4"])
def test_tail_worst_case_result_stays_inside_the_model_view_budget(version) -> None:
    """两流满8 KiB且全为需JSON转义的字符时，仍远低于默认64 KiB单结果视图上限。"""
    original = document(b'"' * (3 * TAIL), b"\\" * (3 * TAIL))
    if version == "v3":
        public = trusted_process_public_output(original, legacy_preview=V3)
        PublicProcessOutputSummaryV3.model_validate(public)
    else:
        public = trusted_process_public_output(original, include_preview=True)
        PublicProcessOutputSummaryV4.model_validate(public)
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
    public = copy.deepcopy(trusted_process_public_output(document(b"ok\n"), legacy_preview=V3))
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
        public["version"] = V2
        PublicProcessOutputSummaryV2.model_validate(public)
        public["version"] = V3
    with pytest.raises(ValidationError):
        PublicProcessOutputSummaryV3.model_validate(public)


def test_v2_v3_and_v4_are_distinct_closed_versions() -> None:
    original = document(b"ok\n")
    shapes = {
        PublicProcessOutputSummaryV2: trusted_process_public_output(original, legacy_preview=V2),
        PublicProcessOutputSummaryV3: trusted_process_public_output(original, legacy_preview=V3),
        PublicProcessOutputSummaryV4: trusted_process_public_output(original, include_preview=True),
    }
    for model, public in shapes.items():
        model.model_validate(public)
        with pytest.raises(ValidationError):
            PublicProcessOutputSummary.model_validate(public)
        for other in shapes:
            if other is not model:
                with pytest.raises(ValidationError):
                    other.model_validate(public)


@pytest.mark.parametrize("recorded", ["v4", "v3", "v2", "v1", "other"])
def test_recorded_output_is_chosen_by_the_audited_hash_without_upgrading_history(recorded):
    # stderr带颜色码：v3整流不可显示而v4去除后可显示，两版公开形状及Hash必然不同。
    original = document(b"x" * (2 * TAIL), b"\x1b[31mdiagnostic\x1b[0m\n")
    current = trusted_process_public_output(original, include_preview=True)
    assert current["version"] == "trusted-process-output/v4"
    shapes = {
        "v4": current,
        "v3": trusted_process_public_output(original, legacy_preview=V3),
        "v2": trusted_process_public_output(original, legacy_preview=V2),
        "v1": trusted_process_public_output(original),
        "other": trusted_process_public_output(document(b"other\n")),
    }
    found = recorded_process_public_output(original, current, canonical_digest(shapes[recorded]))
    assert found == (None if recorded == "other" else shapes[recorded])
    assert shapes["v3"]["diagnostic_preview"]["stderr"]["text"] is None
    assert shapes["v4"]["diagnostic_preview"]["stderr"]["text"] == "diagnostic\n"


@pytest.mark.parametrize("recorded", ["v4", "v3", "v2", "v1"])
def test_recorded_legacy_output_never_tries_a_newer_preview(recorded):
    """以旧版本作为当前形状时，只回退到更早版本；不会把旧终态升级为较新的预览。"""
    original = document(b"ok\n")
    v3 = trusted_process_public_output(original, legacy_preview=V3)
    shapes = {
        "v4": trusted_process_public_output(original, include_preview=True),
        "v3": v3,
        "v2": trusted_process_public_output(original, legacy_preview=V2),
        "v1": trusted_process_public_output(original),
    }
    found = recorded_process_public_output(original, v3, canonical_digest(shapes[recorded]))
    assert found == (None if recorded == "v4" else shapes[recorded])


def test_eval_shape_has_no_historical_preview_candidates() -> None:
    original = document(b"ok\n")
    evaluation = trusted_process_public_output(original, include_passed=True)
    v1 = trusted_process_public_output(original)
    assert recorded_process_public_output(original, evaluation, canonical_digest(v1)) is None
    assert (
        recorded_process_public_output(original, evaluation, canonical_digest(evaluation))
        == evaluation
    )


def v4_preview(stdout: bytes, stderr: bytes = b"") -> PublicProcessOutputSummaryV4:
    public = trusted_process_public_output(document(stdout, stderr), include_preview=True)
    assert public["version"] == "trusted-process-output/v4"
    return PublicProcessOutputSummaryV4.model_validate(public)


def without_sgr(data: bytes) -> bytes:
    """测试用的独立对照：只去除下列用例使用的SGR颜色序列。"""
    return re.sub(rb"\x1b\[[0-9;]*m", b"", data)


# pytest等工具开启颜色时的真实形态：SGR颜色、OSC窗口标题（BEL结束）、OSC 8超链接（ST结束）、
# ESC 7/8保存与恢复光标、CSI清行，以及tput sgr0输出的ESC ( B。
COLORED = (
    b"\x1b[1m===== test session starts =====\x1b[0m\n"
    b"\x1b]0;pytest\x07"
    b"\x1b[31mFAILED\x1b[0m \x1b]8;;file:///t.py\x1b\\t.py\x1b]8;;\x1b\\::test - AssertionError\n"
    b"\x1b7\x1b[2K\x1b8"
    b"\x1b(B\x1b[m1 failed in 0.1s\n"
)
PLAIN = b"===== test session starts =====\nFAILED t.py::test - AssertionError\n1 failed in 0.1s\n"


def test_v4_removes_terminal_escape_sequences_from_colored_output() -> None:
    """v3遇到ESC即整流为null，模型只能另读Artifact；v4去除完整转义序列后给出正文。"""
    checked = v4_preview(COLORED, b"\x1b[33mwarning\x1b[0m\n")
    stdout = checked.diagnostic_preview.stdout
    assert (stdout.text, stdout.offset, stdout.size_bytes, stdout.truncated) == (
        PLAIN.decode(),
        0,
        len(PLAIN),
        False,
    )
    assert stdout.removed_escape_bytes == len(COLORED) - len(PLAIN)
    stderr = checked.diagnostic_preview.stderr
    assert (stderr.text, stderr.removed_escape_bytes, stderr.truncated) == ("warning\n", 9, False)
    assert checked.complete
    assert tail_preview(COLORED).diagnostic_preview.stdout.text is None


def test_v4_tail_budget_is_spent_on_visible_text_and_maps_back_to_the_raw_stream() -> None:
    passed = b"".join(b"\x1b[32mPASSED\x1b[0m tests/test_%d.py\n" % i for i in range(2000))
    failed = b"\x1b[31m\x1b[1mFAILED\x1b[0m tests/test_x.py::test_y - AssertionError\n"
    raw = passed + failed
    preview = v4_preview(raw).diagnostic_preview.stdout
    assert preview.text is not None and preview.truncated
    assert preview.size_bytes == TAIL and preview.removed_escape_bytes > 0
    assert preview.text.endswith("FAILED tests/test_x.py::test_y - AssertionError\n")
    # 预览覆盖原流[offset, 末尾)：去除颜色序列后逐字节等于正文，起点紧接上一个正文字节。
    assert preview.offset + preview.size_bytes + preview.removed_escape_bytes == len(raw)
    assert without_sgr(raw[preview.offset :]) == preview.text.encode()
    shown_before = without_sgr(raw[: preview.offset])
    assert raw[preview.offset - 1] == shown_before[-1]
    assert len(shown_before) + preview.size_bytes == len(without_sgr(raw))


def test_v4_complete_colored_stream_longer_than_the_budget_is_not_truncated() -> None:
    raw = b"\x1b[31m" + b"x" * TAIL + b"\x1b[0m"
    preview = v4_preview(raw).diagnostic_preview.stdout
    assert (preview.text, preview.offset, preview.size_bytes) == ("x" * TAIL, 0, TAIL)
    assert (preview.removed_escape_bytes, preview.truncated) == (9, False)


def test_v4_stream_of_only_escape_sequences_is_complete_and_empty() -> None:
    preview = v4_preview(b"\x1b[0m\x1b(B").diagnostic_preview.stdout
    assert (preview.text, preview.offset, preview.size_bytes) == ("", 0, 0)
    assert (preview.removed_escape_bytes, preview.truncated) == (7, False)


@pytest.mark.parametrize(
    "stdout",
    [b"", b"a", b"a" * (TAIL + 1), "中".encode() * TAIL + b"x", b"\t\r\n", b"\xff", b"ok\x00"],
)
def test_v4_matches_v3_tail_for_streams_without_escape_sequences(stdout: bytes) -> None:
    original = document(stdout, b"E   assert 1 == 2\n")
    v3 = trusted_process_public_output(original, legacy_preview=V3)["diagnostic_preview"]
    v4 = trusted_process_public_output(original, include_preview=True)["diagnostic_preview"]
    for name in ("stdout", "stderr"):
        assert v4[name] == {**v3[name], "removed_escape_bytes": 0}


@pytest.mark.parametrize(
    "unsafe",
    [
        b"\xff",
        b"\x00",
        b"\x07",
        b"\x08",
        b"\x7f",
        "\u202e".encode(),
        "\u009b31m".encode(),  # 8位C1形式的CSI不是可显示字符
        b"\x1b",  # 孤立ESC
        b"\x1b[31",  # 未结束的CSI
        b"\x1b]0;title",  # 未结束的OSC
        b"\x1b]0;a\x00b\x07",  # OSC正文含控制字符
        b"\xe4\x1b[0m\xb8\xad",  # 转义序列切开的多字节字符不会被拼回
    ],
)
@pytest.mark.parametrize("where", ["before-tail", "inside-tail"])
def test_v4_other_control_characters_still_make_the_stream_unavailable(unsafe, where) -> None:
    colored = b"\x1b[31mred\x1b[0m\n"
    if where == "before-tail":
        data = colored + unsafe + b"\n" + b"a" * (TAIL + 1)
    else:
        data = colored + b"a" * 10 + unsafe
    preview = v4_preview(data).diagnostic_preview.stdout
    assert (preview.text, preview.offset, preview.size_bytes) == (None, 0, 0)
    assert (preview.removed_escape_bytes, preview.truncated) == (0, True)


@pytest.mark.parametrize(
    "case",
    [
        "missing-removed",
        "negative-removed",
        "removed-beyond-persisted",
        "removed-understated",
        "truncated",
        "null-with-removed",
        "escape-in-text",
        "extra",
        "v3-version",
    ],
)
def test_v4_contract_rejects_forged_or_unbounded_previews(case: str) -> None:
    original = document(b"\x1b[31mok\x1b[0m\n")
    public = copy.deepcopy(trusted_process_public_output(original, include_preview=True))
    preview = public["diagnostic_preview"]["stdout"]
    assert preview == {
        "text": "ok\n",
        "offset": 0,
        "size_bytes": 3,
        "removed_escape_bytes": 9,
        "truncated": False,
    }
    PublicProcessOutputSummaryV4.model_validate(public)
    if case == "missing-removed":
        del preview["removed_escape_bytes"]
    elif case == "negative-removed":
        preview["removed_escape_bytes"] = -1
    elif case == "removed-beyond-persisted":
        preview["removed_escape_bytes"] = 10
    elif case == "removed-understated":
        preview["removed_escape_bytes"] = 8
    elif case == "truncated":
        preview["truncated"] = True
    elif case == "null-with-removed":
        preview.update(text=None, size_bytes=0, truncated=True)
    elif case == "escape-in-text":
        preview.update(text="\x1b[31mok\x1b[0m\n", size_bytes=12, removed_escape_bytes=0)
    elif case == "extra":
        preview["path"] = "private"
    else:
        public["version"] = V3
    with pytest.raises(ValidationError):
        PublicProcessOutputSummaryV4.model_validate(public)
