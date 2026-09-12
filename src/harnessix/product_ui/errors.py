"""产品客户端稳定错误合同。"""

from __future__ import annotations


class ProductUIError(Exception):
    """向Controller暴露稳定错误码，不泄露文件路径或不可信正文。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
