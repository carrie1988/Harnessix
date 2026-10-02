"""已验真原 stderr 的封闭 Trace2 投影；只处理内存，不提供执行或恢复权限。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from harnessix.delivery.git_material_failure import FAILURE_PREFIX, decode_failure_observation
from harnessix.delivery.git_material_trace2_profile import (
    TRACE2_ERROR_FORMATS,
    TRACE2_EVENT_SCHEMAS,
    TRACE2_EXPECTED_EVT,
    TRACE2_EXPECTED_EXE,
    TRACE2_PROFILE_ID,
    TRACE2_PROFILE_SHA256,
)

MAX_RAW_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 64 * 1024
MAX_TRACE2_EVENTS = 64
MAX_JSON_DEPTH = 16
_UNEXPECTED = frozenset(
    {"alias", "exec", "exec_result", "child_start", "child_exit", "child_ready", "too_many_files"}
)
_WITNESSES = ("ENTRY_START_MATCHED", "DISPATCH_HASH_OBJECT", "REPO_EVENT_SEEN")


def _record(status: str = "UNAVAILABLE", reason: str = "NONE") -> dict[str, Any]:
    """输出仅含固定枚举，动态 Git 字段永不进入返回对象。"""
    return {
        "profile_status": status,
        "completeness": "KNOWN" if reason == "NONE" else "UNKNOWN",
        "reason": reason,
        "stage_witnesses": [],
        "error_format_ids": [],
        "return_consistency": "UNAVAILABLE",
        "source_profile_id": TRACE2_PROFILE_ID if status != "OFF" else None,
    }


def _integer(text: str) -> int:
    """限制单帧数字工作量，不修改解释器全局整数策略。"""
    if len(text) > 20:
        raise ValueError
    value = int(text)
    if not -(2**63) <= value < 2**64:
        raise ValueError
    return value


def _number(text: str) -> float:
    if len(text) > 64:
        raise ValueError
    value = float(text)
    if not math.isfinite(value):
        raise ValueError
    return value


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _nonfinite(_: str) -> object:
    raise ValueError


def _string(value: object) -> bool:
    return type(value) is str and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _finite_json(value: object, depth: int = 0) -> bool:
    """官方 data_json 值仍受完整 JSON 类型及深度合同限制，不透传正文。"""
    if depth > MAX_JSON_DEPTH:
        return False
    if value is None or type(value) is bool:
        return True
    if type(value) is str:
        return _string(value)
    if type(value) is int:
        return -(2**63) <= value < 2**64
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_finite_json(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(_string(key) and _finite_json(item, depth + 1) for key, item in value.items())
    return False


def _typed(value: object, kind: str) -> bool:
    if kind == "string":
        return _string(value)
    if kind == "integer":
        return type(value) is int and -(2**63) <= value < 2**64
    if kind == "number":
        return type(value) in {int, float} and _finite_json(value)
    if kind == "boolean":
        return type(value) is bool
    if kind == "string_array":
        return type(value) is list and all(_string(item) for item in value)
    return kind == "json" and _finite_json(value)


def _valid_event(value: object) -> bool:
    """严格采用实际静态目录的必选、可选、配对键，不容纳未知字段。"""
    if type(value) is not dict or type(value.get("event")) is not str:
        return False
    schema = TRACE2_EVENT_SCHEMAS.get(value["event"])
    if schema is None:
        return False
    keys = set(value)
    if not set(schema.required) <= keys <= set(schema.required) | set(schema.optional):
        return False
    if ("file" in keys) != ("line" in keys):
        return False
    return all(
        _typed(item, schema.required.get(key, schema.optional.get(key, "invalid")))
        for key, item in value.items()
    )


def _decode_frame(frame: bytes) -> dict[str, Any]:
    value = json.loads(
        frame.decode("utf-8", errors="strict"),
        object_pairs_hook=_pairs,
        parse_int=_integer,
        parse_float=_number,
        parse_constant=_nonfinite,
    )
    if type(value) is not dict or not _finite_json(value):
        raise ValueError
    return value


@dataclass
class _Stream:
    """仅在当前解析调用内关联 SID 和有限阶段；不输出动态相关性身份。"""

    expected_argv: tuple[str, ...]
    result: dict[str, Any] = field(default_factory=_record)
    sid: str | None = None
    version_seen: bool = False
    start_seen: bool = False
    event_count: int = 0
    failure_frames: int = 0
    sentinels: int = 0
    codes: set[int] = field(default_factory=set)
    witnesses: set[str] = field(default_factory=set)
    formats: set[str] = field(default_factory=set)

    def unknown(self, reason: str) -> None:
        self.result["completeness"] = "UNKNOWN"
        if self.result["reason"] == "NONE":
            self.result["reason"] = reason

    def consume(self, event: dict[str, Any]) -> None:
        if not _valid_event(event):
            self.unknown("UNKNOWN_EVENT_OR_SCHEMA")
            return
        if self.sid is None:
            self.sid = event["sid"]
        if not self.sid or event["sid"] != self.sid:
            self.unknown("STREAM_BINDING_MISMATCH")
            return
        name = event["event"]
        if name in _UNEXPECTED:
            self.unknown("UNEXPECTED_EXECUTION_EVENT")
        elif name == "version":
            self.version(event)
        elif self.result["profile_status"] != "MATCHED":
            self.unknown("PROFILE_MISMATCH")
        elif name == "start":
            if self.start_seen or tuple(event["argv"]) != self.expected_argv:
                self.unknown("STREAM_BINDING_MISMATCH")
            else:
                self.start_seen = True
                self.witnesses.add("ENTRY_START_MATCHED")
        elif name == "cmd_name":
            if event["name"] == "hash-object":
                self.witnesses.add("DISPATCH_HASH_OBJECT")
            else:
                self.unknown("UNEXPECTED_EXECUTION_EVENT")
        elif name == "def_repo":
            self.witnesses.add("REPO_EVENT_SEEN")
        elif name == "error":
            entry = TRACE2_ERROR_FORMATS.get(event.get("fmt"))
            if entry is None:
                self.unknown("UNCLASSIFIED_FORMAT")
            else:
                self.formats.add(entry.fixed_id)
        elif name in {"exit", "atexit"}:
            self.codes.add(event["code"])
        elif name == "signal":
            self.unknown("RETURN_UNAVAILABLE")

    def version(self, event: dict[str, Any]) -> None:
        if self.version_seen or (event["evt"], event["exe"]) != (
            TRACE2_EXPECTED_EVT,
            TRACE2_EXPECTED_EXE,
        ):
            self.result["profile_status"] = "MISMATCH"
            self.unknown("PROFILE_MISMATCH")
        else:
            self.result["profile_status"] = "MATCHED"
        self.version_seen = True

    def finish(self, git_returncode: object) -> dict[str, Any]:
        if not self.version_seen or not self.start_seen or not self.codes:
            self.unknown("MISSING_EVENTS")
        if len(self.codes) > 1:
            self.result["return_consistency"] = "MISMATCH"
            self.unknown("RETURN_MISMATCH")
        elif self.codes and next(iter(self.codes)) not in {0, 128}:
            self.unknown("RETURN_UNAVAILABLE")
        elif type(git_returncode) is int and self.codes:
            if self.codes == {git_returncode}:
                self.result["return_consistency"] = (
                    "MATCHED_ZERO" if git_returncode == 0 else "MATCHED_128"
                )
            else:
                self.result["return_consistency"] = "MISMATCH"
                self.unknown("RETURN_MISMATCH")
        self.result["stage_witnesses"] = [item for item in _WITNESSES if item in self.witnesses]
        self.result["error_format_ids"] = sorted(self.formats)
        return self.result


def _consume_frame(stream: _Stream, frame: bytes) -> bool:
    """识别完整原帧；到界停止，不拼接、修补或重读损坏数据。"""
    if len(frame) > MAX_FRAME_BYTES:
        stream.unknown("LIMIT")
        return False
    if frame in {b"git_material_worker_failed\n", b"git_material_worker_failed\r\n"}:
        stream.sentinels += 1
        if stream.sentinels > 1:
            stream.unknown("MALFORMED_FRAME")
        return True
    if frame.startswith(FAILURE_PREFIX):
        stream.failure_frames += 1
        status, _ = decode_failure_observation(frame)
        if status != "valid" or stream.failure_frames > 1:
            stream.unknown("MALFORMED_FRAME")
        return True
    prefix = frame.lstrip(b" \t\r")
    if not prefix.startswith((b"{", b"[")) and prefix.strip() not in {b"null", b"true", b"false"}:
        stream.unknown("UNCLASSIFIED_STDERR")
        return True
    if stream.event_count >= MAX_TRACE2_EVENTS:
        stream.unknown("LIMIT")
        return False
    stream.event_count += 1
    try:
        stream.consume(_decode_frame(frame))
    except (ValueError, TypeError, RecursionError, OverflowError):
        stream.unknown("MALFORMED_FRAME")
    return True


def project_git_trace2_events(
    stderr: bytes,
    expected_argv: tuple[str, ...],
    expected_profile_sha256: object,
    *,
    git_returncode: object = None,
) -> dict[str, Any]:
    """投影原守卫已认证的完整 bytes；自身不证明 MAC、Owner、执行权限或效果。"""
    if type(expected_profile_sha256) is not str or expected_profile_sha256 != TRACE2_PROFILE_SHA256:
        result = _record("MISMATCH", "PROFILE_MISMATCH")
        result["source_profile_id"] = None
        return result
    if type(stderr) is not bytes or len(stderr) > MAX_RAW_BYTES:
        return _record(reason="LIMIT")
    if (
        type(expected_argv) is not tuple
        or len(expected_argv) != 22
        or not all(_string(item) for item in expected_argv)
    ):
        return _record("MISMATCH", "STREAM_BINDING_MISMATCH")
    stream = _Stream(expected_argv)
    start = 0
    while (end := stderr.find(b"\n", start)) >= 0:
        if not _consume_frame(stream, stderr[start : end + 1]):
            break
        start = end + 1
    else:
        if start != len(stderr):
            stream.unknown("MALFORMED_FRAME")
    return stream.finish(git_returncode)


def project_operation_trace2(operation: Any, stderr: bytes, git_returncode: object = None) -> None:
    """只能由原完整 raw/protection 之后或原完整 Completion 之后调用。"""
    if operation.probe.trace2_mode == "off":
        operation.data["post_git_trace2"] = _record("OFF")
        return
    request = operation.prepared.write.request
    if request.trace2_mode != "stderr-event-v1":
        operation.data["post_git_trace2"] = _record("MISMATCH", "PROFILE_MISMATCH")
    else:
        operation.data["post_git_trace2"] = project_git_trace2_events(
            stderr, request.git_argv, request.trace2_profile_sha256, git_returncode=git_returncode
        )
    if operation.data["post_git_trace2"]["completeness"] != "KNOWN":
        operation.probe.incomplete = True


def initialize_operation_trace2(operation: Any) -> None:
    """初始化有限占位，缺少完整原观察不得被会话门闩误记为诊断完整。"""
    operation.data["post_git_trace2"] = (
        _record("OFF")
        if operation.probe.trace2_mode == "off"
        else _record(reason="RAW_UNAVAILABLE")
    )


def diagnostic_probes_complete(probes: list[Any]) -> bool:
    """只核对已完成内存状态；不改业务结果、不新增原进程读取或等待。"""
    if len(probes) != 2 or {probe.selector for probe in probes} != {"A", "B"}:
        return False
    for probe in probes:
        if (
            probe.trace2_mode != "stderr-event-v1"
            or not probe.published
            or probe.incomplete
            or probe.truncated
            or probe.installed_hooks != 13
            or probe.outcomes.get("call") not in {"passed", "failed"}
            or probe.outcomes.get("teardown") != "passed"
        ):
            return False
        writes = [
            operation for operation in probe.operations if operation.data.get("kind") == "write"
        ]
        if len(writes) != 1:
            return False
        record = writes[0].data.get("post_git_trace2", {})
        if record.get("profile_status") != "MATCHED" or record.get("completeness") != "KNOWN":
            return False
    return True
