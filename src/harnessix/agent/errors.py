from harnessix.domain.errors import HarnessixError


class KernelError(HarnessixError):
    """仅携带可公开的错误分类和消息，不持久化第三方异常原文。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status_code=409)
