"""精确编辑与换行风格（ADR 0129第2版）：先逐字节匹配，再忽略CRLF/LF差异；写入沿用原处的换行。"""

from __future__ import annotations

import hashlib

import pytest

from harnessix.agent.errors import KernelError
from harnessix.patches import planner
from harnessix.patches.contracts import ExactEdit, PreparedPatch
from harnessix.patches.planner import (
    _edit_ranges,
    _located_ranges,
    apply_exact_edits,
    prepare_patch,
    validate_prepared,
)
from harnessix.tools.workspace import ReadOperation, Workspace
from tests.patches.test_planner import proposal

LIMIT = 1024 * 1024
CRLF_FILE = b"def f():\r\n    a = 1\r\n    b = 2\r\n    return a\r\n"
LF_FILE = CRLF_FILE.replace(b"\r\n", b"\n")
# 同一段文字先后以CRLF和LF出现：逐字节只命中其中一处，忽略换行则两处都命中。
BOTH_STYLES = b"x = 1\r\ny = 2\r\nx = 1\ny = 2\n"


def edits(*pairs):
    return tuple(ExactEdit(old_text=old, new_text=new) for old, new in pairs)


def apply(before, *pairs):
    return apply_exact_edits(before, edits(*pairs), ReadOperation())


def ranges(before, *pairs):
    return _edit_ranges(before, edits(*pairs), ReadOperation(), LIMIT)


def before_newline_handling(before, *pairs):
    """ADR 0129第2版之前的语义：只逐字节匹配，new_text原样写入。"""
    return _located_ranges(before, edits(*pairs), ReadOperation(), LIMIT, newline_aware=False)


@pytest.mark.parametrize(
    ("before", "pairs"),
    [
        (LF_FILE, [("    a = 1\n    b = 2\n", "    a = 10\n    b = 20\n    c = 30\n")]),
        (LF_FILE, [("    a = 1", "    a = 1\n    z = 0"), ("return a", "return b")]),
        (LF_FILE, [("\n    b = 2", "\n    y = 1\n    b = 2")]),
        (b"no newline at all", [("newline", "line\nbreak")]),
        # CRLF文件上不带换行的改写，以及带着\r\n抄下来的锚点。
        (CRLF_FILE, [("a = 1", "a = 100")]),
        (CRLF_FILE, [("    a = 1\r\n    b = 2\r\n", "    a = 10\r\n    b = 20\r\n")]),
    ],
)
def test_edits_that_matched_byte_for_byte_before_keep_their_ranges_and_bytes(before, pairs):
    assert ranges(before, *pairs) == before_newline_handling(before, *pairs)


def test_a_byte_exact_match_is_taken_before_any_newline_tolerant_one():
    # 逐字节恰好命中一处时不再做忽略换行的匹配：另一处只差换行风格的文字不参与唯一性判定。
    assert apply(BOTH_STYLES, ("x = 1\ny = 2", "x = 9\ny = 2")) == (
        b"x = 1\r\ny = 2\r\nx = 9\ny = 2\n"
    )
    assert apply(BOTH_STYLES, ("x = 1\r\ny = 2", "x = 9\r\ny = 2")) == (
        b"x = 9\r\ny = 2\r\nx = 1\ny = 2\n"
    )


