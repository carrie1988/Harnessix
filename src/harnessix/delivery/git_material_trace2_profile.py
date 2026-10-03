"""Git 材料显式 Trace2 的封闭格式目录；不解析 raw、不执行 Git、不授予批准。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Literal

GitMaterialTrace2Mode = Literal["off", "stderr-event-v1"]
Trace2ValueType = Literal["string", "integer", "number", "boolean", "string_array", "json"]
TRACE2_PROFILE_ID = "harnessix.git-material-trace2-profile/v1"
TRACE2_EXPECTED_EVT = "4"
TRACE2_EXPECTED_EXE = "2.55.0.windows.5"
_TAG = "v2.55.0.windows.5"
_EVENT_SHA = "d176fd3f7264f691c295091fa2ea3d045f383bbb0bc34d2774180636ca313de1"


@dataclass(frozen=True, slots=True)
class Trace2Source:
    """官方原字节与调用点身份；不是运行时 Git 二进制构建证明。"""

    tag: str
    path: str
    sha256: str
    line_start: int
    line_end: int


@dataclass(frozen=True, slots=True)
class Trace2EventSchema:
    """某个 event 的完整封闭键和类型；公共字段已包含在两份只读映射中。"""

    required: Mapping[str, Trace2ValueType]
    optional: Mapping[str, Trace2ValueType]
    source: Trace2Source


@dataclass(frozen=True, slots=True)
class Trace2ErrorFormat:
    """精确格式与已求证非穷举调用点；不认定唯一caller、根因或数值errno。"""

    fixed_id: str
    sources: tuple[Trace2Source, ...]


def _schema(
    start: int,
    end: int,
    required: Mapping[str, Trace2ValueType],
    optional: Mapping[str, Trace2ValueType],
) -> Trace2EventSchema:
    """由本模块静态表生成深层只读目录；不接受运行时第三方 schema。"""
    return Trace2EventSchema(
        MappingProxyType(
            {"event": "string", "sid": "string", "thread": "string", "time": "string", **required}
        ),
        MappingProxyType({"file": "string", "line": "integer", **optional}),
        Trace2Source(_TAG, "trace2/tr2_tgt_event.c", _EVENT_SHA, start, end),
    )


TRACE2_EVENT_SCHEMAS: Mapping[str, Trace2EventSchema] = MappingProxyType(
    {
        "too_many_files": _schema(117, 128, {}, {}),
        "version": _schema(130, 146, {"evt": "string", "exe": "string"}, {}),
        "start": _schema(148, 165, {"t_abs": "number", "argv": "string_array"}, {}),
        "exit": _schema(167, 182, {"t_abs": "number", "code": "integer"}, {}),
        "signal": _schema(184, 198, {"t_abs": "number", "signo": "integer"}, {}),
        "atexit": _schema(200, 214, {"t_abs": "number", "code": "integer"}, {}),
        "error": _schema(233, 254, {}, {"msg": "string", "fmt": "string"}),
        "cmd_path": _schema(256, 268, {"path": "string"}, {}),
        "cmd_ancestry": _schema(270, 288, {"ancestry": "string_array"}, {}),
        "cmd_name": _schema(290, 305, {"name": "string"}, {"hierarchy": "string"}),
        "cmd_mode": _schema(307, 319, {"name": "string"}, {}),
        "alias": _schema(321, 337, {"alias": "string", "argv": "string_array"}, {}),
        "child_start": _schema(
            339,
            369,
            {
                "child_id": "integer",
                "child_class": "string",
                "use_shell": "boolean",
                "argv": "string_array",
            },
            {"hook_name": "string", "cd": "string"},
        ),
        "child_exit": _schema(
            371,
            391,
            {"child_id": "integer", "pid": "integer", "code": "integer", "t_rel": "number"},
            {},
        ),
        "child_ready": _schema(
            393,
            413,
            {"child_id": "integer", "pid": "integer", "ready": "string", "t_rel": "number"},
            {},
        ),
        "thread_start": _schema(415, 427, {}, {}),
        "thread_exit": _schema(429, 444, {"t_rel": "number"}, {}),
        "exec": _schema(
            446, 465, {"exec_id": "integer", "argv": "string_array"}, {"exe": "string"}
        ),
        "exec_result": _schema(467, 482, {"exec_id": "integer", "code": "integer"}, {}),
        "def_param": _schema(484, 502, {"scope": "string", "param": "string"}, {"value": "string"}),
        "def_repo": _schema(504, 517, {"repo": "integer", "worktree": "string"}, {}),
        "region_enter": _schema(
            519,
            544,
            {"nesting": "integer"},
            {"repo": "integer", "category": "string", "label": "string", "msg": "string"},
        ),
        "region_leave": _schema(
            546,
            571,
            {"t_rel": "number", "nesting": "integer"},
            {"repo": "integer", "category": "string", "label": "string", "msg": "string"},
        ),
        "data": _schema(
            573,
            598,
            {
                "t_abs": "number",
                "t_rel": "number",
                "nesting": "integer",
                "category": "string",
                "key": "string",
                "value": "string",
            },
            {"repo": "integer"},
        ),
        "data_json": _schema(
            600,
            626,
            {
                "t_abs": "number",
                "t_rel": "number",
                "nesting": "integer",
                "category": "string",
                "key": "string",
                "value": "json",
            },
            {"repo": "integer"},
        ),
        "printf": _schema(628, 644, {"t_abs": "number"}, {"msg": "string"}),
        "timer": _schema(
            646,
            668,
            {
                "category": "string",
                "name": "string",
                "intervals": "integer",
                "t_total": "number",
                "t_min": "number",
                "t_max": "number",
            },
            {},
        ),
        "th_timer": _schema(
            646,
            668,
            {
                "category": "string",
                "name": "string",
                "intervals": "integer",
                "t_total": "number",
                "t_min": "number",
                "t_max": "number",
            },
            {},
        ),
        "counter": _schema(
            670, 686, {"category": "string", "name": "string", "count": "integer"}, {}
        ),
        "th_counter": _schema(
            670, 686, {"category": "string", "name": "string", "count": "integer"}, {}
        ),
    }
)


TRACE2_ERROR_FORMATS: Mapping[str, Trace2ErrorFormat] = MappingProxyType(
    {
        "not a git repository: '%s'": Trace2ErrorFormat(
            "SETUP_EXPLICIT_NOT_REPOSITORY",
            (
                Trace2Source(
                    _TAG,
                    "setup.c",
                    "3151e42f4893a9034a41103a1c23491181d8d79b80c75dea4d88ce24f626d611",
                    1171,
                    1171,
                ),
            ),
        ),
        "'$%s' too big": Trace2ErrorFormat(
            "SETUP_GITDIR_ENV_BOUND",
            (
                Trace2Source(
                    _TAG,
                    "setup.c",
                    "3151e42f4893a9034a41103a1c23491181d8d79b80c75dea4d88ce24f626d611",
                    1157,
                    1157,
                ),
            ),
        ),
        "Unable to add %s to database": Trace2ErrorFormat(
            "HASH_OBJECT_ADD_AGGREGATE",
            (
                Trace2Source(
                    _TAG,
                    "builtin/hash-object.c",
                    "d5b371cce3ad59dc2aedda43f462ada6a0a9a50153f18b08711e58edd341bcf6",
                    31,
                    33,
                ),
            ),
        ),
        "Unable to hash %s": Trace2ErrorFormat(
            "HASH_OBJECT_HASH_AGGREGATE",
            (
                Trace2Source(
                    _TAG,
                    "builtin/hash-object.c",
                    "d5b371cce3ad59dc2aedda43f462ada6a0a9a50153f18b08711e58edd341bcf6",
                    31,
                    33,
                ),
            ),
        ),
        "refusing to create malformed object": Trace2ErrorFormat(
            "OBJECT_FORMAT_MALFORMED",
            (
                Trace2Source(
                    _TAG,
                    "object-file.c",
                    "d79dbe915c778016f896ddf61dcc3ff48b75d5356856a97c06f36017afc406cc",
                    1014,
                    1014,
                ),
            ),
        ),
        "%s": Trace2ErrorFormat(
            "GENERIC_DYNAMIC_FORMAT",
            (
                Trace2Source(
                    _TAG,
                    "setup.c",
                    "3151e42f4893a9034a41103a1c23491181d8d79b80c75dea4d88ce24f626d611",
                    781,
                    781,
                ),
            ),
        ),
        "short read while indexing %s": Trace2ErrorFormat(
            "OBJECT_INDEX_SHORT_READ",
            (
                Trace2Source(
                    _TAG,
                    "object-file.c",
                    "d79dbe915c778016f896ddf61dcc3ff48b75d5356856a97c06f36017afc406cc",
                    1086,
                    1087,
                ),
            ),
        ),
        "insufficient permission for adding an object to repository database %s": Trace2ErrorFormat(
            "OBJECT_DATABASE_ADD_PERMISSION",
            (
                Trace2Source(
                    _TAG,
                    "object-file.c",
                    "d79dbe915c778016f896ddf61dcc3ff48b75d5356856a97c06f36017afc406cc",
                    672,
                    674,
                ),
            ),
        ),
        "unable to set permission to '%s'": Trace2ErrorFormat(
            "OBJECT_FINALIZE_PERMISSION",
            (
                Trace2Source(
                    _TAG,
                    "object-file.c",
                    "d79dbe915c778016f896ddf61dcc3ff48b75d5356856a97c06f36017afc406cc",
                    469,
                    470,
                ),
            ),
        ),
    }
)


def trace2_profile_bytes() -> bytes:
    """生成完整静态目录的规范持久字节；不读取源码或日志。"""
    value = {
        "profile_id": TRACE2_PROFILE_ID,
        "expected_evt": TRACE2_EXPECTED_EVT,
        "expected_exe": TRACE2_EXPECTED_EXE,
        "mode": "stderr-event-v1",
        "environment": {"GIT_TRACE2_EVENT": "2"},
        "inner_argv_count": 22,
        "brief": False,
        "paired_optional": [["file", "line"]],
        "event_schemas": {
            name: {
                "required": dict(schema.required),
                "optional": dict(schema.optional),
                "source": asdict(schema.source),
            }
            for name, schema in TRACE2_EVENT_SCHEMAS.items()
        },
        "error_formats": {
            literal: {
                "fixed_id": item.fixed_id,
                "sources": [asdict(source) for source in item.sources],
            }
            for literal, item in TRACE2_ERROR_FORMATS.items()
        },
        "rules": {
            "unknown": "fail_unknown",
            "dynamic_format": "unclassified_no_errno",
            "dynamic_fields": "memory_only_no_export",
            "source_reporter": "not_original_fatal_stack",
            "error_sources": "known_examples_non_exhaustive_not_unique_callers",
            "source_identity": "not_binary_build_attestation",
            "json_type": "finite_json_no_nan_no_infinity_no_duplicates",
            "integer_type": "actual_int_not_bool",
            "number_type": "finite_actual_int_or_float_not_bool",
            "string_array_type": "actual_list_of_actual_str",
        },
    }
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        + "\n"
    ).encode("ascii")


TRACE2_PROFILE_SHA256 = hashlib.sha256(trace2_profile_bytes()).hexdigest()


def validate_material_trace2(mode: object, profile_sha: object) -> None:
    """严格验真 mode/profile 组合；只允许 off空值或唯一正式诊断身份。"""
    if (
        type(mode) is not str
        or type(profile_sha) is not str
        or not (
            (mode == "off" and profile_sha == "")
            or (mode == "stderr-event-v1" and profile_sha == TRACE2_PROFILE_SHA256)
        )
    ):
        raise ValueError("git_material_trace2_invalid")


def validate_material_trace2_environment(
    mode: object,
    profile_sha: object,
    environment: Mapping[str, str],
) -> None:
    """检查唯一 Trace 环境键；其他原环境字段仍由原 Binding/worker完整重验。"""
    validate_material_trace2(mode, profile_sha)
    if any(type(key) is not str or type(value) is not str for key, value in environment.items()):
        raise ValueError("git_material_trace2_invalid")
    trace = {
        key: value for key, value in environment.items() if key.upper().startswith("GIT_TRACE")
    }
    expected = {} if mode == "off" else {"GIT_TRACE2_EVENT": "2"}
    if trace != expected:
        raise ValueError("git_material_trace2_invalid")
