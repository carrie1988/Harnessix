"""Git 分层控制直接接线负控：真实 CAS、纯编解码及原异常边界。"""

from __future__ import annotations

import asyncio
import hashlib
from functools import partial

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_inventory_contracts import snapshot_git_inventory_scope
from harnessix.delivery.git_inventory_materials import verify_git_inventory_scope_materials
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_decision_link_wire as decision_wire
from harnessix.product_config import git_delivery_observed_wire as observed_wire
from harnessix.product_config import git_delivery_plan_wire as wire
from harnessix.product_config import git_prepared_link_wire as prepared_wire
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_delivery_plan_snapshot import _snapshot
from tests.product_config.git_delivery_plan_support import canonical_bytes, make_case
from tests.product_config.test_git_decision_link_contracts import declaration
from tests.product_config.test_git_prepared_link_contracts import link_for
from tests.support.git_delivery_observed_core import json_facts, make_observed_case


class Trace:
    """只记录被测入口实际调用；可在指定局部检查点注入首失败或认证漂移。"""

    def __init__(self, stop=0, error=None, drift=False):
        self.events, self.locals = [], 0
        self.stop, self.error, self.drift, self.changed = stop, error, drift, False
        self.control = GitAuthenticationControl(self.local, self.full)

    def local(self):
        self.events.append("local")
        self.locals += 1
        if self.locals == self.stop:
            if self.drift:
                self.changed = True
            else:
                raise self.error

    def full(self):
        self.events.append("full")
        if self.changed:
            raise self.error


@pytest.fixture
def cas(tmp_path):
    with SQLiteWorkspaceTransactionStore(tmp_path / "state") as store:
        yield GitMaterialCAS(store)


@pytest.fixture
def case(cas, tmp_path, request):
    return make_case(cas, tmp_path, action=getattr(request, "param", "checkpoint"), depth=1)


@pytest.fixture
def codecs(case, tmp_path):
    observed, _ = make_observed_case(
        case.cas, tmp_path / "observed", depth=1, action=case.core.object_scope.action_kind
    )
    samples = [
        ("delivery_core", wire, case.core),
        ("delivery_plan", wire, case.plan),
        ("delivery_core_v2", observed_wire, observed.core),
        ("delivery_plan_v2", observed_wire, observed.plan),
        ("prepared_link", prepared_wire, link_for(observed)),
        *(
            ("decision_link", decision_wire, declaration(observed, kind))
            for kind in ("approved", "denied", "cancelled")
        ),
    ]
    return [
        (
            value,
            getattr(module, f"encode_product_git_{name}"),
            getattr(module, f"decode_product_git_{name}"),
        )
        for name, module, value in samples
    ]


@pytest.fixture(params=("snapshot", "load", "encode", "decode", "inventory"))
def port(case, request):
    core = case.core
    body = canonical_bytes(core.model_dump(mode="json", exclude={"fingerprint"}))
    owner = ProductGitDeliveryCoreStore(case.cas.store)
    owner.persist(core, checkpoint=lambda: None)
    operations = {
        "snapshot": (partial(_snapshot, core, type(core)), core),
        "load": (partial(owner.load, core.fingerprint), core),
        "encode": (partial(wire.encode_product_git_delivery_core, core), body),
        "decode": (partial(wire.decode_product_git_delivery_core, body), core),
        "inventory": (partial(snapshot_git_inventory_scope, core.object_scope), core.object_scope),
    }
    return operations[request.param]


