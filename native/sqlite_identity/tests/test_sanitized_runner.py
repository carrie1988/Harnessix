"""只验证测试启动器；伪造动态库、dyld、pytest 不构成原生内存检测证据。"""

import builtins
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

EXPECTED_OPTIONS = {
    "asan": "detect_leaks=1:halt_on_error=1:abort_on_error=0:exitcode=73",
    "lsan": "exitcode=73",
    "ubsan": "print_stacktrace=1:halt_on_error=1:abort_on_error=0:exitcode=74",
}


@pytest.fixture
def runner(monkeypatch):
    path = Path(__file__).with_name("run_sanitized.py").resolve()
    spec = importlib.util.spec_from_file_location("sanitized_runner_unit", path)
    module = importlib.util.module_from_spec(spec)
    # 环境写入和导入期间的 dyld 模拟均限制在本用例，不能污染后续测试。
    module.__builtins__ = vars(builtins).copy()
    spec.loader.exec_module(module)
    environment = {
        "ASAN_OPTIONS": "stale-asan",
        "LSAN_OPTIONS": "stale-lsan",
        "UBSAN_OPTIONS": "stale-ubsan",
        "DYLD_INSERT_LIBRARIES": "/stale/runtime.dylib",
        "UNRELATED": "preserved",
    }
    monkeypatch.setattr(module, "os", SimpleNamespace(environ=environment, fsdecode=os.fsdecode))
    monkeypatch.setattr(
        module, "sys", SimpleNamespace(platform="darwin", executable=sys.executable)
    )
    return module


@pytest.fixture
def mapped_images(runner, monkeypatch):
    images = [b"/usr/lib/libSystem.B.dylib", b"/llvm/lib/libclang_rt.profile_osx.a"]
    loader = SimpleNamespace(
        _dyld_image_count=Mock(side_effect=lambda: len(images)),
        _dyld_get_image_name=Mock(side_effect=images.__getitem__),
    )
    monkeypatch.setattr(runner.ctypes, "CDLL", Mock(return_value=loader))
    return images


@pytest.fixture
def pytest_main(monkeypatch):
    delegate = Mock(return_value=pytest.ExitCode.OK)
    monkeypatch.setattr(pytest, "main", delegate)
    return delegate


@pytest.fixture
def launch_options(tmp_path, mapped_images, pytest_main):
    folder = tmp_path.resolve()
    runtime = folder / "libclang_rt.asan_osx_dynamic.dylib"
    runtime.write_bytes(b"unit-only runtime\x00\xff")
    symbolizer = folder / "tool chain" / "llvm-symbolizer"
    symbolizer.parent.mkdir()
    symbolizer.write_bytes(b"unit-only symbolizer\n")
    return {
        "sanitizer": "asan",
        "runtime": runtime,
        "symbolizer": symbolizer,
        "output": folder / "new evidence" / "run",
    }


def call_main(runner, options):
    return runner.main(
        [item for key, value in options.items() for item in (f"--{key}", str(value))]
    )


def assert_rejected(runner, options, pytest_main, capsys, message):
    environment = runner.os.environ.copy()
    output_existed = options["output"].exists()
    with pytest.raises(SystemExit) as error:
        call_main(runner, options)
    assert error.value.code == 2
    assert message in capsys.readouterr().err
    pytest_main.assert_not_called()
    assert runner.os.environ == environment
    assert options["output"].exists() == output_existed


@pytest.mark.parametrize("option", ["runtime", "symbolizer"])
@pytest.mark.parametrize("invalid_kind", ["relative", "missing", "directory"])
def test_rejects_invalid_input_paths(
    runner,
    launch_options,
    pytest_main,
    tmp_path,
    monkeypatch,
    capsys,
    option,
    invalid_kind,
):
    monkeypatch.chdir(tmp_path)
    invalid_paths = {
        "relative": launch_options[option].relative_to(tmp_path.resolve()),
        "missing": tmp_path / "missing",
        "directory": tmp_path,
    }
    launch_options[option] = invalid_paths[invalid_kind]
    assert_rejected(runner, launch_options, pytest_main, capsys, "已存在文件的绝对路径")


