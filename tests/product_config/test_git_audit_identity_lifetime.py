"""原 Audit 的宿主级身份生命周期；真实 Store/Owner，fake native 只验证接线。

fake 不检查 SQLite FD，不替代非 editable 安装或真实原生身份集成证据。
"""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.product_config import action_runtime
from harnessix.product_config import git_delivery_review_host as hosts
from harnessix.product_config import git_prepared_native_identity as native
from harnessix.product_config import git_review_identity as identities
from harnessix.secrets.provider import SecretProvider
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.terminal_read_control import terminal_read_scope

_REVIEW_INVALID = "git_action_review_host_invalid"
_PREPARED_INVALID = "git_prepared_link_host_invalid"


class _BridgeError(RuntimeError):
    pass


class _AuditSubclass(SQLiteActionAuditStore):
    pass


class _FakeBackend:
    """保留已释放连接的占位；不伪造 Owner，也不执行 SQL 或授予写权限。"""

    BridgeError = _BridgeError

    def __init__(self):
        self.tokens = {}
        self.initialize_backend = Mock(side_effect=AssertionError("不得初始化真实原生桥"))
        self.attach_identity = Mock(side_effect=self._attach)

    def _attach(self, database, device, inode):
        if database in self.tokens:
            raise _BridgeError("connection already reserved")
        assert type(database) is sqlite3.Connection
        token = SimpleNamespace(check=Mock(return_value=True), release=Mock())

        def release():
            # release 必须先于外层 Store / fresh reader 关闭连接。
            assert type(database.in_transaction) is bool

        token.release.side_effect = release
        self.tokens[database] = token
        return token


@pytest.fixture(autouse=True)
def isolated_native_state(monkeypatch):
    monkeypatch.setattr(native, "_state", "not_started")
    monkeypatch.setattr(native, "_backend", None)
    monkeypatch.setattr(native, "_connections_started", False)
    monkeypatch.setattr(
        native, "import_module", Mock(side_effect=AssertionError("单测不得导入原生桥"))
    )
    yield
    assert getattr(identities._audit_connections, "registrations", {}) == {}
    native.import_module.assert_not_called()


@pytest.fixture
def backend(monkeypatch):
    bridge = _FakeBackend()
    monkeypatch.setattr(native, "_state", "ready")
    monkeypatch.setattr(native, "_backend", bridge)
    yield bridge
    for token in bridge.tokens.values():
        token.release.assert_called_once_with()
    bridge.initialize_backend.assert_not_called()
    native.import_module.assert_not_called()


@pytest.fixture
def audit_case(tmp_path, monkeypatch):
    with SQLiteActionAuditStore(tmp_path / "原 Audit #% .db", require_runtime_owner=True) as audit:
        original, path = audit._db, audit._path
        statements, fresh = [], []
        original.set_trace_callback(statements.append)
        factory = hosts.readonly_database

        def open_fresh(path):
            database = factory(path)
            fresh.append(database)
            return database

        monkeypatch.setattr(hosts, "readonly_database", open_fresh)
        yield SimpleNamespace(
            audit=audit,
            original=original,
            path=path,
            pin=identities._audit_file_identity(path),
            statements=statements,
            fresh=fresh,
        )
        for database in fresh:
            database.close()


def _observer(case):
    return identities.original_audit_observer(case.audit, case.original, case.path, case.pin)


def _read_fresh(case):
    return hosts._read_fresh_owner(case.audit, case.path, case.pin, original=case.original)


def _assert_invalid(check):
    with pytest.raises(KernelError) as caught:
        check()
    assert caught.value.code == _REVIEW_INVALID


def _assert_closed(database):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")


def _assert_retired(case, token, check):
    calls = token.check.call_count
    _assert_invalid(check)
    _assert_invalid(lambda: _observer(case))
    assert token.check.call_count == calls
    token.release.assert_called_once_with()
    assert case.audit._db is case.original
    assert case.audit._closed is False
    assert type(case.original.in_transaction) is bool


def test_host_keeps_the_moved_helpers_as_the_same_imports():
    assert hosts._audit_file_identity is identities._audit_file_identity
    assert hosts._observe_review_connection is identities._observe_review_connection


