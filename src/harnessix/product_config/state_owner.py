"""产品全状态Owner：根外稳定锁覆盖目录准备、全部Store及Provider生命周期。"""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.domain.file_lock import acquire_exclusive_file_lock


def state_owner_anchor(state_root: Path) -> Path:
    """从根目录地址派生外部锁目录；折叠名称宁可多互斥，不允许大小写别名绕过。"""
    name = unicodedata.normalize("NFC", state_root.name).casefold()
    digest = hashlib.sha256(os.fsencode(name)).hexdigest()
    return state_root.parent / (".harnessix-state-owner-" + digest)


def _state_address(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    if not candidate.name or (os.name == "nt" and str(candidate).startswith("\\\\")):
        raise OSError
    if (
        candidate.exists()
        or candidate.is_symlink()
        or bool(getattr(candidate, "is_junction", lambda: False)())
    ):
        info = candidate.lstat()
        if not stat.S_ISDIR(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400):
            raise OSError
    if os.name == "nt":
        # 在resolve扩展短名称之前先使用原生逐段Handle拒绝Reparse别名。
        from harnessix.product_config.state_owner_windows import prepare_state_parent
        from harnessix.workspace.windows import WindowsWorkspaceRoot

        prepare_state_parent(candidate)
        bound = WindowsWorkspaceRoot(candidate if candidate.exists() else candidate.parent)
        try:
            return candidate.resolve(strict=False)
        finally:
            bound.close()
    candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return candidate.parent.resolve(strict=True) / candidate.name


def _lock_identity(descriptor: int) -> tuple[int, ...]:
    info = os.fstat(descriptor)
    return info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size


def _lock_body(descriptor: int) -> bytes:
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        return os.read(descriptor, 2)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)


class ProductStateOwner:
    """仅在原上下文内有效的本地Owner；不得序列化、转交另一Root或延长生命周期。"""

    _state_root: Path
    _checkpoint: Callable[[], None]
    _active: bool

    def __init__(self) -> None:
        raise TypeError("ProductStateOwner必须由product_state_owner上下文创建")

    @classmethod
    def _from_context(cls, state_root: Path, checkpoint: Callable[[], None]) -> ProductStateOwner:
        owner = object.__new__(cls)
        owner._state_root = state_root
        owner._checkpoint = checkpoint
        owner._active = True
        return owner

    @property
    def state_root(self) -> Path:
        """返回取得锁时固定的根地址；不是待恢复目录当前inode的身份声明。"""
        return self._state_root

    def require(self, state_root: Path) -> None:
        """进入受管子生命周期前复核原地址、锁句柄和私有目录，不重入OS锁。"""
        if not self._active:
            raise KernelError("product_state_owner_invalid", "产品状态Owner已失效")
        candidate = Path(os.path.abspath(state_root))
        try:
            if state_owner_anchor(candidate).name != state_owner_anchor(self.state_root).name or (
                not candidate.parent.samefile(self.state_root.parent)
            ):
                raise OSError
            self._checkpoint()
        except (OSError, KernelError, ValueError):
            raise KernelError("product_state_owner_invalid", "产品状态Owner身份无效") from None


@contextmanager
def product_state_owner(state_root: Path) -> Iterator[ProductStateOwner]:
    """非阻塞持有根外互斥；不创建状态Root、不删除锁文件，退出时由OS释放。"""
    with ExitStack() as resources:
        try:
            root = _state_address(state_root)
            if os.name == "posix":
                from harnessix.product_config.state_owner_posix import open_state_owner
            elif os.name == "nt":
                from harnessix.product_config.state_owner_windows import open_state_owner
            else:
                raise OSError
            descriptor, verify_files = resources.enter_context(
                open_state_owner(root, state_owner_anchor(root))
            )
            acquire_exclusive_file_lock(descriptor)
            if os.fstat(descriptor).st_size == 0:
                if os.write(descriptor, b"\0") != 1:
                    raise OSError
                os.fsync(descriptor)
            if _lock_body(descriptor) != b"\0":
                raise OSError
            identity = _lock_identity(descriptor)
            verify_files()
        except BlockingIOError:
            raise KernelError("product_state_busy", "产品状态已有活跃宿主") from None
        except KernelError as error:
            if error.code == "publication_key_busy":
                raise KernelError("product_state_busy", "产品状态已有活跃宿主") from None
            raise KernelError("product_state_owner_unavailable", "产品状态宿主锁不可用") from None
        except (OSError, RuntimeError, ValueError):
            raise KernelError("product_state_owner_unavailable", "产品状态宿主锁不可用") from None

        def checkpoint() -> None:
            verify_files()
            if _lock_identity(descriptor) != identity or _lock_body(descriptor) != b"\0":
                raise OSError

        owner = ProductStateOwner._from_context(root, checkpoint)
        try:
            yield owner
        finally:
            # 先撤销借用资格，再由ExitStack关闭原锁及目录句柄。
            owner._active = False
