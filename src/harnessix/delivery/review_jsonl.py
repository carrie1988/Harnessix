"""Workspace与Git Review共用原规范JSONL编码，不复制领域Diff或审批存储。"""

from __future__ import annotations

import json
from collections.abc import Callable

from harnessix.domain.models import ContractModel


class ReviewJSONLLimitError(ValueError):
    """共用编码的总量失败；旧ValueError消息不变，新入口可明确分类。"""


def encode_review_records(
    records: tuple[ContractModel, ...], *, checkpoint: Callable[[], None], max_bytes: int
) -> bytes:
    """完整记录按原键序与UTF-8编码；总量超限时不返回部分正文。"""
    body = bytearray()
    for item in records:
        checkpoint()
        body.extend(
            (
                json.dumps(
                    item.model_dump(mode="json", warnings="error"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        )
        if len(body) > max_bytes:
            raise ReviewJSONLLimitError("Workspace Action Review超过Artifact上限")
    checkpoint()
    return bytes(body)