def test_original_attaches_once_across_repeated_observers_and_fresh_reads(
    audit_case, backend, monkeypatch
):
    case = audit_case
    owner = Mock(wraps=case.audit._read_runtime_owner)
    monkeypatch.setattr(case.audit, "_read_runtime_owner", owner)
    with identities.bind_product_audit_identity(case.audit), case.audit.runtime_owner() as fence:
        token = backend.tokens[case.original]
        for _ in range(3):
            check = _observer(case)
            calls, statements = token.check.call_count, list(case.statements)
            assert check() is None
            assert check() is None
            assert token.check.call_count == calls + 2
            assert case.statements == statements
            assert _read_fresh(case) is None
            assert case.audit._read_runtime_owner() is fence
            assert case.audit._assert_runtime_owner() is fence
            token.release.assert_not_called()
        assert backend.attach_identity.call_args_list == [
            call(case.original, *case.pin),
            *(call(database, *case.pin) for database in case.fresh),
        ]
        assert len(case.fresh) == len(set(case.fresh)) == 3
        assert any(sql.startswith("UPDATE action_audit_metadata") for sql in case.statements)
        assert any(sql.startswith("SELECT key, value") for sql in case.statements)
        for database in case.fresh:
            assert call(database=database) in owner.call_args_list
            backend.tokens[database].check.assert_called()
            backend.tokens[database].release.assert_called_once_with()
            _assert_closed(database)
    _assert_retired(case, token, check)


def test_identity_observation_never_substitutes_for_owner_or_write_authority(audit_case, backend):
    case = audit_case
    with identities.bind_product_audit_identity(case.audit):
        check = _observer(case)
        assert check() is None
        with pytest.raises(KernelError) as missing:
            case.audit._assert_runtime_owner()
        assert missing.value.code == "action_runtime_owner_required"
        with case.audit.runtime_owner() as fence:
            assert case.audit._assert_runtime_owner() is fence
            with terminal_read_scope(object(), case.audit, lambda _: b"", lambda: None):
                assert check() is None
                assert case.audit._read_runtime_owner() is fence
                with pytest.raises(KernelError) as denied:
                    case.audit._assert_runtime_owner()
                assert denied.value.code == "terminal_read_write_denied"
            with closing(sqlite3.connect(case.path)) as external, external:
                external.execute(
                    "UPDATE action_audit_metadata SET value='changed' WHERE key='owner_generation'"
                )
            assert check() is None
            for read in (case.audit._read_runtime_owner, lambda: _read_fresh(case)):
                with pytest.raises(KernelError) as lost:
                    read()
                assert lost.value.code == "action_runtime_fence_lost"


@pytest.mark.parametrize(
    "failure",
    [
        None,
        ValueError("body failure"),
        asyncio.CancelledError("task cancel"),
        TurnCancelled("turn cancel"),
        TimeoutError("deadline"),
        KernelError(_PREPARED_INVALID, "Owner 同名错误不是来源失败"),
    ],
    ids=["normal", "body", "task-cancel", "turn-cancel", "timeout", "owner-same-code"],
)
def test_scope_revokes_before_release_and_preserves_borrowed_database(audit_case, backend, failure):
    case, saved = audit_case, None

    def release():
        assert case.audit._runtime_fence is None
        assert case.original.in_transaction is False
        _assert_invalid(saved)
        _assert_invalid(lambda: _observer(case))

    def run():
        nonlocal saved
        with identities.bind_product_audit_identity(case.audit), case.audit.runtime_owner():
            saved = _observer(case)
            saved()
            backend.tokens[case.original].release.side_effect = release
            if failure is not None:
                raise failure

    if failure is None:
        run()
    else:
        with pytest.raises(type(failure)) as caught:
            run()
        assert caught.value is failure
    _assert_retired(case, backend.tokens[case.original], saved)
    assert case.original.execute("SELECT 1").fetchone() == (1,)


