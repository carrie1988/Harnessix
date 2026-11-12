"""审批差异按文件清单和统一diff显示，而不是原始JSON记录。"""

from __future__ import annotations

import json

from harnessix.product_ui.diff_view import readable_diff

# 247端到端测试中真实出现的差异Artifact记录（摘要、文件条目、diff正文）。
DIFF = (
    "diff --harnessix a/textstats.py b/textstats.py\n--- a/textstats.py\n+++ b/textstats.py\n"
    '@@ -3,7 +3,7 @@\n def word_count(text):\n-    return len(text.split(" "))\n'
    "+    return len(text.split())\n"
)
ARTIFACT = "\n".join(
    json.dumps(record)
    for record in (
        {"record_type": "summary", "file_count": 1, "complete": True},
        {"record_type": "entry", "index": 0, "entry": {"kind": "modified", "path": "textstats.py"}},
        {"record_type": "text", "sequence": 1, "text": DIFF[60:]},
        {"record_type": "text", "sequence": 0, "text": DIFF[:60]},
    )
)


def test_artifact_becomes_a_file_list_and_ordered_colored_diff() -> None:
    lines = readable_diff(ARTIFACT)
    assert lines is not None
    plain = [line.plain for line in lines]
    assert plain[:2] == ["共 1 个文件：", "  修改  textstats.py"]
    # 文件头的diff/---/+++行由“── 路径 ──”代替。
    assert plain[3:] == ["── textstats.py ──", *DIFF.splitlines()[3:]]
    styles = {line.plain: str(line.style) for line in lines}
    assert styles["+    return len(text.split())"] == "green"
    assert styles['-    return len(text.split(" "))'] == "red"
    assert styles["@@ -3,7 +3,7 @@"] == "cyan"


def test_unexpected_text_is_left_to_the_caller() -> None:
    assert readable_diff("plain text, not records") is None
    assert readable_diff('{"record_type": "text"}') is None
    assert readable_diff('{"record_type": "summary"}') is None


def test_renames_show_both_paths_and_removed_dash_lines_stay() -> None:
    body = (
        "diff --harnessix a/old.md b/new.md\n--- a/old.md\n+++ b/new.md\n"
        "@@ -1,2 +1,1 @@\n----\n title\n"
    )
    records = (
        {"record_type": "entry", "entry": {"kind": "renamed", "path": "new.md"}},
        {"record_type": "text", "sequence": 0, "text": body},
    )
    lines = readable_diff("\n".join(json.dumps(record) for record in records))
    assert lines is not None
    plain = [line.plain for line in lines]
    assert plain[3:] == ["── old.md → new.md ──", "@@ -1,2 +1,1 @@", "----", " title"]


def test_directories_the_patch_will_create_are_listed_with_the_files() -> None:
    """父目录由事务补建时（ADR 0137）diff正文以new directory行开头；界面把它们列在文件清单里。"""
    body = (
        "new directory tests\nnew directory tests/unit\n"
        "diff --harnessix a/tests/unit/test_x.py b/tests/unit/test_x.py\nnew file mode 644\n"
        "--- /dev/null\n+++ b/tests/unit/test_x.py\n@@ -0,0 +1 @@\n+x = 1\n"
    )
    records = (
        {"record_type": "entry", "entry": {"kind": "added", "path": "tests/unit/test_x.py"}},
        {"record_type": "text", "sequence": 0, "text": body},
    )
    lines = readable_diff("\n".join(json.dumps(record) for record in records))
    assert lines is not None
    assert [line.plain for line in lines] == [
        "共 1 个文件：",
        "  新增  tests/unit/test_x.py",
        "  新建目录  tests",
        "  新建目录  tests/unit",
        "",
        "── tests/unit/test_x.py ──",
        "@@ -0,0 +1 @@",
        "+x = 1",
    ]
