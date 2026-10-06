"""固定 Git 报告路径的原生句柄能力；不跟随链接，不接受模型选择 Index。"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_identity import _identity, _path_sha256
from harnessix.product_config.git_delivery_plan_contracts import GitIndexFileObservation
from harnessix.workspace.contracts import PlatformKind
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.snapshot import _digest, _NativeRoot, _open_native


def invalid_git_user_paths() -> KernelError:
    """只公开固定分类，第三方路径和原始 Index 均不进入错误信息。"""
    return KernelError("git_user_observation_path_invalid", "Git用户观察路径或原生对象无效")


def reported_git_path(body: bytes, root: Path, *, absolute: bool = False) -> Path:
    """固定完整单行报告；拒绝歧义、控制字符和会隐藏链接语义的遍历路径。"""
    try:
        if type(body) is not bytes or not 2 <= len(body) <= 4096 or not body.endswith(b"\n"):
            raise ValueError
        text = body[:-1].decode("utf-8", "strict")
        if any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ValueError
        path = Path(text)
        if ".." in path.parts or (absolute and not path.is_absolute()):
            raise ValueError
        result = path if path.is_absolute() else root / path
        if not result.is_absolute():
            raise ValueError
        return Path(os.path.abspath(result))
    except (ValueError, UnicodeError, OSError):
        raise invalid_git_user_paths() from None


def _physical_path(path: Path) -> None:
    """POSIX 不先 resolve 掩盖链接；Windows 由原逐段句柄链拒绝 Reparse。"""
    try:
        if os.name == "posix" and path.resolve(strict=True) != path:
            raise invalid_git_user_paths()
    except (OSError, RuntimeError):
        raise invalid_git_user_paths() from None


@dataclass(frozen=True, slots=True)
class PinnedGitUserDirectories:
    """本次读取持有实际 common/admin 根；生命周期不能沿用至后续批准执行。"""

    common: Path = field(repr=False)
    admin: Path = field(repr=False)
    index: _NativeRoot = field(repr=False)
    common_identity: str
    admin_identity: str

    def verify(self, checkpoint: Callable[[], None]) -> None:
        """固定路径及物理身份在每次观察前后复核，不只比较内容 SHA。"""
        checkpoint()
        _physical_path(self.common)
        _physical_path(self.admin)
        if (
            _identity(self.common, directory=True) != self.common_identity
            or _identity(self.admin, directory=True) != self.admin_identity
        ):
            raise KernelError("git_user_observation_changed", "Git用户观察期间绑定发生变化")
        checkpoint()

    def observe_index(self, checkpoint: Callable[[], None]) -> GitIndexFileObservation:
        """原 no-follow 端口读取固定 index，完整字节仅参与摘要而不导出。"""
        self.verify(checkpoint)

        def controlled() -> None:
            try:
                checkpoint()
            except BaseException as error:
                raise UpstreamCheckpointError(error) from None

        try:
            observed = self.index.observe("index", access="read", checkpoint=controlled)
        except UpstreamCheckpointError as error:
            raise error.error from None
        checkpoint()
        if observed.kind == "missing":
            result = GitIndexFileObservation(presence="absent", identity=None, sha256=None, size=0)
        elif observed.kind == "file" and observed.content is not None:
            if len(observed.content) != observed.size:
                raise invalid_git_user_paths()
            result = GitIndexFileObservation(
                presence="file",
                identity=_digest(observed.identity),
                sha256=hashlib.sha256(observed.content).hexdigest(),
                size=observed.size,
            )
        else:
            raise invalid_git_user_paths()
        self.verify(checkpoint)
        return result


@contextmanager
def pin_git_user_directories(
    common: Path, admin: Path, *, checkpoint: Callable[[], None]
) -> Iterator[PinnedGitUserDirectories]:
    """复用原双平台根能力；仅关闭本次打开的句柄，不修改目录、Store 或权限。"""
    checkpoint()
    platform: PlatformKind = "windows" if os.name == "nt" else "posix"
    _physical_path(common)
    _physical_path(admin)
    with ExitStack() as resources:
        common_identity = _identity(common, directory=True)
        admin_identity = _identity(admin, directory=True)
        common_root = _open_native(common, platform)
        resources.callback(common_root.close)
        admin_root = common_root if common == admin else _open_native(admin, platform)
        if admin_root is not common_root:
            resources.callback(admin_root.close)
        if common_root.path != common or admin_root.path != admin:
            raise invalid_git_user_paths()
        pinned = PinnedGitUserDirectories(
            common, admin, admin_root, common_identity, admin_identity
        )
        pinned.verify(checkpoint)
        yield pinned
        pinned.verify(checkpoint)


def git_user_directory_facts(pinned: PinnedGitUserDirectories) -> dict[str, str]:
    """只输出原路径摘要与物理身份，原始目录文本保持私有。"""
    return {
        "common_directory_path_sha256": _path_sha256(pinned.common),
        "common_directory_identity": pinned.common_identity,
        "git_directory_path_sha256": _path_sha256(pinned.admin),
        "git_directory_identity": pinned.admin_identity,
    }
