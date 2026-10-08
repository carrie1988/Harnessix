"""原生只读观察的 Task/控制负控；替身宿主不计为真实授权或 SDK 验收。"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import Thread
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config import git_prepared_link_ledger as ledger
from harnessix.product_config import git_user_observation as user
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_native_control import qualified_native_observer
from harnessix.product_config.git_prepared_link_connection import (
    open_prepared_git_connection,
    require_prepared_git_connection,
)
from harnessix.session.sqlite_history import AuthenticatedThreadHistory
from harnessix.workspace.native_observation_io import UpstreamCheckpointError


def _subject(module, name):
    """显式旧文件红测；不扫描历史、禁区或自动降级实际安装来源。"""
    frozen = os.environ.get("HARNESSIX_USER_NATIVE_BEFORE")
    if not frozen:
        return module
    path = Path(frozen) / name
    spec = importlib.util.spec_from_file_location("_frozen_" + path.stem, path)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


USER = _subject(user, "git_user_observation.py")
LEDGER = _subject(ledger, "git_prepared_link_ledger.py")
ERRORS = (
    KernelError("git_user_observation_unavailable", "原同码控制错误"),
    OSError("原控制错误"),
    TimeoutError("原期限错误"),
    TurnCancelled(),
    asyncio.CancelledError(),
    UpstreamCheckpointError(UpstreamCheckpointError(OSError("原嵌套标记"))),
)


async def test_native_adapter_preserves_exact_type_and_original_binding():
    trace = []
    parent = GitAuthenticationControl(lambda: trace.append("local"), lambda: trace.append("full"))
    origin = parent._origin
    protected = USER._native_checkpointer(parent)
    assert type(protected) is GitAuthenticationControl
    with protected.io_progress() as check:
        for _ in range(20):
            check()
    assert trace == ["full", *(["local"] * 21), "full"]
    assert parent._origin is origin and protected._origin[2] is asyncio.current_task()


@pytest.mark.parametrize("marker", ERRORS)
async def test_native_adapter_marks_local_first_failure_once(marker):
    full = []

    def local():
        raise marker

    parent = GitAuthenticationControl(local, lambda: full.append("full"))
    protected = USER._native_checkpointer(parent)
    assert type(protected) is GitAuthenticationControl
    with pytest.raises(UpstreamCheckpointError) as caught, protected.io_progress() as check:
        check()
    assert caught.value.error is marker and full == ["full"]


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
async def test_unknown_native_adapters_keep_the_original_full_marker_trace(kind):
    trace = []

    class Proxy:
        def __call__(self):
            trace.append("full")

    class Subclass(GitAuthenticationControl):
        pass

    def callback():
        trace.append("full")

    if kind == "proxy":
        callback = Proxy()
    elif kind == "subclass":
        callback = Subclass(lambda: trace.append("local"), lambda: trace.append("full"))
    protected = USER._native_checkpointer(callback)
    assert type(protected) is not GitAuthenticationControl
    for _ in range(5):
        protected()
    assert trace == ["full"] * 5


async def test_foreign_task_native_adapter_does_not_rebind_the_parent():
    trace = []
    parent = GitAuthenticationControl(lambda: trace.append("local"), lambda: trace.append("full"))
    origin = parent._origin

    async def child():
        protected = USER._native_checkpointer(parent)
        assert type(protected) is not GitAuthenticationControl
        protected()

    await asyncio.create_task(child())
    assert parent._origin is origin and trace == ["full"]


async def test_qualified_observer_is_read_only_delegation_not_parent_local_borrowing():
    events = []
    parent = GitAuthenticationControl(lambda: pytest.fail("不能借父局部检查"), lambda: None)
    origin = parent._origin
    observe = qualified_native_observer(parent, lambda: events.append("read-only"))
    assert observe is not None

    async def child():
        observe()
        with pytest.raises(KernelError, match="归属无效"):
            qualified_native_observer(parent, lambda: None)

    await asyncio.create_task(child())
    assert events == ["read-only"] and parent._origin is origin


@pytest.mark.parametrize("field", ["_local_check", "_authenticate", "_task", "_thread"])
async def test_read_observer_refuses_parent_origin_drift(field):
    parent = GitAuthenticationControl(lambda: None, lambda: None)
    observe = qualified_native_observer(parent, lambda: pytest.fail("不得进入观察"))
    assert observe is not None
    setattr(parent, field, object())
    with pytest.raises(KernelError) as caught:
        observe()
    assert caught.value.code == "git_authentication_control_invalid"


@pytest.fixture
def verification(monkeypatch, tmp_path):
    trace = []
    now = datetime.now(UTC)
    thread = Thread(thread_id=uuid4(), workspace=str(tmp_path), created_at=now, updated_at=now)
    history = AuthenticatedThreadHistory(thread, ())
    router = SimpleNamespace(_audit=SimpleNamespace(_checkpoint=object(), _read_blob=object()))
    transactions = SimpleNamespace(_checkpoint=object())
    monkeypatch.setattr(
        USER, "require_git_user_authority", lambda *args: lambda: trace.append("host")
    )
    return SimpleNamespace(
        history=history,
        router=router,
        transactions=transactions,
        cancel=CancelToken(),
        budget=GitOperationBudget(60),
        parent=None,
        trace=trace,
        owner=None,
    )


async def _verify(case, observer):
    case.owner = asyncio.current_task()
    case.parent = GitAuthenticationControl(
        lambda: pytest.fail("不能借父局部控制"), lambda: case.trace.append("full")
    )
    await USER.verify_product_git_user_observation(
        object(),
        case.history,
        case.router,
        case.transactions,
        object(),
        session=object(),
        cancel=case.cancel,
        budget=case.budget,
        checkpoint=case.parent,
        snapshot_ports=object(),
        native_observer=observer,
    )


async def test_verification_constructs_control_in_actual_managed_child(verification, monkeypatch):
    case = verification
    entered = []
    stop = RuntimeError("停止在原纯数据入口，不伪造来源成功")

    def snapshot(value, model, check):
        assert asyncio.current_task() is not case.owner
        assert type(check) is GitAuthenticationControl
        assert check._origin[2] is asyncio.current_task()
        with check.io_progress() as local:
            local()
            local()
        entered.append("child")
        raise stop

    monkeypatch.setattr(USER, "_snapshot", snapshot)
    with pytest.raises(RuntimeError) as caught:
        await _verify(case, lambda: case.trace.append("read-only"))
    assert caught.value is stop and entered == ["child"]
    assert case.trace.count("read-only") == 3  # 两次调用及原成功尾部频检。
    assert case.parent._origin[2] is case.owner


async def test_parent_task_cancellation_stops_native_child_before_next_read(
    verification, monkeypatch
):
    case = verification
    reads = []

    def snapshot(value, model, check):
        with check.io_progress() as local:
            case.owner.cancel()
            local()
            reads.append("不得继续读取")

    monkeypatch.setattr(USER, "_snapshot", snapshot)
    operation = asyncio.create_task(_verify(case, lambda: None))
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert reads == [] and operation.done()
    assert case.trace.count("full") == 2


@pytest.mark.parametrize("marker", ERRORS)
async def test_child_native_first_failure_keeps_original_identity(
    verification, monkeypatch, marker
):
    case = verification

    def observer():
        raise marker

    def snapshot(value, model, check):
        with check.io_progress() as local:
            local()
        pytest.fail("不得返回部分声明")

    monkeypatch.setattr(USER, "_snapshot", snapshot)
    with pytest.raises(type(marker)) as caught:
        await _verify(case, observer)
    assert caught.value is marker
    assert case.trace.count("full") == 2  # 原入口及段入口，无失败后的出口认证。


@pytest.mark.parametrize(
    "drift", ["store-check", "audit-check", "audit-reader", "cancel", "deadline"]
)
async def test_child_native_signals_reject_original_resources_before_next_read(
    verification, monkeypatch, drift
):
    case = verification

    def snapshot(value, model, check):
        with check.io_progress() as local:
            if drift == "store-check":
                case.transactions._checkpoint = object()
            elif drift == "audit-check":
                case.router._audit._checkpoint = object()
            elif drift == "audit-reader":
                case.router._audit._read_blob = object()
            elif drift == "cancel":
                case.cancel.cancel()
            else:
                case.budget._deadline = 0
            local()
        pytest.fail("失效后不再读取")

    monkeypatch.setattr(USER, "_snapshot", snapshot)
    expected = TurnCancelled if drift == "cancel" else KernelError
    with pytest.raises(expected):
        await _verify(case, lambda: None)
    assert case.trace.count("full") == 2


@pytest.fixture
def native_ledger(monkeypatch):
    actual = LEDGER.ProductGitPreparedLinkLedger.__new__(LEDGER.ProductGitPreparedLinkLedger)
    for key in ("_router", "_core_store", "_artifacts", "_reader", "_ports", "_workspace_scope"):
        setattr(actual, key, object())
    actual._database = object()
    events, epoch = [], (object(), 17)
    monkeypatch.setattr(
        LEDGER,
        "_prepared_git_connection_registration_observer",
        lambda db: lambda: events.append("registration"),
        raising=False,
    )
    monkeypatch.setattr(
        LEDGER, "_prepared_runtime_thread_observer", lambda *args: lambda: events.append("runtime")
    )
    monkeypatch.setattr(LEDGER, "git_prefix_transaction_epoch", lambda db: epoch)
    monkeypatch.setattr(
        LEDGER,
        "require_git_prefix_transaction_epoch",
        lambda db, value: events.append("epoch") if value is epoch else pytest.fail("新代际"),
    )
    monkeypatch.setattr(
        LEDGER,
        "_git_review_host_checks",
        lambda *args: (lambda: events.append("bound"), lambda: pytest.fail("不应鲜读Owner")),
        raising=False,
    )
    return SimpleNamespace(
        ledger=actual,
        events=events,
        epoch=epoch,
        parent=None,
        cancel=CancelToken(),
        budget=GitOperationBudget(60),
    )


def native_parent(case):
    case.parent = GitAuthenticationControl(
        lambda: pytest.fail("父局部禁止"), lambda: case.events.append("full")
    )
    return case.parent


async def test_ledger_read_observer_keeps_original_registry_runtime_and_epoch(native_ledger):
    case = native_ledger
    observe = LEDGER._native_user_observer(
        case.ledger, case.cancel, case.budget, native_parent(case)
    )
    assert observe is not None
    case.events.clear()

    async def child():
        observe()

    await asyncio.create_task(child())
    assert case.events == ["bound", "registration", "runtime", "epoch"]


@pytest.mark.parametrize("field", ["_database", "_router", "_core_store", "_reader"])
async def test_ledger_native_observer_rejects_resource_replacement(native_ledger, field):
    case = native_ledger
    observe = LEDGER._native_user_observer(
        case.ledger, case.cancel, case.budget, native_parent(case)
    )
    assert observe is not None
    setattr(case.ledger, field, object())
    with pytest.raises(KernelError):
        observe()
    assert "epoch" not in case.events


async def test_ledger_unknown_or_foreign_creator_does_not_issue_native_observer(native_ledger):
    case = native_ledger
    native_parent(case)
    assert LEDGER._native_user_observer(case.ledger, case.cancel, case.budget, lambda: None) is None

    async def child():
        assert (
            LEDGER._native_user_observer(case.ledger, case.cancel, case.budget, case.parent) is None
        )

    await asyncio.create_task(child())
    assert not case.events


async def test_real_connection_registration_is_observed_not_transferred(
    native_ledger, monkeypatch, tmp_path
):
    case = native_ledger
    path = tmp_path / "git-delivery.db"
    sqlite3.connect(path).close()
    from harnessix.product_config.git_prepared_link_connection import (
        _prepared_git_connection_registration_observer,
    )

    monkeypatch.setattr(
        LEDGER,
        "_prepared_git_connection_registration_observer",
        _prepared_git_connection_registration_observer,
    )
    with open_prepared_git_connection(path, read_only=False) as database:
        case.ledger._database = database
        observe = LEDGER._native_user_observer(
            case.ledger, case.cancel, case.budget, native_parent(case)
        )
        assert observe is not None

        async def child():
            observe()
            with pytest.raises(KernelError) as caught:
                require_prepared_git_connection(database, path)
            assert caught.value.code == "git_prepared_link_host_invalid"

        await asyncio.create_task(child())
        require_prepared_git_connection(database, path)
    with pytest.raises(KernelError):
        observe()


def test_shared_native_implementation_is_freshly_bound_even_with_unchanged_metadata(monkeypatch):
    shared = [b"original shared control bytes"]
    reads = []

    def read(path):
        reads.append(path.name)
        return shared[0] if path.name == "git_native_control.py" else b"fixed source bytes"

    monkeypatch.setattr(Path, "read_bytes", read)
    before = USER.git_user_observation_implementation_digest()
    shared[0] = b"modified shared control bytes"
    after = USER.git_user_observation_implementation_digest()
    assert before != after
    assert reads.count("git_native_control.py") == 2
    assert reads[: len(reads) // 2] == reads[len(reads) // 2 :]


def test_unavailable_shared_control_cannot_reuse_old_observation_digest(monkeypatch):
    def read(path):
        if path.name == "git_native_control.py":
            raise OSError("private source name")
        return b"fixed source bytes"

    monkeypatch.setattr(Path, "read_bytes", read)
    with pytest.raises(KernelError) as caught:
        USER.git_user_observation_implementation_digest()
    assert caught.value.code == "git_user_observation_unavailable"
    assert "private source" not in str(caught.value)
