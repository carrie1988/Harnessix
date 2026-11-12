"""全部测试共享的收集规则与临时目录约定。"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import zlib
from pathlib import Path

import pytest

_UUID_TEXT = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


_MAX_ID_CHARS = 200


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "nightly: 每次推送的CI跳过、由nightly工作流每天运行的用例"
        "（单项耗时数分钟的容量用例，以及尚未接入产品的内置Git交付测试族）",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard",
        default=None,
        metavar="I/N",
        help="只运行第I片（共N片）；按测试文件稳定划分，CI用它把全量测试拆成并行任务",
    )


_SHARD_WEIGHTS = Path(__file__).with_name("shard_weights.json")


@functools.cache
def _heavy_file_shards(total: int) -> dict[str, int]:
    """耗时已知的文件按“从重到轻、放进当前最轻的一片”分配，各片总耗时接近。"""

    weights: dict[str, float] = json.loads(_SHARD_WEIGHTS.read_text(encoding="utf-8"))["files"]
    load = [0.0] * total
    assigned: dict[str, int] = {}
    for name, seconds in sorted(weights.items(), key=lambda item: (-item[1], item[0])):
        lightest = load.index(min(load))
        load[lightest] += seconds
        assigned[name] = lightest + 1
    return assigned


def shard_of(test_file: str, total: int) -> int:
    """测试文件所属的分片（从1起）。只依赖文件路径与仓库内的耗时表，各进程、各平台结果相同。

    耗时表（shard_weights.json，由scripts/shard_weights.py从CI日志生成）只列慢文件；
    其余文件很轻，按路径的CRC32均匀分散。表里没有的新文件不需要登记。
    """

    name = test_file.replace("\\", "/")
    heavy = _heavy_file_shards(total).get(name)
    return heavy if heavy is not None else zlib.crc32(name.encode("utf-8")) % total + 1


def _requested_shard(config: pytest.Config) -> tuple[int, int] | None:
    value = config.getoption("--shard")
    if value is None:
        return None
    match = re.fullmatch(r"([1-9]\d*)/([1-9]\d*)", value)
    if match is None or int(match[1]) > int(match[2]):
        raise pytest.UsageError("--shard 的写法是 I/N，且 1 <= I <= N，例如 2/3")
    return int(match[1]), int(match[2])


_UNWIRED_GIT_DELIVERY_TESTS = "tests/product_config/test_git_"
# 已上线路径在用的Git模块的测试留在每次推送的CI里：审批运行时启动时绑定审计身份。
_WIRED_GIT_TEST_FILES = frozenset({"tests/product_config/test_git_audit_identity_lifetime.py"})


def runs_nightly_only(test_file: str) -> bool:
    """内置Git交付在1.1接入产品前，其测试族只在nightly运行；接入时删除本规则。

    这一族占每次推送CI慢用例耗时的七成以上，而用户走不到这些代码。按文件名直接点名的任务
    （Windows Git原生验收等）不带 -m 筛选，照常运行这些用例。
    """

    name = test_file.replace("\\", "/")
    return name.startswith(_UNWIRED_GIT_DELIVERY_TESTS) and name not in _WIRED_GIT_TEST_FILES


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """给只在nightly运行的文件加标记（须早于 -m 筛选）；再按文件分片。

    同一文件的用例留在同一片，模块级夹具与文件内顺序不受影响。
    """

    for item in items:
        if runs_nightly_only(item.nodeid.split("::", 1)[0]):
            item.add_marker(pytest.mark.nightly)
    shard = _requested_shard(config)
    if shard is None:
        return
    index, total = shard
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        mine = shard_of(item.nodeid.split("::", 1)[0], total) == index
        (kept if mine else dropped).append(item)
    config.hook.pytest_deselected(items=dropped)
    items[:] = kept


def pytest_make_parametrize_id(config: pytest.Config, val: object, argname: str) -> str | None:
    """UUID文本与超长文本参数用固定ID；参数值不变。

    - 部分用例在参数里现生成uuid4；pytest-xdist要求每个worker进程收集到完全相同的用例ID，
      否则整轮拒绝运行。
    - pytest把用例ID写入环境变量PYTEST_CURRENT_TEST，Windows的环境变量上限是32767字符；
      边界用例的参数常有几万到几百万字符，直接当ID会让用例在setup阶段报错。
    """

    if isinstance(val, str) and _UUID_TEXT.fullmatch(val):
        return "uuid"
    if isinstance(val, str | bytes) and len(val) > _MAX_ID_CHARS:
        return f"{argname}-{len(val)}-{'bytes' if isinstance(val, bytes) else 'chars'}"
    return None


@pytest.fixture(scope="session", autouse=True)
def _private_windows_tmp(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Windows上把测试临时目录收紧为仅当前用户与SYSTEM可访问，并向下继承。

    POSIX上pytest的临时目录本来就是0700；Windows上它继承父目录的权限（含Administrators等），
    而产品只接受“当前用户+SYSTEM”的状态目录，且从不改写已有目录的权限，
    用例里普通mkdir出来的状态目录会被全部拒绝。
    """

    if os.name == "nt":
        _restrict_to_user_and_system(tmp_path_factory.getbasetemp())


def _restrict_to_user_and_system(directory: Path) -> None:
    listing = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True
    )
    user_sid = listing.stdout.strip().rsplit(",", 1)[-1].strip('"')
    subprocess.run(
        [
            "icacls",
            str(directory),
            "/inheritance:r",
            "/grant:r",
            f"*{user_sid}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
        ],
        capture_output=True,
        check=True,
    )
