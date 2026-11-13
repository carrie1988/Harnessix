"""跨平台文件锁的稳定兼容导出。"""

from __future__ import annotations

from harnessix.domain.file_lock import (
    acquire_exclusive_file_lock as acquire_exclusive_file_lock,
)
from harnessix.domain.file_lock import (
    release_exclusive_file_lock as release_exclusive_file_lock,
)