@pytest.mark.parametrize(
    ("before", "old", "region"),
    [
        # 多行LF锚点落在CRLF区：区间覆盖完整的CRLF，不拆开换行。
        (CRLF_FILE, "    a = 1\n    b = 2\n", b"    a = 1\r\n    b = 2\r\n"),
        (CRLF_FILE, "    a = 1\n    b = 2", b"    a = 1\r\n    b = 2"),
        (CRLF_FILE, "def f():\n    a = 1", b"def f():\r\n    a = 1"),
        (CRLF_FILE, "\n    b = 2\n    return a\n", b"\r\n    b = 2\r\n    return a\r\n"),
        # 反过来：带着\r\n的锚点对LF文件，以及锚点自身混用两种换行。
        (LF_FILE, "    a = 1\r\n    b = 2\r\n", b"    a = 1\n    b = 2\n"),
        (
            CRLF_FILE,
            "    a = 1\n    b = 2\r\n    return a",
            b"    a = 1\r\n    b = 2\r\n    return a",
        ),
        # 混合换行的文件：CRLF区之后的LF区、跨两种换行的区域。
        (b"a\r\nb\r\nc\nd\ne\n", "c\r\nd\r\n", b"c\nd\n"),
        (b"a\r\nb\r\nc\nd\ne\n", "b\nc\nd", b"b\r\nc\nd"),
    ],
)
def test_a_newline_tolerant_match_maps_back_to_the_original_byte_range(before, old, region):
    assert before.count(old.encode()) == 0  # 逐字节不命中，才轮到忽略换行的匹配。
    [(start, stop, _)] = ranges(before, (old, "replacement"))
    assert before[start:stop] == region
    assert (start, stop) == (before.index(region), before.index(region) + len(region))
    assert apply(before, (old, "replacement")) == before.replace(region, b"replacement")


@pytest.mark.parametrize(
    ("before", "old", "new", "after"),
    [
        # CRLF区保持CRLF：多行LF锚点（Beta RT-06），以及单行锚点换成多行（Beta事件726）。
        (
            CRLF_FILE,
            "    a = 1\n    b = 2\n",
            "    a = 10\n    b = 20\n    c = 30\n",
            b"def f():\r\n    a = 10\r\n    b = 20\r\n    c = 30\r\n    return a\r\n",
        ),
        (
            CRLF_FILE,
            "    a = 1",
            "    a = 1\n    z = 0",
            b"def f():\r\n    a = 1\r\n    z = 0\r\n    b = 2\r\n    return a\r\n",
        ),
        # 带着\r\n抄下锚点、新文本却是LF或两种混用：仍按该处的CRLF写入。
        (
            CRLF_FILE,
            "    a = 1\r\n    b = 2",
            "    a = 1\n    y = 0\r\n    b = 3",
            b"def f():\r\n    a = 1\r\n    y = 0\r\n    b = 3\r\n    return a\r\n",
        ),
        # LF区保持LF，即使新文本带着\r\n。
        (
            LF_FILE,
            "    a = 1",
            "    a = 1\r\n    z = 0",
            b"def f():\n    a = 1\n    z = 0\n    b = 2\n    return a\n",
        ),
        # 以LF开头的锚点命中CRLF的后半：不留下孤立的\r，也不写出\r\r\n。
        (
            CRLF_FILE,
            "\n    b = 2",
            "\n    y = 1\n    b = 2",
            b"def f():\r\n    a = 1\r\n    y = 1\r\n    b = 2\r\n    return a\r\n",
        ),
        (
            CRLF_FILE,
            "\n    b = 2",
            " # joined",
            b"def f():\r\n    a = 1 # joined\r\n    return a\r\n",
        ),
    ],
)
def test_new_text_is_written_in_the_newline_style_of_the_place_it_replaces(before, old, new, after):
    assert apply(before, (old, new)) == after


@pytest.mark.parametrize(
    ("before", "old", "after"),
    [
        # 区域内没有换行：取所在行的行尾。
        (b"one\ntwo\r\nthree\n", "two", b"one\nTWO\r\nmore\r\nthree\n"),
        (b"one\r\ntwo\nthree\r\n", "two", b"one\r\nTWO\nmore\nthree\r\n"),
        # 所在行是没有行尾的末行：取上一行的行尾。
        (b"one\r\nlast", "last", b"one\r\nTWO\r\nmore"),
        # 整个文件没有换行：原样写入。
        (b"only line", "line", b"only TWO\nmore"),
        # 区域内两种换行都有：取占多数的一种；持平时取区域内第一个。
        (b"a\r\nb\nc\r\nd\n", "a\nb\nc\nd", b"TWO\r\nmore\n"),
        (b"a\nb\r\nc\nd\r\n", "a\nb\nc\nd", b"TWO\nmore\r\n"),
        (b"a\r\nb\nc\n", "a\nb\nc", b"TWO\r\nmore\n"),
        (b"a\nb\r\nc\n", "a\nb\nc", b"TWO\nmore\n"),
    ],
)
def test_the_newline_style_of_a_place_is_decided_by_one_fixed_rule(before, old, after):
    assert apply(before, (old, "TWO\nmore")) == after


