"""未执行的原生Patch拒绝及进程内来源标记；不隔离宿主Python代码。"""

from __future__ import annotations

from uuid import UUID

from harnessix.agent.errors import KernelError

_SHA_MISMATCH_ORIGIN = object()
_NO_CHANGE_ORIGIN = object()


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


class WorkspacePatchNoChangeError(KernelError):
    def __init__(self) -> None:
        super().__init__("delivery_no_change", "Workspace事务包含无变化文件")
        self._no_change_origin: object | None = None
        self.unchanged_path: str | None = None
        self.unchanged_sha256: str | None = None
        self.unchanged_mode: int | None = None
        self.review_plan_id: UUID | None = None


def _no_change_error(path: str, sha256: str, mode: int) -> WorkspacePatchNoChangeError:
    error = WorkspacePatchNoChangeError()
    error._no_change_origin = _NO_CHANGE_ORIGIN
    error.unchanged_path = path
    error.unchanged_sha256 = sha256
    error.unchanged_mode = mode
    return error


def is_workspace_patch_no_change(error: object) -> bool:
    return (
        type(error) is WorkspacePatchNoChangeError and error._no_change_origin is _NO_CHANGE_ORIGIN
    )
