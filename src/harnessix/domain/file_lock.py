"""跨平台、随文件描述符关闭释放的进程互斥锁原语。"""

from __future__ import annotations

import os


def acquire_exclusive_file_lock(descriptor: int) -> None:
    """非阻塞取得一个字节的独占锁；占用统一归一化为BlockingIOError。"""

    if os.name == "nt":
        import msvcrt

        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(  # type: ignore[attr-defined]
                    descriptor,
                    msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                    1,
                )
            except OSError as error:
                raise BlockingIOError(error.errno, "文件锁已被占用") from error
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def release_exclusive_file_lock(descriptor: int) -> None:
    """立即放开acquire_exclusive_file_lock取得的锁。

    关闭描述符也会放开，但Windows说明这要等系统回收，时间不定；频繁争用的短锁应先显式放开再关闭。
    """

    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)
