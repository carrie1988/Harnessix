"""边界只拥有自己创建的运输标记；同码、嵌套和陌生原错误不能被解包。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from harnessix.agent.cancellation import TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.product_config import (
    git_native_control,
    git_prepared_link_proof,
    git_user_observation,
)
from harnessix.workspace.native_observation_io import UpstreamCheckpointError


def _error(kind):
    if kind == "nested":
        return UpstreamCheckpointError(UpstreamCheckpointError(OSError("original")))
    if kind == "cancel":
        return TurnCancelled()
    if kind == "task":
        return asyncio.CancelledError()
    if kind == "same_code":
        return KernelError("workspace_patch_source_not_owned", "control, not ownership")
    return TimeoutError("original deadline")


@pytest.mark.parametrize("kind", ["nested", "cancel", "task", "same_code", "deadline"])
@pytest.mark.parametrize("phase", ["full", "local", "foreign_body"])
async def test_boundary_unwraps_own_layer_only_and_keeps_first_object(kind, phase):
    error, trace = _error(kind), []

    def local():
        trace.append("local")
        if phase == "local":
            raise error

    def full():
        trace.append("full")
        if phase == "full":
            raise error

    original = GitAuthenticationControl(local, full)
    binding = original._origin
    with pytest.raises(BaseException) as caught:
        with git_native_control.git_checkpoint_boundary(original) as control:
            if phase == "foreign_body":
                raise error
            with control.io_progress() as progress:
                progress()
    assert caught.value is error
    assert original._origin is binding
    assert trace == {"full": ["full"], "local": ["full", "local"], "foreign_body": []}[phase]


@pytest.mark.parametrize("kind", ["function", "proxy", "subclass", "foreign_task"])
async def test_unknown_controls_keep_original_full_trace_and_binding(kind):
    trace = []

    class Proxy:
        def __call__(self):
            trace.append("full")

    class Subclass(GitAuthenticationControl):
        pass

    parent = GitAuthenticationControl(lambda: trace.append("local"), lambda: trace.append("full"))
    checkpoint = {
        "function": lambda: trace.append("full"),
        "proxy": Proxy(),
        "subclass": Subclass(lambda: trace.append("local"), lambda: trace.append("full")),
        "foreign_task": parent,
    }[kind]
    binding = parent._origin

    async def read():
        with git_native_control.git_checkpoint_boundary(checkpoint) as control:
            assert type(control) is not GitAuthenticationControl
            for _ in range(7):
                control()

    if kind == "foreign_task":
        await asyncio.create_task(read())
    else:
        await read()
    assert trace == ["full"] * 7 and parent._origin is binding


@pytest.mark.parametrize("field", ["_local_check", "_authenticate", "_task", "_thread"])
async def test_original_binding_drift_is_rejected_before_local_or_full(field):
    parent = GitAuthenticationControl(lambda: pytest.fail("drift Local"), lambda: None)
    with pytest.raises(KernelError) as caught:
        with git_native_control.git_checkpoint_boundary(parent) as control:
            setattr(parent, field, object())
            control()
    assert caught.value.code == "git_authentication_control_invalid"


async def test_saved_first_marker_is_owned_after_later_caught_checkpoint_failure():
    errors = [OSError("first"), TimeoutError("cleanup")]
    iterator = iter(errors)

    def checkpoint():
        raise next(iterator)

    with pytest.raises(OSError) as caught:
        with git_native_control.git_checkpoint_boundary(checkpoint) as control:
            try:
                control()
            except UpstreamCheckpointError as first:
                with pytest.raises(UpstreamCheckpointError):
                    control()
                raise first
    assert caught.value is errors[0]


def _actual_source_transport_blocks():
    blocks = []
    for module in (git_user_observation, git_prepared_link_proof):
        tree = ast.parse(Path(module.__file__).read_text())
        blocks.extend(
            (module, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.With)
            and any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "git_checkpoint_boundary"
                for item in node.items
            )
        )
    return blocks


@pytest.mark.parametrize("index", range(6))
@pytest.mark.parametrize("phase", ["full", "local", "foreign_body"])
@pytest.mark.parametrize("kind", ["nested", "cancel", "task", "same_code", "deadline"])
async def test_six_actual_transport_blocks_preserve_first_error(index, phase, kind):
    """覆盖五处原 Source 和新增 Route AST 边界；隔离调用不是认证 SDK。"""
    blocks = _actual_source_transport_blocks()
    assert len(blocks) == 6
    module, block = blocks[index]
    error = _error(kind)

    def local():
        if phase == "local":
            raise error

    def full():
        if phase == "full":
            raise error

    def read(*args, checkpoint=None, **kwargs):
        control = checkpoint if checkpoint is not None else args[2]
        if phase == "foreign_body":
            raise error
        if phase == "full":
            control()
        else:
            with control.io_progress() as progress:
                progress()

    async def async_read(*args, **kwargs):
        return read(*args, **kwargs)

    control = GitAuthenticationControl(local, full)
    namespace = {
        **vars(module),
        "check": control,
        "checkpoint": control,
        "route_id": object(),
        "actual": SimpleNamespace(thread=object()),
        "history": SimpleNamespace(thread=object()),
        "link": SimpleNamespace(
            plan=SimpleNamespace(core=SimpleNamespace(baseline=SimpleNamespace(source=object())))
        ),
        "source": SimpleNamespace(workspace=object()),
        "verify_git_delivery_source": read,
        "collect_git_delivery_source": read,
        "_collect_baseline_from_source": async_read,
        "_verify_final_snapshot": read,
        **dict.fromkeys(
            (
                "transactions",
                "snapshot_ports",
                "ports",
                "targets",
                "reader",
                "cancel",
                "deadline",
                "root",
            )
        ),
        "router": SimpleNamespace(status=read),
        "core_store": SimpleNamespace(store=object()),
    }
    wrapper = ast.AsyncFunctionDef(
        name="actual_boundary",
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[block],
        decorator_list=[],
    )
    tree = ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[]))
    exec(compile(tree, module.__file__, "exec"), namespace)
    with pytest.raises(BaseException) as caught:
        await namespace[wrapper.name]()
    assert caught.value is error
