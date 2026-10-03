"""用同一真实根对比提取前后快照及资源准入错误，不替换原生端口。"""

from __future__ import annotations

import subprocess
import sys
from types import ModuleType

import pytest

from harnessix.agent.errors import KernelError
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.snapshot import _snapshot_resource_requests, capture_workspace_snapshot
from tests.governance.test_readability_policy import ROOT


@pytest.fixture(scope="module")
def original_snapshot():
    source = subprocess.run(
        [
            "git",
            "show",
            "0f1948c3a258943698a8fe3e4309b81e78b8d5b3:src/harnessix/workspace/snapshot.py",
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
    ).stdout
    name = "_snapshot_before_resource_refactor"
    module = ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(source, name, "exec"), module.__dict__)
        yield module.capture_workspace_snapshot
    finally:
        del sys.modules[name]


@pytest.mark.parametrize(
    "count,explicit", [(0, False), (1, False), (255, False), (254, True), (255, True)]
)
def test_real_snapshot_models_match_original(original_snapshot, tmp_path, count, explicit):
    requests = [
        WorkspaceResourceRequest(path=f"leaf-{index}", access="read") for index in range(count)
    ]
    if explicit:
        requests.append(WorkspaceResourceRequest(path=".", access="read"))
    for index in range(min(count, 3)):
        (tmp_path / f"leaf-{index}").write_text("固定正文\n", encoding="utf-8")
    before = original_snapshot(tmp_path, resources=tuple(requests))
    after = capture_workspace_snapshot(tmp_path, resources=tuple(requests))
    assert after == before
    assert after.model_dump(mode="json") == before.model_dump(mode="json")


@pytest.mark.parametrize("mode", ["implicit-cwd-limit", "input-limit", "duplicate"])
def test_original_errors_and_capture_input_frame_are_preserved(original_snapshot, tmp_path, mode):
    count = 256 if mode == "implicit-cwd-limit" else 257
    requests = tuple(
        WorkspaceResourceRequest(path=f"leaf-{index}", access="read") for index in range(count)
    )
    if mode == "duplicate":
        request = WorkspaceResourceRequest(path=".", access="read")
        requests = (request, request)
    outcomes = []
    for capture in (original_snapshot, capture_workspace_snapshot):
        with pytest.raises(KernelError) as caught:
            capture(tmp_path, resources=requests)
        frame = caught.value.__traceback__
        while frame is not None and frame.tb_frame.f_code is not capture.__code__:
            frame = frame.tb_next
        assert frame is not None and frame.tb_frame.f_locals["resources"] == requests
        outcomes.append(caught.value.code)
    expected = "workspace_snapshot_duplicate" if mode == "duplicate" else "workspace_snapshot_limit"
    assert outcomes == [expected, expected]


@pytest.mark.parametrize("platform", ["posix", "windows"])
@pytest.mark.parametrize("explicit", [False, True])
def test_requested_sequence_preserves_mixed_locations_and_cwd_position(platform, explicit):
    """最终Snapshot排序不能代替准入序列验证；原请求不得排序或按根重新分组。"""
    cwd = WorkspaceResourceRequest(path=".", access="read")
    original = [
        WorkspaceResourceRequest(path="z-leaf", access="write"),
        WorkspaceResourceRequest(location="cache", path=".", access="read"),
        WorkspaceResourceRequest(path="a-leaf", access="execute"),
    ]
    if explicit:
        original.insert(1, cwd)
    resources = tuple(original)
    requested = _snapshot_resource_requests(resources, ".", platform)
    assert requested == (original if explicit else [*original, cwd])
    assert resources == tuple(original)
    assert requested is not original