def test_inventory_declaration_is_pure_but_actual_cas_reads_are_not(case, monkeypatch):
    trace, reads = Trace(), []
    scope = case.core.object_scope
    assert snapshot_git_inventory_scope(scope, checkpoint=trace.control) == scope
    assert trace.events.count("full") == 2 and trace.locals > 2
    original = case.cas.read

    def read(cas, reference):
        # 真实 CAS 入口必须已经结束声明纯段；不以局部频检替代材料 I/O 认证。
        assert cas is case.cas
        assert trace.control._segment is None
        start = len(trace.events)
        trace.control()
        result = original(reference)
        assert set(trace.events[start:]) == {"full"}
        reads.append(reference)
        return result

    monkeypatch.setattr(GitMaterialCAS, "read", read)
    assert verify_git_inventory_scope_materials(case.cas, scope, checkpoint=trace.control) == scope
    assert reads and trace.events[-1] == "full"


@pytest.mark.parametrize(
    "index",
    range(8),
    ids="core1 plan1 core2 plan2 prepared approved denied cancelled".split(),
)
def test_actual_snapshot_and_wire_use_local_frequency_and_full_boundaries(codecs, index):
    value, encode, decode = codecs[index]
    payload = json_facts(value)
    if index in (0, 2):
        payload.pop("fingerprint")
    body = canonical_bytes(payload)
    for operation, expected, boundaries in (
        (partial(_snapshot, value, type(value)), value, 2),
        (partial(encode, value), body, 5 if index >= 5 else 4),
        (partial(decode, body), value, 2),
    ):
        trace = Trace()
        result = operation(checkpoint=trace.control)
        assert result == expected and result is not expected
        assert trace.events[0] == trace.events[-1] == "full"
        assert trace.events.count("full") == boundaries
        assert trace.locals > boundaries
    assert encode(decode(body, checkpoint=Trace().control), checkpoint=Trace().control) == body


@pytest.mark.parametrize("index", range(8))
@pytest.mark.parametrize("surface", ["task", "thread"])
async def test_foreign_owner_codec_falls_back_to_full_without_recursive_adaptation(
    codecs, index, surface
):
    value, encode, decode = codecs[index]
    body = encode(value, checkpoint=lambda: None)
    trace = Trace()

    def foreign():
        assert _snapshot(value, type(value), trace.control) == value
        assert encode(value, checkpoint=trace.control) == body
        assert decode(body, checkpoint=trace.control) == value

    if surface == "task":

        async def child():
            foreign()

        await asyncio.create_task(child())
    else:
        await asyncio.to_thread(foreign)
    assert trace.locals == 0 and set(trace.events) == {"full"}


@pytest.mark.parametrize("case", ["checkpoint", "commit"], indirect=True)
@pytest.mark.parametrize("version", [1, 2])
def test_actual_core_store_io_is_full_only_and_bytes_roundtrip(case, codecs, monkeypatch, version):
    core = codecs[0 if version == 1 else 2][0]
    owner, trace, calls = ProductGitDeliveryCoreStore(case.cas.store), Trace(), []
    original_blob = case.cas.store.blob
    body = canonical_bytes({k: v for k, v in json_facts(core).items() if k != "fingerprint"})

    def observe(method):
        def io(*args, checkpoint=None):
            start = len(trace.events)
            calls.append(method.__name__)
            checkpoint()
            result = method(*args, checkpoint=checkpoint)
            assert set(trace.events[start:]) == {"full"}
            return result

        return io

    for name in ("put_blob", "blob"):
        monkeypatch.setattr(case.cas.store, name, observe(getattr(case.cas.store, name)))
    persist = owner.persist if version == 1 else owner.persist_v2
    load = owner.load if version == 1 else owner.load_v2
    saved = persist(core, checkpoint=trace.control)
    assert saved == core and saved is not core
    # 原 put_blob 内部两次耐久回读，CoreStore 再完整回读。
    assert calls == ["put_blob", "blob", "blob", "blob"]
    assert original_blob(core.fingerprint) == body
    assert hashlib.sha256(body).hexdigest() == core.fingerprint
    trace.events.clear()
    trace.locals = 0
    calls.clear()
    restored = load(core.fingerprint, checkpoint=trace.control)
    assert restored == saved and restored is not saved
    assert calls == ["blob"] and trace.locals > 2
    first_local = trace.events.index("local")
    assert set(trace.events[:first_local]) == {"full"}
    assert set(trace.events[first_local:-1]) == {"local"}
    assert trace.events[-1] == "full"


