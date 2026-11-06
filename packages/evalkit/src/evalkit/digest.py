"""合同的规范JSON摘要：键排序、紧凑分隔、UTF-8，结果带算法前缀。"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel


def canonical_json(model: BaseModel) -> bytes:
    data = model.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def content_digest(model: BaseModel) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(model)).hexdigest()


def bytes_digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()
