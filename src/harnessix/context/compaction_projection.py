from __future__ import annotations

import json
from uuid import uuid5

from harnessix.agent.models import Item, ItemStatus, TextContent
from harnessix.context.compaction_contracts import CompactionSummary


def compaction_summary_item(summary: CompactionSummary) -> Item:
    """以稳定身份投影低信任摘要；正文不获得用户或系统权限。"""
    return Item(
        item_id=uuid5(summary.compaction_id, "harnessix.compaction-summary/v1"),
        status=ItemStatus.COMPLETED,
        content=TextContent(
            kind="assistant_message",
            text=json.dumps(
                {
                    "schema": summary.spec_version,
                    "compaction_id": str(summary.compaction_id),
                    "trust": "derived_history",
                    "authority": "none",
                    "summary": summary.text,
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
