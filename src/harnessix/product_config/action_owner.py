"""产品Action Runtime复用全状态Owner；独立宿主在打开Store前取得同一根外锁。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from harnessix.agent.errors import KernelError
from harnessix.product_config.state_owner import ProductStateOwner, product_state_owner


@contextmanager
def product_action_runtime_lock(
    state_root: Path, *, root_owner: ProductStateOwner | None = None
) -> Iterator[None]:
    """已有产品Owner时只借用；独立调用保留原竞争错误，不建立第二套锁。"""
    if root_owner is not None:
        root_owner.require(state_root)
        yield
        return
    with _standalone_owner(state_root):
        yield


@contextmanager
def _standalone_owner(state_root: Path) -> Iterator[None]:
    # 仅转换取得锁时的错误；不得把Action业务抛出的同名错误误认为启动失败。
    with ExitStack() as resources:
        try:
            resources.enter_context(product_state_owner(state_root))
        except KernelError as error:
            code = (
                "action_runtime_busy"
                if error.code == "product_state_busy"
                else "action_runtime_owner_unavailable"
            )
            raise KernelError(code, "产品Action宿主锁不可用") from None
        yield
