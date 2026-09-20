"""Eval命令共享的私有严格JSON配置读取边界。"""

from __future__ import annotations

import os
import stat

from harnessix.domain.models import ContractModel
from harnessix.models._json import strict_json


def read_private_eval_config[T: ContractModel](
    path: str,
    model: type[T],
    *,
    max_bytes: int = 512 * 1024,
) -> T:
    """只读取0600普通文件，并拒绝链接、模糊JSON、无界内容与类型强转。"""

    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o777 != 0o600:
            raise ValueError("配置必须是0600普通文件")
        raw = source.read(max_bytes + 1)
    if not raw or len(raw) > max_bytes:
        raise ValueError("配置为空或超过上限")
    text = raw.decode("utf-8")
    strict_json(text)
    return model.model_validate_json(text, strict=True)
