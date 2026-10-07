"""固定期望路径的局部纯值复用；短测不代替真实 Owner/MAC 认证。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_user_authority as candidate
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.publication_seal import EventPublicationAuthority
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import SessionPublicationBinding
from harnessix.tools.git import GitReadRuntime, _git_arguments
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts

NAMES = ("sessions.db", "workspace-transactions", "action-audit.db", "execution-plans.db")


def allocated(kind, **fields):
    value = object.__new__(kind)
    for name, item in fields.items():
        setattr(value, name, item)
    return value


def host(path=Path("/private/state")):
    protection = allocated(SecretPublicationScope, _closed=False)
    events = allocated(
        EventPublicationAuthority, _closed=False, _key_id="key", _protection=protection
    )
    publication = allocated(
        SessionPublicationBinding, _closed=False, _store_id="store", _key_id="key", _events=events
    )
    session = allocated(SQLiteSessionStore, path=path / "sessions.db", _publication=publication)
    transactions = allocated(
        SQLiteWorkspaceTransactionStore, _root=path / "workspace-transactions", _closed=False
    )
    ports = WorkspaceSnapshotPorts(transactions.put_blob, transactions.blob)
    audit = SimpleNamespace(_path=path / "action-audit.db", _closed=False)
    plans = SimpleNamespace(_path=path / "execution-plans.db", _closed=False)
    router = allocated(TrustedActionRouter, _audit=audit, _plans=plans, _snapshot_ports=ports)
    reader = allocated(
        GitReadRuntime,
        _root=Path("/private/work"),
        _executable=Path("/private/git"),
        _state_directory=Path("/private/process"),
        _output_redaction=protection,
        _for_delivery=True,
        _global_arguments=_git_arguments(for_delivery=True),
        _binding_fingerprint="frozen",
    )
    return session, router, transactions, ports, reader


def watch(monkeypatch, kind, events, marker=None):
    divide = kind.__truediv__

    def observed(self, name):
        if str(self) == str(Path("/private/state")) and name in NAMES:
            events.append(name)
            if marker is not None and name == marker[0]:
                raise marker[1]
        return divide(self, name)

    monkeypatch.setattr(kind, "__truediv__", observed)


def test_native_fixed_right_values_construct_only_once(monkeypatch):
    args = host()
    observed = []
    watch(monkeypatch, type(Path()), observed)
    check = candidate.require_git_user_authority(*args)
    check()
    check()
    assert observed == list(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_first_construction_error_identity_and_position(monkeypatch, name):
    args = host()
    seen = []
    marker = OSError("original RHS construction")
    with monkeypatch.context() as patch:
        watch(patch, type(Path()), seen, (name, marker))
        with pytest.raises(OSError) as caught:
            candidate.require_git_user_authority(*args)
        assert caught.value is marker
    assert seen == list(NAMES[: NAMES.index(name) + 1])


@pytest.mark.parametrize("index", range(4))
def test_each_live_address_drift_same_rejection_point(monkeypatch, index):
    args = host()
    check = candidate.require_git_user_authority(*args)
    trace = []

    class LiveAddress:
        def __init__(self, value, ordinal):
            self.value, self.ordinal = value, ordinal

        def __ne__(self, expected):
            trace.append(self.ordinal)
            return self.ordinal == index or self.value != expected

    session, router, transactions, ports, reader = args
    session.path = LiveAddress(session.path, 0)
    transactions._root = LiveAddress(transactions._root, 1)
    router._audit._path = LiveAddress(router._audit._path, 2)
    router._plans._path = LiveAddress(router._plans._path, 3)
    with pytest.raises(KernelError) as caught:
        check()
    assert caught.value.code == "git_user_observation_host_invalid"
    assert trace == list(range(index + 1))


@pytest.mark.parametrize("index", range(4))
def test_live_left_exception_identity_and_short_circuit(index):
    args = host()
    check = candidate.require_git_user_authority(*args)
    trace = []
    marker = OSError("original live address")

    class LiveAddress:
        def __init__(self, value, ordinal):
            self.value, self.ordinal = value, ordinal

        def __ne__(self, expected):
            trace.append(self.ordinal)
            if self.ordinal == index:
                raise marker
            return self.value != expected

    session, router, transactions, ports, reader = args
    session.path = LiveAddress(session.path, 0)
    transactions._root = LiveAddress(transactions._root, 1)
    router._audit._path = LiveAddress(router._audit._path, 2)
    router._plans._path = LiveAddress(router._plans._path, 3)
    with pytest.raises(OSError) as caught:
        check()
    assert caught.value is marker
    assert trace == list(range(index + 1))


def test_earlier_rejection_never_constructs_right_values(monkeypatch):
    args = host()
    args[0]._publication._closed = True
    seen = []
    with monkeypatch.context() as patch:
        watch(patch, type(Path()), seen)
        with pytest.raises(KernelError):
            candidate.require_git_user_authority(*args)
    assert seen == []


def test_native_warm_check_still_reads_all_live_left_values():
    args = host()
    check = candidate.require_git_user_authority(*args)
    trace = []

    class Live:
        def __init__(self, value, ordinal):
            self.value, self.ordinal = value, ordinal

        def __ne__(self, right):
            trace.append(self.ordinal)
            return self.value != right

    args[0].path = Live(args[0].path, 0)
    args[2]._root = Live(args[2]._root, 1)
    args[1]._audit._path = Live(args[1]._audit._path, 2)
    args[1]._plans._path = Live(args[1]._plans._path, 3)
    check()
    check()
    assert trace == [0, 1, 2, 3] * 2


@pytest.mark.parametrize("fail_at", [None, "sessions.db", "action-audit.db"])
def test_subclass_fallback_keeps_each_construction_and_exception(monkeypatch, fail_at):
    seen = []
    marker = OSError("subclass repeated RHS")

    class EffectPath(type(Path())):
        armed = False

        def __truediv__(self, name):
            if self.armed and name in NAMES:
                seen.append(name)
                if name == fail_at:
                    raise marker
            return super().__truediv__(name)

    args = host(EffectPath("/private/state"))
    check = candidate.require_git_user_authority(*args)
    EffectPath.armed = True
    if fail_at:
        with pytest.raises(OSError) as caught:
            check()
        assert caught.value is marker
        assert seen == list(NAMES[: NAMES.index(fail_at) + 1])
    else:
        check()
        check()
        assert seen == list(NAMES) * 2


@pytest.mark.parametrize(
    "state", [Path("relative/state"), Path("/tmp/空 格/路径"), Path("/tmp/a#b%25")]
)
def test_native_absolute_and_relative_lexical_values_remain_original(state):
    args = host(state)
    check = candidate.require_git_user_authority(*args)
    check()
    check()


@pytest.mark.parametrize(
    "kind", ["kernel", "os", "sqlite", "turn-cancel", "timeout", "task-cancel"]
)
@pytest.mark.parametrize("position", ["native-first", "subclass-later", "live-left"])
def test_original_exception_matrix_identity_and_short_circuit(monkeypatch, kind, position):
    import asyncio
    import sqlite3

    from harnessix.agent.cancellation import TurnCancelled

    marker = {
        "kernel": KernelError("original_marker", "原检查点异常"),
        "os": OSError("original_marker"),
        "sqlite": sqlite3.OperationalError("original_marker"),
        "turn-cancel": TurnCancelled("original_marker"),
        "timeout": TimeoutError("original_marker"),
        "task-cancel": asyncio.CancelledError("original_marker"),
    }[kind]
    trace = []
    if position == "native-first":
        args = host()
        with monkeypatch.context() as patch:
            watch(patch, type(Path()), trace, ("action-audit.db", marker))
            with pytest.raises(type(marker)) as caught:
                candidate.require_git_user_authority(*args)
        assert trace == list(NAMES[:3])
    elif position == "subclass-later":

        class EffectPath(type(Path())):
            armed = False

            def __truediv__(self, name):
                if self.armed and name in NAMES:
                    trace.append(name)
                    if name == "action-audit.db":
                        raise marker
                return super().__truediv__(name)

        args = host(EffectPath("/private/state"))
        check = candidate.require_git_user_authority(*args)
        EffectPath.armed = True
        with pytest.raises(type(marker)) as caught:
            check()
        assert trace == list(NAMES[:3])
    else:
        args = host()
        check = candidate.require_git_user_authority(*args)

        class Live:
            def __ne__(self, right):
                trace.append("sessions.db")
                raise marker

        args[0].path = Live()
        with pytest.raises(type(marker)) as caught:
            check()
        assert trace == ["sessions.db"]
    assert caught.value is marker