def test_existing_absolute_file_resolves_symlinks(runner, tmp_path):
    target = tmp_path / "file"
    target.write_bytes(b"unit-only")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert runner.existing_absolute_file(str(link)) == target.resolve()


@pytest.mark.parametrize(
    ("sanitizer", "runtime_name"),
    [
        ("asan", "llvm-symbolizer"),
        ("asan", "libclang_rt.lsan_osx_dynamic.dylib"),
        ("lsan", "libclang_rt.ubsan_osx_dynamic.dylib"),
        ("ubsan", "libclang_rt.asan_osx_dynamic.dylib"),
    ],
)
def test_rejects_runtime_role_mismatch(
    runner, launch_options, pytest_main, capsys, sanitizer, runtime_name
):
    runtime = launch_options["runtime"].with_name(runtime_name)
    runtime.write_bytes(b"unit-only wrong runtime")
    launch_options.update(sanitizer=sanitizer, runtime=runtime)
    assert_rejected(runner, launch_options, pytest_main, capsys, "检测器与运行库不匹配")


def test_rejects_unknown_sanitizer(runner, launch_options, pytest_main, capsys):
    launch_options["sanitizer"] = "tsan"
    assert_rejected(runner, launch_options, pytest_main, capsys, "invalid choice")


@pytest.mark.parametrize("option", ["runtime", "symbolizer"])
def test_rejects_colon_in_paths(runner, launch_options, pytest_main, capsys, option):
    path = launch_options[option]
    colon_path = path.parent / "with:colon" / path.name
    colon_path.parent.mkdir()
    colon_path.write_bytes(path.read_bytes())
    launch_options[option] = colon_path
    assert_rejected(runner, launch_options, pytest_main, capsys, "选项分隔符冒号")


def test_rejects_embedded_symbolizer_quote(runner, launch_options, pytest_main, capsys):
    symbolizer = launch_options["symbolizer"].with_name('llvm-"symbolizer')
    symbolizer.write_bytes(b"unit-only symbolizer")
    launch_options["symbolizer"] = symbolizer
    assert_rejected(runner, launch_options, pytest_main, capsys, "选项引号")


@pytest.mark.parametrize("output_kind", ["relative", "directory", "file"])
def test_rejects_nonfresh_or_relative_output(
    runner, launch_options, pytest_main, capsys, monkeypatch, tmp_path, output_kind
):
    monkeypatch.chdir(tmp_path)
    output = launch_options["output"]
    if output_kind == "relative":
        launch_options["output"] = Path("relative-output")
    elif output_kind == "directory":
        output.mkdir(parents=True)
    else:
        output.parent.mkdir()
        output.write_bytes(b"historical evidence")
    assert_rejected(runner, launch_options, pytest_main, capsys, "禁止覆盖历史证据")
    if output_kind == "file":
        assert output.read_bytes() == b"historical evidence"


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_rejects_nonmac_before_dyld_probe(
    runner, launch_options, pytest_main, capsys, monkeypatch, platform
):
    monkeypatch.setattr(runner.sys, "platform", platform)
    assert_rejected(runner, launch_options, pytest_main, capsys, "仅验证 macOS")
    runner.ctypes.CDLL.assert_not_called()


