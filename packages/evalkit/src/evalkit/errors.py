"""evalkit的唯一异常类型：固定代码加固定说明，不携带外部响应、异常正文或凭据。"""

from __future__ import annotations


class EvalkitError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
