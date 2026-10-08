"""同次 Git 消费的原生来源资源所有权；只登记已完成 U 验证的末端读集合。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from types import TracebackType
from typing import Self

from harnessix.agent.errors import KernelError
from harnessix.product_config.git_user_observation_contracts import (
    ProductGitUserObservation,
    product_git_user_observation_fingerprint,
)
from harnessix.product_config.git_user_source_files import (
    GitUserSourceFiles,
    pin_git_user_source_files,
)


class GitUserSourceScope:
    """原消费者拥有一个根集合；受管子任务可登记 U，但不能签发业务能力。"""

    def __init__(self) -> None:
        self._active = False
        self._resources = ExitStack()
        self._binding: tuple[Path, Path, Path] | None = None
        self._sources: GitUserSourceFiles | None = None
        self._verified: set[str] = set()

    def __enter__(self) -> Self:
        if self._active:
            raise self._invalid()
        self._active = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._active = False
        self._binding = None
        self._sources = None
        self._verified.clear()
        self._resources.__exit__(exc_type, exc, traceback)

    def pin(
        self, root: Path, common: Path, admin: Path, check: Callable[[], None]
    ) -> GitUserSourceFiles:
        """原根集合不移动；重复 U 只重验，不按关联数量累计原生句柄。"""
        self._require_active()
        binding = (root, common, admin)
        if self._sources is None:
            self._sources = self._resources.enter_context(
                pin_git_user_source_files(root, common, admin, checkpoint=check)
            )
            self._binding = binding
        else:
            if self._binding != binding:
                raise self._invalid()
            self._sources.verify(check)
        return self._sources

    def retain(self, expected: ProductGitUserObservation, sources: GitUserSourceFiles) -> None:
        """指纹仅查找已完成验证的原 U；原生快照本身不充当认证凭据。"""
        self._require_active()
        if sources is not self._sources or self._sources is None:
            raise self._invalid()
        self._verified.add(product_git_user_observation_fingerprint(expected))

    def require(self, expected: ProductGitUserObservation, check: Callable[[], None]) -> None:
        """全集每条原 U 都须登记并同步复核，不能以最后一条代表全集。"""
        self._require_active()
        if (
            product_git_user_observation_fingerprint(expected) not in self._verified
            or self._sources is None
        ):
            raise self._invalid()
        self._sources.verify(check)

    def _require_active(self) -> None:
        if not self._active:
            raise self._invalid()

    @staticmethod
    def _invalid() -> KernelError:
        return KernelError("git_user_observation_changed", "Git用户观察原生来源不可验证")