@pytest.mark.parametrize("sanitizer", EXPECTED_OPTIONS)
@pytest.mark.parametrize("phase", ["startup", "pytest_import"])
def test_rejects_mapped_sanitizer_even_without_preload_environment(
    runner,
    launch_options,
    pytest_main,
    mapped_images,
    capsys,
    monkeypatch,
    sanitizer,
    phase,
):
    image = f"/llvm/lib/libclang_rt.{sanitizer}_osx_dynamic.dylib".encode()
    runner.os.environ.pop("DYLD_INSERT_LIBRARIES")
    imports = []

    def import_with_dyld_change(name, *args, **kwargs):
        module = builtins.__import__(name, *args, **kwargs)
        if name == "pytest":
            imports.append(name)
            if phase == "pytest_import":
                mapped_images.append(image)
        return module

    monkeypatch.setitem(runner.__builtins__, "__import__", import_with_dyld_change)
    if phase == "startup":
        mapped_images.append(image)
    message = "父解释器已加载检测器" if phase == "startup" else "pytest 导入期间加载了检测器"
    assert_rejected(runner, launch_options, pytest_main, capsys, message)
    assert imports == ([] if phase == "startup" else ["pytest"])
    assert runner.loaded_sanitizers() == [os.fsdecode(image)]


@pytest.mark.parametrize("sanitizer", EXPECTED_OPTIONS)
def test_launch_binds_original_suite_hashes_and_one_quoted_options_set(
    runner, launch_options, pytest_main, sanitizer
):
    runtime = launch_options["runtime"].with_name(f"libclang_rt.{sanitizer}_osx_dynamic.dylib")
    runtime.write_bytes(b"unit-only runtime\x00\xff")
    launch_options.update(sanitizer=sanitizer, runtime=runtime)
    expected_environment = {
        "DYLD_INSERT_LIBRARIES": str(runtime),
        f"{sanitizer.upper()}_OPTIONS": (
            f'{EXPECTED_OPTIONS[sanitizer]}:external_symbolizer_path="{launch_options["symbolizer"]}"'
        ),
    }
    environment_at_pytest = []

    def run_pytest(arguments):
        environment_at_pytest.append(runner.os.environ.copy())
        assert (launch_options["output"] / "invocation.json").is_file()
        return pytest.ExitCode.OK

    pytest_main.side_effect = run_pytest
    assert call_main(runner, launch_options) == 0
    assert environment_at_pytest == [{"UNRELATED": "preserved", **expected_environment}]
    case_file = Path(__file__).with_name("test_identity.py").resolve()
    output = launch_options["output"]
    pytest_main.assert_called_once_with(
        [
            "-q",
            str(case_file),
            "--basetemp",
            str(output / "cases"),
            f"--junitxml={output / 'results.xml'}",
            "-o",
            f"cache_dir={output / 'pytest-cache'}",
        ]
    )
    invocation = json.loads((output / "invocation.json").read_text(encoding="utf-8"))
    assert invocation["sanitizer"] == sanitizer
    assert invocation["executable"] == sys.executable
    assert invocation["child_environment"] == expected_environment
    assert invocation["sha256"] == {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (
            Path(runner.__file__),
            case_file,
            runtime,
            launch_options["symbolizer"],
        )
    }


@pytest.mark.parametrize("exit_code", list(pytest.ExitCode))
def test_preserves_pytest_exit_code_in_return_and_record(
    runner, launch_options, pytest_main, exit_code
):
    pytest_main.return_value = exit_code
    result = call_main(runner, launch_options)
    assert type(result) is int
    assert result == int(exit_code)
    record = launch_options["output"] / "result.json"
    assert json.loads(record.read_text(encoding="utf-8")) == {"pytest_exit_code": int(exit_code)}
    pytest_main.assert_called_once()


def test_rerun_never_overwrites_historical_output(runner, launch_options, pytest_main, capsys):
    assert call_main(runner, launch_options) == 0
    output = launch_options["output"]
    (output / "run.log").write_bytes(b"historical scenario evidence\x00")
    history = {path.name: path.read_bytes() for path in output.iterdir()}
    launch_options["runtime"].write_bytes(b"changed runtime for attempted rerun")
    pytest_main.reset_mock()
    assert_rejected(runner, launch_options, pytest_main, capsys, "禁止覆盖历史证据")
    assert {path.name: path.read_bytes() for path in output.iterdir()} == history