def test_binding_exit_does_not_commit_or_rollback_borrowed_transaction(audit_case, backend):
    case = audit_case
    with identities.bind_product_audit_identity(case.audit), case.audit.runtime_owner():
        check = _observer(case)
        case.original.execute("BEGIN IMMEDIATE")
        case.original.execute(
            "INSERT INTO action_audit_metadata(key, value) VALUES ('lifetime_probe', 'uncommitted')"
        )
        case.statements.clear()
    assert case.original.in_transaction is True
    assert case.statements == []
    _assert_retired(case, backend.tokens[case.original], check)
    with closing(sqlite3.connect(case.path)) as external:
        assert (
            external.execute(
                "SELECT value FROM action_audit_metadata WHERE key='lifetime_probe'"
            ).fetchone()
            is None
        )
    case.original.rollback()


def test_nested_and_released_bind_cannot_attach_the_same_database_again(audit_case, backend):
    case = audit_case
    with identities.bind_product_audit_identity(case.audit):
        check = _observer(case)
        with pytest.raises(KernelError) as nested:
            with identities.bind_product_audit_identity(case.audit):
                pytest.fail("嵌套绑定不能进入")
        assert nested.value.code == _REVIEW_INVALID
        backend.attach_identity.assert_called_once_with(case.original, *case.pin)
        assert check() is None
    with pytest.raises(KernelError) as repeated:
        with identities.bind_product_audit_identity(case.audit):
            pytest.fail("已释放连接也不能再次绑定")
    assert repeated.value.code == _REVIEW_INVALID
    assert len(backend.tokens) == 1
    _assert_retired(case, backend.tokens[case.original], check)


def test_pending_registration_rejects_attach_hook_reentrancy(audit_case, backend):
    case = audit_case

    def reentrant_attach(database, device, inode):
        _assert_invalid(lambda: _observer(case))
        with pytest.raises(KernelError) as nested:
            with identities.bind_product_audit_identity(case.audit):
                pytest.fail("pending 登记不能重入")
        assert nested.value.code == _REVIEW_INVALID
        return backend._attach(database, device, inode)

    backend.attach_identity.side_effect = reentrant_attach
    with identities.bind_product_audit_identity(case.audit):
        assert _observer(case)() is None
    backend.attach_identity.assert_called_once_with(case.original, *case.pin)


def test_independent_stores_can_bind_without_replacing_each_others_registration(
    audit_case, backend, tmp_path
):
    case = audit_case
    with (
        SQLiteActionAuditStore(tmp_path / "independent.db", require_runtime_owner=True) as other,
        identities.bind_product_audit_identity(case.audit),
        case.audit.runtime_owner(),
    ):
        original_check = _observer(case)
        with identities.bind_product_audit_identity(other), other.runtime_owner():
            other_check = identities.original_audit_observer(
                other, other._db, other._path, identities._audit_file_identity(other._path)
            )
            assert original_check() is None
            assert other_check() is None
        _assert_invalid(other_check)
        assert original_check() is None
        backend.tokens[case.original].release.assert_not_called()
        backend.tokens[other._db].release.assert_called_once_with()
    assert len(backend.tokens) == 2
    _assert_retired(case, backend.tokens[case.original], original_check)


@pytest.mark.parametrize("wrong", ["subclass", "database", "path", "pin", "other-store"])
def test_observer_construction_requires_original_store_database_path_and_pin(
    audit_case, backend, tmp_path, wrong
):
    case = audit_case
    with (
        SQLiteActionAuditStore(tmp_path / "other.db") as other,
        _AuditSubclass(tmp_path / "subclass.db") as subclass,
        identities.bind_product_audit_identity(case.audit),
    ):
        args = [case.audit, case.original, case.path, case.pin]
        if wrong == "subclass":
            args[0] = subclass
        elif wrong == "database":
            args[1] = other._db
        elif wrong == "path":
            args[2] = Path(str(case.path))
            assert args[2] == case.path and args[2] is not case.path
        elif wrong == "pin":
            args[3] = (case.pin[0], case.pin[1] + 1)
        else:
            args[0] = other
        token = backend.tokens[case.original]
        checks = token.check.call_count
        _assert_invalid(lambda: identities.original_audit_observer(*args))
        assert token.check.call_count == checks
        assert _observer(case)() is None


