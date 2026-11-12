"""整仓库搜索的提速不改变结果（Beta 0.10.0b20复测B20-05，ADR 0145）。

真实现场：vLLM仓库（6,682个文件、约100 MiB文本）在根目录搜索一个标识符，要么读到64 MiB上限报
limit_exceeded，要么撞上5秒读取时限，模型拿不到任何结果。时间主要花在两处逐字符、逐行的Python循环上。
"""

from __future__ import annotations

import pytest

from harnessix.tools import files, grep_matcher, search
from harnessix.tools.contracts import MAX_LINE_BYTES, ReadToolError
from harnessix.tools.grep_matcher import whole_text_decides
from harnessix.tools.search_contracts import (
    MAX_SNAPSHOT_SEARCH_ENTRIES,
    MAX_SNAPSHOT_SEARCH_TOTAL_BYTES,
    GrepInput,
    GrepSnapshotInput,
    RegexGrepInput,
    SnapshotSearchStats,
)
from harnessix.tools.workspace import ReadOperation, Workspace


def _old_binary_check(text: str) -> bool:
    """改动前的逐字符判断，作为对照。"""
    return any((ord(char) < 32 and char not in "\t\r\n") or ord(char) == 127 for char in text)


def test_binary_detection_matches_the_per_character_rule_for_every_code_point() -> None:
    for code in [*range(0x180), 0x2028, 0x4E2D, 0x1F642]:
        text = f"a{chr(code)}b"
        expected = _old_binary_check(text)
        if expected:
            with pytest.raises(ReadToolError) as raised:
                files._decode(text.encode("utf-8"))
            assert raised.value.code == "binary_file"
        else:
            assert files._decode(text.encode("utf-8")) == text


@pytest.mark.parametrize(
    ("args", "decides"),
    [
        (GrepInput(query="needle"), False),
        (GrepSnapshotInput(query="needle"), True),
        (RegexGrepInput(query="needle", match_mode="literal"), True),
        (RegexGrepInput(query="^needle", match_mode="literal"), True),
        (RegexGrepInput(query=r"class\s+\w+Error\b"), True),
        (RegexGrepInput(query="^def main"), False),
        (RegexGrepInput(query="needle$"), False),
        (RegexGrepInput(query=r"\Aneedle"), False),
        (RegexGrepInput(query=r"needle\z"), False),
        (RegexGrepInput(query="[^a]needle"), False),
    ],
)
def test_whole_text_shortcut_is_only_taken_where_it_cannot_miss_a_line(args, decides) -> None:
    assert whole_text_decides(args) is decides


def _grep(tmp_path, args, sources):
    for path, body in sources.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    with Workspace(tmp_path) as workspace:
        return search.grep(workspace, args, ReadOperation())


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # 行尾锚点在CRLF文件里只有逐行（去掉回车）才成立；整份文本搜索会漏掉。
        ("needle$", [("crlf.py", 2), ("lf.py", 1)]),
        ("^needle", [("crlf.py", 2), ("lf.py", 1)]),
        (r"\Aneedle", [("crlf.py", 2), ("lf.py", 1)]),
        ("needle", [("crlf.py", 2), ("lf.py", 1)]),
        (r"ne+dle\b", [("crlf.py", 2), ("lf.py", 1)]),
    ],
)
def test_anchored_and_unanchored_queries_find_the_same_lines(tmp_path, query, expected) -> None:
    sources = {
        "crlf.py": b"first\r\nneedle\r\nlast\r\n",
        "lf.py": b"needle\nother\n",
        "none.py": b"nothing here\n",
    }
    output = _grep(tmp_path, RegexGrepInput(query=query), sources)
    assert [(hit.path, hit.line) for hit in output.matches] == expected
    assert output.scan_complete


def test_files_without_the_query_are_not_scanned_line_by_line(tmp_path, monkeypatch) -> None:
    lines = b"".join(b"line %d\n" % index for index in range(500))
    sources = {f"pkg/m{index}.py": lines for index in range(20)}
    sources["pkg/hit.py"] = lines + b"needle here\n"
    calls: list[str] = []
    build = grep_matcher.build_line_matcher

    def counting(args, operation):
        matcher = build(args, operation)

        def wrapped(line: str) -> int:
            calls.append(line)
            return matcher(line)

        return wrapped

    monkeypatch.setattr(search, "build_line_matcher", counting)
    output = _grep(tmp_path, RegexGrepInput(query="needle", context_lines=0), sources)
    assert [(hit.path, hit.line) for hit in output.matches] == [("pkg/hit.py", 501)]
    # 20个不含查询的文件各判断一次整份文本；只有命中的那个文件逐行扫描（501行）外加一次整份判断。
    assert len(calls) == 20 + 1 + 501
    assert output.stats.files_read == 21 and output.scan_complete


def test_long_lines_only_count_where_they_could_hide_a_match(tmp_path) -> None:
    long_line = b"x" * (MAX_LINE_BYTES + 1) + b"\n"
    sources = {
        "without.py": long_line + b"plain\n",
        "with.py": b"needle " + long_line + b"needle\n",
    }
    output = _grep(tmp_path, RegexGrepInput(query="needle", context_lines=0), sources)
    assert [(hit.path, hit.line) for hit in output.matches] == [("with.py", 2)]
    # without.py整份文本都不含查询，它的超长行藏不住命中，不再算作缺口。
    assert output.stats.long_lines == 1 and not output.scan_complete

    legacy = _grep(tmp_path, GrepInput(query="needle"), {})
    # 原字面量合同保持原来的统计口径：两个文件的超长行都计入。
    assert legacy.stats.long_lines == 2


def test_snapshot_scan_caps_are_four_times_the_previous_values() -> None:
    assert MAX_SNAPSHOT_SEARCH_TOTAL_BYTES == 256 * 1024 * 1024
    assert MAX_SNAPSHOT_SEARCH_ENTRIES == 200_000
    stats = SnapshotSearchStats(
        entries_scanned=7_566,
        files_read=6_679,
        # vLLM副本一次全仓搜索实际读取的字节数，超过原先的64 MiB上限。
        bytes_read=90_323_703,
        ignored_entries=5,
        unreadable_entries=0,
        oversized_files=4,
        invalid_utf8_files=92,
        binary_files=1,
        long_lines=0,
    )
    assert stats.bytes_read > 64 * 1024 * 1024
