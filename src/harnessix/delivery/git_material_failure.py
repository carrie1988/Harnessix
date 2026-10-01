"""材料Worker的有限失败帧；不含业务权限、正文或自动恢复语义。"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

from harnessix.delivery.git_material_input_contracts import GitMaterialInputError

FAILURE_SCHEMA = "harnessix.git-material-worker-failure/v1"
FAILURE_PREFIX = b"HX_GIT_MATERIAL_WORKER_FAILURE "
MAX_FAILURE_FRAME_BYTES = 2048
STAGES = frozenset(
    "unobserved entry stream_mode arguments input launch_binding command namespace snapshot "
    "git_launch git_wait git_join git_validate git_cleanup snapshot_recheck command_recheck "
    "namespace_recheck proof resources_close proof_output".split()
)
# 只取四个原材料模块的固定code，不将任意GitMaterialInputError.code直接序列化。
MATERIAL_ERROR_CODES = frozenset(
    "git_material_input_invalid git_material_worker_invalid git_material_worker_timeout "
    "git_material_binding_changed git_material_body_changed git_material_configuration_invalid "
    "git_material_namespace_invalid git_material_namespace_limit git_material_private_invalid "
    "git_material_platform_unsupported git_material_launch_mismatch git_material_git_failed "
    "git_material_proof_invalid git_material_implementation_unavailable".split()
)
_GENERIC_CODES = {
    OSError: "os_error",
    PermissionError: "os_error",
    FileNotFoundError: "os_error",
    BrokenPipeError: "os_error",
    TimeoutError: "os_error",
    ValueError: "value_error",
    TypeError: "type_error",
    subprocess.SubprocessError: "subprocess_error",
    subprocess.TimeoutExpired: "subprocess_error",
    subprocess.CalledProcessError: "subprocess_error",
}
_STATE_FIELDS = frozenset(
    "stage git_popen_returned git_returncode git_stdout_complete git_stdout_expected".split()
)
_OBSERVATION_FIELDS = _STATE_FIELDS | {"_first_frame"}
_FRAME_FIELDS = _STATE_FIELDS | {"schema", "error_code", "origin", "handler_error_code"}
_ERROR_CODES = MATERIAL_ERROR_CODES | frozenset(_GENERIC_CODES.values()) | {"unclassified"}


@dataclass
class GitMaterialFailureObservation:
    """只保存同调用内已观察的标量；False／None不证明外部效果未发生。"""

    stage: str = "entry"
    git_popen_returned: bool = False
    git_returncode: int | None = None
    git_stdout_complete: bool | None = None
    git_stdout_expected: bool | None = None
    # 仅冻结严格有限帧，不保留原异常对象、args或动态正文。
    _first_frame: bytes | None = field(default_factory=lambda: None, init=False, repr=False)


def _valid(record: object) -> bool:
    """同时检查实际类型、字段集合和有限观察的一致性，不先规范化输入。"""
    if (
        type(record) is not dict
        or len(record) != len(_FRAME_FIELDS)
        or any(type(key) is not str for key in record)
        or set(record) != _FRAME_FIELDS
    ):
        return False
    if any(
        type(record[key]) is not str
        for key in ("schema", "stage", "error_code", "origin", "handler_error_code")
    ) or (
        record["schema"] != FAILURE_SCHEMA
        or record["stage"] not in STAGES
        or record["error_code"] not in _ERROR_CODES
        or record["handler_error_code"] not in _ERROR_CODES
        or record["origin"] not in {"pre_cleanup", "handler"}
        or (record["origin"] == "handler" and record["error_code"] != record["handler_error_code"])
    ):
        return False
    return _git_valid(record)


def _git_valid(record: dict[str, object]) -> bool:
    """Git观察只允许原wait结果与实际已执行谓词，不补做业务检查。"""
    returned, code = record["git_popen_returned"], record["git_returncode"]
    complete, expected = record["git_stdout_complete"], record["git_stdout_expected"]
    if (
        type(returned) is not bool
        or (code is not None and (type(code) is not int or not -(2**31) <= code < 2**32))
        or any(value is not None and type(value) is not bool for value in (complete, expected))
    ):
        return False
    return (
        (returned or (code is None and complete is None and expected is None))
        and (complete is None or code is not None)
        and (expected is None or (complete is True and code == 0))
    )


def _error_code(error: BaseException) -> str:
    """精确类型和原dict的固定code查表；不调用任意异常属性或文本方法。"""
    if type(error) is GitMaterialInputError:
        attributes = object.__getattribute__(error, "__dict__")
        code = (
            attributes.get("code")
            if (
                type(attributes) is dict
                and len(attributes) == 1
                and all(type(key) is str for key in attributes)
                and "code" in attributes
            )
            else None
        )
        return code if type(code) is str and code in MATERIAL_ERROR_CODES else "unclassified"
    return _GENERIC_CODES.get(type(error), "unclassified")


def _canonical(record: dict[str, object]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _state(observation: object) -> dict[str, object] | None:
    state = (
        object.__getattribute__(observation, "__dict__")
        if type(observation) is GitMaterialFailureObservation
        else None
    )
    if (
        type(state) is dict
        and len(state) == len(_OBSERVATION_FIELDS)
        and all(type(key) is str for key in state)
        and set(state) == _OBSERVATION_FIELDS
    ):
        return state
    return None


def _record(observation: object, error: BaseException, origin: str) -> dict[str, object]:
    code = _error_code(error)
    record: dict[str, object] = {
        "schema": FAILURE_SCHEMA,
        "origin": origin,
        "error_code": code,
        "handler_error_code": code,
    }
    state = _state(observation)
    if state is not None:
        record.update({key: state[key] for key in _STATE_FIELDS})
    if not _valid(record):
        record = {
            "schema": FAILURE_SCHEMA,
            "origin": origin,
            "error_code": "unclassified",
            "handler_error_code": "unclassified",
            "stage": "unobserved",
            "git_popen_returned": False,
            "git_returncode": None,
            "git_stdout_complete": None,
            "git_stdout_expected": None,
        }
    return record


def _encode(record: dict[str, object]) -> bytes:
    frame = FAILURE_PREFIX + _canonical(record) + b"\n"
    if len(frame) > MAX_FAILURE_FRAME_BYTES:
        raise ValueError("有限失败帧超限")
    return frame


def encode_failure_observation(observation: object, error: BaseException) -> bytes:
    """首失败与最终捕获分别投影；坏冻结帧不输出正文或恢复业务权限。"""
    record = _record(observation, error, "handler")
    state = _state(observation)
    first = state["_first_frame"] if state is not None else None
    if first is not None:
        status, frozen = (
            decode_failure_observation(first)
            if type(first) is bytes and len(first) <= MAX_FAILURE_FRAME_BYTES
            else ("invalid", None)
        )
        if status == "valid" and frozen is not None and frozen["origin"] == "pre_cleanup":
            record = {**frozen, "handler_error_code": _error_code(error)}
        else:
            record = _record(None, error, "handler")
    return _encode(record)


def freeze_failure_observation(
    observation: GitMaterialFailureObservation, error: BaseException
) -> None:
    """原清理前只冻结第一次有限帧；诊断自身异常不得覆盖原业务异常。"""
    try:
        state = _state(observation)
        if state is not None and state["_first_frame"] is None:
            frame = _encode(_record(observation, error, "pre_cleanup"))
            object.__setattr__(observation, "_first_frame", frame)
    except Exception:
        # 只限纯内存诊断，不吞原业务异常或KeyboardInterrupt／SystemExit。
        pass


@contextmanager
def observe_failure_before_cleanup(
    observation: GitMaterialFailureObservation,
    cleanup_stage: Literal["git_cleanup", "resources_close"],
) -> Iterator[None]:
    """仅增加内存观察作用域；实际资源退出仍由原Popen／ExitStack执行。"""
    try:
        yield
    except (
        GitMaterialInputError,
        OSError,
        ValueError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        freeze_failure_observation(observation, error)
        raise
    finally:
        observation.stage = cleanup_stage


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    record: dict[str, object] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("有限失败帧重复字段")
        record[key] = value
    return record


def decode_failure_observation(
    stderr: bytes,
) -> tuple[Literal["not_observed", "invalid", "valid"], dict[str, object] | None]:
    """完整原raw验真后的有限帧投影；缺席、损坏和合法帧各自返回固定状态。"""
    if type(stderr) is not bytes:
        return "invalid", None
    start, found = 0, None
    while start < len(stderr):
        end = stderr.find(b"\n", start)
        if stderr.startswith(FAILURE_PREFIX, start):
            if found is not None or end < 0 or end - start + 1 > MAX_FAILURE_FRAME_BYTES:
                return "invalid", None
            payload = stderr[start + len(FAILURE_PREFIX) : end]
            try:
                record = json.loads(payload, object_pairs_hook=_unique_object)
                if not _valid(record) or _canonical(record) != payload:
                    return "invalid", None
            except (ValueError, TypeError, UnicodeError, RecursionError):
                return "invalid", None
            found = record
        if end < 0:
            break
        start = end + 1
    return ("not_observed", None) if found is None else ("valid", found)
