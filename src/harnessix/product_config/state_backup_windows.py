"""Windows私有状态发布：复用原同目录NT句柄Rename，不重开被锚定的父目录。"""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path
from typing import cast

from harnessix.agent.errors import KernelError
from harnessix.delivery.windows_io import WindowsFileOperations
from harnessix.product_config.session_key_windows_files import WindowsKeyFiles
from harnessix.workspace.windows import WindowsWorkspaceRoot
from harnessix.workspace.windows_private_security import PrivateStateSecurity


def publish_private_object(source: Path, target: Path) -> None:
    """仅同一私有父目录的既有文件或目录；不覆盖、不复制、不共享DELETE或重试。"""
    if (
        not source.is_absolute()
        or not target.is_absolute()
        or os.path.normcase(str(source.parent)) != os.path.normcase(str(target.parent))
    ):
        raise _invalid()
    try:
        with ExitStack() as resources:
            root = WindowsWorkspaceRoot(target.parent)
            resources.callback(root.close)
            chain, _ = root._open_chain(".", data=False)
            resources.callback(root._close_all, chain)
            operations = WindowsFileOperations(root._kernel32)
            operations.require_local_ntfs(chain[-1], root.path.anchor)
            files = WindowsKeyFiles(root, security_factory=PrivateStateSecurity)
            resources.callback(files.close)
            security = cast(PrivateStateSecurity, files.security)
            handle = operations.open_existing(source, delete=True)
            if handle is None:
                raise _invalid()
            resources.callback(root._kernel32.CloseHandle, handle)
            info = root._information(handle)
            if info.attributes & 0x400 or (not info.attributes & 0x10 and info.links != 1):
                raise _invalid()
            if info.attributes & 0x10:
                security.verify_root(handle)
            else:
                security.verify(handle)
            # 原事务端口通过源Handle定位同一父目录，flags=0禁止替换；不走MoveFileEx路径重开。
            operations.rename(handle, target.name, replace=False)
    except (OSError, ValueError):
        raise _invalid() from None


def _invalid() -> KernelError:
    return KernelError("product_backup_files_invalid", "产品备份文件权限、身份或容量无效")