@pytest.mark.parametrize("field", ["_db", "_path", "_closed", "__class__"])
def test_saved_observer_rejects_field_drift_before_native_check(
    audit_case, backend, monkeypatch, field
):
    case = audit_case
    with closing(sqlite3.connect(":memory:")) as replacement:
        with identities.bind_product_audit_identity(case.audit), case.audit.runtime_owner():
            check = _observer(case)
            token = backend.tokens[case.original]
            values = {
                "_db": replacement,
                "_path": Path(str(case.path)),
                "_closed": True,
                "__class__": _AuditSubclass,
            }
            with monkeypatch.context() as patch:
                patch.setattr(case.audit, field, values[field])
                calls = token.check.call_count
                _assert_invalid(check)
                assert token.check.call_count == calls
            assert check() is None
        _assert_retired(case, token, check)


@pytest.mark.parametrize("change_pin", [False, True], ids=["equal-record", "different-pin"])
def test_saved_observer_requires_the_original_registration_instance(
    audit_case, backend, monkeypatch, change_pin
):
    case = audit_case
    with identities.bind_product_audit_identity(case.audit):
        check = _observer(case)
        registrations = identities._audit_connections.registrations
        issued = registrations[id(case.audit)]
        replacement = (
            issued._replace(pin=(case.pin[0], case.pin[1] + 1))
            if change_pin
            else (issued._replace())
        )
        assert replacement is not issued
        with monkeypatch.context() as patch:
            patch.setitem(registrations, id(case.audit), replacement)
            calls = backend.tokens[case.original].check.call_count
            _assert_invalid(check)
            assert backend.tokens[case.original].check.call_count == calls
        assert check() is None


@pytest.mark.parametrize("invalid", ["subclass", "closed", "database-type", "path-type"])
def test_invalid_store_binding_fails_before_native_attach(
    audit_case, backend, monkeypatch, tmp_path, invalid
):
    case = audit_case
    with _AuditSubclass(tmp_path / "subclass.db") as subclass:
        audit = subclass if invalid == "subclass" else case.audit
        with monkeypatch.context() as patch:
            if invalid == "closed":
                audit.close()
            elif invalid == "database-type":
                patch.setattr(audit, "_db", object())
            elif invalid == "path-type":
                patch.setattr(audit, "_path", str(case.path))
            with pytest.raises(KernelError) as caught:
                with identities.bind_product_audit_identity(audit):
                    pytest.fail("无效 Store 不能取得登记")
            assert caught.value.code == _REVIEW_INVALID
        backend.attach_identity.assert_not_called()


def test_native_missing_registration_fails_before_fresh_open_or_sql(audit_case, backend):
    case = audit_case
    with case.audit.runtime_owner():
        case.statements.clear()
        _assert_invalid(lambda: _observer(case))
        _assert_invalid(lambda: _read_fresh(case))
        assert case.fresh == []
        assert case.statements == []
        backend.attach_identity.assert_not_called()


def test_legacy_binding_and_observer_are_no_io_even_with_unusable_metadata(audit_case, monkeypatch):
    case = audit_case
    forbidden = Mock(side_effect=AssertionError("legacy 身份观察不得执行路径/native/SQL I/O"))
    with monkeypatch.context() as patch:
        patch.setattr(identities, "_audit_file_identity", forbidden)
        patch.setattr(native, "attach_prepared_identity", forbidden)
        patch.setattr(native, "check_prepared_identity", forbidden)
        patch.setattr(case.audit, "_path", object())
        patch.setattr(case.audit, "_db", object())
        patch.setattr(case.audit, "_closed", True)
        with identities.bind_product_audit_identity(case.audit):
            check = identities.original_audit_observer(case.audit, object(), object(), object())
            assert check() is None
        assert check() is None
    assert case.statements == []
    forbidden.assert_not_called()
    native.import_module.assert_not_called()


def test_observer_construction_is_lazy_but_explicit_check_calls_token(audit_case, backend):
    case = audit_case
    with identities.bind_product_audit_identity(case.audit):
        token = backend.tokens[case.original]
        calls = token.check.call_count
        check = _observer(case)
        assert token.check.call_count == calls
        token.check.return_value = False
        _assert_invalid(check)
        assert token.check.call_count == calls + 1


