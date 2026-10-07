"""只验证原末轮配方复用顺序；模拟端口不能作为认证成功证据。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.product_config import git_checkpoint_preparation as preparation
from harnessix.product_config import git_user_observation as observation_module
from harnessix.product_config.git_delivery_process import GitOperationBudget


def recipe(monkeypatch, *, fault=None):
    calls = []
    value = SimpleNamespace(directory="original", baseline=object(), config_sha256="config")
    value.index_file_observation = object()
    reader = SimpleNamespace(_root=object())
    original = RuntimeError("synthetic control")

    async def reports(*args):
        calls.append("reports")
        return object(), object()

    async def git(*args):
        calls.append("logical-git")

    @contextmanager
    def pin(*args, **kwargs):
        calls.append("pin-open")

        def index(check):
            calls.append("physical-index")
            return object() if fault == "index" else value.index_file_observation

        try:
            yield SimpleNamespace(observe_index=index)
        finally:
            calls.append("pin-close")

    def facts(*args):
        calls.append("directory-facts")
        return {"directory": "changed" if fault == "directory" else value.directory}

    async def history():
        calls.append("history")
        if fault == "history":
            raise original

    def source():
        calls.append("source")
        if fault == "source":
            raise original

    def check():
        calls.append("control")
        if fault == "control":
            raise original

    for name, function in (
        ("_reports", reports),
        ("_verify_final_git_facts", git),
        ("pin_git_user_directories", pin),
        ("git_user_directory_facts", facts),
    ):
        monkeypatch.setattr(observation_module, name, function)
    return value, reader, calls, history, source, check, original


@pytest.mark.parametrize("fault", [None, "directory", "index", "history", "source", "control"])
async def test_shared_final_recipe_preserves_order_and_closes_pin(monkeypatch, fault):
    value, reader, calls, history, source, check, original = recipe(monkeypatch, fault=fault)
    invoke = observation_module._verify_observed_git_state
    rejected = KernelError("synthetic_mismatch", "合成配方不匹配")
    arguments = dict(verify_history=history, verify_source=source, invalid=lambda: rejected)
    if fault is None:
        await invoke(value, reader, CancelToken(), check, **arguments)
        assert calls == [
            "reports",
            "pin-open",
            "directory-facts",
            "logical-git",
            "history",
            "source",
            "physical-index",
            "control",
            "pin-close",
        ]
    else:
        with pytest.raises(BaseException) as caught:
            await invoke(value, reader, CancelToken(), check, **arguments)
        assert caught.value is (rejected if fault in {"directory", "index"} else original)
        assert calls[-1] == "pin-close"
        if fault == "directory":
            assert "logical-git" not in calls
        if fault == "history":
            assert "source" not in calls


async def test_original_preparer_delegates_same_stage_history_and_source(monkeypatch):
    """准备器保留原_pending约束和Source注入点，不能换成新的阶段无关入口。"""
    value, reader, calls, history, source, check, original = recipe(monkeypatch)
    value.baseline = SimpleNamespace(source=object())
    planner = SimpleNamespace(
        reader=reader, router=object(), core_store=SimpleNamespace(store=object()), ports=object()
    )
    thread, turn, call, expected = object(), object(), object(), object()
    token, budget = CancelToken(), GitOperationBudget(60)

    async def old_history(*args):
        assert args == (planner, thread, turn, call, token, budget, check, expected)
        await history()

    def old_source(*args, **kwargs):
        assert args == (thread, value.baseline.source, planner.router, planner.core_store.store)
        assert kwargs["snapshot_ports"] is planner.ports
        assert callable(kwargs["checkpoint"])
        source()

    monkeypatch.setattr(preparation, "_history", old_history)
    monkeypatch.setattr(preparation, "verify_git_delivery_source", old_source)
    # 配方端口归属原观察模块；准备器必须调用同一实现而不是留一份复制体。
    await preparation._verify_observation(
        planner, value, thread, turn, call, token, budget, check, expected
    )
    assert calls == [
        "reports",
        "pin-open",
        "directory-facts",
        "logical-git",
        "history",
        "source",
        "physical-index",
        "control",
        "pin-close",
    ]
