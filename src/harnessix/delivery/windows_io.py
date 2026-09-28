"""Windows文件事务的兼容导入与宿主支持声明；原生IO由Workspace共享端口实现。"""

from __future__ import annotations

import os

from harnessix.workspace.windows_file_io import (
    WindowsFileOperations as WindowsFileOperations,
)
from harnessix.workspace.windows_file_io import (
    _IoStatusBlock as _IoStatusBlock,
)
from harnessix.workspace.windows_file_io import (
    _rename_buffer as _rename_buffer,
)
from harnessix.workspace.windows_file_io import (
    _RenameInfo as _RenameInfo,
)


def windows_transaction_supported() -> bool:
    """广告原生Win32候选的宿主边界；卷、路径、元数据在每次操作时再次核验。"""

    if os.name != "nt":
        return False
    import sys

    version = getattr(sys, "getwindowsversion", None)
    return version is not None and version().build >= 22000
