"""Review 只接线 Owner 鲜读身份；原 Audit 仅测 helper 借用，不宣称生命周期认证。

fake Token 约束单连接只 attach 一次，但不证明真实 FD 或原生安装资格。
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.product_config import git_delivery_review_host as hosts
from harnessix.product_config import git_prepared_native_identity as native
from harnessix.trusted_actions.store import SQLiteActionAuditStore

_REVIEW_INVALID = "git_action_review_host_invalid"
_PREPARED_INVALID = "git_prepared_link_host_invalid"


class _BridgeError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def isolated_native_state(monkeypatch):
    monkeypatch.setattr(native, "_state", "not_started")
    monkeypatch.setattr(native, "_backend", None)
    monkeypatch.setattr(native, "_connections_started", False)
    monkeypatch.setattr(
        native, "import_module", Mock(side_effect=AssertionError("单测不得加载原生桥"))
    )


@pytest.fixture
def review_case(tmp_path, monkeypatch):
    with (
        SQLiteActionAuditStore(tmp_path / "audit.db", require_runtime_owner=True) as audit,
        audit.runtime_owner(),
    ):
        original, events, views = audit._db, Mock(), []
        pin = hosts._audit_file_identity(audit._path)
        factory = hosts.readonly_database
        original.set_trace_callback(events.original.sql)

        def open_observer(path):
            events.fresh.open(path)
            observer = factory(path)
            views.append(observer)
            # 原工厂的 query_only/foreign_keys 设置不计入新增来源观察 SQL。
            observer.set_trace_callback(events.fresh.sql)
            return observer

        monkeypatch.setattr(hosts, "readonly_database", open_observer)
        try:
            yield SimpleNamespace(
                audit=audit,
                original=original,
                pin=pin,
                events=events,
                views=views,
                read=partial(hosts._read_fresh_owner, audit, audit._path, pin, original=original),
            )
        finally:
            original.set_trace_callback(None)
            for observer in views:
                observer.close()


@pytest.fixture
def backend(review_case, monkeypatch):
    events = review_case.events
    tokens, attached = [events.original, events.fresh], {}

    def release(token):
        # exact Connection 的原生属性在关闭后会抛错，验证 release 先于 close。
        assert type(token.attach.call_args.args[0].in_transaction) is bool

    def configure(token):
        token.check.return_value = True
        token.release.side_effect = partial(release, token)

    for token in tokens:
        configure(token)

    def attach(database, device, inode):
        # 不因 release 清除此登记，避免 fake 接受原生明确禁止的重复 attach。
        if database in attached:
            raise _BridgeError("connection already reserved")
        if database is review_case.original:
            token = events.original
        elif not events.fresh.attach.called:
            token = events.fresh
        else:
            token = Mock()
            events.attach_mock(token, f"fresh_{len(tokens)}")
            configure(token)
            tokens.append(token)
        token.attach(database, device, inode)
        attached[database] = token
        return token

    bridge = SimpleNamespace(
        BridgeError=_BridgeError,
        initialize_backend=Mock(side_effect=AssertionError("已有 backend 不得重新初始化")),
        attach_identity=Mock(side_effect=attach),
        original=tokens[0],
        fresh=tokens[1],
        tokens=tokens,
    )
    monkeypatch.setattr(native, "_state", "ready")
    monkeypatch.setattr(native, "_backend", bridge)
    return bridge


def _names(case):
    return [event[0] for event in case.events.mock_calls]


def _assert_closed(database):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")


def _assert_cleanup(case, backend, *, borrowed_helper=False):
    assert case.audit._db is case.original
    assert case.original.in_transaction is False
    if not borrowed_helper:
        backend.original.attach.assert_not_called()
    case.events.original.sql.assert_not_called()
    for token in backend.tokens:
        assert token.release.call_count == token.attach.call_count
    for observer in case.views:
        _assert_closed(observer)
    backend.initialize_backend.assert_not_called()
    native.import_module.assert_not_called()


def test_fresh_connection_uses_caller_pin_and_brackets_owner_read(
    review_case, backend, monkeypatch
):
    case = review_case
    owner = Mock(wraps=case.audit._read_runtime_owner)
    monkeypatch.setattr(case.audit, "_read_runtime_owner", owner)

    assert case.read() is None

    (observer,) = case.views
    assert observer is not case.original
    backend.attach_identity.assert_called_once_with(observer, *case.pin)
    owner.assert_called_once_with(database=observer)
    assert _names(case) == [
        "fresh.open",
        "fresh.attach",
        "fresh.check",
        "fresh.sql",
        "fresh.check",
        "fresh.release",
    ]
    case.events.original.sql.assert_not_called()
    statement = case.events.fresh.sql.call_args.args[0]
    assert statement.startswith("SELECT ") and "action_audit_metadata" in statement
    _assert_cleanup(case, backend)


def test_three_reads_attach_three_distinct_fresh_connections_not_original_audit(
    review_case, backend, monkeypatch
):
    case = review_case
    owner = Mock(wraps=case.audit._read_runtime_owner)
    monkeypatch.setattr(case.audit, "_read_runtime_owner", owner)
    for _ in range(3):
        assert case.read() is None
    assert len(case.views) == len(set(case.views)) == 3
    assert all(observer is not case.original for observer in case.views)
    assert backend.attach_identity.call_args_list == [
        call(observer, *case.pin) for observer in case.views
    ]
    assert owner.call_args_list == [call(database=observer) for observer in case.views]
    assert case.events.fresh.sql.call_count == 3
    used_tokens = [token for token in backend.tokens if token.attach.called]
    assert len(used_tokens) == len({id(token) for token in used_tokens}) == 3
    for token in used_tokens:
        assert token.check.call_count == 2
        token.release.assert_called_once()
    _assert_cleanup(case, backend)


def test_fresh_observer_is_readonly_and_cannot_write_owner_metadata(
    review_case, backend, monkeypatch
):
    case = review_case
    read_owner = case.audit._read_runtime_owner

    def inspect_readonly(*, database):
        assert database.execute("PRAGMA query_only").fetchone() == (1,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            database.execute("UPDATE action_audit_metadata SET value=value")
        return read_owner(database=database)

    changes = case.original.total_changes
    monkeypatch.setattr(case.audit, "_read_runtime_owner", inspect_readonly)
    case.read()
    assert case.original.total_changes == changes
    _assert_cleanup(case, backend)


@pytest.mark.parametrize("check_number", [1, 2])
def test_fresh_token_rejection_stops_at_its_query_boundary(review_case, backend, check_number):
    case = review_case
    token = backend.fresh
    token.check.side_effect = [True] * (check_number - 1) + [False]

    with pytest.raises(KernelError) as caught:
        case.read()

    assert caught.value.code == _REVIEW_INVALID
    assert token.check.call_count == check_number
    assert case.events.fresh.sql.call_count == int(check_number == 2)
    case.events.fresh.open.assert_called_once()
    names = _names(case)
    if check_number == 2:
        assert names.index("fresh.sql") < max(
            index for index, name in enumerate(names) if name == "fresh.check"
        )
    case.events.original.sql.assert_not_called()
    _assert_cleanup(case, backend)


@pytest.mark.parametrize("error_type", [_BridgeError, sqlite3.OperationalError])
def test_fresh_native_check_exception_is_review_invalid(review_case, backend, error_type):
    backend.fresh.check.side_effect = error_type("private-native-detail")
    with pytest.raises(KernelError) as caught:
        review_case.read()
    assert caught.value.code == _REVIEW_INVALID
    assert "private-native-detail" not in str(caught.value)
    review_case.events.fresh.sql.assert_not_called()
    _assert_cleanup(review_case, backend)


@pytest.mark.parametrize("stage", ["backend", "attach"])
def test_source_setup_failure_closes_fresh_without_owner_query(
    review_case, backend, monkeypatch, stage
):
    if stage == "backend":
        monkeypatch.setattr(native, "_state", "failed")
    else:
        backend.attach_identity.side_effect = _BridgeError("attach refused")
    with pytest.raises(KernelError) as caught:
        review_case.read()
    assert caught.value.code == _REVIEW_INVALID
    review_case.events.fresh.open.assert_called_once()
    review_case.events.fresh.sql.assert_not_called()
    _assert_cleanup(review_case, backend)


@pytest.mark.parametrize(
    "stage,failure",
    [
        pytest.param(
            "owner", KernelError(_PREPARED_INVALID, "owner same code"), id="owner-same-code"
        ),
        pytest.param("owner", KernelError("action_runtime_fence_lost", "owner"), id="owner-fence"),
        pytest.param("owner", asyncio.CancelledError("cancel"), id="owner-task-cancel"),
        pytest.param("check", TurnCancelled("cancel"), id="check-turn-cancel"),
        pytest.param("attach", asyncio.CancelledError("cancel"), id="attach-task-cancel"),
        pytest.param("check", KernelError("git_process_timeout", "deadline"), id="check-deadline"),
        pytest.param("owner", KernelError("git_process_timeout", "deadline"), id="owner-deadline"),
        pytest.param("check", TimeoutError("deadline"), id="check-python-timeout"),
        pytest.param("owner", TimeoutError("deadline"), id="owner-python-timeout"),
        pytest.param("release", TurnCancelled("cancel"), id="release-cancel"),
        pytest.param("owner", ValueError("owner failure"), id="owner-other"),
    ],
)
def test_non_source_failures_keep_the_original_exception(
    review_case, backend, monkeypatch, stage, failure
):
    if stage == "attach":
        backend.attach_identity.side_effect = failure
    elif stage == "check":
        backend.fresh.check.side_effect = failure
    elif stage == "release":
        backend.fresh.release.side_effect = failure
    else:
        monkeypatch.setattr(review_case.audit, "_read_runtime_owner", Mock(side_effect=failure))

    with pytest.raises(type(failure)) as caught:
        review_case.read()

    assert caught.value is failure
    _assert_cleanup(review_case, backend)


def test_release_failure_is_classified_and_still_closes_fresh(review_case, backend):
    backend.fresh.release.side_effect = _BridgeError("release refused")
    with pytest.raises(KernelError) as caught:
        review_case.read()
    assert caught.value.code == _REVIEW_INVALID
    assert _names(review_case)[-1] == "fresh.release"
    _assert_cleanup(review_case, backend)


@pytest.mark.parametrize(
    "failure",
    [
        KernelError(_PREPARED_INVALID, "owner same code"),
        asyncio.CancelledError("cancel"),
        KernelError("git_process_timeout", "first deadline"),
    ],
    ids=["owner-same-code", "cancel", "timeout"],
)
def test_release_failure_preserves_the_first_owner_exception(
    review_case, backend, monkeypatch, failure
):
    monkeypatch.setattr(review_case.audit, "_read_runtime_owner", Mock(side_effect=failure))
    backend.fresh.release.side_effect = _BridgeError("cleanup must not win")
    with pytest.raises(type(failure)) as caught:
        review_case.read()
    assert caught.value is failure
    assert backend.fresh.check.call_count == 1
    _assert_cleanup(review_case, backend)


def test_real_owner_fence_failure_is_not_reclassified_by_identity_cleanup(review_case, backend):
    with closing(sqlite3.connect(review_case.audit._path)) as external, external:
        external.execute(
            "UPDATE action_audit_metadata SET value='changed' WHERE key='owner_generation'"
        )
    backend.fresh.release.side_effect = _BridgeError("cleanup must not win")
    with pytest.raises(KernelError) as caught:
        review_case.read()
    assert caught.value.code == "action_runtime_fence_lost"
    assert review_case.events.fresh.sql.call_count == 1
    assert backend.fresh.check.call_count == 1
    _assert_cleanup(review_case, backend)


@pytest.mark.parametrize("exit_failure", [None, "body", "release"])
def test_helper_revokes_observer_before_release_without_closing_borrowed_connection(
    review_case, backend, exit_failure
):
    case, saved = review_case, None
    failure = ValueError("body") if exit_failure == "body" else _BridgeError("release")

    def release():
        assert case.original.in_transaction is False
        with pytest.raises(KernelError) as expired:
            saved()
        assert expired.value.code == _REVIEW_INVALID
        if exit_failure == "release":
            raise failure

    backend.original.release.side_effect = release

    def observe():
        nonlocal saved
        with hosts._observe_review_connection(case.original, case.pin) as saved:
            assert backend.original.check.call_count == 1
            assert saved() is None
            if exit_failure == "body":
                raise failure

    if exit_failure is None:
        observe()
    else:
        with pytest.raises(ValueError if exit_failure == "body" else KernelError) as caught:
            observe()
        if exit_failure == "body":
            assert caught.value is failure
        else:
            assert caught.value.code == _REVIEW_INVALID
    with pytest.raises(KernelError) as expired:
        saved()
    assert expired.value.code == _REVIEW_INVALID
    assert backend.original.check.call_count == 2
    case.events.original.sql.assert_not_called()
    _assert_cleanup(case, backend, borrowed_helper=True)


@pytest.mark.parametrize(
    "check_number,result",
    [(1, False), (2, False), (1, _BridgeError("check")), (1, sqlite3.OperationalError("check"))],
    ids=["entry-false", "recheck-false", "bridge-error", "sqlite-error"],
)
def test_helper_checks_borrowed_connection_independently_of_owner_wiring(
    review_case, backend, check_number, result
):
    backend.original.check.side_effect = [True] * (check_number - 1) + [result]
    with pytest.raises(KernelError) as caught:
        with hosts._observe_review_connection(review_case.original, review_case.pin) as check:
            check()
    assert caught.value.code == _REVIEW_INVALID
    assert backend.original.check.call_count == check_number
    assert review_case.views == []
    _assert_cleanup(review_case, backend, borrowed_helper=True)


def test_helper_does_not_allow_reattach_after_release(review_case, backend):
    case = review_case
    with hosts._observe_review_connection(case.original, case.pin) as check:
        check()
    with pytest.raises(KernelError) as caught:
        with hosts._observe_review_connection(case.original, case.pin):
            pytest.fail("fake 也必须保留同一连接的单次 attach 约束")
    assert caught.value.code == _REVIEW_INVALID
    assert backend.attach_identity.call_count == 2
    backend.original.attach.assert_called_once_with(case.original, *case.pin)
    _assert_cleanup(case, backend, borrowed_helper=True)


def test_outer_handled_exception_does_not_hide_helper_release_failure(review_case, backend):
    backend.original.release.side_effect = _BridgeError("release must surface")
    try:
        raise RuntimeError("already handled outside helper")
    except RuntimeError:
        with pytest.raises(KernelError) as caught:
            with hosts._observe_review_connection(review_case.original, review_case.pin) as check:
                assert check() is None
        assert caught.value.code == _REVIEW_INVALID
    _assert_cleanup(review_case, backend, borrowed_helper=True)


@pytest.mark.parametrize("returned", ["original", "rebound-current", "frozen-original", "subclass"])
def test_fresh_factory_rejects_borrowed_connections_and_subclasses(
    review_case, backend, monkeypatch, returned
):
    case = review_case

    class ObserverSubclass(sqlite3.Connection):
        def close(self):
            pytest.fail("拒绝子类不能调用被覆盖的 close")

    replacement = sqlite3.connect(":memory:")
    subclass = sqlite3.connect(":memory:", factory=ObserverSubclass)
    try:
        if returned in {"rebound-current", "frozen-original"}:
            case.audit._db = replacement
        observer = {
            "original": case.original,
            "rebound-current": replacement,
            "frozen-original": case.original,
            "subclass": subclass,
        }[returned]
        monkeypatch.setattr(hosts, "readonly_database", lambda _path: observer)
        with pytest.raises(KernelError) as caught:
            case.read()
        assert caught.value.code == _REVIEW_INVALID
        assert replacement.in_transaction is False
        if returned == "subclass":
            _assert_closed(subclass)
        backend.attach_identity.assert_not_called()
        assert native._connections_started is False
        case.events.fresh.sql.assert_not_called()
    finally:
        case.audit._db = case.original
        replacement.close()
        sqlite3.Connection.close(subclass)
    _assert_cleanup(case, backend)


def test_initial_path_mismatch_precedes_any_native_or_fresh_work(review_case, backend):
    case = review_case
    with pytest.raises(KernelError) as caught:
        hosts._read_fresh_owner(
            case.audit, case.audit._path, (case.pin[0], case.pin[1] + 1), original=case.original
        )
    assert caught.value.code == _REVIEW_INVALID
    assert native._connections_started is False
    assert _names(case) == []
    backend.attach_identity.assert_not_called()
    _assert_cleanup(case, backend)


def test_legacy_mode_keeps_one_fresh_owner_query_without_native_loading(review_case):
    case = review_case
    case.read()
    assert _names(case) == ["fresh.open", "fresh.sql"]
    assert case.events.fresh.sql.call_args.args[0].startswith("SELECT ")
    _assert_closed(case.views[0])
    case.events.reset_mock()

    with hosts._observe_review_connection(case.original, case.pin) as check:
        assert check() is None
    with pytest.raises(KernelError) as caught:
        check()
    assert caught.value.code == _REVIEW_INVALID
    assert _names(case) == []
    assert case.original.execute("SELECT 1").fetchone() == (1,)
    assert native._state == "not_started" and native._backend is None
    native.import_module.assert_not_called()
