"""CI把全量测试按文件拆成并行分片（tests/conftest.py的--shard）：每个文件恰好属于一片。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import _requested_shard, shard_of

ROOT = Path(__file__).resolve().parents[2]


def _config(value: str | None) -> pytest.Config:
    return SimpleNamespace(getoption=lambda name: value)  # type: ignore[return-value]


@pytest.mark.parametrize("total", [1, 2, 3, 4])
def test_every_test_file_belongs_to_exactly_one_shard(total: int) -> None:
    tests = (ROOT / "tests").rglob("test_*.py")
    files = sorted(path.relative_to(ROOT).as_posix() for path in tests)
    assert len(files) > 500
    owners = [shard_of(name, total) for name in files]
    assert all(1 <= owner <= total for owner in owners)
    # 稳定：重复计算、Windows路径写法都落在同一片。
    assert owners == [shard_of(name.replace("/", "\\"), total) for name in files]
    # 没有哪一片是空的，最大的一片不超过平均值的1.5倍（按文件数）。
    sizes = [owners.count(index) for index in range(1, total + 1)]
    assert min(sizes) > 0 and max(sizes) <= 1.5 * len(files) / total


def test_shard_option_is_optional_and_parsed() -> None:
    assert _requested_shard(_config(None)) is None
    assert _requested_shard(_config("2/3")) == (2, 3)
    assert _requested_shard(_config("1/1")) == (1, 1)


@pytest.mark.parametrize("value", ["0/3", "4/3", "1", "a/b", "1/0", "-1/3", ""])
def test_invalid_shard_values_are_usage_errors(value: str) -> None:
    with pytest.raises(pytest.UsageError):
        _requested_shard(_config(value))
