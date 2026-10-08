"""真实临时 SQLite CAS 的分层持久化回归；替身控制不证明 SDK 来源认证。"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest
from pydantic import AwareDatetime, TypeAdapter
from pydantic_core import TzInfo

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_delivery_core_store as candidate
from harnessix.product_config import git_delivery_plan_snapshot as snapshot_module
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.product_config import git_delivery_plan_support as plan_support
from tests.product_config.git_delivery_plan_support import canonical_bytes, make_case
from tests.support.git_delivery_observed_core import core_bytes, make_observed_case

ERROR_KINDS = ("os", "value", "kernel", "timeout", "task-cancel", "upstream")


def _error(kind):
    return {
        "os": OSError("original failure"),
        "value": ValueError("original failure"),
        "kernel": KernelError("time_budget_exceeded", "original failure"),
        "timeout": TimeoutError("original failure"),
        "task-cancel": asyncio.CancelledError("original failure"),
        "upstream": UpstreamCheckpointError(ValueError("original failure")),
    }[kind]


@pytest.fixture(params=[1, 2], ids=["core1", "core2"])
def case(request, tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        cas = GitMaterialCAS(store)
        value = (
            make_case(cas, tmp_path) if request.param == 1 else make_observed_case(cas, tmp_path)[0]
        )
        suffix = "" if request.param == 1 else "_v2"
        owner = candidate.ProductGitDeliveryCoreStore(store)
        expected = (
            canonical_bytes(
                value.core.model_dump(mode="json", exclude={"fingerprint"}, warnings="error")
            )
            if request.param == 1
            else core_bytes(value.core)
        )
        yield value.core, store, getattr(owner, "persist" + suffix), suffix, expected


class _Probe:
    def __init__(self):
        self.phase = "boundary"
        self.scope = None
        self.events = []
        self.scopes = []
        self.retired = []
        self.failure_at = None
        self.failure = None
        self.control = GitAuthenticationControl(self.local, self.authenticate)

    def record(self, channel):
        event = (self.phase, channel)
        self.events.append(event)
        if event == self.failure_at:
            raise self.failure

    def local(self):
        self.record("local")

    def authenticate(self):
        self.record("auth")


def _observe(monkeypatch, store, suffix, probe, *, scopes=True):
    def watch(target, name, phase, *, io=False):
        original = getattr(target, name)

        def observed(*args, **kwargs):
            previous = probe.phase
            probe.phase = phase
            probe.events.append((phase, "enter"))
            try:
                if io:
                    assert probe.scope is None
                    assert probe.control._segment is None
                return original(*args, **kwargs)
            finally:
                probe.events.append((phase, "leave"))
                probe.phase = previous

        monkeypatch.setattr(target, name, observed)

    for phase in ("snapshot", "encode", "decode"):
        watch(candidate, phase + "_product_git_delivery_core" + suffix, phase)
    for name in ("put_blob", "blob", "_put_blob", "_read_blob", "_check", "_require_writable"):
        watch(store, name, name, io=True)
    watch(candidate, "_body", "body", io=True)
    if not scopes:
        return
    original_pure = candidate.pure_git_authentication

    @contextmanager
    def pure(checkpoint):
        phase = "build" if not probe.scopes else "decode"
        probe.scopes.append(phase)
        probe.phase = phase + "-enter"
        with original_pure(checkpoint) as check:
            probe.retired.append(check)
            probe.scope = phase
            probe.phase = "boundary"
            try:
                yield check
                probe.phase = phase + "-exit"
            finally:
                probe.scope = None
        probe.phase = "boundary"

    monkeypatch.setattr(candidate, "pure_git_authentication", pure)


def test_persist_layers_only_pure_algorithms_and_preserves_source_and_canonical_bytes(
    case, monkeypatch
):
    core, store, persist, suffix, expected = case
    probe = _Probe()
    _observe(monkeypatch, store, suffix, probe)
    original_checkpoint = store._checkpoint
    before_db = tuple(store._db.iterdump()), store._db.total_changes
    saved = persist(core, checkpoint=probe.control)
    assert probe.scopes == ["build", "decode"]
    for phase in ("snapshot", "encode", "decode"):
        assert (phase, "local") in probe.events
        assert (phase, "auth") not in probe.events
    for phase in ("build-enter", "build-exit", "decode-enter", "decode-exit"):
        assert probe.events.count((phase, "auth")) == 1
    assert ("put_blob", "auth") in probe.events
    assert ("blob", "auth") in probe.events
    assert saved == core and saved is not core
    assert saved.call is not core.call and saved.call.arguments is not core.call.arguments
    assert store.blob(saved.fingerprint) == expected
    assert hashlib.sha256(expected).hexdigest() == saved.fingerprint
    assert (
        canonical_bytes(core.model_dump(mode="json", exclude={"fingerprint"}, warnings="error"))
        == expected
    )
    assert (tuple(store._db.iterdump()), store._db.total_changes) == before_db
    assert store._checkpoint is original_checkpoint
    for check in probe.retired:
        before = len(probe.events)
        check()
        assert probe.events[before:] == [("boundary", "auth")]
    # 再次持久化仍走真实原 I/O 和两段边界，不复用正文或认证结果。
    before_reads = probe.events.count(("_read_blob", "enter"))
    probe.scopes.clear()
    assert persist(core, checkpoint=probe.control) == saved
    assert probe.scopes == ["build", "decode"]
    assert probe.events.count(("_read_blob", "enter")) > before_reads


@pytest.mark.parametrize("error_kind", ERROR_KINDS)
@pytest.mark.parametrize(
    "phase,channel",
    [
        ("build-enter", "auth"),
        ("build-exit", "auth"),
        ("decode-enter", "auth"),
        ("decode-exit", "auth"),
        ("build-exit", "local"),
        ("decode-exit", "local"),
        ("snapshot", "local"),
        ("encode", "local"),
        ("decode", "local"),
        ("put_blob", "auth"),
        ("blob", "auth"),
    ],
)
def test_boundary_leaf_and_cas_callback_failures_keep_first_exception_identity(
    case, monkeypatch, error_kind, phase, channel
):
    core, store, persist, suffix, expected = case
    probe = _Probe()
    _observe(monkeypatch, store, suffix, probe)
    marker = _error(error_kind)
    probe.failure_at, probe.failure = (phase, channel), marker
    delivered = None
    with pytest.raises(type(marker)) as caught:
        delivered = persist(core, checkpoint=probe.control)
    assert caught.value is marker and delivered is None
    callbacks = [event for event in probe.events if event[1] in {"local", "auth"}]
    assert callbacks[-1] == (phase, channel)
    assert probe.control._segment is None
    if phase.startswith("decode"):
        assert store.blob(core.fingerprint) == expected
    if probe.retired:
        probe.failure_at = None
        probe.phase = "retired"
        probe.retired[-1]()
        assert probe.events[-1] == ("retired", "auth")


@pytest.mark.parametrize("error_kind", ERROR_KINDS)
def test_decoder_cannot_shadow_a_recorded_callback_failure(case, monkeypatch, error_kind):
    core, store, persist, suffix, _expected = case
    probe = _Probe()
    _observe(monkeypatch, store, suffix, probe)
    marker = _error(error_kind)
    probe.failure_at, probe.failure = ("decode", "local"), marker

    def decode(_body, *, checkpoint):
        probe.phase = "decode"
        try:
            checkpoint()
        except Exception:
            raise ValueError("secondary parser failure") from None
        pytest.fail("检查点必须失败")

    monkeypatch.setattr(candidate, "decode_product_git_delivery_core" + suffix, decode)
    with pytest.raises(type(marker)) as caught:
        persist(core, checkpoint=probe.control)
    assert caught.value is marker
    assert ("decode-exit", "auth") not in probe.events
    assert probe.control._segment is None


@pytest.mark.parametrize("error_kind", ["os", "value", "kernel", "timeout"])
@pytest.mark.parametrize("phase", ["snapshot", "encode", "decode"])
def test_noncallback_algorithm_failures_keep_original_persist_classification(
    case, monkeypatch, error_kind, phase
):
    core, store, persist, suffix, _expected = case
    probe = _Probe()
    _observe(monkeypatch, store, suffix, probe)
    marker = _error(error_kind)

    def fail(_value, *, checkpoint):
        checkpoint()
        raise marker

    monkeypatch.setattr(candidate, phase + "_product_git_delivery_core" + suffix, fail)
    with pytest.raises(type(marker) if phase != "decode" else KernelError) as caught:
        persist(core, checkpoint=probe.control)
    if phase == "decode":
        assert caught.value is not marker
        assert caught.value.code == "git_delivery_core_write_failed"
        assert caught.value.__cause__ is None
        assert "original failure" not in str(caught.value)
    else:
        assert caught.value is marker
        assert ("put_blob", "enter") not in probe.events
    assert ("decode-exit" if phase == "decode" else "build-exit", "auth") not in probe.events
    assert probe.control._segment is None


def test_invalid_original_snapshot_still_rejects_before_cas(case, monkeypatch):
    core, store, persist, suffix, _expected = case
    probe = _Probe()
    _observe(monkeypatch, store, suffix, probe)
    core.__dict__["fingerprint"] = "0" * 64
    with pytest.raises(KernelError) as caught:
        persist(core, checkpoint=probe.control)
    assert caught.value.code == "git_delivery_plan_invalid"
    assert ("put_blob", "enter") not in probe.events
    assert probe.control._segment is None


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_plain_callback_proxy_and_subclass_keep_exact_unlayered_trace(case, monkeypatch, kind):
    core, store, persist, suffix, _expected = case
    persist(core, checkpoint=lambda: None)
    probe = _Probe()
    _observe(monkeypatch, store, suffix, probe, scopes=False)
    persist(core, checkpoint=probe.authenticate)
    expected_trace = probe.events[:]
    probe.events.clear()

    def forbidden_pure():
        pytest.fail("普通函数、代理、子类不得自动进入分层纯段")

    def function():
        probe.authenticate()

    function.pure = forbidden_pure

    class Proxy:
        pure = staticmethod(forbidden_pure)

        def __call__(self):
            probe.authenticate()

    class Subclass(GitAuthenticationControl):
        pure = staticmethod(forbidden_pure)

    callback = {
        "function": function,
        "proxy": Proxy(),
        "subclass": Subclass(probe.local, probe.authenticate),
    }[kind]
    persist(core, checkpoint=callback)
    assert probe.events == expected_trace
    assert not any(channel == "local" for _phase, channel in probe.events)
    marker = ValueError("plain callback failure")
    probe.failure_at, probe.failure = ("decode", "auth"), marker
    with pytest.raises(ValueError) as caught:
        persist(core, checkpoint=callback)
    assert caught.value is marker


@pytest.mark.parametrize("error_kind", ["os", "value", "kernel", "timeout"])
@pytest.mark.parametrize("phase", ["write", "readback", "body", "load"])
def test_cas_and_body_noncallback_failures_keep_write_or_read_classification(
    case, monkeypatch, error_kind, phase
):
    core, store, persist, suffix, expected = case
    if phase == "load":
        persist(core, checkpoint=lambda: None)
    probe = _Probe()
    _observe(monkeypatch, store, suffix, probe)
    marker = _error(error_kind)
    original_put = store.put_blob
    armed = phase in {"write", "load"}

    def fail(*args, **kwargs):
        assert probe.scope is None and probe.control._segment is None
        raise marker

    if phase == "write":
        monkeypatch.setattr(store, "_put_blob", fail)
    elif phase == "body":
        monkeypatch.setattr(candidate, "_body", fail)
    else:
        original_read = store._read_blob

        def read(digest):
            if armed:
                fail()
            return original_read(digest)

        def write(*args, **kwargs):
            nonlocal armed
            original_put(*args, **kwargs)
            armed = True

        monkeypatch.setattr(store, "_read_blob", read)
        monkeypatch.setattr(store, "put_blob", write)
    if phase == "load":
        operation = getattr(candidate.ProductGitDeliveryCoreStore(store), "load" + suffix)
        value = core.fingerprint
    else:
        operation, value = persist, core
    with pytest.raises(KernelError) as caught:
        operation(value, checkpoint=probe.control)
    assert caught.value is not marker and caught.value.__cause__ is None
    assert caught.value.code == (
        "git_delivery_core_read_failed" if phase == "load" else "git_delivery_core_write_failed"
    )
    assert "original failure" not in str(caught.value)
    if phase != "write":
        assert (store._blobs / core.fingerprint).read_bytes() == expected


def test_decoder_upstream_marker_keeps_original_single_unwrap(case, monkeypatch):
    core, _store, persist, suffix, _expected = case
    marker = UpstreamCheckpointError(ValueError("original parser marker"))

    def decode(_body, *, checkpoint):
        raise marker

    monkeypatch.setattr(candidate, "decode_product_git_delivery_core" + suffix, decode)
    with pytest.raises(ValueError) as caught:
        persist(core, checkpoint=GitAuthenticationControl(lambda: None, lambda: None))
    assert caught.value is marker.error


def _commit_core(case, tmp_path):
    _core, store, _persist, suffix, _expected = case
    cas = GitMaterialCAS(store)
    root = tmp_path / "commit"
    return (
        make_case(cas, root, action="commit").core
        if not suffix
        else make_observed_case(cas, root, action="commit")[0].core
    )


def _executable_timezone(kind, callbacks, marker):
    class CustomTimezone(tzinfo):
        def utcoffset(self, value):
            callbacks.append("utcoffset")
            raise marker

        def dst(self, value):
            callbacks.append("dst")
            raise marker

        def tzname(self, value):
            callbacks.append("tzname")
            raise marker

    class ZoneSubclass(ZoneInfo):
        utcoffset = CustomTimezone.utcoffset
        dst = CustomTimezone.dst
        tzname = CustomTimezone.tzname

    return CustomTimezone() if kind == "custom" else ZoneSubclass("UTC")


@pytest.mark.parametrize("tz_kind", ["custom", "zoneinfo-subclass"])
@pytest.mark.parametrize("field", ["authored_at", "created_at"])
@pytest.mark.parametrize("control_kind", ["plain", "layered"])
def test_injected_executable_timezone_rejected_without_callbacks_or_cas(
    case, tmp_path, monkeypatch, tz_kind, field, control_kind
):
    core = _commit_core(case, tmp_path)
    _original, store, persist, _suffix, _expected = case
    callbacks = []
    marker = KernelError("external_timezone_marker", "时区回调不得执行")
    executable = _executable_timezone(tz_kind, callbacks, marker)
    model = core.commit_spec if field == "authored_at" else core.commit_spec.checkpoint
    # 原合法模型构造完成后注入，不让夹具预先触发任何恶意时区方法。
    model.__dict__[field] = getattr(model, field).replace(tzinfo=executable)
    assert callbacks == []

    def forbidden(*args, **kwargs):
        pytest.fail("非法时区不可进入 CAS")

    monkeypatch.setattr(store, "put_blob", forbidden)
    monkeypatch.setattr(store, "blob", forbidden)
    monkeypatch.setattr(store, "_read_blob", forbidden)
    checkpoint = (
        (lambda: None)
        if control_kind == "plain"
        else GitAuthenticationControl(lambda: None, lambda: None)
    )
    with pytest.raises(KernelError) as caught:
        persist(core, checkpoint=checkpoint)
    assert caught.value.code == "git_delivery_plan_invalid"
    assert caught.value is not marker and caught.value.__cause__ is None
    assert callbacks == []
    assert not (store._blobs / core.fingerprint).exists()


@pytest.mark.parametrize("annotation", [datetime, AwareDatetime])
@pytest.mark.parametrize("tz_kind", ["custom", "zoneinfo-subclass"])
def test_datetime_field_rejects_executable_timezone_by_type_only(annotation, tz_kind):
    callbacks = []
    marker = AssertionError("时区检查不得调用外部 offset/tzname")
    value = datetime(2026, 10, 7, tzinfo=_executable_timezone(tz_kind, callbacks, marker))
    with pytest.raises(KernelError) as caught:
        snapshot_module._field(value, annotation, lambda: None)
    assert caught.value.code == "git_delivery_plan_invalid"
    assert callbacks == []


@pytest.mark.parametrize("annotation", [datetime, AwareDatetime])
def test_naive_datetime_field_is_left_to_original_validator(annotation):
    value = datetime(2026, 10, 7, 8, 9, 10)
    assert snapshot_module._field(value, annotation, lambda: None) is value
    if annotation is AwareDatetime:
        with pytest.raises(ValueError, match="timezone_aware"):
            TypeAdapter(annotation).validate_python(value)


@pytest.mark.parametrize("field", ["authored_at", "created_at"])
def test_injected_naive_commit_datetime_is_not_upgraded(case, tmp_path, monkeypatch, field):
    core = _commit_core(case, tmp_path)
    _original, store, persist, _suffix, _expected = case
    model = core.commit_spec if field == "authored_at" else core.commit_spec.checkpoint
    model.__dict__[field] = getattr(model, field).replace(tzinfo=None)

    def forbidden(*args, **kwargs):
        pytest.fail("原 AwareDatetime 拒绝的 naive 时间不可进入 CAS")

    monkeypatch.setattr(store, "put_blob", forbidden)
    with pytest.raises(KernelError) as caught:
        persist(core, checkpoint=GitAuthenticationControl(lambda: None, lambda: None))
    assert caught.value.code == "git_delivery_plan_invalid"


@pytest.mark.parametrize("tz_kind", ["utc", "offset", "pydantic", "zoneinfo"])
@pytest.mark.parametrize("control_kind", ["plain", "layered"])
def test_supported_exact_timezone_types_preserve_commit_canonical_bytes(
    case, tmp_path, monkeypatch, tz_kind, control_kind
):
    if tz_kind == "pydantic":
        timestamp = TypeAdapter(AwareDatetime).validate_json('"2026-10-07T08:09:10-07:00"')
        assert type(timestamp.tzinfo) is TzInfo
    else:
        zone = {
            "utc": UTC,
            "offset": timezone(timedelta(hours=5, minutes=30)),
            "zoneinfo": ZoneInfo("Asia/Shanghai"),
        }[tz_kind]
        timestamp = datetime(2026, 10, 7, 8, 9, 10, tzinfo=zone)
    monkeypatch.setattr(plan_support, "NOW", timestamp)
    core = _commit_core(case, tmp_path)
    _original, store, persist, suffix, _expected = case
    expected = canonical_bytes(
        core.model_dump(mode="json", exclude={"fingerprint"}, warnings="error")
    )
    # 夹具经 JSON 重建为 Pydantic TZ；恢复同声明的原时区对象，规范输出必须不变。
    core.commit_spec.__dict__["authored_at"] = timestamp
    core.commit_spec.checkpoint.__dict__["created_at"] = timestamp
    assert (
        canonical_bytes(core.model_dump(mode="json", exclude={"fingerprint"}, warnings="error"))
        == expected
    )
    probe = _Probe()
    _observe(monkeypatch, store, suffix, probe, scopes=control_kind == "layered")
    checkpoint = probe.control if control_kind == "layered" else probe.authenticate
    saved = persist(core, checkpoint=checkpoint)
    assert saved == core and saved is not core
    assert store.blob(saved.fingerprint) == expected
    assert hashlib.sha256(expected).hexdigest() == saved.fingerprint == core.fingerprint
