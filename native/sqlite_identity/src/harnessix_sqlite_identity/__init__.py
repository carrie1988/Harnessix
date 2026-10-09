"""Harnessix 内部原连接身份组件；不自动初始化、不打开业务连接。

宿主必须先完成独占启动期 initialize_backend，再使用工厂已固定的设备及 inode
调用 attach_identity。令牌只证明点时 main 身份，不能替代 Task、Owner 鲜读或事务检查。
"""

from ._bridge import (
    BackendUnavailable,
    BridgeError,
    ConnectionIdentityError,
    IdentityToken,
    attach_identity,
    initialize_backend,
)

__all__ = [
    "BackendUnavailable",
    "BridgeError",
    "ConnectionIdentityError",
    "IdentityToken",
    "attach_identity",
    "initialize_backend",
]
