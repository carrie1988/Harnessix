"""材料父历史的真实 CAS/400 parent 回归；不代表 Session 或批准认证。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import replace

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery import store as store_module
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_delivery_plan_materials as materials
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.workspace import parent_closure_codec as codec
from harnessix.workspace.contracts import WorkspaceResourceRequest
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.snapshot_contracts import snapshot_requests
from harnessix.workspace.snapshot_v2 import capture_workspace_snapshot_v2
from tests.product_config.git_delivery_plan_support import canonical_bytes, observe_state, sealed
from tests.support.git_delivery_observed_core import make_observed_case, model_fields
from tests.workspace.test_snapshot_v2_native_progress import _spread

PARENTS = 400


def _with_snapshot(case, snapshot):
    baseline = case.core.baseline
    source = sealed(
        type(baseline.source),
        {**model_fields(baseline.source, "digest"), "workspace": snapshot},
        "digest",
    )
    baseline = sealed(
        type(baseline), {**model_fields(baseline, "digest"), "source": source}, "digest"
    )
    payload = model_fields(case.core)
    if "user_observation" in payload:
        observation = case.core.user_observation
        payload["user_observation"] = sealed(
            type(observation), {**model_fields(observation), "baseline": baseline}
        )
    else:
        payload["baseline"] = baseline
    return replace(case, core=sealed(type(case.core), payload))


@pytest.fixture(scope="module")
def material_cases(tmp_path_factory):
    """原完整 Core 夹具加真实宽父历史；小块只作用于测试编码，Reader 上限不变。"""
    cases = {}
    with ExitStack() as stack:
        for action in ("checkpoint", "commit"):
            root = tmp_path_factory.mktemp(f"material-parent-{action}")
            store = stack.enter_context(SQLiteWorkspaceTransactionStore(root / "state"))
            observed, legacy = make_observed_case(GitMaterialCAS(store), root, action=action)
            requests = tuple(
                WorkspaceResourceRequest(path=f"broad/{item.path}", access=item.access)
                for item in _spread(legacy.workspace_root / "broad", 132, depth=3)
            )
            with pytest.MonkeyPatch.context() as encoding:
                encoding.setattr(codec, "MAX_CLOSURE_BLOB_BYTES", 32 * 1024)
                snapshot = capture_workspace_snapshot_v2(
                    legacy.workspace_root,
                    resources=(
                        *snapshot_requests(legacy.core.baseline.source.workspace),
                        *requests,
                    ),
                    checkpoint=lambda: None,
                    write_blob=store.put_blob,
                    read_blob=store.blob,
                )
            assert snapshot.parent_closure.parent_count == PARENTS
            history = codec.read_workspace_parent_closure(
                snapshot, store.blob, checkpoint=lambda: None
            )
            manifest = json.loads(store.blob(snapshot.parent_closure.sha256))
            assert len(history) == PARENTS and len(manifest["chunks"]) > 1
            cases[action] = tuple(
                replace(_with_snapshot(case, snapshot), history=history)
                for case in (legacy, observed)
            )
        yield cases


class _Probe:
    def __init__(self):
        self.control = None
        self.phase = "outside"
        self.hook = None
        self.failure_at = None
        self.failure = None
        self.raised = False
        self.clear()

    def clear(self):
        self.events = []
        self.hits = Counter()
        self.reader_options = []
        self.digest_inputs = []
        self.saved_checks = []
        self.chunk_counts = []
        self.segment_index = 0

    def record(self, mode, detail=None):
        event = (mode, self.phase, detail)
        self.events.append(event)
        self.hits[mode, self.phase] += 1
        assert not self.raised, "首错后不得继续读正文或追加出口认证"
        if self.failure_at == (mode, self.phase, self.hits[mode, self.phase]):
            self.raised = True
            raise self.failure
        if self.hook is not None:
            self.hook(event)

    def full(self):
        self.record("full")

    def local(self):
        self.record("local")

    def history_trace(self):
        return tuple(event for event in self.events if event[1] != "outside")

    def modes(self, phase):
        return [
            mode for mode, actual, _ in self.events if actual == phase and mode in {"full", "local"}
        ]


@pytest.fixture
def probe(monkeypatch):
    probe = _Probe()
    original_reader = materials.read_workspace_parent_closure
    original_model = codec._read_model
    original_canonical = codec.canonical_bytes
    original_digest = codec.observations_digest
    original_physical = store_module.read_blob_body

    def history(snapshot, read_blob, **options):
        probe.reader_options.append(options.copy())
        factory = options.get("pure_progress")

        @contextmanager
        def progress():
            # 仅标记原工厂的首末阶段；不替换其控制点、验真或退出行为。
            previous = probe.phase
            if probe.segment_index == len(probe.chunk_counts):
                probe.phase = "digest"
            probe.segment_index += 1
            try:
                with factory() as check:
                    probe.saved_checks.append(check)
                    yield check
            finally:
                probe.phase = previous

        if factory is not None:
            options["pure_progress"] = progress
        previous, probe.phase = probe.phase, "expand"
        try:
            return original_reader(snapshot, read_blob, **options)
        finally:
            probe.phase = previous

    def model(*args, **kwargs):
        previous, probe.phase = probe.phase, "canonical"
        try:
            result = original_model(*args, **kwargs)
            if hasattr(result, "chunks"):
                probe.chunk_counts = [chunk.count for chunk in result.chunks]
            return result
        finally:
            probe.phase = previous

    def canonical(value):
        if probe.phase == "canonical":
            if type(probe.control) is GitAuthenticationControl:
                assert probe.control._segment is None
            probe.record("canonical", value["spec_version"])
        return original_canonical(value)

    def physical(path, digest):
        if type(probe.control) is GitAuthenticationControl:
            assert probe.control._segment is None, "物理 CAS 不能借用纯段"
        probe.record("blob", digest)
        return original_physical(path, digest)

    def digest(parents, checkpoint):
        previous, probe.phase = probe.phase, "digest"
        probe.digest_inputs.append(tuple(parents))
        probe.saved_checks.append(checkpoint)
        try:
            return original_digest(parents, checkpoint)
        finally:
            probe.phase = previous

    monkeypatch.setattr(materials, "read_workspace_parent_closure", history)
    monkeypatch.setattr(codec, "_read_model", model)
    monkeypatch.setattr(codec, "canonical_bytes", canonical)
    monkeypatch.setattr(codec, "observations_digest", digest)
    monkeypatch.setattr(store_module, "read_blob_body", physical)
    return probe


def _manifest(case):
    snapshot = case.core.baseline.source.workspace
    # 夹具元数据只读文件，不计入正在受测的物理 Reader 次数。
    return json.loads((case.cas.store._blobs / snapshot.parent_closure.sha256).read_bytes())


def _expected_reads(case):
    return (
        case.core.baseline.source.workspace.parent_closure.sha256,
        *(chunk["sha256"] for chunk in _manifest(case)["chunks"]),
    )


def _verify(case, checkpoint, reader="v2", *, readonly=True):
    operation = {
        "v1": materials.verify_product_git_delivery_core_materials,
        "v2": materials.verify_product_git_delivery_core_materials_v2,
        "v2-read": materials.read_product_git_delivery_core_materials_v2,
    }[reader]
    before = observe_state(case) if readonly else None
    try:
        return operation(case.cas, case.core, checkpoint=checkpoint)
    finally:
        if readonly:
            assert observe_state(case) == before


def _old_history(case, checkpoint, *, explicit_none=False):
    options = {"pure_progress": None} if explicit_none else {}
    return materials.read_workspace_parent_closure(
        case.core.baseline.source.workspace,
        case.cas.store.blob,
        checkpoint=checkpoint,
        **options,
    )


def _callback(probe, kind):
    def forbidden():
        pytest.fail("普通函数、proxy 或子类不得获得纯段")

    def function():
        probe.full()

    function.pure = forbidden

    class Proxy:
        __call__ = staticmethod(function)
        pure = staticmethod(forbidden)

    class Subclass(GitAuthenticationControl):
        pure = staticmethod(forbidden)

    probe.control = {
        "exact": lambda: GitAuthenticationControl(probe.local, probe.full),
        "function": lambda: function,
        "proxy": Proxy,
        "subclass": lambda: Subclass(forbidden, function),
    }[kind]()
    return probe.control


@pytest.mark.parametrize("action", ["checkpoint", "commit"])
@pytest.mark.parametrize("reader", ["v1", "v2", "v2-read"])
async def test_exact_same_task_local_inside_full_boundaries_and_all_original_blobs(
    material_cases, probe, action, reader
):
    case = material_cases[action][reader != "v1"]
    control = _callback(probe, "exact")
    result = _verify(case, control, reader)
    assert (result.core if reader == "v2-read" else result) == case.core
    if reader == "v2-read":
        assert result.diff.content.text == case.diff_text
    assert probe.reader_options[0]["checkpoint"] is control
    assert set(probe.reader_options[0]) == {"checkpoint", "pure_progress"}
    counts = [chunk["count"] for chunk in _manifest(case)["chunks"]]
    expected_expand = ["full", "full"]
    for count in counts:
        expected_expand.extend(["full", *(["local"] * (count + 1)), "full"])
    assert probe.modes("expand") == expected_expand
    assert probe.modes("digest") == ["full", *(["local"] * (PARENTS + 1)), "full"]
    assert probe.digest_inputs == [case.history]
    history = probe.history_trace()
    assert Counter(detail for mode, _, detail in history if mode == "blob") == Counter(
        _expected_reads(case)
    )
    assert sum(mode == "canonical" for mode, _, _ in history) == len(_expected_reads(case))
    for index, (mode, phase, _) in enumerate(history):
        if mode == "blob":
            assert history[index - 1][:2] == history[index + 1][:2] == ("full", phase)
    assert control._segment is None
    saved = probe.saved_checks[-1]
    probe.clear()
    saved()
    control()
    assert probe.events == [("full", "outside", None)] * 2


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_unknown_controls_keep_complete_omitted_factory_trace(material_cases, probe, kind):
    case = material_cases["checkpoint"][1]
    control = _callback(probe, kind)
    assert _old_history(case, control) == case.history
    expected = probe.history_trace()
    probe.clear()
    assert _verify(case, control) == case.core
    assert probe.history_trace() == expected
    assert set(probe.reader_options[0]) == {"checkpoint"}
    assert not any(mode == "local" for mode, _, _ in expected)
    assert (
        sum(mode == "full" for mode, _, _ in expected)
        == 2 + 2 * len(_expected_reads(case)) + 2 * PARENTS
    )


@pytest.mark.parametrize("kind", ["exact", "function", "proxy", "subclass"])
def test_reader_explicit_none_only_disables_factory_and_preserves_old_trace(
    material_cases, probe, kind
):
    case = material_cases["checkpoint"][1]
    control = _callback(probe, kind)
    assert _old_history(case, control) == case.history
    expected = probe.history_trace()
    probe.clear()
    assert _old_history(case, control, explicit_none=True) == case.history
    assert probe.history_trace() == expected
    assert probe.reader_options[0]["pure_progress"] is None
    assert not any(mode == "local" for mode, _, _ in expected)


def test_none_is_not_an_optional_material_checkpoint(material_cases, probe):
    with pytest.raises(TypeError):
        _verify(material_cases["checkpoint"][1], None)
    assert probe.reader_options == [] and probe.events == []


@pytest.mark.parametrize("foreign", ["task", "thread"])
async def test_actual_foreign_task_or_thread_retains_old_full_call_count(
    material_cases, probe, foreign
):
    case = material_cases["checkpoint"][1]
    control = _callback(probe, "exact")
    origin = control._origin
    before = observe_state(case)

    async def child():
        assert asyncio.current_task() is not control._task
        assert _old_history(case, control) == case.history
        expected = probe.history_trace()
        probe.clear()
        assert _verify(case, control, readonly=False) == case.core
        assert probe.history_trace() == expected
        assert set(probe.reader_options[0]) == {"checkpoint", "pure_progress"}
        assert not any(mode == "local" for mode, _, _ in probe.events)
        assert (
            sum(mode == "full" for mode, _, _ in expected)
            == 2 + 2 * len(_expected_reads(case)) + 2 * PARENTS
        )

    if foreign == "task":
        await asyncio.create_task(child())
    else:
        with ThreadPoolExecutor(max_workers=1) as executor:
            await asyncio.wrap_future(executor.submit(asyncio.run, child()))
    assert control._origin is origin and control._segment is None
    assert observe_state(case) == before


def _error(kind):
    return {
        "cancel": lambda: TurnCancelled("original cancellation"),
        "task-cancel": lambda: asyncio.CancelledError("original task cancellation"),
        "timeout": lambda: TimeoutError("original deadline"),
        "same-code": lambda: KernelError("workspace_closure_corrupt", "control, not data"),
        "value": lambda: ValueError("original control"),
        "type": lambda: TypeError("original control"),
        "os": lambda: OSError("control, not CAS"),
        "nested": lambda: UpstreamCheckpointError(UpstreamCheckpointError(TimeoutError())),
    }[kind]()


@pytest.mark.parametrize(
    "kind", ["cancel", "task-cancel", "timeout", "same-code", "value", "type", "os", "nested"]
)
@pytest.mark.parametrize(
    "boundary",
    [
        ("full", "expand", 1),
        ("full", "canonical", 1),
        ("full", "canonical", 2),
        ("full", "expand", 3),
        ("local", "expand", 1),
        ("full", "expand", 4),
        ("full", "digest", 1),
        ("local", "digest", PARENTS),
        ("local", "digest", PARENTS + 1),
        ("full", "digest", 2),
    ],
)
def test_control_first_error_identity_and_failed_exit_never_return_prefix(
    material_cases, probe, kind, boundary
):
    case = material_cases["checkpoint"][1]
    control = _callback(probe, "exact")
    failure = _error(kind)
    probe.failure_at, probe.failure = boundary, failure
    result = None
    with pytest.raises(type(failure)) as caught:
        result = _verify(case, control)
    assert caught.value is failure and result is None
    assert probe.events[-1][:2] == boundary[:2]
    assert probe.hits[boundary[:2]] == boundary[2]
    assert control._segment is None


@pytest.mark.parametrize("stop", ["cancel", "deadline", "cancel-before-deadline"])
@pytest.mark.parametrize("phase", ["expand", "digest"])
def test_original_cancel_token_and_absolute_budget_stop_local_progress(
    material_cases, probe, stop, phase
):
    case = material_cases["checkpoint"][1]
    cancel, budget = CancelToken(), GitOperationBudget(60)
    failures = []

    def local():
        probe.local()
        if probe.phase == phase and probe.hits["local", phase] == 2:
            if stop != "deadline":
                cancel.cancel()
            if stop != "cancel":
                budget._deadline = 0
        try:
            cancel.checkpoint()
            budget.remaining()
        except BaseException as error:
            failures.append(error)
            raise

    control = probe.control = GitAuthenticationControl(local, probe.full)
    deadline = budget.expires_at_monotonic_ns
    with pytest.raises(KernelError if stop == "deadline" else TurnCancelled) as caught:
        _verify(case, control)
    assert caught.value is failures[0] and len(failures) == 1
    assert probe.events[-1][:2] == ("local", phase) and control._segment is None
    assert budget.expires_at_monotonic_ns == (deadline if stop == "cancel" else 0)
    if stop == "deadline":
        assert caught.value.code == "git_process_timeout"


@pytest.mark.parametrize("drift_at", ["blob", "expand", "digest"])
def test_owner_change_still_requires_full_authentication_before_success(
    material_cases, probe, drift_at
):
    case = material_cases["checkpoint"][1]
    original_owner = object()
    owner = original_owner
    failure = KernelError("git_material_owner_changed", "original Owner binding")

    def full():
        probe.full()
        if owner is not original_owner:
            raise failure

    def hook(event):
        nonlocal owner
        mode, phase, _ = event
        if (drift_at == "blob" and mode == "blob" and phase == "canonical") or (
            phase == drift_at and mode == "local"
        ):
            owner = object()

    probe.hook = hook
    control = probe.control = GitAuthenticationControl(probe.local, full)
    with pytest.raises(KernelError) as caught:
        _verify(case, control)
    assert caught.value is failure and control._segment is None
    assert probe.events[-1][:2] == ("full", "canonical" if drift_at == "blob" else drift_at)


def _persist_manifest(case, manifest, *, body=None):
    body = canonical_bytes(manifest) if body is None else body
    digest = hashlib.sha256(body).hexdigest()
    case.cas.store.put_blob(digest, body)
    snapshot = case.core.baseline.source.workspace
    reference = snapshot.parent_closure.model_copy(update={"sha256": digest, "size": len(body)})
    # Snapshot 的摘要域排除 spec_version；不能沿用 Core 的 sealed 配方。
    payload = snapshot.model_dump(mode="json", exclude={"spec_version", "revision"})
    payload["parent_closure"] = reference.model_dump(mode="json")
    snapshot = type(snapshot).model_validate_json(
        canonical_bytes(
            {
                **payload,
                "spec_version": snapshot.spec_version,
                "revision": hashlib.sha256(canonical_bytes(payload)).hexdigest(),
            }
        ),
        strict=True,
    )
    return _with_snapshot(case, snapshot)


@pytest.mark.parametrize("version", [0, 1], ids=["core1", "core2"])
@pytest.mark.parametrize(
    "damage",
    [
        "manifest-bytes",
        "manifest-schema",
        "manifest-canonical",
        "last-chunk-bytes",
        "last-chunk-canonical",
        "complete-digest",
        "tail-observation",
    ],
)
def test_bad_manifest_last_chunk_and_complete_observation_digest_still_reject(
    material_cases, probe, version, damage
):
    case = material_cases["checkpoint"][version]
    manifest = _manifest(case)
    original_reads = _expected_reads(case)
    changed_path = None
    original_body = None
    if damage in {"manifest-bytes", "last-chunk-bytes"}:
        changed_path = (
            case.cas.store._blobs / original_reads[0 if damage == "manifest-bytes" else -1]
        )
        original_body = changed_path.read_bytes()
        changed_path.write_bytes(original_body[:-1] + b"!")
    elif damage == "manifest-schema":
        manifest["chunks"][-1]["start_index"] += 1
        case = _persist_manifest(case, manifest)
    elif damage == "manifest-canonical":
        case = _persist_manifest(case, manifest, body=canonical_bytes(manifest) + b"\n")
    elif damage == "complete-digest":
        manifest["observations_digest"] = "0" * 64
        case = _persist_manifest(case, manifest)
    else:
        chunk = manifest["chunks"][-1]
        payload = json.loads((case.cas.store._blobs / chunk["sha256"]).read_bytes())
        if damage == "tail-observation":
            identity = payload["entries"][-1]["identity"]
            payload["entries"][-1]["identity"] = ("1" if identity[0] != "1" else "2") + identity[1:]
        body = canonical_bytes(payload) + (b"\n" if damage == "last-chunk-canonical" else b"")
        digest = hashlib.sha256(body).hexdigest()
        case.cas.store.put_blob(digest, body)
        chunk.update(sha256=digest, size=len(body))
        case = _persist_manifest(case, manifest)
    control = _callback(probe, "exact")
    probe.clear()
    delivered = None
    try:
        with pytest.raises(KernelError) as caught:
            delivered = _verify(case, control, "v1" if version == 0 else "v2")
        assert caught.value.code == "workspace_closure_corrupt" and delivered is None
        assert caught.value.__cause__ is None and control._segment is None
        reads = [
            detail
            for mode, phase, detail in probe.events
            if mode == "blob" and phase == "canonical"
        ]
        expected = original_reads if damage == "manifest-bytes" else _expected_reads(case)
        assert reads == list(expected[:1] if damage.startswith("manifest-") else expected)
        if damage in {"complete-digest", "tail-observation"}:
            assert len(probe.digest_inputs[0]) == PARENTS
            assert probe.modes("digest") == ["full", *(["local"] * PARENTS)]
    finally:
        if changed_path is not None:
            changed_path.write_bytes(original_body)


def test_second_verification_rereads_every_blob_and_detects_new_tail_corruption(
    material_cases, probe
):
    case = material_cases["checkpoint"][1]
    control = _callback(probe, "exact")
    expected = Counter(_expected_reads(case))
    for _ in range(2):
        probe.clear()
        assert _verify(case, control) == case.core
        assert (
            Counter(
                detail
                for mode, phase, detail in probe.events
                if mode == "blob" and phase == "canonical"
            )
            == expected
        )
    path = case.cas.store._blobs / _expected_reads(case)[-1]
    body = path.read_bytes()
    path.write_bytes(body[:-1] + b"!")
    try:
        probe.clear()
        with pytest.raises(KernelError) as caught:
            _verify(case, control)
        assert caught.value.code == "workspace_closure_corrupt"
        assert (
            Counter(
                detail
                for mode, phase, detail in probe.events
                if mode == "blob" and phase == "canonical"
            )
            == expected
        )
    finally:
        path.write_bytes(body)
