"""CI把全量测试按文件拆成并行分片（tests/conftest.py的--shard）：每个文件恰好属于一片。

尚未接入产品的内置Git交付测试族只在nightly运行；已上线路径在用的那一个文件留在每次推送里。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import _WIRED_GIT_TEST_FILES, _requested_shard, runs_nightly_only, shard_of

ROOT = Path(__file__).resolve().parents[2]


def _config(value: str | None) -> pytest.Config:
    return SimpleNamespace(getoption=lambda name: value)  # type: ignore[return-value]


@pytest.mark.parametrize("total", [1, 2, 3, 6])
def test_every_test_file_belongs_to_exactly_one_shard(total: int) -> None:
    tests = (ROOT / "tests").rglob("test_*.py")
    files = sorted(path.relative_to(ROOT).as_posix() for path in tests)
    assert len(files) > 500
    owners = [shard_of(name, total) for name in files]
    assert all(1 <= owner <= total for owner in owners)
    # 稳定：重复计算、Windows路径写法都落在同一片。
    assert owners == [shard_of(name.replace("/", "\\"), total) for name in files]
    assert all(owners.count(index) > 0 for index in range(1, total + 1))


@pytest.mark.parametrize("total", [3, 6])
def test_known_slow_files_are_spread_evenly(total: int) -> None:
    weights = json.loads((ROOT / "tests/shard_weights.json").read_text(encoding="utf-8"))["files"]
    assert weights, "耗时表为空：用scripts/shard_weights.py从CI日志重新生成"
    assert all((ROOT / name).is_file() for name in weights), "耗时表里有已不存在的测试文件"
    assert not any(runs_nightly_only(name) for name in weights), "耗时表只登记每次推送都跑的文件"
    load = [0.0] * total
    for name, seconds in weights.items():
        load[shard_of(name, total) - 1] += seconds
    # 贪心分配的结果：最重与最轻的一片相差不超过表里最慢的那个文件。
    assert max(load) - min(load) <= max(weights.values())


def test_shard_option_is_optional_and_parsed() -> None:
    assert _requested_shard(_config(None)) is None
    assert _requested_shard(_config("2/3")) == (2, 3)
    assert _requested_shard(_config("1/1")) == (1, 1)


@pytest.mark.parametrize("value", ["0/3", "4/3", "1", "a/b", "1/0", "-1/3", ""])
def test_invalid_shard_values_are_usage_errors(value: str) -> None:
    with pytest.raises(pytest.UsageError):
        _requested_shard(_config(value))


def test_unwired_git_delivery_family_runs_nightly_only() -> None:
    family = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests/product_config").glob("test_git_*.py")
    )
    nightly = [name for name in family if runs_nightly_only(name)]
    assert len(family) > 100
    assert sorted(set(family) - set(nightly)) == sorted(_WIRED_GIT_TEST_FILES)
    assert runs_nightly_only("tests\\product_config\\test_git_delivery_process.py")


@pytest.mark.parametrize(
    "name",
    [
        "tests/product_config/test_git_audit_identity_lifetime.py",
        "tests/product_config/test_product_state_backup.py",
        "tests/product_config/git_delivery_plan_support.py",
        "tests/delivery/test_git_parent_closure.py",
        "tests/tools/test_git.py",
    ],
)
def test_other_files_stay_in_every_push(name: str) -> None:
    assert (ROOT / name).is_file()
    assert not runs_nightly_only(name)
