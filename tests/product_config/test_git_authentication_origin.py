"""创建期绑定的 source-level mechanics；控制／资源替身不证明实际 SDK 来源。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from harnessix.agent.errors import KernelError
from harnessix.delivery.git_authentication_control import (
    GitAuthenticationControl,
    pure_git_authentication,
)
from harnessix.delivery.git_material_cas import GitMaterialCAS
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config.git_checkpoint_materials import _material_control
from harnessix.product_config.git_delivery_process import GitDeliveryProcess
from harnessix.product_config.git_process_host import GitProcessRuntimeHost
from harnessix.workspace.native_observation_io import UpstreamCheckpointError

ORIGIN_INVALID = "git_authentication_control_invalid"
MATERIAL_INVALID = "git_checkpoint_materials_invalid"
ORIGIN_FIELDS = ("_task", "_thread", "_local_check", "_authenticate")


class _Probe:
    def __init__(self):
        self.events = []
        self.on_authenticate = None
        self.control = GitAuthenticationControl(self.local, self.authenticate)

    def local(self):
        self.events.append("local")

    def authenticate(self):
        self.events.append("full")
        if self.on_authenticate is not None:
            self.on_authenticate()

    def forbidden(self, *args, **kwargs):
        self.events.append("replaced")
        raise AssertionError("replacement must not execute")

    def replace(self, field):
        replacement = {
            "_task": object(),
            "_thread": self.control._thread + 1,
            "_local_check": self.forbidden,
            "_authenticate": self.forbidden,
        }[field]
        setattr(self.control, field, replacement)


def test_instance_pure_shadow_cannot_replace_the_original_control_entry():
    probe = _Probe()

    @contextmanager
    def replaced():
        yield probe.forbidden

    probe.control.__dict__["pure"] = replaced
    assert _pure_result(probe) == "complete"
    assert probe.events == ["full", "local", "local", "full"]


def test_foreign_full_delegate_ignores_instance_call_shadow():
    async def run():
        probe = _Probe()
        probe.control.__dict__["__call__"] = probe.forbidden

        async def foreign():
            with pure_git_authentication(probe.control) as check:
                check()

        await asyncio.create_task(foreign())
        assert probe.events == ["full"] * 3

    asyncio.run(run())


def test_segment_collision_key_is_rejected_without_executing_equality():
    probe = _Probe()
    invoked = []

    class Name(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            invoked.append(True)
            raise AssertionError("token lookup must not invoke key equality")

    with pytest.raises(KernelError) as failure:
        with pure_git_authentication(probe.control) as check:
            attributes = probe.control.__dict__.copy()
            segment = probe.control._segment
            attributes.pop("_segment", None)
            attributes[Name("_segment")] = segment
            probe.control.__dict__ = attributes
            check()
    assert failure.value.code == ORIGIN_INVALID and invoked == []
    assert probe.control._segment is None


def test_segment_dict_deletion_does_not_mask_first_body_error():
    probe = _Probe()
    marker = ValueError("original body")
    with pytest.raises(ValueError) as failure:
        with pure_git_authentication(probe.control):
            probe.control.__dict__.pop("_segment", None)
            raise marker
    assert failure.value is marker and probe.control._segment is None
    assert probe.events == ["full"]


@pytest.mark.parametrize("phase", ("full", "pure-leaf", "material-leaf"))
async def test_sparse_declaration_rejects_keys_before_copy_can_execute_equality(phase, tmp_path):
    probe = _Probe()
    calls, armed = [], [False]

    class Name(str):
        def __hash__(self):
            return 17

        def __eq__(self, other):
            if armed[0]:
                calls.append("equality")
                raise AssertionError("sparse dictionary copy must not invoke key equality")
            return str.__eq__(self, other)

    def replace_dictionary():
        attributes = probe.control.__dict__.copy()
        attributes.update({Name("bad-one"): None, Name("bad-two"): None})
        for index in range(100):
            attributes[f"filler-{index}"] = None
        for index in range(100):
            del attributes[f"filler-{index}"]
        probe.control.__dict__ = attributes
        armed[0] = True

    with pytest.raises(KernelError) as failure:
        if phase == "pure-leaf":
            with pure_git_authentication(probe.control) as check:
                replace_dictionary()
                check()
        elif phase == "material-leaf":
            material = _bind_material(probe, tmp_path)
            replace_dictionary()
            material._local_check()
        else:
            replace_dictionary()
            probe.control()
    assert failure.value.code == (MATERIAL_INVALID if phase == "material-leaf" else ORIGIN_INVALID)
    assert calls == [] and probe.control._segment is None


@pytest.mark.parametrize("write", ("attribute", "dictionary"))
def test_revoked_token_cannot_be_restored_through_declared_fields(write):
    probe = _Probe()
    with pytest.raises(KernelError) as failure:
        with pure_git_authentication(probe.control) as retired:
            retired()
            token = probe.control._segment
            probe.control()
            assert probe.control._segment is None
            if write == "attribute":
                probe.control._segment = token
            else:
                probe.control.__dict__["_segment"] = token
            retired()
    assert failure.value.code == ORIGIN_INVALID
    assert probe.control._segment is None
    assert probe.events == ["full", "local", "full"]


def _bind_material(probe, root):
    # 只进入绑定／局部闭包：不初始化 Store，不调用 Host、CAS、基线或进程。
    protection = object()
    host = object.__new__(GitProcessRuntimeHost)
    object.__setattr__(host, "protection", protection)
    port = object.__new__(GitDeliveryProcess)
    port._runtime_host = host
    port._runner = probe.forbidden
    port._state = root
    port._output_redaction = protection
    port._closed = False
    store = object.__new__(SQLiteWorkspaceTransactionStore)
    store._root = root / "workspace-transactions"
    store._closed = False
    return _material_control(
        port,
        root,
        probe.forbidden,
        GitMaterialCAS(store),
        SimpleNamespace(checkpoint=lambda: None),
        SimpleNamespace(remaining=lambda: None),
        probe.control,
    )


def _pure_result(probe):
    with pure_git_authentication(probe.control) as check:
        check()
    return "complete"


@pytest.mark.parametrize("field", ORIGIN_FIELDS)
@pytest.mark.parametrize(
    "phase", ("before-pure", "before-material", "pure-leaf", "material-leaf", "exit-full")
)
def test_origin_field_replacement_rejects_before_replaced_callback(field, phase, tmp_path):
    async def run():
        probe = _Probe()
        delivered = []
        expected_code = MATERIAL_INVALID if phase == "material-leaf" else ORIGIN_INVALID
        with pytest.raises(KernelError) as failure:
            if phase == "before-pure":
                probe.replace(field)
                delivered.append(_pure_result(probe))
            elif phase == "before-material":
                probe.replace(field)
                delivered.append(_bind_material(probe, tmp_path))
            elif phase == "pure-leaf":
                with pure_git_authentication(probe.control) as check:
                    probe.replace(field)
                    check()
                delivered.append("complete")
            elif phase == "material-leaf":
                material = _bind_material(probe, tmp_path)
                material._local_check()
                assert probe.events == ["local"]
                probe.events.clear()
                probe.replace(field)
                material._local_check()
                delivered.append("complete")
            else:

                def replace_on_exit():
                    if probe.events.count("full") == 2:
                        probe.replace(field)

                probe.on_authenticate = replace_on_exit
                delivered.append(_pure_result(probe))
        assert failure.value.code == expected_code
        assert delivered == []
        assert "replaced" not in probe.events
        expected_events = {
            "before-pure": [],
            "before-material": [],
            "pure-leaf": ["full"],
            "material-leaf": [],
            "exit-full": ["full", "local", "local", "full"],
        }
        assert probe.events == expected_events[phase]
        assert probe.control._segment is None

    asyncio.run(run())


class _ExecutableDict(dict):
    def copy(self):
        raise AssertionError("non-exact dict copy must not execute")

    def __iter__(self):
        raise AssertionError("non-exact dict iteration must not execute")


class _ExecutableKey(str):
    armed = False

    def __hash__(self):
        if self.armed:
            raise AssertionError("non-exact key hash must not execute")
        return str.__hash__(self)

    def __eq__(self, other):
        if self.armed:
            raise AssertionError("non-exact key equality must not execute")
        return str.__eq__(self, other)


@pytest.mark.parametrize("shape", ("dict-subclass", "str-subclass-key", "integer-key"))
def test_origin_requires_non_executable_exact_dict_and_string_keys(shape):
    probe = _Probe()
    if shape == "dict-subclass":
        probe.control.__dict__ = _ExecutableDict(probe.control.__dict__)
    elif shape == "str-subclass-key":
        key = _ExecutableKey("unexpected")
        probe.control.__dict__[key] = None
        key.armed = True
    else:
        probe.control.__dict__[7] = None
    with pytest.raises(KernelError) as failure:
        _pure_result(probe)
    assert failure.value.code == ORIGIN_INVALID
    assert probe.events == []


def test_instance_binding_shadow_cannot_bypass_control_or_material_guard(tmp_path):
    async def run():
        for phase in ("full", "pure-leaf", "before-material", "material-leaf"):
            probe = _Probe()
            material = _bind_material(probe, tmp_path) if phase == "material-leaf" else None

            def corrupt(current):
                current.control.__dict__["_binding"] = current.forbidden
                current.replace("_local_check")

            with pytest.raises(KernelError) as failure:
                if phase == "pure-leaf":
                    with pure_git_authentication(probe.control) as check:
                        corrupt(probe)
                        check()
                else:
                    corrupt(probe)
                    if phase == "full":
                        probe.control()
                    elif phase == "before-material":
                        _bind_material(probe, tmp_path)
                    else:
                        material._local_check()
            expected = MATERIAL_INVALID if phase == "material-leaf" else ORIGIN_INVALID
            assert failure.value.code == expected
            assert probe.events == (["full"] if phase == "pure-leaf" else [])

    asyncio.run(run())


def test_readonly_origin_assignment_inside_segment_rejects_and_retains_record():
    probe = _Probe()
    original = probe.control._origin
    with pytest.raises(KernelError) as failure:
        with pure_git_authentication(probe.control):
            probe.control._origin = tuple(list(original))
    assert failure.value.code == ORIGIN_INVALID
    assert probe.control._origin is original
    assert probe.control._segment is None
    assert probe.events == ["full"]


def test_foreign_task_forged_whole_dict_and_origin_cannot_promote_owner(tmp_path):
    async def run():
        probe = _Probe()
        original = probe.control._origin

        async def foreign():
            assert asyncio.current_task() is not original[2]
            forged = probe.control.__dict__.copy()
            forged.update(
                _task=asyncio.current_task(),
                _local_check=probe.forbidden,
                _authenticate=probe.forbidden,
            )
            forged["_origin"] = tuple(
                forged[field] for field in ("_local_check", "_authenticate", "_task", "_thread")
            )
            probe.control.__dict__ = forged
            delivered = []
            for operation in (lambda: _pure_result(probe), lambda: _bind_material(probe, tmp_path)):
                with pytest.raises(KernelError) as failure:
                    delivered.append(operation())
                assert failure.value.code == ORIGIN_INVALID
            assert delivered == []
            assert probe.control._origin is original

        await asyncio.create_task(foreign())
        assert probe.events == []

    asyncio.run(run())


@pytest.mark.parametrize("phase", ("entry", "exit"))
@pytest.mark.parametrize(
    "error_kind", ("os", "value", "kernel", "timeout", "upstream", "nested-upstream", "task-cancel")
)
def test_full_callback_metadata_mutation_then_failure_preserves_first_object(phase, error_kind):
    probe = _Probe()
    original = ValueError("original callback")
    upstream = UpstreamCheckpointError(original)
    marker = {
        "os": OSError("original callback"),
        "value": original,
        "kernel": KernelError("original_callback", "original callback"),
        "timeout": TimeoutError("original callback"),
        "upstream": upstream,
        "nested-upstream": UpstreamCheckpointError(upstream),
        "task-cancel": asyncio.CancelledError("original callback"),
    }[error_kind]
    fail_at = 1 if phase == "entry" else 2

    def mutate_and_fail():
        if probe.events.count("full") == fail_at:
            probe.replace("_authenticate")
            raise marker

    probe.on_authenticate = mutate_and_fail
    delivered = []
    with pytest.raises(type(marker)) as failure:
        delivered.append(_pure_result(probe))
    assert failure.value is marker
    if error_kind == "upstream":
        assert failure.value.error is original
    elif error_kind == "nested-upstream":
        assert failure.value.error is upstream
        assert upstream.error is original
    assert delivered == []
    assert probe.events.count("full") == fail_at
    assert "replaced" not in probe.events
    assert probe.control._segment is None


@pytest.mark.parametrize("where", ("saved", "foreign-task", "foreign-thread"))
def test_retired_or_foreign_checkpoints_degrade_to_original_full_trace(where):
    async def run():
        probe = _Probe()
        with pure_git_authentication(probe.control) as saved:
            saved()
            if where != "saved":

                def foreign():
                    saved()
                    with pure_git_authentication(probe.control) as check:
                        check()

                if where == "foreign-task":

                    async def child():
                        foreign()

                    await asyncio.create_task(child())
                else:
                    await asyncio.to_thread(foreign)
                assert probe.control._segment is None
                saved()
        if where == "saved":
            saved()
            assert probe.events == ["full", "local", "local", "full", "full"]
        else:
            assert probe.events == ["full", "local"] + ["full"] * 7
        assert probe.control._segment is None

    asyncio.run(run())


@pytest.mark.parametrize("kind", ("function", "proxy", "subclass"))
def test_unknown_callable_or_subclass_keeps_full_calls_without_custom_pure(kind):
    probe = _Probe()

    def full():
        probe.control()

    class Proxy:
        __call__ = staticmethod(full)
        pure = staticmethod(probe.forbidden)

    class Subclass(GitAuthenticationControl):
        pure = probe.forbidden

    full.pure = probe.forbidden
    checkpoint = {
        "function": full,
        "proxy": Proxy(),
        "subclass": Subclass(probe.local, probe.authenticate),
    }[kind]
    with pure_git_authentication(checkpoint) as check:
        assert check is checkpoint
        check()
        check()
    assert probe.events == ["full", "full"]