@pytest.mark.parametrize("state", ["loading", "failed"])
def test_explicit_native_failure_never_falls_back_to_legacy(audit_case, monkeypatch, state):
    monkeypatch.setattr(native, "_state", state)
    case = audit_case
    _assert_invalid(lambda: _observer(case))
    with pytest.raises(KernelError) as caught:
        with identities.bind_product_audit_identity(case.audit):
            pytest.fail("native 启动失败不能降级")
    assert caught.value.code == _REVIEW_INVALID
    assert case.statements == []


@pytest.mark.asyncio
async def test_same_thread_child_task_can_observe_but_inherits_no_sql_write_grant(
    audit_case, backend
):
    case = audit_case
    with identities.bind_product_audit_identity(case.audit), case.audit.runtime_owner() as fence:
        check = _observer(case)

        async def child():
            assert check() is None
            assert _observer(case)() is None
            assert case.audit._read_runtime_owner() is fence
            with pytest.raises(KernelError) as denied:
                case.audit._assert_runtime_owner()
            assert denied.value.code == "terminal_read_write_denied"

        with terminal_read_scope(object(), case.audit, lambda _: b"", lambda: None):
            await asyncio.wait_for(asyncio.create_task(child()), timeout=3)
    _assert_retired(case, backend.tokens[case.original], check)


@pytest.mark.asyncio
async def test_real_task_cancellation_revokes_the_registration_and_keeps_the_original_error(
    audit_case, backend
):
    case, ready, blocked = audit_case, asyncio.Event(), asyncio.Event()
    saved = {}

    async def owner_task():
        with identities.bind_product_audit_identity(case.audit), case.audit.runtime_owner():
            saved["check"] = _observer(case)
            saved["check"]()
            ready.set()
            try:
                await blocked.wait()
            except asyncio.CancelledError as error:
                saved["error"] = error
                raise

    task = asyncio.create_task(owner_task())
    try:
        await asyncio.wait_for(ready.wait(), timeout=3)
        task.cancel("lifetime cancellation")
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, timeout=3)
        assert caught.value is saved["error"]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert case.audit._runtime_fence is None
    _assert_retired(case, backend.tokens[case.original], saved["check"])


@pytest.mark.asyncio
async def test_delayed_child_cannot_use_a_saved_observer_after_parent_scope_exit(
    audit_case, backend
):
    case, proceed = audit_case, asyncio.Event()
    with identities.bind_product_audit_identity(case.audit), case.audit.runtime_owner():
        check = _observer(case)

        async def delayed_child():
            await proceed.wait()
            _assert_invalid(check)
            _assert_invalid(lambda: _observer(case))

        child = asyncio.create_task(delayed_child())
    calls = backend.tokens[case.original].check.call_count
    proceed.set()
    await asyncio.wait_for(child, timeout=3)
    assert backend.tokens[case.original].check.call_count == calls


def test_foreign_thread_cannot_construct_or_consume_original_observer(audit_case, backend):
    case = audit_case
    with identities.bind_product_audit_identity(case.audit), case.audit.runtime_owner():
        check = _observer(case)
        token = backend.tokens[case.original]
        calls = token.check.call_count

        def foreign():
            _assert_invalid(lambda: _observer(case))
            _assert_invalid(check)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(foreign).result(timeout=3)
        assert token.check.call_count == calls
        assert check() is None


@pytest.mark.parametrize("failure", [_BridgeError("native"), sqlite3.OperationalError("sqlite")])
@pytest.mark.parametrize("stage", ["attach", "check", "release"])
def test_source_failures_map_to_review_invalid_and_revoke_registration(
    audit_case, backend, stage, failure
):
    case = audit_case
    if stage == "attach":
        backend.attach_identity.side_effect = failure
    with pytest.raises(KernelError) as caught:
        with identities.bind_product_audit_identity(case.audit):
            token = backend.tokens[case.original]
            check = _observer(case)
            if stage == "check":
                token.check.side_effect = failure
                check()
            else:
                token.release.side_effect = failure
    assert caught.value.code == _REVIEW_INVALID
    _assert_invalid(lambda: _observer(case))
    assert case.original.in_transaction is False
    if stage != "attach":
        _assert_retired(case, token, check)


