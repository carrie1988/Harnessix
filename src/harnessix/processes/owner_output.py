"""受监督进程：持久捕获进程输出并生成有界完整性观察。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from harnessix.processes.supervision_contracts import ProcessOutputObservation
from harnessix.secrets.redaction import StreamingSecretRedactor


class CapturedProcessOutput:
    """先脱敏再计量和落盘的单流有界输出。"""

    def __init__(self, path: Path, secrets: tuple[bytes, ...]) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self._fd = os.open(path, flags, 0o600)
        self._redactor = StreamingSecretRedactor(secrets)
        self._digest = hashlib.sha256()
        self._persisted_digest = hashlib.sha256()
        self._observed = 0
        self._persisted = 0
        self._closed = False
        self.eof = False

    @property
    def observed(self) -> int:
        return self._observed

    @property
    def persisted(self) -> int:
        return self._persisted

    def feed(self, data: bytes, allowance: int) -> int:
        return self._publish(self._redactor.feed(data), allowance)

    def finish(self, allowance: int, *, eof: bool) -> int:
        if self._closed:
            return 0
        emitted = self._publish(self._redactor.finish(), allowance)
        self.eof = eof
        self._closed = True
        return emitted

    def _publish(self, data: bytes, allowance: int) -> int:
        self._observed += len(data)
        self._digest.update(data)
        persisted = data[: max(0, allowance)]
        view = memoryview(persisted)
        while view:
            written = os.write(self._fd, view)
            if written <= 0:
                raise OSError("short process output write")
            view = view[written:]
        self._persisted += len(persisted)
        self._persisted_digest.update(persisted)
        return len(data)

    def sync(self) -> None:
        os.fsync(self._fd)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def observation(self) -> ProcessOutputObservation:
        return ProcessOutputObservation(
            observed_bytes=self._observed,
            persisted_bytes=self._persisted,
            sha256=self._digest.hexdigest(),
            persisted_sha256=self._persisted_digest.hexdigest(),
            truncated=self._persisted < self._observed,
            eof=self.eof,
        )
