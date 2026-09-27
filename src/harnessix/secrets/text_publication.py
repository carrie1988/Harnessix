"""模型原文安全前缀扫描；跨增量窗口与全步骤预算不依赖网络等待时间。"""

from __future__ import annotations

from collections.abc import Callable

from harnessix.agent.errors import KernelError
from harnessix.secrets import publication


class SecretTextStep:
    """有限登记模式逐文本块检查，所有块共用输入字节和扫描工作预算。"""

    def __init__(self, patterns: tuple[bytes, ...], ensure_open: Callable[[], None]) -> None:
        self._patterns = patterns
        self._ensure_open = ensure_open
        self._maximum = max(map(len, patterns), default=1)
        self._pending: dict[str, bytes] = {}
        self._finished: set[str] = set()
        self._budget = publication._ScanBudget(lambda: None)
        self._closed = False

    def _prepare(self, content_id: str, checkpoint: Callable[[], None]) -> None:
        self._ensure_open()
        if self._closed or content_id in self._finished:
            raise KernelError("trusted_action_output_mismatch", "文本保护步骤已关闭或文本块已结束")
        self._budget.checkpoint = checkpoint
        checkpoint()
        if content_id not in self._pending:
            if len(self._pending) + len(self._finished) >= 128:
                self._budget.reject()
            self._pending[content_id] = b""

    def feed(self, content_id: str, text: str, *, checkpoint: Callable[[], None]) -> str:
        self._prepare(content_id, checkpoint)
        remaining = publication.MAX_SCAN_BYTES - self._budget.size
        if type(text) is not str or len(text) > remaining:
            self._budget.reject()
        try:
            encoded = text.encode("utf-8")
        except UnicodeError:
            raise KernelError("trusted_action_output_mismatch", "文本保护输入无效") from None
        if len(encoded) > remaining:
            self._budget.reject()
        self._budget.size += len(encoded)
        pending = self._pending[content_id] + encoded
        self._budget.scan_bytes(pending, self._patterns, account_size=False)
        boundary = max(0, len(pending) - self._maximum + 1)
        # 原文必须按完整UTF8码点发布；切入最后一个码点时只多保留最多三字节。
        try:
            prefix = pending[:boundary].decode("utf-8")
        except UnicodeDecodeError as error:
            boundary = error.start
            prefix = pending[:boundary].decode("utf-8")
        self._pending[content_id] = pending[boundary:]
        return prefix

    def finish(self, content_id: str, *, checkpoint: Callable[[], None]) -> str:
        self._prepare(content_id, checkpoint)
        pending = self._pending[content_id]
        self._budget.scan_bytes(pending, self._patterns, account_size=False)
        del self._pending[content_id]
        self._finished.add(content_id)
        return pending.decode("utf-8")

    def close(self) -> None:
        self._pending.clear()
        self._finished.clear()
        self._patterns = ()
        self._closed = True