@pytest.mark.parametrize(
    "failure",
    [
        False,
        _BridgeError("entry check"),
        asyncio.CancelledError("cancel"),
        TimeoutError("deadline"),
    ],
    ids=["false", "bridge", "cancel", "timeout"],
)
def test_initial_token_check_failure_releases_and_removes_pending_registration(
    audit_case, backend, failure
):
    case = audit_case

    def failing_attach(database, device, inode):
        token = backend._attach(database, device, inode)
        token.check.side_effect = [failure]
        token.release.side_effect = _BridgeError("cleanup must not replace the first failure")
        return token

    backend.attach_identity.side_effect = failing_attach
    expected = (
        type(failure)
        if isinstance(failure, (asyncio.CancelledError, TimeoutError))
        else (KernelError)
    )
    with pytest.raises(expected) as caught:
        with identities.bind_product_audit_identity(case.audit):
            pytest.fail("首次 token.check 失败不能签发宿主登记")
    if expected is KernelError:
        assert caught.value.code == _REVIEW_INVALID
    else:
        assert caught.value is failure
    assert id(case.audit) not in identities._audit_connections.registrations
    token = backend.tokens[case.original]
    token.check.assert_called_once_with()
    token.release.assert_called_once_with()
    _assert_invalid(lambda: _observer(case))
    assert case.original.in_transaction is False


@pytest.mark.parametrize(
    "failure",
    [
        asyncio.CancelledError("cancel"),
        TurnCancelled("cancel"),
        TimeoutError("deadline"),
        KernelError("git_process_timeout", "deadline"),
    ],
    ids=["task-cancel", "turn-cancel", "timeout", "kernel-timeout"],
)
@pytest.mark.parametrize("stage", ["attach", "check", "release"])
def test_native_control_exceptions_keep_the_same_object(audit_case, backend, stage, failure):
    case = audit_case
    if stage == "attach":
        backend.attach_identity.side_effect = failure
    with pytest.raises(type(failure)) as caught:
        with identities.bind_product_audit_identity(case.audit):
            token = backend.tokens[case.original]
            check = _observer(case)
            getattr(token, stage).side_effect = failure
            if stage == "check":
                check()
    assert caught.value is failure
    _assert_invalid(lambda: _observer(case))
    assert case.original.in_transaction is False
    if stage != "attach":
        _assert_retired(case, token, check)


@pytest.mark.parametrize(
    "failure",
    [
        KernelError(_PREPARED_INVALID, "Owner 同名错误"),
        asyncio.CancelledError("cancel"),
        TimeoutError("deadline"),
        ValueError("body"),
    ],
    ids=["owner-same-code", "cancel", "timeout", "body"],
)
def test_first_owner_failure_survives_fresh_and_original_cleanup_failures(
    audit_case, backend, monkeypatch, failure
):
    case = audit_case
    with pytest.raises(type(failure)) as caught:
        with identities.bind_product_audit_identity(case.audit), case.audit.runtime_owner():
            original = backend.tokens[case.original]
            check = _observer(case)
            original.release.side_effect = _BridgeError("original cleanup")

            def failing_owner(*, database=None):
                assert database is not None and database is not case.original
                backend.tokens[database].release.side_effect = _BridgeError("fresh cleanup")
                raise failure

            monkeypatch.setattr(case.audit, "_read_runtime_owner", failing_owner)
            _read_fresh(case)
    assert caught.value is failure
    _assert_retired(case, original, check)
    assert len(case.fresh) == 1
    for database in case.fresh:
        backend.tokens[database].release.assert_called_once_with()
        _assert_closed(database)


def test_caught_outer_exception_does_not_hide_a_new_cleanup_failure(audit_case, backend):
    try:
        raise ValueError("already handled")
    except ValueError:
        with pytest.raises(KernelError) as caught:
            with identities.bind_product_audit_identity(audit_case.audit):
                backend.tokens[audit_case.original].release.side_effect = _BridgeError("cleanup")
        assert caught.value.code == _REVIEW_INVALID


