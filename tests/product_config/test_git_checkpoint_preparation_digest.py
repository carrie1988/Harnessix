"""准备配方的热路径成本回归；保留每次四份源码完整字节验真，不缓存摘要。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import canonical_digest
from harnessix.product_config import git_checkpoint_preparation as preparation

SOURCE_NAMES = (
    "product_config/git_checkpoint_preparation.py",
    "product_config/git_checkpoint_materials.py",
    "product_config/git_checkpoint_scope.py",
    "delivery/git_inventory_wire.py",
)


def sources():
    root = Path(preparation.__file__).parent.parent
    return tuple((str(Path(name)), root / name) for name in SOURCE_NAMES)


def test_same_canonical_four_sources_without_rebuilding_relative_paths(monkeypatch):
    originals = sources()
    read = Path.read_bytes
    expected = canonical_digest(
        {name: hashlib.sha256(read(p)).hexdigest() for name, p in originals}
    )
    reads = []
    relative = Path.relative_to
    relative_calls = []

    def observed(path):
        reads.append(path)
        return read(path)

    def redundant(*args, **kwargs):
        relative_calls.append(True)
        return relative(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", observed)
        patch.setattr(Path, "relative_to", redundant)
        actual = preparation.git_checkpoint_preparation_implementation_digest()
    assert actual == expected
    assert reads == [path for _, path in originals]
    assert relative_calls == [], "热检查点不得重复构造固定源码相对路径"


def test_every_call_reads_fresh_bytes_even_when_size_and_timestamp_are_unchanged(
    tmp_path, monkeypatch
):
    """仅将读取映射到测试文件；不改真实安装源码，不将文件元数据当作字节证明。"""
    read = Path.read_bytes
    copies = {}
    for name, original in sources():
        copy = tmp_path / name
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(b"original source bytes\n")
        copies[original] = copy
    reads = []

    def from_copy(path):
        reads.append(path)
        return read(copies[path])

    monkeypatch.setattr(Path, "read_bytes", from_copy)
    first = preparation.git_checkpoint_preparation_implementation_digest()
    changed = next(iter(copies.values()))
    before = changed.stat()
    changed.write_bytes(b"modified source bytes\n")
    os.utime(changed, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert changed.stat().st_size == before.st_size
    assert changed.stat().st_mtime_ns == before.st_mtime_ns
    second = preparation.git_checkpoint_preparation_implementation_digest()
    assert second != first
    assert len(reads) == 8 and reads[:4] == reads[4:]


def test_source_read_failure_remains_a_fixed_preparation_error(monkeypatch):
    def unavailable(path):
        raise OSError("private source path must not escape")

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    with pytest.raises(KernelError) as caught:
        preparation.git_checkpoint_preparation_implementation_digest()
    assert caught.value.code == "git_checkpoint_preparation_invalid"
    assert "private source" not in str(caught.value)
    assert caught.value.__cause__ is None