@pytest.mark.parametrize(
    ("before", "old", "new"),
    [
        (CRLF_FILE, "    a = 1\r\n    b = 2\r\n", "    a = 1\n    b = 2\n"),
        (LF_FILE, "    a = 1\n    b = 2\n", "    a = 1\r\n    b = 2\r\n"),
        # Beta事件1079：模型把自己此前写进LF区的CRLF行改回LF。
        (b"keep\nimport x\r\n\r\n\r\ndef f():\n", "x\r\n\r\n\r\ndef", "x\n\n\ndef"),
    ],
)
def test_an_edit_that_only_changes_line_endings_is_written_as_given(before, old, new):
    assert apply(before, (old, new)) == before.replace(old.encode(), new.encode())


def test_converting_line_endings_that_the_file_already_has_changes_nothing():
    # 锚点只在忽略换行时命中，新文本正是文件现有的字节：如实报告无变化，而不是“未找到”。
    with pytest.raises(KernelError) as error:
        apply(CRLF_FILE, ("    a = 1\n    b = 2\n", "    a = 1\r\n    b = 2\r\n"))
    assert error.value.code == "patch_no_change"


@pytest.mark.parametrize(
    ("before", "old", "lines"),
    [
        # 两处都只在忽略换行时命中：不按“更像哪一处”挑选。
        (b"x = 1\r\ny = 2\nz\r\nx = 1\ny = 2\r\n", "x = 1\ny = 2\n", "1, 4"),
        (b"x = 1\r\ny = 2\r\nz\r\nx = 1\r\ny = 2\r\n", "x = 1\ny = 2\n", "1, 4"),
        (b"x = 1\ny = 2\nz\nx = 1\ny = 2\n", "x = 1\r\ny = 2\r\n", "1, 4"),
    ],
)
def test_an_anchor_must_be_unique_with_newline_style_ignored(before, old, lines):
    with pytest.raises(KernelError) as error:
        apply(before, (old, "x = 9\n"))
    assert error.value.code == "patch_ambiguous_context"
    assert error.value.message.startswith(
        f"edits[0]: old_text matches more than one place (lines {lines})"
    )


def test_newline_tolerant_edits_still_may_not_overlap():
    with pytest.raises(KernelError) as error:
        apply(
            CRLF_FILE,
            ("    a = 1\n    b = 2", "    a = 0\n    b = 2"),
            ("    b = 2\n    return a", "    b = 2\n    return b"),
        )
    assert error.value.code == "patch_overlapping_edits"
    assert error.value.message.startswith(
        "edits[0] and edits[1] overlap: both old_text cover line 3"
    )


