"""未执行的原生Patch拒绝及进程内来源标记；不隔离宿主Python代码。

拒绝正文写给模型（英文）：指出出错的files下标、宿主已核对到的事实和最小改法，不含文件内容；
已定位的拒绝还注明整批未应用。界面按错误码另行展示用户文案（product_ui.tool_summary）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from harnessix.agent.errors import KernelError
from harnessix.execution.public_tool_contracts import MAX_WORKSPACE_PATCH_FILES

_SHA_MISMATCH_ORIGIN = object()
_NO_CHANGE_ORIGIN = object()
_PREPARATION_ORIGIN = object()
# 精确编辑规划器（patches.planner）的确定性提案错误：只读了前镜像，模型改正该处编辑即可重发。
PLANNER_EDIT_REJECTIONS = frozenset(
    {
        "patch_context_not_found",
        "patch_ambiguous_context",
        "patch_overlapping_edits",
        "patch_limit_exceeded",
        "patch_no_change",
    }
)


class _LocatedPatchRejection(KernelError):
    """尚未执行的Patch拒绝；file_index是模型所发files数组中的下标，未定位时为None。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self.file_index: int | None = None
        self.review_plan_id: UUID | None = None


class WorkspacePatchPreconditionError(_LocatedPatchRejection):
    def __init__(self) -> None:
        super().__init__(
            "workspace_patch_precondition_failed",
            "expected_sha256 does not match the current file, which changed after that digest "
            "was read; call read_file on this file and resend with the content_sha256 it returns "
            "(digest_status=complete), never a guessed digest",
        )
        self._sha_origin: object | None = None


def _sha_mismatch_error() -> WorkspacePatchPreconditionError:
    error = WorkspacePatchPreconditionError()
    error._sha_origin = _SHA_MISMATCH_ORIGIN
    return error


def is_workspace_patch_sha_mismatch(error: object) -> bool:
    return (
        type(error) is WorkspacePatchPreconditionError and error._sha_origin is _SHA_MISMATCH_ORIGIN
    )


class WorkspacePatchNoChangeError(_LocatedPatchRejection):
    def __init__(self) -> None:
        super().__init__(
            "delivery_no_change",
            "the file already has exactly this content, so nothing would change; remove this "
            "entry and resend the remaining files",
        )
        self._no_change_origin: object | None = None
        self.unchanged_path: str | None = None
        self.unchanged_sha256: str | None = None
        self.unchanged_mode: int | None = None


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


class WorkspacePatchPreparationError(_LocatedPatchRejection):
    """可信Patch在执行前发现、可由模型修正的确定性提案错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)
        self._preparation_origin: object | None = None


def workspace_patch_preparation_error(code: str, message: str) -> WorkspacePatchPreparationError:
    error = WorkspacePatchPreparationError(code, message)
    error._preparation_origin = _PREPARATION_ORIGIN
    return error


def workspace_patch_preparation_timeout_error() -> WorkspacePatchPreparationError:
    """规划只读了工作区、尚未保存事务，超时可如实报告未执行，交模型重试或拆小。"""
    return workspace_patch_preparation_error(
        "workspace_patch_preparation_timeout",
        "preparing the approval preview timed out (large workspace or busy disk); the patch "
        "was not executed and the workspace is unchanged; retry, or split it into smaller patches",
    )


def unexecuted_planning_error(error: KernelError) -> WorkspacePatchPreparationError:
    """规划期只读了工作区就确定的拒绝（权限位无法保留、路径只差大小写）：注明本次未执行。

    原说明来自Delivery规划器与路径大小写检查，其他交付路径也在用，含磁盘上的实际名称；
    仍是中文，ADR 0129未改。
    """
    return workspace_patch_preparation_error(
        error.code, f"{error.message}；本次Patch未执行，工作区没有变化"
    )


def is_workspace_patch_preparation_error(error: object) -> bool:
    return (
        type(error) is WorkspacePatchPreparationError
        and error._preparation_origin is _PREPARATION_ORIGIN
    )


def target_exists_error(kind: str = "file") -> WorkspacePatchPreparationError:
    """create的目标已存在；kind是现有条目的种类（file或directory），两种情况的改法不同。"""
    return workspace_patch_preparation_error(
        "workspace_patch_target_exists",
        "create targets a path that already exists; to change that file use operation edit "
        "with edits, to overwrite it use replace with its expected_sha256, or pick a new path"
        if kind == "file"
        else "create targets an existing directory; use the path of a file inside it",
    )


def target_missing_error(operation: str, kind: str = "missing") -> WorkspacePatchPreparationError:
    """edit、replace或delete的目标不是现有普通文件；kind是观察到的现状（missing或directory）。"""
    return workspace_patch_preparation_error(
        "workspace_patch_target_missing",
        f"{operation} targets a path that is not an existing regular file; check the path with "
        "glob, or use operation create with content and mode for a new file"
        if kind == "missing"
        else f"{operation} targets a directory; a patch only changes files, so address the "
        "files inside it one by one",
    )


def unsafe_full_replace_error(new_bytes: int, current_bytes: int) -> WorkspacePatchPreparationError:
    """replace的content须是完整新文件；明显短于现有文件多半是把局部片段当成了全文。"""
    return workspace_patch_preparation_error(
        "workspace_patch_unsafe_full_replace",
        f"replace content is {new_bytes} bytes but the file is {current_bytes} bytes, so over a "
        "quarter of it would be deleted; content must be the complete new file. To change part "
        "of the file use operation edit with edits; to remove the file use delete",
    )


@contextmanager
def locating_patch_file(index: int) -> Iterator[None]:
    """块内抛出的Patch拒绝记下所属files下标（已定位的不覆盖），模型据此只改出错的一项。"""
    try:
        yield
    except _LocatedPatchRejection as error:
        if error.file_index is None:
            error.file_index = index
        raise


def unapplied_batch_notice(file_count: int) -> str:
    """整批被拒后补给模型的事实：批次是原子的，一项被拒时没有任何文件写入，同批的其余文件项也没有。

    只陈述事实，不给重发指令：各条拒绝已写明自己的改法，有的要求删掉出错项。没有这句时，
    模型无从知道同批里无错的文件是否已经写入（Beta HX-13：同一批次连续失败18次，
    其中的新建文件始终没有落盘）。
    """
    if file_count < 2:
        return "Nothing was written."
    others = "file entry was" if file_count == 2 else f"{file_count - 1} file entries were"
    return f"Nothing was written: the batch is atomic, so the other {others} not applied either."


def located_patch_message(message: str, file_index: object, file_count: int) -> str:
    """给拒绝正文加上files下标并注明整批未应用；下标必须是合同范围内的整数，否则原样返回。

    未定位的拒绝正文里已写明本次未执行，不再追加：准备超时是英文；规划期的确定性拒绝是中文，
    且原样显示在界面上。
    """
    if type(file_index) is not int or not 0 <= file_index < MAX_WORKSPACE_PATCH_FILES:
        return message
    separator = "." if message.startswith("edits[") else ": "
    return f"files[{file_index}]{separator}{message}. {unapplied_batch_notice(file_count)}"
