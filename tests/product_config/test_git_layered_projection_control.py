"""Git 纯声明投影分层回归；数据夹具不代表 MAC 认证、批准或执行验收。"""

import asyncio
from functools import partial
from hashlib import sha256

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config.git_decision_link_sources import build_git_decision_link_sources
from harnessix.product_config.git_decision_link_wire import encode_product_git_decision_link
from harnessix.product_config.git_delivery_review_codec import (
    build_product_git_action_review,
    decode_product_git_action_review,
    encode_product_git_action_review,
)
from tests.product_config.test_git_decision_link_sources import case as case
from tests.product_config.test_git_decision_link_sources import fixture as decision_fixture
from tests.support.git_delivery_observed_core import canonical, json_facts
from tests.support.git_delivery_review import material_case


class Trace:
    """记录真实控制轨迹；分别注入首失败和退出认证漂移。"""

    def __init__(self):
        self.events, self.counts, self.failures = [], {"local": 0, "full": 0}, {}
        self.drift_at, self.changed = 0, False
        self.drift_error = KernelError("git_authentication_drift", "来源认证已改变")
        self.control = GitAuthenticationControl(
            partial(self.check, "local"), partial(self.check, "full")
        )

    def check(self, phase):
        self.events.append(phase)
        self.counts[phase] += 1
        if phase == "local" and self.counts[phase] == self.drift_at:
            self.changed = True
        error = self.failures.get((phase, self.counts[phase]))
        if phase == "full" and self.changed:
            error = self.drift_error
        if error is not None:
            raise error


def review_bytes(case, content):
    """沿原 JSONL 算法逐字段定义预期，不使用产品编码器或序列化输出。"""
    core, text = case.core, case.diff_text
    summary = {
        "record_type": "summary",
        "spec_version": "harnessix.product-git-action-review/v1",
        "core_fingerprint": core.fingerprint,
        "delivery_id": str(core.delivery_id),
        "call": json_facts(core.call),
        "workspace_revision": core.baseline.source.workspace.revision,
        "base_commit_oid": core.baseline.head_oid,
        "target_tree_oid": core.object_scope.roots.target_tree.object_id,
        "file_count": len(content.entries),
        "diff_utf8_bytes": len(text.encode("utf-8")),
        "diff_sha256": sha256(text.encode("utf-8")).hexdigest(),
        "complete": True,
    }
    records = (
        [summary]
        + [
            {"record_type": "entry", "index": index, "entry": json_facts(entry)}
            for index, entry in enumerate(content.entries)
        ]
        + [
            {"record_type": "text", "sequence": index, "text": text[offset : offset + 1900]}
            for index, offset in enumerate(range(0, len(text), 1900))
        ]
    )
    return b"".join(canonical(record) + b"\n" for record in records)


@pytest.fixture(
    params=("approved", "denied", "cancelled", "review_build", "review_encode", "review_decode")
)
def port(case, tmp_path, request):
    body = None
    if request.param in {"approved", "denied", "cancelled"}:
        operation = partial(build_git_decision_link_sources, decision_fixture(case, request.param))
        render = partial(encode_product_git_decision_link, checkpoint=lambda: None)
    else:
        review, verified = material_case(case.cas, tmp_path / "review", depth=1)
        document = build_product_git_action_review(
            review.core, verified.diff, checkpoint=lambda: None
        )
        body = review_bytes(review, verified.diff.content)
        operation = {
            "review_build": partial(build_product_git_action_review, review.core, verified.diff),
            "review_encode": partial(encode_product_git_action_review, document),
            "review_decode": partial(decode_product_git_action_review, body),
        }[request.param]
        render = (
            (lambda value: value)
            if request.param == "review_encode"
            else partial(encode_product_git_action_review, checkpoint=lambda: None)
        )
    legacy = Trace()
    expected = operation(checkpoint=partial(legacy.check, "full"))
    if body is None:
        assert expected.fact_kind == request.param
        body = canonical(json_facts(expected))
    assert render(expected) == body
    return operation, expected, body, render, legacy.counts["full"]


def assert_output(port, result):
    _, expected, body, render, _ = port
    assert result == expected
    assert render(result) == body