@pytest.mark.parametrize("port", ["snapshot", "load"], indirect=True)
@pytest.mark.parametrize("failure", [TurnCancelled, TimeoutError, ValueError])
def test_every_local_checkpoint_preserves_original_failure_identity(port, failure):
    operation, expected = port
    baseline = Trace()
    assert operation(checkpoint=baseline.control) == expected
    assert baseline.locals > 2
    for k in range(1, baseline.locals + 1):
        error = failure("原控制首失败")
        trace = Trace(k, error)
        result = None
        with pytest.raises(failure) as caught:
            result = operation(checkpoint=trace.control)
        assert caught.value is error and caught.value.__cause__ is None, k
        assert result is None and trace.locals == k, k
        assert trace.events[-1] == "local", k


def test_exit_authentication_drift_rejects_without_returning_result(port):
    operation, expected = port
    baseline = Trace()
    assert operation(checkpoint=baseline.control) == expected
    error = KernelError("git_authentication_drift", "来源认证已改变")
    trace = Trace(baseline.locals, error, drift=True)
    result = None
    with pytest.raises(KernelError) as caught:
        result = operation(checkpoint=trace.control)
    assert caught.value is error and result is None
    assert trace.locals == baseline.locals and trace.events[-2:] == ["local", "full"]


@pytest.mark.parametrize("index", range(8))
@pytest.mark.parametrize("bad", [b"", b"{", b'{"x":1,"x":2}', b"\xff", b"NaN"])
def test_pure_codec_illegal_bytes_keep_original_domain_classification(codecs, index, bad):
    _, _, decode = codecs[index]
    trace = Trace()
    with pytest.raises(KernelError) as caught:
        decode(bad, checkpoint=trace.control)
    assert caught.value.code == "git_delivery_plan_invalid"
    assert caught.value.__cause__ is None and trace.events[0] == "full"


@pytest.mark.parametrize("port", ["snapshot", "encode"], indirect=True)
def test_invalid_actual_model_keeps_original_domain_classification(port):
    operation, _ = port
    with pytest.raises(KernelError) as caught:
        operation.func(None, *operation.args[1:], checkpoint=Trace().control)
    assert caught.value.code == "git_delivery_plan_invalid" and caught.value.__cause__ is None


@pytest.mark.parametrize("failure", [OSError, TurnCancelled, TimeoutError, ValueError])
def test_store_read_failure_is_not_misclassified_as_control_failure(case, monkeypatch, failure):
    error, trace = failure("底层读取失败"), Trace()

    def fail(*args, checkpoint=None):
        checkpoint()
        raise error

    monkeypatch.setattr(case.cas.store, "blob", fail)
    with pytest.raises(KernelError) as caught:
        ProductGitDeliveryCoreStore(case.cas.store).load(
            case.core.fingerprint, checkpoint=trace.control
        )
    assert caught.value is not error and caught.value.code == "git_delivery_core_read_failed"
    assert caught.value.__cause__ is None and trace.locals == 0


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_unknown_checkpoint_kinds_keep_full_trajectory_without_layering(port, kind):
    operation, expected = port
    baseline, trace = Trace(), Trace()
    assert operation(checkpoint=baseline.full) == expected

    class Proxy:
        def __call__(self):
            trace.control()

        def __getattr__(self, name):
            return getattr(trace.control, name)

    class Subclass(GitAuthenticationControl):
        pass

    checkpoint = {
        "function": lambda: trace.control(),
        "proxy": Proxy(),
        "subclass": Subclass(trace.local, trace.full),
    }[kind]
    assert operation(checkpoint=checkpoint) == expected
    assert trace.locals == 0 and trace.events == baseline.events
