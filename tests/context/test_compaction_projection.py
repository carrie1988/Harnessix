"""摘要投影：宿主写入说明头的摘要投影为纯文本；此前的摘要按v1 JSON投影，已记录Hash重放不变。"""

from __future__ import annotations

import json
from uuid import uuid4

from harnessix.agent.models import TextContent
from harnessix.agent.runtime import SUMMARY_INSTRUCTIONS
from harnessix.context.compaction_contracts import CompactionSummary
from harnessix.context.compaction_projection import (
    SUMMARY_PROJECTION_HEADER,
    compaction_summary_item,
)


def test_host_marked_summary_is_projected_as_plain_text():
    text = SUMMARY_PROJECTION_HEADER + "Verified facts: the parser drops empty keys."
    item = compaction_summary_item(CompactionSummary(compaction_id=uuid4(), text=text))
    assert isinstance(item.content, TextContent)
    assert item.content.kind == "assistant_message"
    assert item.content.text == text
    assert not item.content.text.lstrip().startswith("{")


def test_summary_without_the_header_keeps_the_v1_json_shape_and_identity():
    compaction_id = uuid4()
    old = compaction_summary_item(CompactionSummary(compaction_id=compaction_id, text="旧摘要"))
    assert json.loads(old.content.text) == {
        "schema": "harnessix.compaction-summary/v1",
        "compaction_id": str(compaction_id),
        "trust": "derived_history",
        "authority": "none",
        "summary": "旧摘要",
    }
    new = compaction_summary_item(
        CompactionSummary(compaction_id=compaction_id, text=SUMMARY_PROJECTION_HEADER + "x")
    )
    # 身份只由compaction_id派生：窗口引用的Item在两种形状下相同。
    assert new.item_id == old.item_id


def test_summary_instructions_ask_for_english_sections_within_the_original_size():
    assert SUMMARY_INSTRUCTIONS.isascii()
    for section in ("Verified facts", "Hypotheses", "Contradicting observations"):
        assert section in SUMMARY_INSTRUCTIONS
    assert len(SUMMARY_INSTRUCTIONS.encode()) <= 324
