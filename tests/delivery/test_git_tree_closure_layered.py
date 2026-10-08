"""R4 Closure 分层机械回归：真实只读 CAS、冻结 cc85 对照，不加载仓库外来 fixture。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery import git_tree_closure as current
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.git_object_material import GitObjectMaterial, GitObjectRead
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.workspace.native_observation_io import UpstreamCheckpointError

ROOT = Path(__file__).resolve().parents[2]
BEFORE_SHA256 = "ccdc5a9a09d2aab6aad9c11fb8aca3b253511c122bdbca985bb391932bfe7984"


def load_snapshot(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses 需从此处解析独立加载的模块。
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def before(tmp_path_factory):
    path = os.environ.get("HARNESSIX_CLOSURE_BEFORE")
    if path is None:
        path = tmp_path_factory.mktemp("oracle") / "git_tree_closure_before.py"
        source = subprocess.run(
            ["git", "show", "cc85f1f1:src/harnessix/delivery/git_tree_closure.py"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        path.write_bytes(source)
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == BEFORE_SHA256
    return load_snapshot(path, "harnessix.delivery._closure_cc85_oracle")


@pytest.fixture(scope="session")
def subject():
    path = os.environ.get("HARNESSIX_CLOSURE_SUBJECT")
    return load_snapshot(path, "harnessix.delivery._closure_red_subject") if path else current


def limits(module, **changes):
    values = dict(max_objects=16, max_body_bytes=4096, max_entries=32, max_depth=4)
    return module.GitTreeClosureLimits(**(values | changes))


def verify(module, case, check, **changes):
    return module.verify_git_tree_closure(
        case.cas,
        case.root,
        case.catalog,
        platform="posix",
        limits=limits(module, **changes),
        checkpoint=check,
    )


@pytest.fixture
def case_factory(tmp_path):
    stores = []

    def build(fmt="sha1", shape="shared"):
        def material(kind, body):
            return GitObjectMaterial.from_body(kind, fmt, body)

        def entry(mode, name, child):
            return mode + b" " + name + b"\0" + bytes.fromhex(child.object_id)

        blob = material("blob", b"payload\n")
        shared = material("tree", entry(b"100644", b"inside.txt", blob))
        empty = material("tree", b"")
        root = material(
            "tree",
            b"".join(
                [
                    entry(b"40000", b"a", shared),
                    entry(b"40000", b"b", shared),
                    entry(b"40000", b"empty", empty),
                    entry(b"100755", b"z", blob),
                ]
            ),
        )
        objects = [blob, shared, empty, root]
        if shape != "shared":
            root = empty if shape == "empty" else material("tree", b"100644 bad\0x")
            objects = [root]
        directory = tmp_path / f"cas-{len(stores)}"
        with SQLiteWorkspaceTransactionStore(directory) as writer:
            cas = GitMaterialCAS(writer)
            refs = {item.object_id: cas.persist(item) for item in objects}
        store = SQLiteWorkspaceTransactionStore(directory, read_only=True)
        stores.append(store)
        return SimpleNamespace(
            cas=GitMaterialCAS(store),
            root=refs[root.object_id],
            directory=directory,
            catalog=tuple(ref for oid, ref in refs.items() if oid != root.object_id),
            objects=objects,
            order=[root.object_id, blob.object_id, shared.object_id, empty.object_id]
            if shape == "shared"
            else [root.object_id],
        )

    yield build
    for store in stores:
        store.close()


class Probe:
    def __init__(self):
        self.events = []
        self.control = GitAuthenticationControl(self.local, self.full)

    def full(self):
        assert self.control._segment is None
        self.events.append("full")

    def local(self):
        assert self.control._segment is not None
        self.events.append("local")


def shadow_pure():
    pytest.fail("instance/proxy/subclass .pure must not be dispatched")


@pytest.mark.parametrize("fmt", ["sha1", "sha256"])
@pytest.mark.parametrize("shape", ["shared", "empty"])
def test_real_readonly_canonical_cas_and_revoked_tokens(
    subject, before, case_factory, monkeypatch, fmt, shape
):
    case, probe, saved, reads = case_factory(fmt, shape), Probe(), [], []
    probe.control.pure = shadow_pure
    original_read, original_parse = GitMaterialCAS.read, subject.parse_git_tree
    original_catalog = subject._catalog

    def catalog(root, refs, capacity, checkpoint):
        if checkpoint is not probe.control:
            assert probe.control._segment is not None
            saved.append(checkpoint)
        return original_catalog(root, refs, capacity, checkpoint)

    def parse(material, *, max_entries, checkpoint):
        assert probe.control._segment is not None
        saved.append(checkpoint)
        start = len(probe.events)
        result = original_parse(material, max_entries=max_entries, checkpoint=checkpoint)
        assert probe.events[start:] == ["local"] * (len(result) + 2)
        return result

    def read(cas, ref):
        assert probe.control._segment is None  # 包括真实 CAS 读取，而非替代读取实现。
        reads.append(ref.object_id)
        return original_read(cas, ref)

    def store_check():
        assert probe.control._segment is None
        start = len(probe.events)
        for token in saved:
            token()
        assert probe.events[start:] == ["full"] * len(saved)

    snapshot = {p.name: p.read_bytes() for p in case.directory.rglob("*") if p.is_file()}
    with monkeypatch.context() as patch:
        patch.setattr(subject, "_catalog", catalog)
        patch.setattr(subject, "parse_git_tree", parse)
        patch.setattr(GitMaterialCAS, "read", read)
        case.cas.store._checkpoint = store_check
        actual = verify(subject, case, probe.control)
    case.cas.store._checkpoint = None
    actual_reads, reads[:] = reads.copy(), []
    with monkeypatch.context() as patch:
        patch.setattr(GitMaterialCAS, "read", read)
        expected = verify(before, case, lambda: None)
    assert asdict(actual) == asdict(expected)
    assert actual_reads == reads == case.order and len(reads) == len(set(reads))
    assert actual.body_bytes == sum(item.body_bytes for item in case.objects)
    assert (actual.expanded_entries, actual.tree_depth) == ((6, 1) if shape == "shared" else (0, 0))
    assert [f.path for f in actual.files] == (
        ["a/inside.txt", "b/inside.txt", "z"] if shape == "shared" else []
    )
    assert [r.object_id for r in actual.objects] == sorted(case.order)
    assert saved and "local" in probe.events and probe.control._segment is None
    start = len(probe.events)
    for token in saved:
        token()
    assert probe.events[start:] == ["full"] * len(saved)
    assert case.cas.store._db.execute("PRAGMA query_only").fetchone() == (1,)
    assert case.cas.store._db.total_changes == 0
    assert snapshot == {p.name: p.read_bytes() for p in case.directory.rglob("*") if p.is_file()}


@pytest.mark.parametrize("kind,local_count", [("blob", 2), ("tree", 8), ("empty", 4)])
def test_post_read_entry_local_exit_full_before_observed(
    subject, case_factory, monkeypatch, kind, local_count
):
    case, probe = case_factory(), Probe()
    ref = (
        case.root
        if kind == "tree"
        else next(
            r
            for r in case.catalog
            if (r.object_type == "blob" if kind == "blob" else r.body_bytes == 0)
        )
    )
    original_read = GitMaterialCAS.read

    def full():
        assert not state.observed and state.body_bytes == 0
        probe.full()

    def read(cas, reference):
        probe.events.append("CAS")
        result = original_read(cas, reference)
        probe.events.append("CAS-done")
        return result

    probe.control = GitAuthenticationControl(probe.local, full)
    state = subject._Traversal(case.cas, {ref.object_id: ref}, limits(subject), probe.control)
    case.cas.store._checkpoint = lambda: probe.events.append("store")
    monkeypatch.setattr(GitMaterialCAS, "read", read)
    request = GitObjectRead(ref.object_type, ref.object_id, ref.object_format)
    assert state.read(request) == ref
    assert probe.events == ["full", "CAS", "store", "store", "CAS-done", "full"] + [
        "local"
    ] * local_count + ["full"]
    assert state.observed == {ref.object_id: ref} and state.body_bytes == ref.body_bytes


@pytest.mark.parametrize("bad_catalog", ["not-tuple", "over-capacity"])
def test_typed_catalog_entry_full_precedes_declaration_but_unknown_does_not(
    subject,
    case_factory,
    bad_catalog,
):
    case, calls, error = case_factory(), [], asyncio.CancelledError("entry")

    def fail():
        calls.append("full")
        raise error

    catalog = [] if bad_catalog == "not-tuple" else case.catalog
    capacity = limits(subject, max_objects=0) if bad_catalog == "over-capacity" else limits(subject)
    with pytest.raises(BaseException) as caught:
        subject._catalog(case.root, catalog, capacity, GitAuthenticationControl(shadow_pure, fail))
    assert caught.value is error and calls == ["full"]
    calls.clear()
    with pytest.raises(KernelError) as caught:
        subject._catalog(case.root, catalog, capacity, fail)
    assert caught.value.code == (
        "git_tree_closure_invalid" if bad_catalog == "not-tuple" else "git_tree_closure_limit"
    )
    assert not calls


@pytest.mark.parametrize("kind", ["function", "object", "proxy", "subclass"])
@pytest.mark.parametrize(
    "scenario", ["shared", "empty", "malformed", "capacity", "missing", "path", "cancel"]
)
def test_unknown_full_trace_matches_frozen_module(
    subject, before, case_factory, monkeypatch, kind, scenario
):
    case = case_factory(shape=scenario if scenario in {"empty", "malformed"} else "shared")
    if scenario == "missing":
        case.catalog = ()

    def run(module):
        events, error = [], asyncio.CancelledError("unknown-checkpoint")

        def full():
            events.append("full")
            if scenario == "cancel" and events.count("full") == 7:
                raise error

        class Unknown:
            pure = staticmethod(shadow_pure)

            def __call__(self):
                full()

        class Proxy(Unknown):
            def __getattr__(self, name):
                return getattr(control, name)

        class Subclass(GitAuthenticationControl):
            pure = staticmethod(shadow_pure)

        control = GitAuthenticationControl(shadow_pure, full)
        check = {
            "function": full,
            "object": Unknown(),
            "proxy": Proxy(),
            "subclass": Subclass(shadow_pure, full),
        }[kind]
        original_read, original_parse, original_path = (
            GitMaterialCAS.read,
            module.parse_git_tree,
            module._Traversal.path,
        )

        def read(cas, ref):
            events.append(("CAS", ref.object_id))
            return original_read(cas, ref)

        def parse(material, **kwargs):
            events.append(("parse", material.object_id))
            return original_parse(material, **kwargs)

        def path(state, prefix, name, platform):
            events.append(("path", prefix, name))
            if scenario == "path":
                raise KernelError("git_tree_closure_path_denied", "path")
            return original_path(state, prefix, name, platform)

        with monkeypatch.context() as patch:
            patch.setattr(GitMaterialCAS, "read", read)
            patch.setattr(module, "parse_git_tree", parse)
            patch.setattr(module._Traversal, "path", path)
            case.cas.store._checkpoint = lambda: events.append("store")
            try:
                outcome = asdict(
                    verify(
                        module,
                        case,
                        check,
                        **({"max_body_bytes": 0} if scenario == "capacity" else {}),
                    )
                )
            except BaseException as caught:
                outcome = (type(caught), getattr(caught, "code", None), caught is error)
        return outcome, events

    assert run(subject) == run(before)


@pytest.mark.parametrize(
    "stage", ["pre", "entry", "exit", "post-local", "parser-local", "exit-local"]
)
@pytest.mark.parametrize("error_kind", ["kernel", "cancelled", "nested-upstream"])
def test_segment_fault_identity_no_commit_or_masking(subject, case_factory, stage, error_kind):
    case, events = case_factory(), []
    error = (
        KernelError("test_parent", "parent")
        if error_kind == "kernel"
        else asyncio.CancelledError("parent")
    )
    if error_kind == "nested-upstream":
        error = UpstreamCheckpointError(UpstreamCheckpointError(error))
    full_at = {"pre": 1, "entry": 2, "exit": 3}.get(stage)
    local_at = {"post-local": 1, "parser-local": 3, "exit-local": 8}.get(stage)

    def check(kind, fail_at):
        events.append(kind)
        if events.count(kind) == fail_at:
            raise error

    control = GitAuthenticationControl(
        lambda: check("local", local_at), lambda: check("full", full_at)
    )
    state = subject._Traversal(case.cas, {case.root.object_id: case.root}, limits(subject), control)
    case.cas.store._checkpoint = lambda: events.append("store")
    with pytest.raises(BaseException) as caught:
        state.read(GitObjectRead("tree", case.root.object_id, case.root.object_format))
    assert caught.value is error
    assert not state.observed and state.body_bytes == 0 and control._segment is None
    assert events.count("full") == (full_at or 2)
    assert events.count("local") == (local_at or (8 if stage == "exit" else 0))
    assert events.count("store") == (0 if stage == "pre" else 2)


@pytest.mark.parametrize("scenario", ["missing", "type", "cached", "objects", "bytes"])
@pytest.mark.parametrize("typed", [False, True])
def test_read_precheck_catalog_observed_capacity_order(subject, case_factory, scenario, typed):
    case, events = case_factory(), []

    def full():
        events.append("full")

    check = GitAuthenticationControl(shadow_pure, full) if typed else full
    ref = case.root
    catalog = {} if scenario == "missing" else {ref.object_id: ref}
    capacity = (
        limits(subject, max_objects=0, max_body_bytes=0)
        if scenario in {"cached", "objects"}
        else limits(subject, max_body_bytes=0)
    )
    state = subject._Traversal(case.cas, catalog, capacity, check)
    if scenario == "cached":
        state.observed[ref.object_id] = ref
    case.cas.store._checkpoint = lambda: pytest.fail("precheck must not enter CAS")
    request = GitObjectRead(
        "blob" if scenario == "type" else "tree", ref.object_id, ref.object_format
    )
    if scenario == "cached":
        assert state.read(request) == ref
    else:
        with pytest.raises(KernelError) as caught:
            state.read(request)
        assert (
            caught.value.code
            == {
                "missing": "git_tree_closure_missing",
                "type": "git_tree_closure_type_mismatch",
                "objects": "git_tree_closure_limit",
                "bytes": "git_tree_closure_limit",
            }[scenario]
        )
    assert events == ["full"] and state.body_bytes == 0


@pytest.mark.parametrize("scenario", ["store-cancel", "store-nested", "corrupt", "parser"])
def test_cas_first_failure_precedes_post_auth_and_actual_parser(
    subject, case_factory, monkeypatch, scenario
):
    case, events = case_factory(shape="malformed"), []
    error = asyncio.CancelledError("store")
    if scenario == "store-nested":
        error = UpstreamCheckpointError(UpstreamCheckpointError(error))

    def store_check():
        events.append("store")
        assert control._segment is None
        if scenario.startswith("store-"):
            raise error

    def parse(material, **kwargs):
        events.append("parse")
        return original_parse(material, **kwargs)

    control = GitAuthenticationControl(
        lambda: events.append("local"), lambda: events.append("full")
    )
    original_parse = subject.parse_git_tree
    monkeypatch.setattr(subject, "parse_git_tree", parse)
    case.cas.store._checkpoint = store_check
    if scenario == "corrupt":
        (case.directory / "blobs" / case.root.cas_digest).write_bytes(b"corrupt fixture")
    state = subject._Traversal(case.cas, {case.root.object_id: case.root}, limits(subject), control)
    with pytest.raises(BaseException) as caught:
        state.read(GitObjectRead("tree", case.root.object_id, case.root.object_format))
    if scenario.startswith("store-"):
        assert caught.value is error
    else:
        assert caught.value.code == (
            "git_material_cas_read_failed"
            if scenario == "corrupt"
            else "git_object_references_invalid"
        )
    assert ("parse" in events) == (scenario == "parser")
    assert events.count("full") == (2 if scenario == "parser" else 1)
    assert not state.observed and state.body_bytes == 0 and control._segment is None


@pytest.mark.parametrize("owner", ["task", "thread"])
def test_foreign_owner_falls_back_full_without_rebinding(subject, before, case_factory, owner):
    case = case_factory()

    def run():
        probe = Probe()
        probe.control.pure = shadow_pure
        origin = probe.control._origin

        def child():
            expected = verify(before, case, probe.control)
            old_count = len(probe.events)
            probe.events.clear()
            actual = verify(subject, case, probe.control)
            assert asdict(actual) == asdict(expected)
            assert probe.events == ["full"] * (old_count + 2 + 2 * len(case.order))
            assert probe.control._origin is origin and probe.control._segment is None
            assert probe.control._task is origin[2] and probe.control._thread is origin[3]

        return child

    if owner == "task":

        async def parent():
            child = run()

            async def foreign():
                child()

            await asyncio.create_task(foreign())

        asyncio.run(parent())
    else:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(run()).result()


@pytest.mark.parametrize("segment", ["catalog", "read"])
@pytest.mark.parametrize(
    "field", ["_local_check", "_authenticate", "_task", "_thread", "_origin", "_segment"]
)
def test_parent_binding_drift_rejected_not_rebound(subject, case_factory, segment, field):
    case, events = case_factory(), []

    def drift():
        events.append("local")
        control.__dict__[field] = object()

    control = GitAuthenticationControl(drift, lambda: events.append("full"))
    origin = control._origin
    state = subject._Traversal(case.cas, {case.root.object_id: case.root}, limits(subject), control)
    with pytest.raises(KernelError) as caught:
        if segment == "catalog":
            subject._catalog(case.root, case.catalog, limits(subject), control)
        else:
            state.read(GitObjectRead("tree", case.root.object_id, case.root.object_format))
    assert caught.value.code == "git_authentication_control_invalid"
    assert events == (["full", "local"] if segment == "catalog" else ["full", "full", "local"])
    assert control._origin is origin and control._segment is None
    assert not state.observed and state.body_bytes == 0


@pytest.mark.parametrize("stage", ["entry", "local", "exit-local", "exit"])
@pytest.mark.parametrize("error_kind", ["kernel", "cancelled", "nested-upstream"])
def test_catalog_fault_identity_and_success_exit_not_skipped(
    subject, case_factory, stage, error_kind
):
    case, events = case_factory(), []
    error = KernelError("test_parent", "parent")
    if error_kind == "cancelled":
        error = asyncio.CancelledError("parent")
    elif error_kind == "nested-upstream":
        error = UpstreamCheckpointError(UpstreamCheckpointError(error))
    full_at = {"entry": 1, "exit": 2}.get(stage)
    local_at = {"local": 1, "exit-local": len(case.catalog) + 1}.get(stage)

    def check(kind, fail_at):
        events.append(kind)
        if events.count(kind) == fail_at:
            raise error

    control = GitAuthenticationControl(
        lambda: check("local", local_at), lambda: check("full", full_at)
    )
    case.cas.store._checkpoint = lambda: pytest.fail("声明纯段不得访问 CAS")
    with pytest.raises(BaseException) as caught:
        subject._catalog(case.root, case.catalog, limits(subject), control)
    assert caught.value is error and control._segment is None
    assert events.count("full") == (full_at or 1)
    assert events.count("local") == (local_at or (len(case.catalog) + 1 if stage == "exit" else 0))
