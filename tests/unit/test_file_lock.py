from __future__ import annotations

import os
from pathlib import Path

import pytest

from harnessix.file_lock import acquire_exclusive_file_lock


def test_exclusive_file_lock_is_non_blocking_and_released_on_close(tmp_path: Path) -> None:
    path = tmp_path / "owner.lock"
    path.write_bytes(b"\0")
    first = os.open(path, os.O_RDWR)
    second = os.open(path, os.O_RDWR)
    try:
        acquire_exclusive_file_lock(first)
        with pytest.raises(BlockingIOError):
            acquire_exclusive_file_lock(second)
        os.close(first)
        first = -1
        acquire_exclusive_file_lock(second)
    finally:
        if first >= 0:
            os.close(first)
        os.close(second)
