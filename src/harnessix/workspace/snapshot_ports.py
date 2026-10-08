"""宿主注入的原私有CAS端口；不持有业务Store、批准或迁移权限。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from harnessix.workspace.native_observation_io import UpstreamCheckpointError

type WorkspacePureProgressFactory = Callable[[], AbstractContextManager[Callable[[], None]]]


def observed_workspace_pure_progress(
    factory: WorkspacePureProgressFactory, observer: Callable[[], None]
) -> WorkspacePureProgressFactory:
    """每个原局部检查点仍消费 Store 自身观察，不替换共享构造回调。"""

    @contextmanager
    def progress() -> Iterator[Callable[[], None]]:
        with factory() as local:

            def check() -> None:
                observer()
                local()

            yield check

    return progress


def protected_workspace_pure_progress(
    factory: WorkspacePureProgressFactory,
    *,
    mark_error: Callable[[BaseException], UpstreamCheckpointError] = UpstreamCheckpointError,
) -> WorkspacePureProgressFactory:
    """只标记工厂／检查点控制一层；主体首失败不被出口或抑制行为覆盖。"""

    @contextmanager
    def progress() -> Iterator[Callable[[], None]]:
        body_error: BaseException | None = None
        try:
            with factory() as local:

                def check() -> None:
                    try:
                        local()
                    except BaseException as error:
                        raise mark_error(error) from None

                try:
                    yield check
                except BaseException as error:
                    body_error = error
                    raise
        except BaseException as error:
            if body_error is not None:
                raise body_error from None
            raise mark_error(error) from None
        if body_error is not None:
            raise body_error

    return progress


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshotPorts:
    """完整历史的耐久写入与回读；操作检查点由每次调用单独持有。"""

    write_blob: Callable[[str, bytes], None]
    read_blob: Callable[[str], bytes]
