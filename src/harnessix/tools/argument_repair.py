"""工作区读工具参数的确定性修复：把模型常用写法换成正式参数，再交给原有严格校验。

只作用于只读的read_file、grep、glob与list_files，依据是评测中被拒参数的实际分布：
- 常见别名映射到正式字段（offset/limit/end_line/lines_after/file_path，regex，limit），
  正式字段已给出时不覆盖；
- 页大小类整数字段接受纯数字字符串（浮点、布尔与其余字段保持严格类型）；
- 页大小类参数（max_lines、lines_before、context_lines、max_results、limit）超出契约上限时
  截到上限（低于下限照常拒绝）：只影响返回多少，不扩大可读路径或工作区边界；
- read_file：无anchor时的lines_before换算为起始行；多行anchor取第一个非空行；
  有anchor时忽略start_line。

修复后仍不满足契约的参数照常被拒（如把grep或补丁参数发给read_file）；审计记录保留模型原始参数。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from functools import cache

from pydantic import JsonValue

from harnessix.domain.models import ContractModel

_INTEGER = re.compile(r"^[+-]?\d{1,12}$")
# 命令输出等存档编号（read_artifact的artifact_id）；read_file收到它时不做行分页修复，
# 由提案阶段改道read_artifact，或由原有提示改用read_artifact。
ARTIFACT_ID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_PAGE_SIZE_FIELDS = frozenset(
    {"max_lines", "lines_before", "context_lines", "max_results", "limit"}
)

Arguments = dict[str, JsonValue]


def repair_read_arguments(
    tool: str, arguments: Arguments, input_model: type[ContractModel]
) -> Arguments:
    """返回修复后的参数副本；未知工具与无需修复的参数原样返回。"""
    repair = _REPAIRS.get(tool)
    if repair is None:
        return arguments
    fields = _integer_fields(input_model)
    repaired = repair(_coerce({**arguments}, fields), _properties(input_model))
    return _clamp(_coerce(repaired, fields), fields)


@cache
def _properties(input_model: type[ContractModel]) -> frozenset[str]:
    return frozenset(input_model.model_json_schema().get("properties", {}))


@cache
def _integer_fields(
    input_model: type[ContractModel],
) -> Mapping[str, tuple[int | None, int | None]]:
    properties = input_model.model_json_schema().get("properties", {})
    return {
        name: (spec.get("minimum"), spec.get("maximum"))
        for name, spec in properties.items()
        if isinstance(spec, Mapping) and spec.get("type") == "integer"
    }


def _as_int(value: JsonValue) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER.match(value.strip()):
        return int(value.strip())
    return None


def _coerce(arguments: Arguments, fields: Mapping[str, tuple[int | None, int | None]]) -> Arguments:
    for name in _PAGE_SIZE_FIELDS & fields.keys():
        if name in arguments and not isinstance(arguments[name], int | bool):
            number = _as_int(arguments[name])
            if number is not None:
                arguments[name] = number
    return arguments


def _clamp(arguments: Arguments, fields: Mapping[str, tuple[int | None, int | None]]) -> Arguments:
    for name in _PAGE_SIZE_FIELDS & arguments.keys() & fields.keys():
        value = arguments[name]
        _, high = fields[name]
        # 只截高于上限的值：低于下限（负数、0页）是明显错误，照常拒绝。
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and high is not None
            and value > high
        ):
            arguments[name] = high
    return arguments


def _rename(arguments: Arguments, alias: str, canonical: str) -> None:
    """正式字段缺失时采用别名；两者都给出且相同则丢弃别名，不同则保留以便照常拒绝。"""
    if alias not in arguments:
        return
    if canonical not in arguments:
        arguments[canonical] = arguments.pop(alias)
    elif arguments[canonical] == arguments[alias]:
        del arguments[alias]


def _repair_read_file(arguments: Arguments, properties: frozenset[str]) -> Arguments:
    del properties
    for alias in ("file_path", "filepath", "path_"):
        _rename(arguments, alias, "path")
    if not is_artifact_read(arguments):  # 存档分页参数留给read_artifact改道或原有提示
        _offset_to_start_line(arguments)
        _rename(arguments, "limit", "max_lines")
    _window_to_max_lines(arguments)
    _first_anchor_line(arguments)
    _anchor_or_lines_before(arguments)
    return arguments


def is_artifact_read(arguments: Mapping[str, JsonValue]) -> bool:
    """read_file收到存档编号（artifact_id字段或作为path）时，调用的其实是read_artifact。"""
    path = arguments.get("path")
    return "artifact_id" in arguments or (
        isinstance(path, str) and ARTIFACT_ID.match(path) is not None
    )


def _offset_to_start_line(arguments: Arguments) -> None:
    """offset按从0计（Qwen Code、read_artifact）；与start_line一致（从0或从1计）时丢弃别名。"""
    offset = _as_int(arguments.get("offset"))
    if "offset" not in arguments or offset is None:
        return
    if "start_line" not in arguments:
        del arguments["offset"]
        arguments["start_line"] = max(1, offset + 1)
    elif _as_int(arguments.get("start_line")) in (offset, offset + 1):
        del arguments["offset"]


def _window_to_max_lines(arguments: Arguments) -> None:
    """end_line与lines_after换算为max_lines；max_lines已给出时不覆盖。"""
    start = _as_int(arguments.get("start_line")) or 1
    end = _as_int(arguments.get("end_line"))
    if end is not None and "max_lines" not in arguments and end >= start:
        del arguments["end_line"]
        arguments["max_lines"] = end - start + 1
    after = _as_int(arguments.get("lines_after"))
    if after is not None:
        del arguments["lines_after"]
        before = _as_int(arguments.get("lines_before")) or 0
        arguments.setdefault("max_lines", before + after + 1)


def _first_anchor_line(arguments: Arguments) -> None:
    """多行anchor取第一个非空行；全为空白时去掉anchor。"""
    anchor = arguments.get("anchor")
    if not isinstance(anchor, str) or ("\n" not in anchor and "\r" not in anchor):
        return
    first = next((line for line in re.split(r"\r\n|\r|\n", anchor) if line.strip()), "")
    if first:
        arguments["anchor"] = first
    else:
        del arguments["anchor"]


def _anchor_or_lines_before(arguments: Arguments) -> None:
    """有anchor时忽略start_line；无anchor时把lines_before换算为起始行。"""
    if arguments.get("anchor") is not None:
        arguments.pop("start_line", None)
        return
    arguments.pop("anchor", None)
    before = _as_int(arguments.get("lines_before")) or 0
    if before and "lines_before" in arguments:
        start = _as_int(arguments.get("start_line")) or 1
        del arguments["lines_before"]
        arguments["start_line"] = max(1, start - before)


def _repair_grep(arguments: Arguments, properties: frozenset[str]) -> Arguments:
    if "regex" in arguments and "query" not in arguments:
        arguments["query"] = arguments.pop("regex")
        if "match_mode" in properties:
            arguments.setdefault("match_mode", "regex")
    context = [
        number
        for key in ("lines_before", "lines_after")
        if key in arguments and (number := _as_int(arguments[key])) is not None
    ]
    if context:
        for key in ("lines_before", "lines_after"):
            arguments.pop(key, None)
        arguments.setdefault("context_lines", max(context))
    return arguments


def _repair_glob(arguments: Arguments, properties: frozenset[str]) -> Arguments:
    del properties
    _rename(arguments, "limit", "max_results")
    return arguments


def _repair_list_files(arguments: Arguments, properties: frozenset[str]) -> Arguments:
    del properties
    return arguments


_REPAIRS: Mapping[str, Callable[[Arguments, frozenset[str]], Arguments]] = {
    "read_file": _repair_read_file,
    "grep": _repair_grep,
    "glob": _repair_glob,
    "list_files": _repair_list_files,
}
