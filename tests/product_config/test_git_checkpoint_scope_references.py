"""Scope 引用规划纯段的真实 CAS 边界；不冒充 Session/SDK 来源认证。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.product_config import git_checkpoint_scope as current
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from tests.product_config.test_git_checkpoint_scope import cas as cas
from tests.product_config.test_git_checkpoint_scope import case as case
from tests.product_config.test_git_checkpoint_scope import inputs
from tests.support.git_delivery_observed_core import canonical, json_facts

BASE = "7d7489dc6df5b269de3a93b0a6c5cad3f661cd55"
BEFORE_SHA = "e348a19b3b8097f884f9ef5b55b927b9f0014f40ce68de81bea388194a656ae9"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def before(tmp_path_factory):
    path = os.environ.get("HARNESSIX_SCOPE_BEFORE")
    if path is None:
        path = tmp_path_factory.mktemp("before") / "scope.py"
        source = subprocess.check_output(
            ["git", "show", BASE + ":src/harnessix/product_config/git_checkpoint_scope.py"],
            cwd=Path(__file__).resolve().parents[2],
        )
        path.write_bytes(source)
    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == BEFORE_SHA
    return _load(path, "harnessix.product_config._scope_before_refs")


@pytest.fixture(scope="session")
def subject():
    path = os.environ.get("HARNESSIX_SCOPE_SUBJECT")
    return _load(path, "harnessix.product_config._scope_refs_red") if path else current


def _diff(case):
    args = inputs(case)
    nodes = {node.material.object_id: node.material for node in case.core.object_scope.objects}
    return current.prepare_git_tree_diff(
        case.cas,
        nodes[args["baseline"].head_tree_oid],
        args["base_catalog"],
        args["baseline"].source.mutations,
        args["after_catalog"],
        platform="posix",
        limits=args["limits"],
        max_diff_bytes=current.MAX_WORKSPACE_DIFF_BYTES,
        checkpoint=lambda: None,
    )


def _references(module, case, diff, checkpoint, **changes):
    args = inputs(case) | changes
    return module._projection_references(
        args["baseline"],
        args["base_commit"],
        args["base_catalog"],
        args["after_catalog"],
        diff,
        args["limits"],
        checkpoint,
    )


def test_actual_build_union_is_pure_but_all_cas_reads_and_writes_are_full(
    subject,
    case,
    monkeypatch,
):
    events, saved, writes = [], [], []
    control = GitAuthenticationControl(
        lambda: events.append("local"), lambda: events.append("full")
    )
    control.pure = lambda: pytest.fail("实例 shadow 不是可信入口")
    original_union = subject._union
    original_read, original_persist = GitMaterialCAS.read, GitMaterialCAS.persist

    def union(refs, limits, checkpoint):
        assert type(checkpoint) is not GitAuthenticationControl
        saved.append(checkpoint)
        start = len(events)
        result = original_union(refs, limits, checkpoint)
        assert events[start:] == ["local"] * (len(refs) + 1)
        return result

    def read(cas, reference):
        assert control._segment is None
        return original_read(cas, reference)

    def persist(cas, material):
        assert control._segment is None
        start = len(events)
        for check in saved:
            check()
        assert events[start:] == ["full"] * len(saved)
        writes.append(material.object_id)
        return original_persist(cas, material)

    monkeypatch.setattr(subject, "_union", union)
    monkeypatch.setattr(GitMaterialCAS, "read", read)
    monkeypatch.setattr(GitMaterialCAS, "persist", persist)
    scope, diff = subject.build_product_git_checkpoint_scope(
        case.cas,
        **inputs(case),
        checkpoint=control,
    )
    assert canonical(json_facts(scope)) == canonical(json_facts(case.core.object_scope))
    assert diff.content.text == case.diff_text and writes and saved
    assert control._segment is None


def test_reference_plan_has_no_cas_and_exact_union_and_budget_before_writes(
    subject,
    case,
    monkeypatch,
):
    diff = _diff(case)
    expected = _references(subject, case, diff, lambda: None)
    counts = Counter()

    def forbidden(*args):
        pytest.fail("引用规划不能调用实际 CAS")

    monkeypatch.setattr(GitMaterialCAS, "read", forbidden)
    monkeypatch.setattr(GitMaterialCAS, "persist", forbidden)
    control = GitAuthenticationControl(
        lambda: counts.update(["local"]), lambda: counts.update(["full"])
    )
    actual = _references(subject, case, diff, control)
    assert actual == expected and counts["full"] == 2 and counts["local"] > 1
    with pytest.raises(KernelError) as caught:
        _references(
            subject, case, diff, control, limits=replace(inputs(case)["limits"], max_objects=0)
        )
    assert caught.value.code == "git_inventory_limit" and control._segment is None


@pytest.mark.parametrize("stage", ["entry", "local", "exit-local", "exit"])
@pytest.mark.parametrize("kind", ["kernel", "cancelled", "nested-upstream"])
def test_reference_segment_failure_identity_prevents_io_and_result(
    subject,
    case,
    monkeypatch,
    stage,
    kind,
):
    diff = _diff(case)
    events = []
    error = KernelError("parent", "原控制失败")
    if kind == "cancelled":
        error = asyncio.CancelledError("parent")
    elif kind == "nested-upstream":
        error = UpstreamCheckpointError(UpstreamCheckpointError(error))
    full_at = {"entry": 1, "exit": 2}.get(stage)
    phase = "body"

    def full():
        events.append("full")
        if events.count("full") == full_at:
            raise error

    def local():
        events.append("local")
        if stage == "local" or (stage == "exit-local" and phase == "done"):
            raise error

    original_union = subject._union

    def union(*args):
        nonlocal phase
        result = original_union(*args)
        phase = "done"
        return result

    monkeypatch.setattr(subject, "_union", union)
    control = GitAuthenticationControl(local, full)
    result = None
    with pytest.raises(BaseException) as caught:
        result = _references(subject, case, diff, control)
    assert caught.value is error and result is None and control._segment is None
    assert events.count("full") == (full_at or 1)


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_unknown_entire_build_keeps_frozen_io_and_callback_order(
    subject, before, case, monkeypatch, kind
):
    def run(module):
        events = []

        def full():
            events.append("full")

        class Proxy:
            pure = staticmethod(lambda: pytest.fail("代理 pure 不可执行"))

            def __call__(self):
                full()

        class Subclass(GitAuthenticationControl):
            def pure(self):
                pytest.fail("子类 pure 不可执行")

        checkpoint = {"function": full, "proxy": Proxy(), "subclass": Subclass(full, full)}[kind]
        original_read, original_persist = GitMaterialCAS.read, GitMaterialCAS.persist

        def read(cas, ref):
            events.append(("read", ref.object_id))
            return original_read(cas, ref)

        def persist(cas, material):
            events.append(("persist", material.object_id))
            return original_persist(cas, material)

        with monkeypatch.context() as patch:
            patch.setattr(GitMaterialCAS, "read", read)
            patch.setattr(GitMaterialCAS, "persist", persist)
            scope, diff = module.build_product_git_checkpoint_scope(
                case.cas, **inputs(case), checkpoint=checkpoint
            )
        return canonical(json_facts(scope)), diff.content, events

    assert run(subject) == run(before)
