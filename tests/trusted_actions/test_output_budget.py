"""序列化前的字节、深度、节点、原生类型、复制、取消与同步时限回归。"""

from __future__ import annotations

import json
from time import monotonic

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.trusted_actions.output_budget import ActionOutputBudget, bounded_projection


def bounded(value, **limits):
    return bounded_projection(
        value, budget=ActionOutputBudget(**limits), cancel=CancelToken(), deadline=monotonic() + 5
    )


@pytest.mark.parametrize(
    "value",
    [None, True, False, 0, -1, 1.5, "", "中文", '\x00\n"\\', [], {}, {"键": ["中文", True, None]}],
)
async def test_exact_utf8_and_escaping_byte_boundary(value):
    size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
    assert bounded(value, max_bytes=size) == value
    if size > 1:
        with pytest.raises(KernelError) as caught:
            bounded(value, max_bytes=size - 1)
        assert caught.value.code == "trusted_action_output_limit"


@pytest.mark.parametrize("value", [[], [1], {"x": 1}, {"x": [1, 2]}])
def test_exact_node_boundary(value):
    expected = {str([]): 1, str([1]): 2, str({"x": 1}): 3, str({"x": [1, 2]}): 5}[str(value)]
    assert bounded(value, max_nodes=expected) == value
    if expected > 1:
        with pytest.raises(KernelError) as caught:
            bounded(value, max_nodes=expected - 1)
        assert caught.value.code == "trusted_action_output_limit"


def test_depth_and_wide_containers_are_rejected_before_expansion():
    assert bounded([[0]], max_depth=3) == [[0]]
    with pytest.raises(KernelError, match="资源上限"):
        bounded([[0]], max_depth=2)
    with pytest.raises(KernelError, match="资源上限"):
        bounded([None] * 200000)


@pytest.mark.parametrize(
    "value,code",
    [
        ("x" * (1024 * 1024 + 1), "limit"),
        (1 << 128, "limit"),
        (-(1 << 128), "limit"),
        (float("nan"), "mismatch"),
        (float("inf"), "mismatch"),
        ("\ud800", "mismatch"),
        (b"bytes", "mismatch"),
        ((1, 2), "mismatch"),
        ({1: "key"}, "mismatch"),
        (object(), "mismatch"),
    ],
)
def test_unsupported_native_shapes_fail_closed(value, code):
    with pytest.raises(KernelError) as caught:
        bounded(value)
    assert caught.value.code == f"trusted_action_output_{code}"


def test_128_bit_integer_and_shared_subtree_are_valid_independent_copy():
    shared = [1, (1 << 128) - 1]
    original = {"a": shared, "b": shared}
    cloned = bounded(original)
    assert cloned == original and cloned is not original
    shared.clear()
    assert cloned["a"] == cloned["b"] == [1, (1 << 128) - 1]
    assert cloned["a"] is not cloned["b"]


@pytest.mark.parametrize("kind", ["dict", "list"])
def test_cycles_are_rejected_without_recursive_serialization(kind):
    value = {} if kind == "dict" else []
    if kind == "dict":
        value["self"] = value
    else:
        value.append(value)
    with pytest.raises(KernelError) as caught:
        bounded(value)
    assert caught.value.code == "trusted_action_output_mismatch"


@pytest.mark.parametrize("base", [dict, list, str, int, float])
def test_subclass_callbacks_are_never_invoked(base):
    class Adversarial(base):
        def __len__(self):
            pytest.fail("不应调用扩展对象")

        def items(self):
            pytest.fail("不应遍历扩展对象")

        def __iter__(self):
            pytest.fail("不应遍历扩展对象")

        def __str__(self):
            pytest.fail("不应字符串化扩展对象")

    value = Adversarial() if base in {dict, list} else Adversarial(1)
    with pytest.raises(KernelError) as caught:
        bounded(value)
    assert caught.value.code == "trusted_action_output_mismatch"


def test_sync_deadline_and_cancel_checkpoint():
    with pytest.raises(KernelError) as caught:
        bounded_projection(
            {}, budget=ActionOutputBudget(), cancel=CancelToken(), deadline=monotonic() - 1
        )
    assert caught.value.code == "trusted_action_output_timeout"
    cancel = CancelToken()
    cancel.cancel()
    with pytest.raises(TurnCancelled):
        bounded_projection({}, budget=ActionOutputBudget(), cancel=cancel, deadline=monotonic() + 1)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_bytes": 1024 * 1024 + 1},
        {"max_nodes": 10257},
        {"max_depth": 65},
        {"timeout_seconds": 0.0},
        {"timeout_seconds": float("inf")},
        {"max_bytes": True},
        {"max_nodes": "100"},
    ],
)
def test_budget_contract_cannot_disable_host_caps(limits):
    with pytest.raises(ValidationError):
        ActionOutputBudget(**limits)


@pytest.mark.parametrize("new_request", [False, True])
async def test_projection_scope_ignores_consumed_cancel_but_detects_new_request(new_request):
    import asyncio

    from harnessix.trusted_actions.output_budget import projection_checkpointer

    async def operation():
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert task.cancelling() == 1
        checkpoint = projection_checkpointer(CancelToken(), monotonic() + 5)
        checkpoint()
        assert bounded({"summary": "confirmed"}) == {"summary": "confirmed"}
        if new_request:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                checkpoint()
            # 交付取消信号而不调用uncancel，不把同步检查误当作事件循环已经消费。
            with pytest.raises(asyncio.CancelledError):
                await asyncio.sleep(0)
            assert task.cancelling() == 2
        else:
            await asyncio.sleep(0)
            checkpoint()

    await asyncio.create_task(operation())
