"""原逻辑路径算法差分；只消除纯计算重复，不提供物理路径或权限证明。"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest

from harnessix.workspace import paths as candidate


@pytest.fixture(scope="module")
def original():
    # 随仓库携带原算法的固定测试输入；独立安装验证可以显式指定同一冻结原件。
    frozen = Path(
        os.environ.get(
            "HARNESSIX_PATH_BEFORE", Path(__file__).parent / "fixtures/workspace_paths_before.py"
        )
    )
    assert frozen.is_file()
    assert (
        hashlib.sha256(frozen.read_bytes()).hexdigest()
        == "3cca0955106af7a480b5d7e361ee5fa9b8cfffbd8a3908e8f4bba960a995e28d"
    )
    spec = importlib.util.spec_from_file_location("original_workspace_paths", frozen)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _outcome(operation, value, platform):
    try:
        return "value", operation(value, platform)
    except Exception as error:
        return "error", type(error), getattr(error, "code", None), str(error)


def _vectors():
    # 边界覆盖原 UTF-8 字节、段数及 Windows UTF-16 单段计数，不扩大限额。
    fixed = (
        ".",
        "a",
        "深/层/源码.py",
        "a:b",
        "Src/ß.py",
        "src/İ.py",
        "a/空 格",
        "",
        "/",
        "//host/a",
        "\\root",
        "C:a",
        "C:/a",
        "a\\b",
        "a/",
        "a//b",
        "a/./b",
        "a/../b",
        "../a",
        "a\ud800",
        "a\udfff",
        "a ",
        "a.",
        "a" * 4096,
        "a" * 4097,
        "界" * 1365,
        "界" * 1366,
        "/".join(["a"] * 128),
        "/".join(["a"] * 129),
        "a/" + "x" * 255,
        "a/" + "x" * 256,
        "a/" + "😀" * 127,
        "a/" + "😀" * 128,
    )
    controls = tuple("prefix/" + chr(i) + "tail" for i in (*range(32), 127, 128, 159, 160))
    reserved = tuple(
        "dir/" + name + suffix
        for name in (
            "CON",
            "con",
            "PRN",
            "AUX",
            "NUL",
            "CONIN$",
            "CONOUT$",
            "COM1",
            "COM9",
            "COM¹",
            "LPT9",
            "LPT³",
            "COM0",
            "COM10",
        )
        for suffix in ("", ".txt", "x")
    )
    # 深目录、不同大小写及组合 Unicode 保持原字符，不使用宿主 Path 规范化。
    deep = tuple(
        "/".join([f"层{level:02d}" for level in range(depth)] + [name])
        for depth in (1, 2, 25, 64, 127, 128)
        for name in ("文件.py", "e\u0301.py", "é.py", "MAIN.PY", "a:b", "NUL.txt")
    )
    return (*fixed, *controls, *reserved, *deep)


@pytest.mark.parametrize("platform", ["posix", "windows"])
@pytest.mark.parametrize("name", ["normalize_workspace_path", "path_comparison_key"])
def test_original_values_and_error_classification_are_identical(original, platform, name):
    before, after = getattr(original, name), getattr(candidate, name)
    for value in _vectors():
        assert _outcome(after, value, platform) == _outcome(before, value, platform), repr(value)


@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_validated_canonical_path_returns_original_string_without_reconstruction(platform):
    value = "/".join([f"层{i:02d}" for i in range(25)] + ["源码.py"])
    assert candidate.normalize_workspace_path(value, platform) is value


def test_every_control_codepoint_and_adjacent_unicode_keeps_original_result(original):
    for point in (*range(256), 0x200B, 0x2028, 0x2029, 0xFEFF, 0x10FFFF):
        for platform in ("posix", "windows"):
            value = "x" + chr(point) + "y"
            assert _outcome(candidate.normalize_workspace_path, value, platform) == _outcome(
                original.normalize_workspace_path, value, platform
            ), (point, platform)


@pytest.mark.parametrize("value", [None, b"a", 1, [], {}, object()])
@pytest.mark.parametrize("platform", ["posix", "windows", "unknown", None])
def test_original_platform_before_value_rejection_is_preserved(original, value, platform):
    assert _outcome(candidate.normalize_workspace_path, value, platform) == _outcome(
        original.normalize_workspace_path, value, platform
    )


def test_repeated_calls_still_validate_each_original_input(monkeypatch):
    calls = []
    drive = candidate._DRIVE_PREFIX

    class ObservedDrive:
        def match(self, value):
            calls.append(value)
            return drive.match(value)

    monkeypatch.setattr(candidate, "_DRIVE_PREFIX", ObservedDrive())
    value = "目录/源码.py"
    for platform in ("posix", "windows", "posix"):
        assert candidate.normalize_workspace_path(value, platform) is value
    assert calls == [value, value, value]  # 不引入跨次输入或认证缓存。