async def test_full_boundaries_and_same_task_thread_local_frequency(port):
    operation, _, _, _, calls = port
    trace = Trace()
    assert_output(port, operation(checkpoint=trace.control))
    assert calls > 10
    assert trace.counts == {"full": 2, "local": calls + 1}
    assert trace.events == ["full"] + ["local"] * (calls + 1) + ["full"]


@pytest.mark.parametrize("failure", [ValueError, KernelError, TurnCancelled, OSError])
@pytest.mark.parametrize("position", ["beginning", "middle", "end"])
def test_bounded_local_first_failure_identity_even_with_exit_drift(port, failure, position):
    operation, _, _, _, calls = port
    stop = {"beginning": 1, "middle": (calls + 1) // 2, "end": calls + 1}[position]
    error = KernelError("git_first", "首失败") if failure is KernelError else failure("首失败")
    trace, result = Trace(), None
    trace.failures["local", stop] = error
    trace.drift_at = stop
    with pytest.raises(failure) as caught:
        result = operation(checkpoint=trace.control)
    assert caught.value is error and caught.value.__cause__ is None
    assert result is None and trace.counts == {"full": 1, "local": stop}
    assert trace.events == ["full"] + ["local"] * stop


@pytest.mark.parametrize("failure", [ValueError, KernelError, TurnCancelled, OSError])
@pytest.mark.parametrize("boundary", [1, 2], ids=["entry", "exit"])
def test_boundary_failure_preserves_identity_without_result(port, failure, boundary):
    operation, _, _, _, calls = port
    error = KernelError("git_edge", "认证失败") if failure is KernelError else failure("认证失败")
    trace, result = Trace(), None
    trace.failures["full", boundary] = error
    with pytest.raises(failure) as caught:
        result = operation(checkpoint=trace.control)
    assert caught.value is error and caught.value.__cause__ is None and result is None
    locals_ = 0 if boundary == 1 else calls + 1
    assert trace.counts == {"full": boundary, "local": locals_}
    assert trace.events == ["full"] + (["local"] * locals_ + ["full"] if boundary == 2 else [])


def test_exit_drift_rejects_computed_result_with_original_failure(port):
    operation, _, _, _, calls = port
    trace, result = Trace(), None
    trace.drift_at = (calls + 1) // 2
    with pytest.raises(KernelError) as caught:
        result = operation(checkpoint=trace.control)
    assert caught.value is trace.drift_error and caught.value.__cause__ is None
    assert result is None and trace.counts == {"full": 2, "local": calls + 1}
    assert trace.events == ["full"] + ["local"] * (calls + 1) + ["full"]


@pytest.mark.parametrize("surface", ["foreign_task", "foreign_thread"])
async def test_exact_control_foreign_owner_keeps_full_fallback(port, surface):
    operation, _, _, _, calls = port
    trace = Trace()

    async def child():
        return operation(checkpoint=trace.control)

    if surface == "foreign_task":
        result = await asyncio.create_task(child())
    else:
        result = await asyncio.to_thread(operation, checkpoint=trace.control)
    assert_output(port, result)
    assert trace.counts == {"full": calls + 2, "local": 0}
    assert trace.events == ["full"] * (calls + 2)


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass"])
def test_unknown_callbacks_keep_legacy_trajectory_and_exact_output(port, kind):
    operation, _, _, _, calls = port
    trace = Trace()

    def forbidden_pure():
        pytest.fail("未知回调不得自动进入分层纯段")

    def function():
        trace.control()

    function.pure = forbidden_pure

    class Proxy:
        pure = staticmethod(forbidden_pure)

        def __call__(self):
            trace.control()

        def __getattr__(self, name):
            return getattr(trace.control, name)

    class Subclass(GitAuthenticationControl):
        pure = staticmethod(forbidden_pure)

    checkpoint = {
        "function": function,
        "proxy": Proxy(),
        "subclass": Subclass(partial(trace.check, "local"), partial(trace.check, "full")),
    }[kind]
    assert_output(port, operation(checkpoint=checkpoint))
    assert trace.counts == {"full": calls, "local": 0}
    assert trace.events == ["full"] * calls
