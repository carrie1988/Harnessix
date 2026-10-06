"""共享资源实现身份变化必须使原计划失效，不沿用旧批准或创建新Lease。"""

from pathlib import Path

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.product_config.git_delivery_process import _implementation_digest
from tests.product_config import test_git_delivery_process as original

make_process = original.make_process


def _changed_host_source(monkeypatch, name="git_process_host.py"):
    """只改变本测试的实际安装源码观察，不编辑磁盘文件或改批准。"""
    read = Path.read_bytes

    def changed(path: Path):
        body = read(path)
        return body + b"\n# source identity mutation\n" if path.name == name else body

    monkeypatch.setattr(Path, "read_bytes", changed)


@pytest.mark.parametrize(
    "name", ["git_process_host.py", "git_repository_recipe.py", "git_repository_observation.py"]
)
def test_shared_host_module_bytes_change_original_implementation_digest(monkeypatch, name):
    before = _implementation_digest()
    _changed_host_source(monkeypatch, name)
    assert _implementation_digest() != before


@pytest.mark.parametrize(
    "name", ["git_process_host.py", "git_repository_recipe.py", "git_repository_observation.py"]
)
async def test_old_plan_cannot_reuse_approval_after_shared_host_source_change(
    make_process, monkeypatch, name
):
    case = make_process()
    prepared = case.port.prepare(
        case.workspace, ("version",), budget=original.GitOperationBudget(20)
    )
    plan = original._plan(case, prepared)
    checkpoint = original._checkpoint(plan)
    _changed_host_source(monkeypatch, name)
    with pytest.raises(KernelError) as error:
        await case.port.run(
            prepared, plan, CancelToken(), budget=prepared.budget, checkpoint=checkpoint
        )
    assert error.value.code == "git_process_plan_mismatch"
    original._assert_not_started(case)
