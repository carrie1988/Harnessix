"""Windows Owner回执的同目录原子发布；旧读句柄保留原MAC快照，不阻塞新名称。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.workspace.windows import WindowsWorkspaceRoot
from harnessix.workspace.windows_file_io import WindowsFileOperations


@contextmanager
def _receipt_operations(
    parent: Path,
) -> Iterator[tuple[WindowsWorkspaceRoot, WindowsFileOperations]]:
    """读取和发布共用固定父链，所有退出均回收父Handle及Root。"""
    with ExitStack() as resources:
        root = WindowsWorkspaceRoot(parent)
        resources.callback(root.close)
        chain, _ = root._open_chain(".", data=False)
        resources.callback(root._close_all, chain)
        operations = WindowsFileOperations(root._kernel32)
        operations.require_local_ntfs(chain[-1], root.path.anchor)
        yield root, operations


@contextmanager
def open_owner_receipt(path: Path) -> Iterator[int]:
    """仅Receipt允许名称替换共享；只读、无Write共享，旧FD保留原MAC快照。"""
    import msvcrt

    path = Path(os.path.abspath(path))
    with _receipt_operations(path.parent) as (root, operations):
        try:
            handle = operations.open_existing(path, replace=True)
        except OSError as error:
            # 共享端口以数值Win32码报告IO；投影成CRT/Win32标准异常供原有界重试识别。
            raise OSError(0, "Windows Owner回执读取失败", None, error.errno or 0) from None
        if handle is None:
            raise FileNotFoundError("Process owner回执不存在")
        try:
            info = root._information(handle)
            if info.attributes & (0x400 | 0x10) or info.links != 1:
                raise ValueError("Process owner回执不是普通单链接文件")
            root._assert_under_root(root._final_path(handle))
            descriptor = int(
                msvcrt.__dict__["open_osfhandle"](handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            )
        except BaseException:
            root._kernel32.CloseHandle(handle)
            raise
        # open_osfhandle成功后所有权移交CRT；不能再CloseHandle造成双重关闭。
        try:
            yield descriptor
        finally:
            os.close(descriptor)


def publish_owner_receipt(path: Path, body: bytes) -> None:
    """复用已有NT写端口；仅调用方已限长的回执，不执行路径fallback或未决重试。"""
    path = Path(os.path.abspath(path))
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with _receipt_operations(path.parent) as (root, operations), ExitStack() as resources:
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
