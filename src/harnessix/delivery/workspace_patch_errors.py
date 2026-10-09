"""SHA拒绝的固定错误与进程内来源标记；不隔离宿主Python代码。"""

from __future__ import annotations

from uuid import UUID

from harnessix.agent.errors import KernelError

_SHA_MISMATCH_ORIGIN = object()


class WorkspacePatchPreconditionError(KernelError):
    def __init__(self) -> None:
        super().__init__(
            "workspace_patch_precondition_failed",
            "expected_sha256不匹配；请重新调用目标文件的read_file，"
            "仅使用digest_status=complete时的content_sha256，禁止猜测摘要",
        )
        self._sha_origin: object | None = None
        self.review_plan_id: UUID | None = None


def _sha_mismatch_error() -> WorkspacePatchPreconditionError:
    error = WorkspacePatchPreconditionError()
    error._sha_origin = _SHA_MISMATCH_ORIGIN
    return error


def is_workspace_patch_sha_mismatch(error: object) -> bool:
    return (
        type(error) is WorkspacePatchPreconditionError and error._sha_origin is _SHA_MISMATCH_ORIGIN
    )
