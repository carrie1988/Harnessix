"""定义Agent SDK跨Transport与协议边界共享的稳定错误。"""

from __future__ import annotations


class AgentSDKError(RuntimeError):
    """Agent SDK协议、传输或服务失败的稳定错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        path: tuple[str | int, ...] = (),
    ) -> None:
        location = "/".join(str(part) for part in path)
        super().__init__(f"{code}: {message}" + (f" [{location}]" if location else ""))
        self.code = code
        self.message = message
        self.retryable = retryable
        self.path = path
