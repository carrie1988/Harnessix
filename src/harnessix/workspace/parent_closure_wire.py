"""闭包的规范 JSON 与流式集合摘要；不访问宿主文件或持久状态。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence

from harnessix.workspace.contracts import WorkspaceResourceObservation

MAX_CLOSURE_BLOB_BYTES = 8 * 1024 * 1024
MAX_CLOSURE_TOTAL_BYTES = 32 * 1024 * 1024


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def observations_digest(
    observations: Sequence[WorkspaceResourceObservation],
    checkpoint: Callable[[], None],
) -> str:
    """与完整规范 JSON 数组逐字节一致，避免累计所有长路径正文。"""
    digest = hashlib.sha256(b"[")
    for index, observation in enumerate(observations):
        checkpoint()
        if index:
            digest.update(b",")
        digest.update(canonical_bytes(observation.model_dump(mode="json", warnings="error")))
    digest.update(b"]")
    return digest.hexdigest()