@pytest.mark.parametrize("failing_connection", ["original", "fresh"])
def test_fresh_owner_query_is_followed_by_both_identity_checks(
    audit_case, backend, monkeypatch, failing_connection
):
    case = audit_case
    with identities.bind_product_audit_identity(case.audit), case.audit.runtime_owner():
        read_owner = case.audit._read_runtime_owner
        owner_calls = []

        def invalidate_after_owner(*, database=None):
            result = read_owner(database=database)
            owner_calls.append(database)
            target = case.original if failing_connection == "original" else database
            backend.tokens[target].check.return_value = False
            return result

        monkeypatch.setattr(case.audit, "_read_runtime_owner", invalidate_after_owner)
        _assert_invalid(lambda: _read_fresh(case))
        assert owner_calls == case.fresh and len(owner_calls) == 1
        _assert_closed(case.fresh[0])
        backend.tokens[case.fresh[0]].release.assert_called_once_with()
        backend.tokens[case.original].release.assert_not_called()


def test_original_token_failure_precedes_any_fresh_connection_or_owner_query(
    audit_case, backend, monkeypatch
):
    case = audit_case
    with identities.bind_product_audit_identity(case.audit), case.audit.runtime_owner():
        token = backend.tokens[case.original]
        token.check.return_value = False
        owner = Mock(side_effect=AssertionError("原身份失效不能查询 Owner"))
        monkeypatch.setattr(case.audit, "_read_runtime_owner", owner)
        _assert_invalid(lambda: _read_fresh(case))
        assert case.fresh == []
        owner.assert_not_called()
        backend.attach_identity.assert_called_once_with(case.original, *case.pin)


@pytest.mark.asyncio
async def test_action_dependencies_bind_before_owner_and_release_before_store_close(
    tmp_path, backend
):
    secrets = Mock(spec=SecretProvider)
    saved = {}

    def attach_before_owner(database, device, inode):
        assert database.execute(
            "SELECT value FROM action_audit_metadata WHERE key='owner_generation'"
        ).fetchone() == ("0",)
        return backend._attach(database, device, inode)

    backend.attach_identity.side_effect = attach_before_owner
    async with action_runtime._open_action_dependencies(tmp_path, secrets) as dependencies:
        audit = dependencies.audit
        original, path = audit._db, audit._path
        pin = identities._audit_file_identity(path)
        check = identities.original_audit_observer(audit, original, path, pin)
        assert check() is None
        assert audit._read_runtime_owner() is dependencies.fence
        token = backend.tokens[original]

        def release():
            assert audit._runtime_fence is None
            assert original.in_transaction is False
            _assert_invalid(check)
            _assert_invalid(lambda: identities.original_audit_observer(audit, original, path, pin))
            saved["released"] = True

        token.release.side_effect = release
        backend.attach_identity.assert_called_once_with(original, *pin)
    assert saved == {"released": True}
    token.release.assert_called_once_with()
    assert audit._closed is True
    _assert_closed(original)
    _assert_invalid(check)
    secrets.resolve.assert_not_called()


@pytest.mark.parametrize("during_attach", [False, True])
def test_class_drift_never_calls_foreign_fields_or_hash_during_check_and_cleanup(
    audit_case, backend, during_attach
):
    case, forbidden = audit_case, []

    class PoisonAudit(SQLiteActionAuditStore):
        def __hash__(self):
            forbidden.append("hash")
            raise AssertionError("清理不得调用替身 hash")

        def __getattribute__(self, name):
            forbidden.append(name)
            raise AssertionError("类型拒绝必须先于替身字段访问")

    attach = backend.attach_identity.side_effect

    def replace_after_attach(*args):
        token = attach(*args)
        object.__setattr__(case.audit, "__class__", PoisonAudit)
        return token

    if during_attach:
        backend.attach_identity.side_effect = replace_after_attach
    try:
        if during_attach:
            with pytest.raises(KernelError) as caught:
                with identities.bind_product_audit_identity(case.audit):
                    pytest.fail("改变类型的宿主不得发布登记")
            assert caught.value.code == _REVIEW_INVALID
        else:
            with identities.bind_product_audit_identity(case.audit):
                check = _observer(case)
                object.__setattr__(case.audit, "__class__", PoisonAudit)
                _assert_invalid(check)
    finally:
        object.__setattr__(case.audit, "__class__", SQLiteActionAuditStore)
    assert not forbidden
    assert identities._audit_connections.registrations == {}
    assert case.original.execute("SELECT 1").fetchone() == (1,)
