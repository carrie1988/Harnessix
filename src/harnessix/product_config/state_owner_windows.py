"""Windows全状态锁端口：原生逐段Handle、私有DACL及不共享锁文件，不作路径回退。"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from harnessix.product_config.session_key_windows_files import WindowsKeyFiles
from harnessix.workspace.windows import WindowsWorkspaceRoot


def prepare_state_parent(state_root: Path) -> None:
    """先锚定已存在的原生父链，再创建缺失私有父目录；不能先mkdir穿过Junction。"""
    missing: list[Path] = []
    ancestor = state_root.parent
    while not ancestor.exists():
        if ancestor.is_symlink() or bool(getattr(ancestor, "is_junction", lambda: False)()):
            raise OSError
        missing.append(ancestor)
        if ancestor.parent == ancestor:
            raise OSError
        ancestor = ancestor.parent
    with ExitStack() as resources:
        root = WindowsWorkspaceRoot(ancestor)
        resources.callback(root.close)
        files = WindowsKeyFiles(root)
        resources.callback(files.close)
        for path in reversed(missing):
            files.create_directory(path)
            handle = files.open(path, directory=True)
            resources.callback(files.kernel.CloseHandle, handle)


@contextmanager
def open_state_owner(state_root: Path, anchor: Path) -> Iterator[tuple[int, Callable[[], None]]]:
    """不持有待恢复Root的句柄；只固定根外父链和私有锚点，允许未来停机Root切换。"""
    with ExitStack() as resources:
        root = WindowsWorkspaceRoot(state_root.parent)
        resources.callback(root.close)
        files = WindowsKeyFiles(root)
        resources.callback(files.close)
        files.create_directory(anchor)
        chain, _final_path = root._open_chain(anchor.name)
        resources.callback(root._close_all, chain)
        directory = files.open(anchor, directory=True)
        resources.callback(files.kernel.CloseHandle, directory)
        directory_identity = root._object_identity(root._information(directory))
        descriptor = files.lock(anchor / ".lock")
        resources.callback(os.close, descriptor)

        import msvcrt

        handle = msvcrt.get_osfhandle(descriptor)  # type: ignore[attr-defined]
        lock_identity = root._object_identity(root._information(handle))

        def checkpoint() -> None:
            files.security.verify(directory)
            files.security.verify(handle)
            info = root._information(handle)
            if (
                directory_identity != root._object_identity(root._information(directory))
                or lock_identity != root._object_identity(info)
                or info.attributes & 0x410
                or info.links != 1
            ):
                raise OSError

        checkpoint()
        yield descriptor, checkpoint
