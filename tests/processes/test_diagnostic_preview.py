"""诊断预览只优化模型往返，不改变原始流、退出结论或历史摘要。"""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from harnessix.processes.public_output import (
    MAX_PROCESS_PREVIEW_STREAM_BYTES,
    PublicEvalOutputSummary,
    PublicProcessOutputSummary,
    PublicProcessOutputSummaryV2,
)
from harnessix.processes.trusted_output import (
    build_trusted_process_output,
    parse_trusted_process_output,
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
    public = trusted_process_public_output(original, include_preview=True)

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
    public = trusted_process_public_output(document(stdout, stderr), include_preview=True)
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
    public = trusted_process_public_output(original, include_preview=True)
    assert public["diagnostic_preview"]["stdout"] == {
        "text": None,
        "size_bytes": 0,
        "truncated": True,
    }
    assert b"".join(chunk.data() for chunk in original.chunks) == data
    PublicProcessOutputSummaryV2.model_validate(public)


def test_empty_and_whitespace_streams_remain_truthful_text():
    public = trusted_process_public_output(document(b"", b"\t\r\n"), include_preview=True)
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
    public = trusted_process_public_output(original, include_preview=True)
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
    public = copy.deepcopy(trusted_process_public_output(document(b"ok\n"), include_preview=True))
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
    v2 = trusted_process_public_output(original, include_preview=True)
    evaluation = trusted_process_public_output(original, include_passed=True)
    PublicProcessOutputSummary.model_validate(legacy)
    PublicEvalOutputSummary.model_validate(evaluation)
    with pytest.raises(ValidationError):
        PublicProcessOutputSummary.model_validate(v2)
    with pytest.raises(ValidationError):
        PublicEvalOutputSummary.model_validate({**v2, "passed": True})
    with pytest.raises(ValueError, match="Eval v1"):
        trusted_process_public_output(original, include_passed=True, include_preview=True)
    assert "diagnostic_preview" not in legacy
    assert len(json.dumps(v2, ensure_ascii=False).encode()) < 4096
