"""认证Source2用户仓库只读观察；物理Index、完整配置、代际与控制边界验收。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.delivery.contracts import MAX_TRANSACTION_FILE_BYTES
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.execution.contracts import canonical_digest
from harnessix.processes.capture import CaptureProtocol
from harnessix.product_config.git_baseline import collect_product_git_baseline
from harnessix.product_config.git_delivery_plan_contracts import GitIndexFileObservation
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_parent_contracts import (
    ProductGitDeliveryBaselineV2,
    ProductGitDeliverySourceV2,
)
from harnessix.product_config.runtime import environment_secret_provider
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.git import GitReadRuntime
from harnessix.workspace.snapshot import _digest as revision_digest
from harnessix.workspace.snapshot import _open_native
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from tests.product_config.test_git_baseline import (
    _published_stdout,
    command,
)
from tests.product_config.test_server_and_cli import CANARY
from tests.support.git_user_observation import run_authenticated_observation
from tests.tools.test_git import _git


def native_identity(path):
    """独立打开原NativeRoot核验身份，不调用受测模块的身份辅助方法。"""
    native = _open_native(path.parent, "windows" if os.name == "nt" else "posix")
    try:
        return revision_digest(native.observe(path.name, access="read").identity)
    finally:
        native.close()


def path_sha256(path):
    text = str(path.resolve(strict=True))
    return hashlib.sha256(
        (os.path.normcase(text) if os.name == "nt" else text).encode()
    ).hexdigest()


def directory_identity(path):
    """独立按原目录metadata合同重算，不误用Index的完整revision身份。"""
    info = path.lstat()
    return canonical_digest(
        {
            "path": os.path.normcase(str(path.resolve()))
            if os.name == "nt"
            else str(path.resolve()),
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode_type": stat.S_IFMT(info.st_mode),
        }
    )


def assert_no_disclosure(error, scenario, caplog, capsys):
    exposed = str(error) + repr(error) + caplog.text
    captured = capsys.readouterr()
    exposed += captured.out + captured.err
    assert CANARY not in exposed
    assert str(scenario.root) not in exposed


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_authenticated_fixture_is_real_source2_not_a_constructed_positive(
    tmp_path, config, monkeypatch, object_format
):
    async def inspect(scenario):
        before = scenario.unchanged_state()
        index = (scenario.root / ".git/index").read_bytes()
        actual = await collect_product_git_baseline(
            scenario.thread,
            scenario.targets,
            scenario.router,
            scenario.transactions,
            scenario.reader,
            cancel=CancelToken(),
            snapshot_ports=scenario.router._snapshot_ports,
        )
        assert type(actual) is ProductGitDeliveryBaselineV2
        assert type(actual.source) is ProductGitDeliverySourceV2
        assert len(actual.head_oid) == (40 if object_format == "sha1" else 64)
        assert (scenario.root / ".git/index").read_bytes() == index
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(
        tmp_path, config, monkeypatch, inspect, object_format=object_format
    )


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_authenticated_continuous_dirty_observation_and_reopen_are_read_only(
    tmp_path, config, monkeypatch, object_format
):
    previous = []

    async def inspect(scenario):
        root, port = scenario.root, scenario.reader
        before, physical = scenario.unchanged_state(), (root / ".git/index").read_bytes()
        index_identity, expected_directory_identity = (
            native_identity(root / ".git/index"),
            directory_identity(root / ".git"),
        )
        head = command(root, "rev-parse", "HEAD").strip().decode()
        original, config_results = port._run_baseline, []

        async def observed(arguments, *args, **kwargs):
            result = await original(arguments, *args, **kwargs)
            tail = arguments[len(port._global_arguments) :]
            if tail[0] == "config" and "--list" in tail and "--name-only" not in tail:
                body = result.full_stdout()
                assert b"Harnessix Test" in body and b"user.name" in body
                config_results.append(hashlib.sha256(body).hexdigest())
            return result

        with monkeypatch.context() as context:
            context.setattr(port, "_run_baseline", observed)
            actual = await scenario.collect()
            repeated = await scenario.collect()
        assert actual == repeated
        assert type(actual.baseline) is ProductGitDeliveryBaselineV2
        assert type(actual.baseline.source) is ProductGitDeliverySourceV2
        assert actual.baseline.head_oid == head
        assert len(head) == (40 if object_format == "sha1" else 64)
        assert tuple(patch.transaction_id for patch in actual.baseline.source.patches) == tuple(
            reversed(scenario.targets)
        )
        assert len(actual.baseline.source.workspace.resources) == 17
        assert {member.path for member in actual.baseline.members} == set(scenario.selected_paths)
        for mutation in actual.baseline.source.mutations:
            assert mutation.before.sha256 == hashlib.sha256(b"before\n").hexdigest()
            assert mutation.after.sha256 == hashlib.sha256(b"final\n").hexdigest()
        assert actual.common_directory_path_sha256 == path_sha256(root / ".git")
        assert actual.git_directory_path_sha256 == path_sha256(root / ".git")
        assert (
            actual.common_directory_identity
            == actual.git_directory_identity
            == expected_directory_identity
        )
        assert type(actual.index_file_observation) is GitIndexFileObservation
        assert actual.index_file_observation.presence == "file"
        assert actual.index_file_observation.sha256 == hashlib.sha256(physical).hexdigest()
        assert actual.index_file_observation.size == len(physical)
        assert actual.index_file_observation.identity == index_identity
        assert len(config_results) >= 4 and set(config_results) == {actual.config_sha256}
        assert actual.config_sha256 != actual.baseline.config_names_sha256
        assert len(actual.fingerprint) == 64 and set(actual.fingerprint) <= set("0123456789abcdef")
        assert len(actual.implementation_digest) == 64
        implementation_root = Path(collect_product_git_baseline.__code__.co_filename).parent
        assert actual.implementation_digest == canonical_digest(
            {
                name: hashlib.sha256((implementation_root / name).read_bytes()).hexdigest()
                for name in (
                    "git_user_observation.py",
                    "git_native_control.py",
                    "git_user_observation_contracts.py",
                    "git_user_observation_paths.py",
                    "git_user_source_files.py",
                    "git_user_source_scope.py",
                    "git_user_authority.py",
                    "git_baseline.py",
                    "git_delivery_source.py",
                    "workspace_patch_source.py",
                )
            }
        )
        assert type(actual.store_id) is type(actual.key_id) is UUID
        assert actual.store_id == scenario.session._publication._store_id
        assert actual.key_id == scenario.session._publication._key_id
        assert type(actual).model_validate_json(actual.model_dump_json()) == actual
        for field in type(actual).model_fields:
            if field in {"spec_version", "baseline"}:
                continue
            changed = actual.model_dump(mode="json")
            changed[field] = (
                str(UUID(int=0))
                if field in {"store_id", "key_id"}
                else (
                    {**changed[field], "sha256": "0" * 64}
                    if field == "index_file_observation"
                    else "0" * 64
                )
            )
            with pytest.raises(ValidationError):
                type(actual).model_validate(changed)
        assert CANARY not in repr(actual) and str(root) not in repr(actual)
        assert (root / ".git/index").read_bytes() == physical
        assert native_identity(root / ".git/index") == index_identity
        assert command(root, "rev-parse", "HEAD").strip().decode() == head
        assert command(root, "show", ":user.txt") == b"user staged\n"
        assert (root / "user.txt").read_bytes() == b"user unstaged\n"
        assert (root / "unstaged.txt").read_bytes() == b"unstaged change\n"
        assert (root / "untracked.txt").read_bytes() == b"untracked change\n"
        assert scenario.unchanged_state() == before
        if scenario.reopened:
            assert actual == previous[0]
        else:
            previous.append(actual)

    await run_authenticated_observation(
        tmp_path,
        config,
        monkeypatch,
        inspect,
        object_format=object_format,
        continuous=True,
        reopen=True,
        deep=True,
    )


async def test_missing_snapshot2_ports_cannot_fall_back_or_query_git(tmp_path, config, monkeypatch):
    async def inspect(scenario):
        async def forbidden(*_args, **_kwargs):
            pytest.fail("缺少完整Snapshot2端口不得查询Git或退回Snapshot1")

        monkeypatch.setattr(scenario.reader, "_run_baseline", forbidden)
        with pytest.raises(KernelError) as caught:
            await scenario.collect(snapshot_ports=None)
        assert caught.value.code == "git_user_observation_host_invalid"

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


async def test_real_legacy_patch1_cannot_be_promoted_to_user_source2(tmp_path, config, monkeypatch):
    async def inspect(scenario):
        assert (
            scenario.transactions.load(scenario.targets[0]).spec_version
            == "harnessix.workspace-transaction-record/v1"
        )

        async def forbidden(*_args, **_kwargs):
            pytest.fail("真实旧Patch1不得先访问Git后降级返回")

        monkeypatch.setattr(scenario.reader, "_run_baseline", forbidden)
        with pytest.raises(KernelError) as caught:
            await scenario.collect()
        assert caught.value.code == "workspace_closure_unavailable"

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect, legacy=True)


@pytest.mark.parametrize(
    "fault",
    [
        "session-parent",
        "transaction-parent",
        "audit-parent",
        "plans-parent",
        "session-file",
        "transaction-directory",
        "audit-file",
        "plans-file",
    ],
)
async def test_only_original_state_root_and_fixed_paths_are_accepted(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        root = scenario.state
        object_, attribute, path = {
            "session-parent": (scenario.session, "path", root / "other/sessions.db"),
            "transaction-parent": (
                scenario.transactions,
                "_root",
                root / "other/workspace-transactions",
            ),
            "audit-parent": (scenario.router._audit, "_path", root / "other/action-audit.db"),
            "plans-parent": (scenario.router._plans, "_path", root / "other/execution-plans.db"),
            "session-file": (scenario.session, "path", root / "foreign-sessions.db"),
            "transaction-directory": (
                scenario.transactions,
                "_root",
                root / "foreign-transactions",
            ),
            "audit-file": (scenario.router._audit, "_path", root / "foreign-audit.db"),
            "plans-file": (scenario.router._plans, "_path", root / "foreign-plans.db"),
        }[fault]

        async def forbidden(*_args, **_kwargs):
            pytest.fail("非原State根或固定路径不得读取Git")

        with monkeypatch.context() as context:
            context.setattr(object_, attribute, path)
            context.setattr(scenario.reader, "_run_baseline", forbidden)
            with pytest.raises(KernelError) as caught:
                await scenario.collect()
            assert caught.value.code == "git_user_observation_host_invalid"

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize(
    "fault",
    [
        "session-class",
        "unauthenticated-session",
        "readonly-reopen",
        "wrapped-write",
        "foreign-read",
    ],
)
async def test_authentication_and_actual_bound_store_methods_cannot_be_substituted(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        async def forbidden(*_args, **_kwargs):
            pytest.fail("伪装Session或CAS端口不得读取Git")

        with (
            monkeypatch.context() as context,
            SQLiteWorkspaceTransactionStore(scenario.transactions._root, read_only=True) as other,
        ):
            context.setattr(scenario.reader, "_run_baseline", forbidden)
            kwargs = {}
            if fault == "session-class":
                kwargs["session"] = SimpleNamespace(
                    path=scenario.session.path,
                    authenticated_thread_history=scenario.session.authenticated_thread_history,
                )
            elif fault == "unauthenticated-session":
                kwargs["session"] = SQLiteSessionStore(scenario.session.path)
            elif fault == "readonly-reopen":
                context.setattr(scenario, "transactions", other)
            else:
                ports = WorkspaceSnapshotPorts(
                    (lambda *args: scenario.transactions.put_blob(*args))
                    if fault == "wrapped-write"
                    else scenario.transactions.put_blob,
                    other.blob if fault == "foreign-read" else scenario.transactions.blob,
                )
                kwargs["snapshot_ports"] = ports
            with pytest.raises(KernelError) as caught:
                await scenario.collect(**kwargs)
            assert caught.value.code == "git_user_observation_host_invalid"

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


async def test_same_path_writable_reopen_is_not_the_original_router_cas(
    tmp_path, config, monkeypatch
):
    async def inspect(scenario):
        with (
            SQLiteWorkspaceTransactionStore(scenario.transactions._root) as replacement,
            monkeypatch.context() as context,
        ):
            assert replacement is not scenario.router._snapshot_ports.write_blob.__self__
            context.setattr(scenario, "transactions", replacement)
            ports = WorkspaceSnapshotPorts(replacement.put_blob, replacement.blob)
            with pytest.raises(KernelError) as caught:
                await scenario.collect(snapshot_ports=ports)
            assert caught.value.code == "git_user_observation_host_invalid"

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["none", "same-bytes-scope"])
async def test_reader_must_use_the_original_publication_scope(tmp_path, config, monkeypatch, fault):
    async def inspect(scenario):
        # 替身仅使用固定测试字符串，不读取原scope材料或Session签名密钥。
        provider = environment_secret_provider(
            config, environment={"PRIMARY_API_KEY": CANARY, "BACKUP_API_KEY": "backup-secret"}
        )
        with (
            SecretPublicationScope(
                tuple(item.credential for item in config.providers), provider
            ) as substitute,
            monkeypatch.context() as context,
        ):
            original = scenario.session._publication._events._protection
            assert substitute is not original
            context.setattr(
                scenario.reader, "_output_redaction", None if fault == "none" else substitute
            )

            async def forbidden(*_args, **_kwargs):
                pytest.fail("缺少原Publication Scope不得查询Git")

            context.setattr(scenario.reader, "_run_baseline", forbidden)
            with pytest.raises(KernelError) as caught:
                await scenario.collect()
            assert caught.value.code == "git_user_observation_host_invalid"

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize(
    "fault",
    [
        "publication",
        "publication-closed",
        "events",
        "events-closed",
        "events-key-id",
        "store-id",
        "key-id",
        "scope-closed",
        "scope",
        "reader-scope",
        "reader-root",
        "router-audit",
        "router-plans",
        "transaction-root",
    ],
)
async def test_host_identity_replacement_during_a_query_cannot_return_an_observation(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        provider = environment_secret_provider(
            config, environment={"PRIMARY_API_KEY": CANARY, "BACKUP_API_KEY": "backup-secret"}
        )
        original, entered = scenario.reader._run_baseline, []
        with (
            SecretPublicationScope(
                tuple(item.credential for item in config.providers), provider
            ) as substitute,
            monkeypatch.context() as context,
        ):

            async def changed(*args, **kwargs):
                result = await original(*args, **kwargs)
                if not entered:
                    object_, attribute, replacement = {
                        "publication": (scenario.session, "_publication", SimpleNamespace()),
                        "publication-closed": (scenario.session._publication, "_closed", True),
                        "events": (
                            scenario.session._publication,
                            "_events",
                            SimpleNamespace(
                                _protection=scenario.session._publication._events._protection,
                                _closed=False,
                                _key_id=scenario.session._publication._key_id,
                            ),
                        ),
                        "events-closed": (scenario.session._publication._events, "_closed", True),
                        "events-key-id": (
                            scenario.session._publication._events,
                            "_key_id",
                            UUID(int=1),
                        ),
                        "store-id": (scenario.session._publication, "_store_id", UUID(int=1)),
                        "key-id": (scenario.session._publication, "_key_id", UUID(int=1)),
                        "scope-closed": (
                            scenario.session._publication._events._protection,
                            "_closed",
                            True,
                        ),
                        "scope": (scenario.session._publication._events, "_protection", substitute),
                        "reader-scope": (scenario.reader, "_output_redaction", substitute),
                        "reader-root": (scenario.reader, "_root", tmp_path / "foreign-root"),
                        "router-audit": (
                            scenario.router,
                            "_audit",
                            SimpleNamespace(_path=scenario.router._audit._path),
                        ),
                        "router-plans": (
                            scenario.router,
                            "_plans",
                            SimpleNamespace(_path=scenario.router._plans._path),
                        ),
                        "transaction-root": (
                            scenario.transactions,
                            "_root",
                            scenario.state / "foreign-transactions",
                        ),
                    }[fault]
                    context.setattr(object_, attribute, replacement)
                    entered.append(True)
                return result

            context.setattr(scenario.reader, "_run_baseline", changed)
            with pytest.raises(KernelError) as caught:
                await scenario.collect()
            assert caught.value.code == "git_user_observation_host_invalid"
            assert entered

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("kind", [OSError, TimeoutError, KernelError])
async def test_late_checkpoint_exception_preserves_original_identity(
    tmp_path, config, monkeypatch, kind
):
    async def inspect(scenario):
        marker = (
            KernelError("checkpoint_sentinel", "固定检查点异常")
            if kind is KernelError
            else kind("固定检查点异常")
        )
        armed, original = [], scenario.reader._run_baseline

        def checkpoint():
            if armed:
                raise marker

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            armed.append(True)
            return result

        monkeypatch.setattr(scenario.reader, "_run_baseline", changed)
        with pytest.raises(kind) as caught:
            await scenario.collect(checkpoint=checkpoint)
        assert caught.value is marker and armed

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["wrong-purpose", "different-root"])
async def test_reader_fixed_recipe_and_original_workspace_cannot_be_substituted(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        root = scenario.root
        if fault == "different-root":
            root = tmp_path / "other-workspace"
            shutil.copytree(scenario.root, root)
        port = GitReadRuntime(
            root,
            _git(),
            state_directory=scenario.state / "git-read",
            for_delivery=fault != "wrong-purpose",
            output_redaction=scenario.session._publication._events._protection,
        )

        async def forbidden(*_args, **_kwargs):
            pytest.fail("错误固定配方或另一Workspace不得先查询Git")

        with monkeypatch.context() as context:
            context.setattr(scenario, "reader", port)
            context.setattr(port, "_run_baseline", forbidden)
            with pytest.raises(KernelError) as caught:
                await scenario.collect()
            assert caught.value.code == (
                "git_user_observation_host_invalid"
                if fault == "wrong-purpose"
                else "git_baseline_workspace_mismatch"
            )

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.skipif(os.name != "posix", reason="仅验证POSIX原NativeRoot控制异常身份")
@pytest.mark.parametrize("kind", [OSError, TimeoutError])
async def test_native_index_callback_preserves_the_original_control_exception(
    tmp_path, config, monkeypatch, kind
):
    from harnessix.workspace.snapshot import _PosixRoot

    async def inspect(scenario):
        marker, original, entered = kind("原Index检查点异常"), _PosixRoot.observe, []
        inside = False
        index, before = (scenario.root / ".git/index").read_bytes(), scenario.unchanged_state()

        def observed(native, path, *args, **kwargs):
            nonlocal inside
            target = native.path == scenario.root / ".git" and path == "index"
            if target:
                assert callable(kwargs.get("checkpoint"))
                inside = True
                entered.append(path)
            try:
                return original(native, path, *args, **kwargs)
            finally:
                inside = False

        def checkpoint():
            if inside:
                raise marker

        with monkeypatch.context() as context:
            context.setattr(_PosixRoot, "observe", observed)
            with pytest.raises(kind) as caught:
                await scenario.collect(checkpoint=checkpoint)
        assert caught.value is marker and entered == ["index"]
        assert (scenario.root / ".git/index").read_bytes() == index
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.skipif(os.name != "posix", reason="仅验证POSIX原目录逐项枚举控制")
@pytest.mark.parametrize("stage", ["root-binding", "source-final"])
@pytest.mark.parametrize("kind", [OSError, TimeoutError])
async def test_actual_root_enumeration_consumes_control_without_exception_conversion(
    tmp_path, config, monkeypatch, stage, kind
):
    from harnessix.product_config import git_baseline, git_delivery_source
    from harnessix.workspace import snapshot

    async def inspect(scenario):
        marker, entered = kind("原目录逐项检查点异常"), []
        module = git_baseline if stage == "root-binding" else git_delivery_source
        capture, enumeration = module.capture_snapshot_facts, snapshot.observe_directory
        in_capture, in_entry = False, False

        def controlled_capture(*args, **kwargs):
            nonlocal in_capture
            in_capture = True
            assert callable(kwargs.get("checkpoint"))
            try:
                return capture(*args, **kwargs)
            finally:
                in_capture = False

        def observed_directory(descriptor, *, max_entries, checkpoint=None):
            def checked_entry():
                nonlocal in_entry
                if in_capture:
                    assert callable(checkpoint)
                    in_entry = True
                    entered.append(descriptor)
                try:
                    if checkpoint is not None:
                        checkpoint()
                finally:
                    in_entry = False

            return enumeration(descriptor, max_entries=max_entries, checkpoint=checked_entry)

        def checkpoint():
            if in_entry:
                raise marker

        index, before = (scenario.root / ".git/index").read_bytes(), scenario.unchanged_state()
        with monkeypatch.context() as context:
            context.setattr(module, "capture_snapshot_facts", controlled_capture)
            context.setattr(snapshot, "observe_directory", observed_directory)
            with pytest.raises(kind) as caught:
                await scenario.collect(checkpoint=checkpoint)
        assert caught.value is marker and len(entered) == 1
        assert (scenario.root / ".git/index").read_bytes() == index
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


async def test_foreign_thread_cannot_borrow_authenticated_success_items(
    tmp_path, config, monkeypatch
):
    async def inspect(scenario):
        foreign = await scenario.client.create_thread(
            str(scenario.root), request_id="foreign-observation-thread"
        )
        forged = scenario.thread.model_copy(update={"thread_id": foreign.thread_id})

        async def forbidden(*_args, **_kwargs):
            pytest.fail("外传Thread的伪装成功条目不得查询Git")

        with monkeypatch.context() as context:
            context.setattr(scenario, "thread", forged)
            context.setattr(scenario.reader, "_run_baseline", forbidden)
            with pytest.raises(KernelError):
                await scenario.collect()

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


async def test_authenticated_history_is_read_before_and_after_and_collected_once(
    tmp_path, config, monkeypatch
):
    from harnessix.product_config import git_user_observation as observation_module

    async def inspect(scenario):
        histories, sources, baselines = [], [], []
        history = scenario.session.authenticated_thread_history
        source = observation_module.collect_git_delivery_source
        baseline = observation_module._collect_baseline_from_source

        async def observed_history(*args, **kwargs):
            result = await history(*args, **kwargs)
            histories.append(result)
            return result

        def observed_source(thread, *args, **kwargs):
            assert thread is histories[0].thread
            assert thread is not scenario.thread
            result = source(thread, *args, **kwargs)
            sources.append(result)
            return result

        async def observed_baseline(*args, **kwargs):
            result = await baseline(*args, **kwargs)
            baselines.append(result)
            return result

        with monkeypatch.context() as context:
            context.setattr(scenario.session, "authenticated_thread_history", observed_history)
            context.setattr(observation_module, "collect_git_delivery_source", observed_source)
            context.setattr(observation_module, "_collect_baseline_from_source", observed_baseline)
            result = await scenario.collect()
        assert len(histories) == 3 and histories[0] == histories[1] == histories[2]
        assert len(sources) == len(baselines) == 1
        assert result.baseline == baselines[0] and result.baseline.source == sources[0]

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


async def test_real_authenticated_history_change_during_git_observation_is_rejected(
    tmp_path, config, monkeypatch
):
    async def inspect(scenario):
        original, changed = scenario.reader._run_baseline, []

        async def drifted(*args, **kwargs):
            result = await original(*args, **kwargs)
            if not changed:
                await scenario.client.archive_thread(
                    scenario.thread.thread_id, request_id="history-drift", reason="测试历史变化"
                )
                changed.append(True)
            return result

        monkeypatch.setattr(scenario.reader, "_run_baseline", drifted)
        with pytest.raises(KernelError):
            await scenario.collect()
        assert changed and len(scenario.bundle.requests) == 2

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


async def test_authenticated_event_appended_during_the_last_async_observation_is_rejected(
    tmp_path, config, monkeypatch
):
    """末轮_observe的实际Git结果尚未返回时，经原SDK追加合法认证事件。"""

    async def inspect(scenario):
        original, history = (
            scenario.reader._run_baseline,
            scenario.session.authenticated_thread_history,
        )
        entered, release = asyncio.Event(), asyncio.Event()
        statuses, histories = [], []
        index, before = (scenario.root / ".git/index").read_bytes(), scenario.unchanged_state()

        async def observed_history(*args, **kwargs):
            result = await history(*args, **kwargs)
            histories.append(result)
            return result

        async def blocked_query(arguments, *args, **kwargs):
            result = await original(arguments, *args, **kwargs)
            tail = arguments[len(scenario.reader._global_arguments) :]
            if tail[0] == "status":
                statuses.append(tail)
                if len(statuses) == 3:
                    assert len(histories) == 2
                    entered.set()
                    await release.wait()
            return result

        with monkeypatch.context() as context:
            context.setattr(scenario.session, "authenticated_thread_history", observed_history)
            context.setattr(scenario.reader, "_run_baseline", blocked_query)
            task = asyncio.create_task(scenario.collect())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                archived = await scenario.client.archive_thread(
                    scenario.thread.thread_id,
                    request_id="history-final-query-drift",
                    reason="测试末轮认证事件变化",
                )
                assert archived.thread_id == scenario.thread.thread_id
                release.set()
                with pytest.raises(KernelError) as caught:
                    await asyncio.wait_for(task, 5)
                assert caught.value.code == "git_user_observation_history_changed"
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert len(statuses) == 3 and len(histories) == 3
        assert histories[0] == histories[1] and histories[0] != histories[2]
        assert histories[0].thread == scenario.thread
        assert histories[2].thread != scenario.thread
        assert (scenario.root / ".git/index").read_bytes() == index
        assert scenario.unchanged_state() == before
        assert len(scenario.bundle.requests) == 2

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["head", "selected-worktree", "config-value", "index-identity"])
async def test_repository_drift_during_second_authenticated_history_read_is_rejected(
    tmp_path, config, monkeypatch, fault, caplog, capsys
):
    async def inspect(scenario):
        original, histories = scenario.session.authenticated_thread_history, []
        root = scenario.root
        before = scenario.unchanged_state()

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            histories.append(result)
            if len(histories) == 2:
                physical, identity = (
                    (root / ".git/index").read_bytes(),
                    native_identity(root / ".git/index"),
                )
                if fault == "head":
                    head = command(root, "rev-parse", "HEAD").strip().decode()
                    tree = command(root, "rev-parse", "HEAD^{tree}").strip().decode()
                    commit = (
                        command(root, "commit-tree", "-p", head, "-m", "late external change", tree)
                        .strip()
                        .decode()
                    )
                    command(root, "update-ref", "HEAD", commit, head)
                elif fault == "selected-worktree":
                    arguments = (
                        *scenario.reader._global_arguments,
                        "status",
                        "--porcelain=v2",
                        "--untracked-files=all",
                        "--ignore-submodules=all",
                        "-z",
                    )
                    status = command(root, *arguments)
                    (root / scenario.selected_paths[0]).write_bytes((CANARY + "\n").encode())
                    assert command(root, *arguments) == status
                elif fault == "config-value":
                    names = command(
                        root, "config", "--no-includes", "--null", "--name-only", "--list"
                    )
                    command(root, "config", "user.name", "Late Changed Test")
                    assert (
                        command(root, "config", "--no-includes", "--null", "--name-only", "--list")
                        == names
                    )
                else:
                    replacement = root / ".git/index-replacement"
                    replacement.write_bytes(physical)
                    os.replace(replacement, root / ".git/index")
                assert (root / ".git/index").read_bytes() == physical
                if fault != "index-identity":
                    assert native_identity(root / ".git/index") == identity
            return result

        monkeypatch.setattr(scenario.session, "authenticated_thread_history", changed)
        with pytest.raises(KernelError) as caught:
            await scenario.collect()
        # HEAD/config由H2后的Git比较拒绝；文件/Index由H3后的完整原生核验拒绝。
        expected_reads = 2 if fault in {"head", "config-value"} else 3
        assert len(histories) == expected_reads and all(h == histories[0] for h in histories)
        assert_no_disclosure(caught.value, scenario, caplog, capsys)
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["cancel", "deadline", "checkpoint", "parent-pending"])
async def test_controls_before_query_are_not_ignored_or_reclassified(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        token, budget = CancelToken(), GitOperationBudget(60)
        marker = KernelError("observation_checkpoint_sentinel", "固定检查点异常")

        def checkpoint():
            if fault == "checkpoint":
                raise marker

        async def forbidden(*_args, **_kwargs):
            pytest.fail("入口控制已拒绝，不得读取Git")

        monkeypatch.setattr(scenario.reader, "_run_baseline", forbidden)
        if fault == "cancel":
            token.cancel()
        elif fault == "deadline":
            budget._deadline = time.monotonic() - 1

        async def operation():
            if fault == "parent-pending":
                asyncio.current_task().cancel()
            return await scenario.collect(cancel=token, budget=budget, checkpoint=checkpoint)

        expected = (
            asyncio.CancelledError
            if fault == "parent-pending"
            else TurnCancelled
            if fault == "cancel"
            else KernelError
        )
        before = scenario.unchanged_state()
        with pytest.raises(expected) as caught:
            await asyncio.create_task(operation())
        if fault == "checkpoint":
            assert caught.value is marker
        elif fault == "deadline":
            assert caught.value.code == "git_process_timeout"
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["cancel", "deadline", "parent-task"])
async def test_inflight_query_is_bounded_and_parent_task_reclaims_children(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        started, reclaimed = asyncio.Event(), asyncio.Event()
        token, budget = CancelToken(), GitOperationBudget(0.5 if fault == "deadline" else 60)
        original, tasks_before = scenario.reader._run_baseline, asyncio.all_tasks()
        before, index = scenario.unchanged_state(), (scenario.root / ".git/index").read_bytes()

        async def waiting(*args, **kwargs):
            # 先完成一次真实读取，随后模拟同一端口未完成的IO，不伪造认证来源。
            result = await original(*args, **kwargs)
            started.set()
            try:
                await asyncio.Event().wait()
                return result
            finally:
                reclaimed.set()

        monkeypatch.setattr(scenario.reader, "_run_baseline", waiting)
        task = asyncio.create_task(scenario.collect(cancel=token, budget=budget))
        try:
            await asyncio.wait_for(started.wait(), 5)
            if fault == "cancel":
                token.cancel()
            elif fault == "parent-task":
                task.cancel()
            expected = (
                TurnCancelled
                if fault == "cancel"
                else asyncio.CancelledError
                if fault == "parent-task"
                else KernelError
            )
            with pytest.raises(expected) as caught:
                await asyncio.wait_for(task, 2)
            if fault == "deadline":
                assert caught.value.code == "git_baseline_timeout"
            assert reclaimed.is_set()
            await asyncio.sleep(0)
            assert not {child for child in asyncio.all_tasks() - tasks_before if not child.done()}
            assert (scenario.root / ".git/index").read_bytes() == index
            assert scenario.unchanged_state() == before
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.skipif(os.name != "posix", reason="仅验证POSIX真实Git子进程及FIFO回收")
@pytest.mark.parametrize("fault", ["cancel", "deadline", "parent-task"])
async def test_real_blocked_git_process_is_reaped_on_cancel_or_shared_deadline(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        started, captures = asyncio.Event(), []
        original = CaptureProtocol.connection_made
        path = scenario.root / ".git/config"
        body = path.read_bytes()
        path.unlink()
        os.mkfifo(path)
        before, index = scenario.unchanged_state(), (scenario.root / ".git/index").read_bytes()
        token = CancelToken()

        def connected(capture, transport):
            original(capture, transport)
            captures.append(capture)
            started.set()

        with monkeypatch.context() as context:
            context.setattr(CaptureProtocol, "connection_made", connected)
            task = asyncio.create_task(
                scenario.collect(
                    cancel=token, budget=GitOperationBudget(0.5 if fault == "deadline" else 60)
                )
            )
            try:
                await asyncio.wait_for(started.wait(), 5)
                assert len(captures) == 1
                capture = captures[0]
                assert capture.transport.get_returncode() is None
                if fault == "cancel":
                    token.cancel()
                elif fault == "parent-task":
                    task.cancel()
                expected = (
                    TurnCancelled
                    if fault == "cancel"
                    else asyncio.CancelledError
                    if fault == "parent-task"
                    else KernelError
                )
                with pytest.raises(expected) as caught:
                    await asyncio.wait_for(task, 5)
                if fault == "deadline":
                    assert caught.value.code in {"git_baseline_timeout", "git_process_timeout"}
                # 原process_exited仅在直接子进程已经wait/reap后通知；同时检查管道关闭。
                assert capture.exited.done() and capture.closed.done()
                assert capture.transport.get_returncode() is not None
                assert (scenario.root / ".git/index").read_bytes() == index
                assert scenario.unchanged_state() == before
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                path.unlink()
                path.write_bytes(body)

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize(
    "drift", ["head", "index-bytes", "index-identity", "common", "admin", "config-value"]
)
async def test_repository_drift_including_same_config_names_is_rejected(
    tmp_path, config, monkeypatch, drift, caplog, capsys
):
    async def inspect(scenario):
        root, port = scenario.root, scenario.reader
        original, count = port._run_baseline, 0
        names = command(root, "config", "--no-includes", "--null", "--name-only", "--list")

        async def changed(arguments, *args, **kwargs):
            nonlocal count
            result = await original(arguments, *args, **kwargs)
            tail = arguments[len(port._global_arguments) :]
            if tail == ("ls-files", "--stage", "--debug", "-z"):
                count += 1
                if count == 2:
                    if drift == "head":
                        physical = (root / ".git/index").read_bytes()
                        identity = native_identity(root / ".git/index")
                        head = command(root, "rev-parse", "HEAD").strip().decode()
                        tree = command(root, "rev-parse", "HEAD^{tree}").strip().decode()
                        commit = (
                            command(root, "commit-tree", "-p", head, "-m", "external change", tree)
                            .strip()
                            .decode()
                        )
                        command(root, "update-ref", "HEAD", commit, head)
                        assert (root / ".git/index").read_bytes() == physical
                        assert native_identity(root / ".git/index") == identity
                    elif drift == "index-bytes":
                        command(root, "add", "--", "unstaged.txt")
                    elif drift == "index-identity":
                        target = root / ".git/index"
                        temporary = root / ".git/index-replacement"
                        temporary.write_bytes(target.read_bytes())
                        os.replace(temporary, target)
                    elif drift in {"common", "admin"}:
                        # 普通仓库的common与admin同路径；保留旧inode避免复用。
                        old = tmp_path / "retired-admin"
                        (root / ".git").rename(old)
                        shutil.copytree(old, root / ".git")
                    else:
                        command(root, "config", "user.name", "Changed Test")
                        assert (
                            command(
                                root, "config", "--no-includes", "--null", "--name-only", "--list"
                            )
                            == names
                        )
            return result

        before = scenario.unchanged_state()
        monkeypatch.setattr(port, "_run_baseline", changed)
        with pytest.raises(KernelError) as caught:
            await scenario.collect()
        assert count in {2, 3}
        assert_no_disclosure(caught.value, scenario, caplog, capsys)
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["symlink", "hardlink", "special", "oversize", "directory"])
async def test_unsafe_physical_index_is_rejected_before_baseline_without_canary(
    tmp_path, config, monkeypatch, fault, caplog, capsys
):
    async def inspect(scenario):
        target, outside = scenario.root / ".git/index", tmp_path / "outside-canary"
        outside.write_bytes(CANARY.encode())
        target.unlink()
        if fault == "symlink":
            target.symlink_to(outside)
        elif fault == "hardlink":
            os.link(outside, target)
        elif fault == "special":
            os.mkfifo(target)
        elif fault == "directory":
            target.mkdir()
        else:
            with target.open("wb") as stream:
                stream.write(CANARY.encode())
                stream.truncate(MAX_TRANSACTION_FILE_BYTES + 1)

        original = scenario.reader._run_baseline

        async def metadata_only(arguments, *args, **kwargs):
            tail = arguments[len(scenario.reader._global_arguments) :]
            assert tail in {
                ("rev-parse", "--git-common-dir"),
                ("rev-parse", "--absolute-git-dir"),
                ("rev-parse", "--git-path", "index"),
                ("config", "--no-includes", "--null", "--list", "--show-origin"),
            }, "不安全物理Index必须先于原基准的Index读取拒绝"
            return await original(arguments, *args, **kwargs)

        before = scenario.unchanged_state()
        monkeypatch.setattr(scenario.reader, "_run_baseline", metadata_only)
        with pytest.raises(KernelError) as caught:
            await scenario.collect()
        assert outside.read_bytes() == CANARY.encode()
        assert_no_disclosure(caught.value, scenario, caplog, capsys)
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


async def test_complete_config_value_is_only_a_private_digest_without_canary_publication(
    tmp_path, config, monkeypatch, caplog, capsys
):
    """POSIX不声称正文已脱敏；合成配置值仅绑定SHA，不进入产品结果或日志。"""

    async def inspect(scenario):
        physical = (scenario.root / ".git/index").read_bytes()
        identity, before = native_identity(scenario.root / ".git/index"), scenario.unchanged_state()
        first = await scenario.collect()
        command(scenario.root, "config", "user.name", CANARY)
        original, config_hashes = scenario.reader._run_baseline, []

        async def observed(arguments, *args, **kwargs):
            result = await original(arguments, *args, **kwargs)
            tail = arguments[len(scenario.reader._global_arguments) :]
            if tail[0] == "config" and "--list" in tail and "--name-only" not in tail:
                # 不读取Scope材料或配置正文，只观察原捕获端口的非秘密摘要。
                config_hashes.append(result.raw_stdout.sha256)
            return result

        monkeypatch.setattr(scenario.reader, "_run_baseline", observed)
        actual = await scenario.collect()
        assert type(actual.baseline) is ProductGitDeliveryBaselineV2
        assert first.baseline.config_names_sha256 == actual.baseline.config_names_sha256
        assert first.config_sha256 != actual.config_sha256
        assert len(config_hashes) >= 3 and set(config_hashes) == {actual.config_sha256}
        exposed = actual.model_dump_json() + repr(actual) + caplog.text
        captured = capsys.readouterr()
        exposed += captured.out + captured.err
        assert CANARY not in exposed and str(scenario.root) not in exposed
        assert (scenario.root / ".git/index").read_bytes() == physical
        assert native_identity(scenario.root / ".git/index") == identity
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize(
    "fault",
    [
        "redacted-value",
        "truncated",
        "oversized-raw",
        "invalid-utf8",
        "relative-admin",
        "control-common",
        "nonutf8-common",
        "traversal-common",
        "wrong-index",
        "oversized-admin",
    ],
)
async def test_complete_config_and_path_metadata_fail_closed_without_canary(
    tmp_path, config, monkeypatch, fault, caplog, capsys
):
    async def inspect(scenario):
        root, port = scenario.root, scenario.reader
        original, injected = port._run_baseline, []

        async def changed(arguments, *args, **kwargs):
            result = await original(arguments, *args, **kwargs)
            tail = arguments[len(port._global_arguments) :]
            full_config = tail[0] == "config" and "--list" in tail and "--name-only" not in tail
            path_fault = fault in {
                "relative-admin",
                "control-common",
                "nonutf8-common",
                "traversal-common",
                "wrong-index",
                "oversized-admin",
            }
            if full_config and not path_fault:
                injected.append(tail)
                if fault == "redacted-value":
                    return _published_stdout(result, b"[REDACTED]")
                if fault == "truncated":
                    return replace(
                        result,
                        result=result.result.model_copy(
                            update={
                                "stdout": result.result.stdout.model_copy(
                                    update={"truncated": True}
                                )
                            }
                        ),
                    )
                if fault == "oversized-raw":
                    return replace(
                        result,
                        raw_stdout=result.raw_stdout.model_copy(
                            update={"observed_bytes": MAX_TRANSACTION_FILE_BYTES + 1}
                        ),
                    )
                if fault == "invalid-utf8":
                    return _published_stdout(result, b"\xff" + CANARY.encode())
            report = {
                "relative-admin": ("rev-parse", "--absolute-git-dir"),
                "control-common": ("rev-parse", "--git-common-dir"),
                "nonutf8-common": ("rev-parse", "--git-common-dir"),
                "traversal-common": ("rev-parse", "--git-common-dir"),
                "wrong-index": ("rev-parse", "--git-path", "index"),
                "oversized-admin": ("rev-parse", "--absolute-git-dir"),
            }.get(fault)
            expected = report is not None and tail == report
            if expected:
                injected.append(tail)
                body = {
                    "relative-admin": b"../" + CANARY.encode() + b"\n",
                    "control-common": str(root / ".git").encode()
                    + b"/\x01"
                    + CANARY.encode()
                    + b"\n",
                    "nonutf8-common": b"/\xff/" + CANARY.encode() + b"\n",
                    "traversal-common": b"../" + CANARY.encode() + b"\n",
                    "wrong-index": str(root / CANARY / "index").encode() + b"\n",
                    "oversized-admin": b"/" + CANARY.encode() * 200 + b"\n",
                }[fault]
                # 故障字段保留raw与安全流一致，路径自身不合法，不只制造摘要不一致。
                result = _published_stdout(result, body)
                return replace(
                    result,
                    raw_stdout=result.raw_stdout.model_copy(
                        update={
                            "observed_bytes": len(body),
                            "sha256": hashlib.sha256(body).hexdigest(),
                        }
                    ),
                )
            return result

        before = scenario.unchanged_state()
        monkeypatch.setattr(port, "_run_baseline", changed)
        with pytest.raises((KernelError, ReadToolError)) as caught:
            await scenario.collect()
        assert injected
        assert_no_disclosure(caught.value, scenario, caplog, capsys)
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)
