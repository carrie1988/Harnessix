"""现有文件权限的观察：返回真实普通权限位，特殊位拒绝（ADR 0068第2版）。"""

from __future__ import annotations

import stat

from harnessix.agent.errors import KernelError

_SPECIAL_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX


def observed_permission_mode(st_mode: int) -> int:
    """setuid、setgid、sticky位会改变执行身份或目录语义，Workspace事务不处理这类文件。"""
    if st_mode & _SPECIAL_BITS:
        raise KernelError(
            "delivery_metadata_unsupported", "Workspace事务不支持带setuid、setgid或sticky位的文件"
        )
    return stat.S_IMODE(st_mode)
