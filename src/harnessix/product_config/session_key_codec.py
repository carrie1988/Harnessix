"""独立Session密钥闭合二进制格式；不借用Provider凭据或公开配置正文。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from harnessix.agent.errors import KernelError

PAYLOAD_MAGIC = b"HXSK\x01"
POSIX_MAGIC = b"HXKP\x01"
WINDOWS_MAGIC = b"HXKW\x01"
MAX_KEY_FILE_BYTES = 8192


def unavailable() -> KernelError:
    return KernelError("publication_key_unavailable", "Session独立持久密钥不可用")


@dataclass(slots=True)
class OwnedSessionKey:
    """自有可变Key副本；清零不承诺擦除OS、Codec及调用方的所有副本。"""

    store_id: UUID
    key_id: UUID
    key: bytearray = field(repr=False)

    def close(self) -> None:
        self.key[:] = b"\0" * len(self.key)


def create_payload() -> bytes:
    return PAYLOAD_MAGIC + uuid4().bytes + uuid4().bytes + os.urandom(32)


def decode_payload(body: bytes) -> OwnedSessionKey:
    if type(body) is not bytes or len(body) != 69 or not body.startswith(PAYLOAD_MAGIC):
        raise unavailable()
    return OwnedSessionKey(UUID(bytes=body[5:21]), UUID(bytes=body[21:37]), bytearray(body[37:]))
