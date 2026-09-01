from __future__ import annotations


class HarnessixError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ActionNotFoundError(HarnessixError):
    def __init__(self, action_id: object) -> None:
        super().__init__("action_not_found", f"Action 不存在：{action_id}", status_code=404)


class ToolNotFoundError(HarnessixError):
    def __init__(self, tool_name: str) -> None:
        super().__init__("tool_not_found", f"工具未注册：{tool_name}", status_code=404)


class ActionConflictError(HarnessixError):
    def __init__(self, message: str) -> None:
        super().__init__("action_conflict", message, status_code=409)


class IdempotencyConflictError(HarnessixError):
    def __init__(self) -> None:
        super().__init__(
            "idempotency_conflict",
            "同一租户和幂等键已经绑定到不同的 Action 载荷",
            status_code=409,
        )


class IllegalTransitionError(HarnessixError):
    def __init__(self, current: object, target: object) -> None:
        super().__init__(
            "illegal_transition", f"不允许从 {current} 转换到 {target}", status_code=409
        )


class UncertainEffectError(RuntimeError):
    """执行器确认外部副作用可能已提交，但无法给出确定结果。"""
