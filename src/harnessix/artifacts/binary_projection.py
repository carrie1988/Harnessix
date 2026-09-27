"""只按持久Artifact用途解析正式Process契约，不猜测任意JSON字段的编码。"""

from __future__ import annotations

from collections.abc import Callable

from harnessix.processes.output_artifact import parse_process_output_document
from harnessix.processes.trusted_output import parse_trusted_process_output


def decode_process_artifact(
    body: bytes, purpose: str, checkpoint: Callable[[], None]
) -> tuple[bytes, ...]:
    """原规范、连续偏移和流Hash仍由既有解析器核验，逐流重建后扫描跨Chunk值。"""
    checkpoint()
    if purpose not in {"action_output", "process_output"}:
        raise ValueError("Artifact用途不是正式Process输出")
    document = (
        parse_trusted_process_output(body)
        if purpose == "action_output"
        else parse_process_output_document(body)
    )
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    for chunk in document.chunks:
        checkpoint()
        streams[chunk.stream].extend(chunk.data())
    checkpoint()
    return tuple(bytes(stream) for stream in streams.values())
