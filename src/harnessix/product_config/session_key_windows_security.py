"""Windows密钥保持当前用户Owner及精确受保护DACL；原生机制复用Workspace平台端口。"""

from __future__ import annotations

from typing import Any

from harnessix.product_config.session_key_codec import unavailable
from harnessix.workspace.windows_private_security import (
    PrivateWindowsSecurity,
)
from harnessix.workspace.windows_private_security import (
    SecurityAttributes as SecurityAttributes,
)
from harnessix.workspace.windows_private_security import (
    _AclSize as _AclSize,
)
from harnessix.workspace.windows_private_security import (
    _AllowedAce as _AllowedAce,
)
from harnessix.workspace.windows_private_security import (
    _configure as _configure,
)


class PrivateKeySecurity(PrivateWindowsSecurity):
    """保持密钥原有错误分类和非继承双ACE合同，不采用非密钥状态的继承策略。"""

    def __init__(self, api: Any, kernel: Any) -> None:
        super().__init__(api, kernel, error=unavailable)
