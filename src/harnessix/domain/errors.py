"""定义Agent Kernel错误基类与外部副作用不确定信号。"""

from __future__ import annotations


class HarnessixError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class UncertainEffectError(RuntimeError):
    """执行器确认外部副作用可能已提交，但无法给出确定结果。"""