@pytest.mark.parametrize(
    ("before", "old", "new", "hint"),
    [
        # 回归（Beta RT-06）：CRLF文件、LF锚点，内容确有不同时才报不一致，并说明不是换行的问题。
        (
            b"class A:\r\n    y = 2\r\n",
            "class A:\n    x = 1\n",
            "class A:\n    x = 3\n",
            "its first non-blank line (whitespace aside) is at line 1, but old_text differs "
            "from the file there in more than line endings",
        ),
        # 只差缩进的单行锚点：原说明称“其后的行不一致”，而它根本没有后续行。
        (
            b"class A:\r\n  x = 1\r\n",
            "    x = 1",
            "    x = 3",
            "its first non-blank line (whitespace aside) is at line 2, but old_text differs "
            "from the file there in more than line endings",
        ),
        # 已应用的判定同样忽略换行：新文本以CRLF躺在文件里，重发时带的是LF。
        (
            b"class A:\r\n    flag = True\r\n    x = 1\r\n",
            "class A:\n    x = 1\n",
            "class A:\n    flag = True\n    x = 1\n",
            "new_text is already there at line 1: this edit looks already applied",
        ),
        (
            b"class A:\r\n    y = 2\r\n",
            "class B:\n    x = 1\n",
            "class B:\n    x = 3\n",
            "neither was its first non-blank line",
        ),
    ],
)
def test_a_missing_anchor_is_never_blamed_on_line_endings(before, old, new, hint):
    with pytest.raises(KernelError) as error:
        apply(before, (old, new))
    assert error.value.code == "patch_context_not_found"
    assert error.value.message.startswith("edits[0]: old_text was not found in the file")
    assert hint in error.value.message and error.value.message.isascii()


def test_the_prepared_plan_binds_the_real_bytes_of_a_crlf_file(tmp_path):
    (tmp_path / "main.py").write_bytes(CRLF_FILE)
    multi_line = edits(("    a = 1\n    b = 2\n", "    a = 10\n    b = 20\n"))
    with Workspace(tmp_path) as workspace:
        prepared = prepare_patch(workspace, proposal(workspace, edits=multi_line), ReadOperation())
        validate_prepared(workspace, prepared, ReadOperation())
    assert prepared.before == CRLF_FILE
    assert prepared.after == b"def f():\r\n    a = 10\r\n    b = 20\r\n    return a\r\n"
    assert prepared.manifest.before_sha256 == hashlib.sha256(CRLF_FILE).hexdigest()
    assert prepared.manifest.after_sha256 == hashlib.sha256(prepared.after).hexdigest()
    assert (tmp_path / "main.py").read_bytes() == CRLF_FILE


def test_plans_recorded_before_newline_handling_still_validate(tmp_path):
    # 升级前保存的计划：单行锚点命中CRLF区，LF的new_text被原样写入。升级后回滚与恢复仍要读它。
    (tmp_path / "main.py").write_bytes(CRLF_FILE)
    single_line = edits(("    a = 1", "    a = 1\n    z = 0"))
    recorded_after = CRLF_FILE.replace(b"    a = 1", b"    a = 1\n    z = 0")
    with Workspace(tmp_path) as workspace:
        current = prepare_patch(workspace, proposal(workspace, edits=single_line), ReadOperation())
        assert current.after != recorded_after
        recorded = PreparedPatch(
            planner._manifest(
                workspace, current.proposal, CRLF_FILE, recorded_after, current.manifest.source_mode
            ),
            current.proposal,
            CRLF_FILE,
            recorded_after,
        )
        validate_prepared(workspace, recorded, ReadOperation())
        validate_prepared(workspace, current, ReadOperation())
        # 两种语义都算不出的后像仍按损坏拒绝。
        tampered_after = recorded_after + b"# tampered\r\n"
        tampered = PreparedPatch(
            planner._manifest(
                workspace, current.proposal, CRLF_FILE, tampered_after, current.manifest.source_mode
            ),
            current.proposal,
            CRLF_FILE,
            tampered_after,
        )
        with pytest.raises(KernelError) as error:
            validate_prepared(workspace, tampered, ReadOperation())
    assert error.value.code == "patch_plan_corrupt"
    # 复核与Diff用的是算出该后像的那一组区间。
    digest = hashlib.sha256(recorded_after).hexdigest()
    assert _edit_ranges(CRLF_FILE, single_line, ReadOperation(), LIMIT, digest) == [
        (10, 19, b"    a = 1\n    z = 0")
    ]
    assert _edit_ranges(CRLF_FILE, single_line, ReadOperation(), LIMIT) == [
        (10, 19, b"    a = 1\r\n    z = 0")
    ]
