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
    assert plain[3:] == DIFF.splitlines()
    styles = {line.plain: str(line.style) for line in lines}
    assert styles["+    return len(text.split())"] == "green"
    assert styles['-    return len(text.split(" "))'] == "red"
    assert styles["@@ -3,7 +3,7 @@"] == "cyan"


def test_unexpected_text_is_left_to_the_caller() -> None:
    assert readable_diff("plain text, not records") is None
    assert readable_diff('{"record_type": "text"}') is None
    assert readable_diff('{"record_type": "summary"}') is None
