"""macOS 安装态测试入口：只在场景子进程预加载检测器。

Darwin 检测运行库会消费 DYLD_INSERT_LIBRARIES。若先预加载 pytest 父进程，
其子进程可能没有拦截器，LSAN 甚至会给出假阴性的退出 0。
本入口保留 test_identity.py 的全部断言与超时；检测器正负控须另行验证。
"""

import argparse
import ctypes
import hashlib
import json
import os
import sys
from pathlib import Path

SANITIZER_OPTIONS = {
    "asan": ("ASAN_OPTIONS", "detect_leaks=1:halt_on_error=1:abort_on_error=0:exitcode=73"),
    "lsan": ("LSAN_OPTIONS", "exitcode=73"),
    "ubsan": ("UBSAN_OPTIONS", "print_stacktrace=1:halt_on_error=1:abort_on_error=0:exitcode=74"),
}


def existing_absolute_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise argparse.ArgumentTypeError("必须提供已存在文件的绝对路径")
    return path.resolve()


def loaded_sanitizers() -> list[str]:
    """检查实际映射，不能依赖已被运行库清除的环境变量。"""
    loader = ctypes.CDLL(None)
    loader._dyld_image_count.restype = ctypes.c_uint32
    loader._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    loader._dyld_get_image_name.restype = ctypes.c_char_p
    images = [
        os.fsdecode(loader._dyld_get_image_name(index))
        for index in range(loader._dyld_image_count())
    ]
    return [name for name in images if "libclang_rt." in name and "san_" in name]


def child_settings(sanitizer: str, runtime: Path, symbolizer: Path) -> dict[str, str]:
    expected = f"libclang_rt.{sanitizer}_osx_dynamic.dylib"
    if runtime.name != expected:
        raise ValueError(f"检测器与运行库不匹配：预期 {expected}")
    if ":" in str(runtime) or ":" in str(symbolizer):
        raise ValueError("检测器路径不能包含选项分隔符冒号")
    if '"' in str(symbolizer):
        raise ValueError("符号化工具路径不能包含选项引号")
    option, value = SANITIZER_OPTIONS[sanitizer]
    return {
        "DYLD_INSERT_LIBRARIES": str(runtime),
        option: f'{value}:external_symbolizer_path="{symbolizer}"',
    }


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sanitizer", choices=SANITIZER_OPTIONS, required=True)
    parser.add_argument("--runtime", type=existing_absolute_file, required=True)
    parser.add_argument("--symbolizer", type=existing_absolute_file, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("此入口仅验证 macOS；不代表其他平台交付支持")
    if loaded_sanitizers():
        parser.error("父解释器已加载检测器；请去掉启动父进程时的预加载配置")
    try:
        settings = child_settings(args.sanitizer, args.runtime, args.symbolizer)
    except ValueError as error:
        parser.error(str(error))
    if not args.output.is_absolute() or args.output.exists():
        parser.error("输出目录必须为尚不存在的绝对路径，禁止覆盖历史证据")

    import pytest

    if loaded_sanitizers():
        parser.error("pytest 导入期间加载了检测器，父进程不再适合启动隔离场景")
    args.output.mkdir(parents=True, exist_ok=False)
    case_file = Path(__file__).with_name("test_identity.py").resolve()
    invocation = {
        "sanitizer": args.sanitizer,
        "executable": sys.executable,
        "child_environment": settings,
        "sha256": {
            str(path): file_digest(path)
            for path in (Path(__file__), case_file, args.runtime, args.symbolizer)
        },
        "scope": "原安装态场景；检测器有效性与发布准入须独立验收",
    }
    (args.output / "invocation.json").write_text(
        json.dumps(invocation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # 各 runtime 共用部分选项状态；不可同时设置三套 OPTIONS 导致退出语义互相覆盖。
    for key in (*[value[0] for value in SANITIZER_OPTIONS.values()], "DYLD_INSERT_LIBRARIES"):
        os.environ.pop(key, None)
    os.environ.update(settings)
    result = int(
        pytest.main(
            [
                "-q",
                str(case_file),
                "--basetemp",
                str(args.output / "cases"),
                f"--junitxml={args.output / 'results.xml'}",
                "-o",
                f"cache_dir={args.output / 'pytest-cache'}",
            ]
        )
    )
    (args.output / "result.json").write_text(
        json.dumps({"pytest_exit_code": result}) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
