"""宿主持有任务依赖目录；模型只能在固定容器挂载内写入非Secret依赖。"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.trusted_actions.contracts import ActionRoutePlan
from harnessix.workspace.contracts import ResourceAccess

from .action_contracts import ProductProcessProfile

DependencyRoots = dict[str, tuple[Path, tuple[ResourceAccess, ...]]]
_ACCESS: tuple[ResourceAccess, ...] = ("read", "write", "execute")
_LOCATION = re.compile(r"^deps_([0-9a-f]{32})$")
_PROFILE_HASH = re.compile(r"^[0-9a-f]{64}$")
_QUARANTINE = ".quarantined"


def _invalid() -> KernelError:
    return KernelError("task_dependencies_invalid", "任务依赖目录身份或权限无效")


def _open_directory(path: Path) -> int:
    """逐段拒绝符号链接；后续创建和校验均相对已经打开的目录进行。"""

    if not path.is_absolute() or ".." in path.parts:
        raise _invalid()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _check_directory(descriptor: int, mode: int) -> None:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise _invalid()


def _child_directory(parent: int, name: str, mode: int, *, create: bool) -> int:
    created = False
    if create:
        try:
            os.mkdir(name, mode, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if created:
            # 只校正本次新建目录的umask，不修复任何既有目录。
            os.fchmod(descriptor, mode)
        _check_directory(descriptor, mode)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _check_quarantine(parent: int, *, allow: bool) -> None:
    try:
        info = os.stat(_QUARANTINE, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise _invalid()
    if not allow:
        raise KernelError("task_dependencies_quarantined", "任务依赖环境存在未决效果，禁止复用")


class TaskDependencyEnvironments:
    """按可信Thread与Profile隔离持久依赖；私有父目录不挂载给容器。"""

    def __init__(self, state_root: Path, workspace_root: Path) -> None:
        self._state_root = Path(state_root)
        self._workspace_root = Path(workspace_root)
        try:
            if os.name != "posix":
                raise _invalid()
            for path in (self._state_root, self._workspace_root):
                descriptor = _open_directory(path)
                try:
                    if path == self._state_root:
                        _check_directory(descriptor, 0o700)
                finally:
                    os.close(descriptor)
            if self._state_root.is_relative_to(self._workspace_root) or (
                self._workspace_root.is_relative_to(self._state_root)
            ):
                raise _invalid()
        except (OSError, ValueError):
            raise _invalid() from None

    @staticmethod
    def _profile_hash(profile: ProductProcessProfile) -> str:
        if (
            not profile.persistent_dependencies
            or profile.selector_policy != "shell_command"
            or _PROFILE_HASH.fullmatch(profile.profile_sha256) is None
        ):
            raise _invalid()
        return profile.profile_sha256

    def _leaf(self, thread_id: UUID, profile: ProductProcessProfile) -> Path:
        if not isinstance(thread_id, UUID):
            raise _invalid()
        return (
            self._state_root
            / "task-dependencies"
            / thread_id.hex
            / self._profile_hash(profile)
            / "deps"
        )

    @contextmanager
    def _environment(
        self,
        thread_id: UUID,
        profile: ProductProcessProfile,
        *,
        create: bool,
        allow_quarantined: bool = False,
    ) -> Iterator[tuple[Path, int]]:
        leaf = self._leaf(thread_id, profile)
        descriptors: list[int] = []
        try:
            parent = _open_directory(self._state_root)
            descriptors.append(parent)
            _check_directory(parent, 0o700)
            for name in ("task-dependencies", thread_id.hex, self._profile_hash(profile)):
                parent = _child_directory(parent, name, 0o700, create=create)
                descriptors.append(parent)
            _check_quarantine(parent, allow=allow_quarantined)
            descriptors.append(_child_directory(parent, "deps", 0o777, create=create))
            yield leaf, parent
        except (OSError, ValueError):
            raise _invalid() from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _thread_id(location: str) -> UUID:
        if type(location) is not str or (match := _LOCATION.fullmatch(location)) is None:
            raise _invalid()
        return UUID(hex=match.group(1))

    def for_thread(self, thread_id: UUID, profile: ProductProcessProfile) -> DependencyRoots:
        with self._environment(thread_id, profile, create=True) as (leaf, _):
            return {f"deps_{thread_id.hex}": (leaf, _ACCESS)}

    def validate(
        self,
        mapping: Mapping[str, tuple[str | Path, tuple[ResourceAccess, ...]]],
        profile: ProductProcessProfile,
        *,
        allow_quarantined: bool = False,
    ) -> None:
        if not isinstance(mapping, Mapping) or len(mapping) != 1:
            raise _invalid()
        location, configured = next(iter(mapping.items()))
        thread_id = self._thread_id(location)
        if type(configured) is not tuple or len(configured) != 2:
            raise _invalid()
        path, access = configured
        if not isinstance(path, (str, Path)) or access != _ACCESS or type(access) is not tuple:
            raise _invalid()
        with self._environment(
            thread_id, profile, create=False, allow_quarantined=allow_quarantined
        ) as (leaf, _):
            if Path(path) != leaf:
                raise _invalid()

    def for_route(
        self,
        route: ActionRoutePlan,
        profile: ProductProcessProfile,
        *,
        allow_quarantined: bool = False,
    ) -> DependencyRoots:
        roots = route.execution.workspace.external_roots
        if len(roots) != 1 or roots[0].access != _ACCESS:
            raise _invalid()
        thread_id = self._thread_id(roots[0].location)
        with self._environment(
            thread_id, profile, create=False, allow_quarantined=allow_quarantined
        ) as (leaf, _):
            if roots[0].path_digest != canonical_digest(str(leaf)):
                raise _invalid()
            return {roots[0].location: (leaf, _ACCESS)}

    def quarantine(self, route: ActionRoutePlan, profile: ProductProcessProfile) -> None:
        mapping = self.for_route(route, profile, allow_quarantined=True)
        thread_id = self._thread_id(next(iter(mapping)))
        with self._environment(thread_id, profile, create=False, allow_quarantined=True) as (
            _,
            parent,
        ):
            try:
                descriptor = os.open(
                    _QUARANTINE,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
            except FileExistsError:
                _check_quarantine(parent, allow=True)
                return
            try:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent)
