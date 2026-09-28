"""Windows Owner回执的同目录原子发布；旧读句柄保留原MAC快照，不阻塞新名称。"""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.workspace.windows import WindowsWorkspaceRoot
from harnessix.workspace.windows_file_io import WindowsFileOperations


def publish_owner_receipt(path: Path, body: bytes) -> None:
    """复用已有NT写端口；仅调用方已限长的回执，不执行路径fallback或未决重试。"""
    path = Path(os.path.abspath(path))
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with ExitStack() as resources:
        root = WindowsWorkspaceRoot(path.parent)
        resources.callback(root.close)
        chain, _ = root._open_chain(".", data=False)
        resources.callback(root._close_all, chain)
        operations = WindowsFileOperations(root._kernel32)
        operations.require_local_ntfs(chain[-1], root.path.anchor)
        handle = operations.create_temporary(temporary)
        resources.callback(root._kernel32.CloseHandle, handle)
        rename_attempted = False
        try:
            operations.write_and_flush(handle, body)
            # 与旧读者共存，但不是修改旧文件；名称切换后新读者取得新MAC快照。
            rename_attempted = True
            operations.rename(handle, path.name, replace=True)
        except BaseException:
            if not rename_attempted:
                try:
                    operations.delete(handle)
                except (OSError, KernelError):
                    # 清理失败保留临时对象；不能掩盖原故障或沿路径删除后继对象。
                    pass
            # NT未决/错误不得自动删Handle：它可能已经绑定发布后的名称。
            raise
