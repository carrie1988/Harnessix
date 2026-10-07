"""原认证历史与用户 Git 观察的只读复核；真实 SDK、Source2 和物理端口验收。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sqlite3
import time
import zlib
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ToolCallContent, TurnStatus
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.product_config import git_user_observation as observation_module
from harnessix.product_config.git_baseline import _observe, _Queries
from harnessix.product_config.git_baseline_contracts import product_git_baseline_digest
from harnessix.product_config.git_delivery_process import GitOperationBudget
from harnessix.product_config.git_user_observation_contracts import (
    ProductGitUserObservation,
    product_git_user_observation_fingerprint,
)
from harnessix.product_config.workspace_patch_source_contracts import (
    product_git_delivery_source_digest,
)
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts
from tests.product_config.test_git_baseline import command
from tests.product_config.test_git_user_observation import native_identity
from tests.support.git_user_observation import run_authenticated_observation


async def _ready(scenario):
    expected = await scenario.collect()
    history = await scenario.session.authenticated_thread_history(
        scenario.thread.thread_id,
        cancel=CancelToken(),
        deadline=time.monotonic() + 60.0,
        checkpoint=lambda: None,
    )
    assert history.thread == scenario.thread
    assert history.events and history.events[-1].sequence == history.thread.sequence
    assert history.thread.active_turn_id is None
    assert all(turn.status is TurnStatus.COMPLETED for turn in history.thread.turns)
    assert expected.baseline.source.spec_version == "harnessix.product-git-delivery-source/v2"
    return expected, history


async def _verify(scenario, expected, history, *, cancel=None, budget=None, checkpoint=None, **kw):
    verifier = getattr(observation_module, "verify_product_git_user_observation", None)
    assert callable(verifier), "B3公开只读复核API尚未实现"
    token = cancel if cancel is not None else CancelToken()
    arguments = {
        "session": scenario.session,
        "cancel": token,
        "budget": budget if budget is not None else GitOperationBudget(60.0),
        "checkpoint": checkpoint if checkpoint is not None else token.checkpoint,
        "snapshot_ports": scenario.router._snapshot_ports,
        **kw,
    }
    return await verifier(
        expected, history, scenario.router, scenario.transactions, scenario.reader, **arguments
    )


def _database_rows(path):
    """观察持久表内容而非 WAL 文件布局，不创建连接替身或改写数据库。"""
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return tuple(
            (name, tuple(connection.execute('SELECT * FROM "' + name + '"').fetchall()))
            for (name,) in tables
        )


def _readonly_state(scenario):
    blobs = tuple(
        (
            path.name,
            path.stat().st_ino,
            path.stat().st_mode,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(scenario.transactions._blobs.iterdir())
        if path.is_file()
    )
    index = scenario.root / ".git/index"
    return (
        scenario.unchanged_state(),
        tuple(
            _database_rows(path)
            for path in (
                scenario.session.path,
                scenario.router._audit._path,
                scenario.router._plans._path,
                scenario.transactions._path,
            )
        ),
        blobs,
        index.read_bytes(),
        native_identity(index),
        tuple(
            (path, (scenario.root / path).read_bytes())
            for path in (*scenario.selected_paths, "user.txt", "unstaged.txt", "untracked.txt")
        ),
    )


def _refingerprint(expected, **changes):
    candidate = expected.model_copy(update=changes)
    return ProductGitUserObservation.model_validate(
        {
            **candidate.model_dump(mode="python", exclude={"fingerprint"}),
            "fingerprint": product_git_user_observation_fingerprint(candidate),
        }
    )


@pytest.mark.parametrize("object_format,continuous", [("sha1", False), ("sha256", True)])
async def test_real_completed_patch_history_verifies_read_only_without_recapture_or_cas_write(
    tmp_path, config, monkeypatch, object_format, continuous
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        assert len(expected.baseline.head_oid) == (40 if object_format == "sha1" else 64)
        assert len(expected.baseline.source.patches) == (2 if continuous else 1)
        assert tuple(p.transaction_id for p in expected.baseline.source.patches) == tuple(
            reversed(scenario.targets)
        )
        before = _readonly_state(scenario)
        original = expected.model_dump_json()
        token, budget = CancelToken(), GitOperationBudget(60.0)
        deadline = budget._deadline

        def forbidden(*_args, **_kwargs):
            pytest.fail("复核不得重新collect Source、捕获新基准或写入CAS")

        with monkeypatch.context() as context:
            context.setattr(observation_module, "collect_git_delivery_source", forbidden)
            context.setattr(observation_module, "_collect_baseline_from_source", forbidden)
            context.setattr(SQLiteWorkspaceTransactionStore, "_put_blob", forbidden)
            for _ in range(2):
                assert (
                    await _verify(scenario, expected, history, cancel=token, budget=budget) is None
                )
                assert budget._deadline == deadline
                assert _readonly_state(scenario) == before
                assert expected.model_dump_json() == original
        assert command(scenario.root, "show", ":user.txt") == b"user staged\n"
        assert not (scenario.state / "git-delivery").exists()

    await run_authenticated_observation(
        tmp_path, config, monkeypatch, inspect, object_format=object_format, continuous=continuous
    )


async def test_verifier_uses_shared_final_checks_without_recapturing_source(
    tmp_path, config, monkeypatch
):
    async def inspect(scenario):
        shared = getattr(observation_module, "_verify_observed_git_state", None)
        assert callable(shared), "准备器与复核器尚未共用原末段检查"
        source_collector = observation_module.collect_git_delivery_source
        sources, calls = [], []

        def observed_source(*args, **kwargs):
            value = source_collector(*args, **kwargs)
            sources.append(value)
            return value

        async def counted(observation, reader, cancel, check, **kwargs):
            count = 0

            def checked():
                nonlocal count
                count += 1
                check()

            result = await shared(observation, reader, cancel, checked, **kwargs)
            calls.append((observation, count))
            return result

        with monkeypatch.context() as context:
            context.setattr(observation_module, "collect_git_delivery_source", observed_source)
            context.setattr(observation_module, "_verify_observed_git_state", counted)
            expected, history = await _ready(scenario)
            # Collector有自己的前后窗口，不能为了复用而改变其历史/快照顺序。
            assert not calls and len(sources) == 1
            assert expected.baseline.source == sources[0]
            assert await _verify(scenario, expected, history) is None
        assert len(calls) == 1 and calls[0][0] == expected
        assert calls[0][0] is not expected  # 深快照切断调用方模型别名。
        assert calls[0][1] > 0
        assert len(sources) == 1

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect, deep=True)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
@pytest.mark.parametrize("field", ["reader-binding", "member-oid"])
async def test_repaired_baseline_digest_cannot_replace_native_reader_or_tree_member(
    tmp_path, config, monkeypatch, object_format, field
):
    """公开摘要可重算，但基准每个来源成员与原Reader仍须向实际端口求证。"""

    async def inspect(scenario):
        expected, history = await _ready(scenario)
        baseline = expected.baseline
        if field == "reader-binding":
            changed = baseline.model_copy(update={"reader_binding": "0" * 64})
            assert baseline.reader_binding != changed.reader_binding
        else:
            members = list(baseline.members)
            offset = next(i for i, member in enumerate(members) if member.oid is not None)
            original = members[offset]
            members[offset] = original.model_copy(update={"oid": "0" * len(baseline.head_oid)})
            assert members[offset].oid != original.oid
            changed = baseline.model_copy(update={"members": tuple(members)})
        # 修复公开两层摘要，并重新通过正式结构校验；不能依赖坏摘要拒绝。
        changed = type(baseline).model_validate(
            {
                **changed.model_dump(exclude={"digest"}),
                "digest": product_git_baseline_digest(changed),
            }
        )
        forged = _refingerprint(expected, baseline=changed)
        before = _readonly_state(scenario)
        with pytest.raises(KernelError) as caught:
            await _verify(scenario, forged, history)
        assert caught.value.code == "git_user_observation_changed"
        assert _readonly_state(scenario) == before

    await run_authenticated_observation(
        tmp_path, config, monkeypatch, inspect, object_format=object_format
    )


async def test_repaired_facts_cannot_redirect_actual_git_worktree_away_from_native_workspace(
    tmp_path, config, monkeypatch
):
    """原生Root未变，仍须核验Git实际工作树；普通配置/状态SHA不能替代根校验。"""

    async def inspect(scenario):
        expected, history = await _ready(scenario)
        other = tmp_path / "foreign-worktree"
        shutil.copytree(scenario.root, other, ignore=shutil.ignore_patterns(".git"))
        command(scenario.root, "config", "core.worktree", str(other))
        assert command(scenario.root, "rev-parse", "--show-toplevel").strip().decode() == str(other)
        query = _Queries(scenario.reader, CancelToken())
        actual = await _observe(query)
        changed = expected.baseline.model_copy(
            update={
                "head_oid": actual.head,
                "head_tree_oid": actual.tree,
                "head_ref": actual.ref,
                "index_observation_sha256": actual.index_sha256,
                "index_observation_bytes": actual.index_bytes,
                "status_sha256": actual.status_sha256,
                "config_names_sha256": actual.config_sha256,
            }
        )
        changed = type(changed).model_validate(
            {
                **changed.model_dump(exclude={"digest"}),
                "digest": product_git_baseline_digest(changed),
            }
        )
        forged = _refingerprint(
            expected, baseline=changed, config_sha256=await observation_module._configuration(query)
        )
        before = _readonly_state(scenario)
        with pytest.raises(KernelError) as caught:
            await _verify(scenario, forged, history)
        assert caught.value.code == "git_user_observation_unavailable"
        assert _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


async def test_native_workspace_cannot_inherit_parent_git_repository(tmp_path, config, monkeypatch):
    """根身份相同也不能借父目录的Git仓库；拒绝必须来自实际根查询。"""

    async def inspect(scenario):
        expected, history = await _ready(scenario)
        parent_git = scenario.root.parent / ".git"
        assert not parent_git.exists()
        (scenario.root / ".git").rename(parent_git)
        assert command(scenario.root, "rev-parse", "--show-toplevel").strip().decode() == str(
            scenario.root.parent
        )
        index = parent_git / "index"
        before = (scenario.unchanged_state(), index.read_bytes(), native_identity(index))
        observed, original = [], scenario.reader._run_baseline

        async def recorded(arguments, cancel, *, repository_check=False):
            observed.append((arguments, repository_check))
            return await original(arguments, cancel, repository_check=repository_check)

        with monkeypatch.context() as context:
            context.setattr(scenario.reader, "_run_baseline", recorded)
            with pytest.raises(KernelError) as caught:
                await _verify(scenario, expected, history)
            assert caught.value.code == "git_user_observation_unavailable"
        assert any(
            args[-2:] == ("rev-parse", "--show-toplevel") and root_check
            for args, root_check in observed
        )
        assert (scenario.unchanged_state(), index.read_bytes(), native_identity(index)) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize(
    "fault", ["before-blob", "assume-unchanged", "skip-worktree", "intent-to-add"]
)
async def test_entry_semantically_checks_before_blob_and_index_flags(
    tmp_path, config, monkeypatch, fault
):
    """只核对成员算法特有的失败分类，避免整体Index/状态漂移检查掩盖漏检。"""

    async def inspect(scenario):
        expected, history = await _ready(scenario)
        member = next(item for item in expected.baseline.members if item.oid is not None)
        if fault == "before-blob":
            blob = scenario.root / ".git/objects" / member.oid[:2] / member.oid[2:]
            original = zlib.decompress(blob.read_bytes())
            header, body = original.split(b"\0", 1)
            assert body
            changed = bytes([body[0] ^ 1]) + body[1:]
            replacement = blob.with_name("corrupted-test-object")
            replacement.write_bytes(zlib.compress(header + b"\0" + changed))
            replacement.chmod(blob.stat().st_mode & 0o777)
            os.replace(replacement, blob)
            assert command(scenario.root, "cat-file", "blob", member.oid) == changed
            code = "git_baseline_before_mismatch"
        else:
            if fault == "intent-to-add":
                command(scenario.root, "update-index", "--force-remove", "--", member.path)
                command(scenario.root, "add", "--intent-to-add", "--", member.path)
            else:
                command(scenario.root, "update-index", "--" + fault, "--", member.path)
            code = "git_baseline_index_conflict"
        before = _readonly_state(scenario)
        with pytest.raises(KernelError) as caught:
            await _verify(scenario, expected, history)
        assert caught.value.code == code
        assert _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("stage", ["git-return", "final-checkpoint"])
@pytest.mark.parametrize("field", ["binding", "arguments", "executable"])
async def test_original_reader_binding_drift_is_rejected_after_normal_return(
    tmp_path, config, monkeypatch, stage, field
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        total = 0

        def count():
            nonlocal total
            total += 1

        await _verify(scenario, expected, history, checkpoint=count)
        before, changed = _readonly_state(scenario), []
        with monkeypatch.context() as context:

            def mutate():
                if changed:
                    return
                attribute, value = {
                    "binding": ("_binding_fingerprint", "0" * 64),
                    "arguments": (
                        "_global_arguments",
                        (*scenario.reader._global_arguments, "--no-pager"),
                    ),
                    "executable": ("_executable", tmp_path / "different-executable"),
                }[field]
                context.setattr(scenario.reader, attribute, value)
                changed.append(True)

            calls = 0

            def checkpoint():
                nonlocal calls
                calls += 1
                if stage == "final-checkpoint" and calls == total:
                    mutate()

            original = scenario.reader._run_baseline

            async def returned(*args, **kwargs):
                result = await original(*args, **kwargs)
                mutate()
                return result

            if stage == "git-return":
                context.setattr(scenario.reader, "_run_baseline", returned)
            with pytest.raises(KernelError) as caught:
                await _verify(scenario, expected, history, checkpoint=checkpoint)
            assert caught.value.code == "git_user_observation_host_invalid"
        assert changed and _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("settlement", ["rollback", "close"])
@pytest.mark.parametrize("fault", ["cancel", "owner-error"])
async def test_entry_preserves_session_settlement_failure_over_original_control(
    tmp_path, config, monkeypatch, settlement, fault
):
    """真实认证会话读取底座；ROLLBACK为驱动拒绝，close为实际关闭后的失败注入。"""

    async def inspect(scenario):
        expected, history = await _ready(scenario)
        token = CancelToken()
        state = {"begun": False, "triggered": False, "denials": 0, "closed": False}
        before = _readonly_state(scenario)
        connection = scenario.session._connection

        @asynccontextmanager
        async def instrument(**kwargs):
            database = None
            try:
                async with connection(**kwargs) as database:

                    def authorizer(action, first, _second, _database, _source):
                        if action == sqlite3.SQLITE_TRANSACTION and first == "BEGIN":
                            state["begun"] = True
                        if (
                            settlement == "rollback"
                            and action == sqlite3.SQLITE_TRANSACTION
                            and first == "ROLLBACK"
                        ):
                            state["denials"] += 1
                            return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK

                    await database.set_authorizer(authorizer)
                    if settlement == "close":
                        original_close = database.close

                        async def failed_close():
                            await original_close()
                            raise OSError("injected-session-close-failure")

                        monkeypatch.setattr(database, "close", failed_close)
                    yield database
            finally:
                state["closed"] = database is not None and database._connection is None

        def checkpoint():
            if not state["begun"] or state["triggered"]:
                return
            state["triggered"] = True
            if fault == "cancel":
                token.cancel()
            else:
                raise OSError("original-owner-control-failure")

        with monkeypatch.context() as context:
            context.setattr(scenario.session, "_connection", instrument)
            with pytest.raises(KernelError) as caught:
                await _verify(scenario, expected, history, cancel=token, checkpoint=checkpoint)
        assert caught.value.code == "storage_unavailable"
        assert state["triggered"] and state["closed"]
        assert state["denials"] == (1 if settlement == "rollback" else 0)
        assert _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["events-prefix", "historical-call", "foreign-thread"])
async def test_caller_history_cannot_replace_original_authenticated_complete_history(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        if fault == "events-prefix":
            forged = replace(history, events=history.events[:-1])
            assert forged.thread == history.thread
        elif fault == "foreign-thread":
            forged = replace(
                history, thread=history.thread.model_copy(update={"thread_id": UUID(int=1)})
            )
        else:
            thread = history.thread.model_copy(deep=True)
            calls = [
                item.content
                for turn in thread.turns
                for item in turn.items
                if isinstance(item.content, ToolCallContent)
            ]
            assert calls
            calls[0].arguments["files"][0]["content"] = "forged historical request\n"
            forged = replace(history, thread=thread)
            assert forged.events == history.events and forged.thread != history.thread
        before = _readonly_state(scenario)
        with pytest.raises(KernelError):
            await _verify(scenario, expected, forged)
        assert _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect, continuous=True)


@pytest.mark.parametrize("stage", ["before-entry", "last-git-return"])
async def test_real_authenticated_history_growth_invalidates_original_request(
    tmp_path, config, monkeypatch, stage
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        scenario_index = (scenario.root / ".git/index").read_bytes()
        original, query_count, entered = scenario.reader._run_baseline, 0, []

        async def counted(*args, **kwargs):
            nonlocal query_count
            result = await original(*args, **kwargs)
            query_count += 1
            return result

        async def append_event():
            await scenario.client.archive_thread(
                scenario.thread.thread_id,
                request_id="verify-history-growth",
                reason="历史一致性测试",
            )
            entered.append(True)

        with monkeypatch.context() as context:
            if stage == "before-entry":
                await append_event()
            else:
                context.setattr(scenario.reader, "_run_baseline", counted)
                assert await _verify(scenario, expected, history) is None
                last_query, query_count = query_count, 0
                assert last_query > 0

                async def changed(*args, **kwargs):
                    result = await counted(*args, **kwargs)
                    if query_count == last_query:
                        await append_event()
                    return result

                context.setattr(scenario.reader, "_run_baseline", changed)
            with pytest.raises(KernelError) as caught:
                await _verify(scenario, expected, history)
        assert entered and caught.value.code == "git_user_observation_history_changed"
        assert len(scenario.bundle.requests) == 2
        assert (scenario.root / ".git/index").read_bytes() == scenario_index

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize(
    "fault", ["head", "head-ref", "status", "logical-index", "config-value", "source-bytes"]
)
async def test_real_logical_and_original_source_drift_is_not_a_new_observation(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        root = scenario.root
        index = (root / ".git/index").read_bytes()
        if fault == "head":
            head = command(root, "rev-parse", "HEAD").strip().decode()
            tree = command(root, "rev-parse", "HEAD^{tree}").strip().decode()
            commit = (
                command(root, "commit-tree", "-p", head, "-m", "external", tree).strip().decode()
            )
            command(root, "update-ref", "HEAD", commit, head)
        elif fault == "head-ref":
            command(root, "update-ref", "refs/heads/other", expected.baseline.head_oid)
            command(root, "symbolic-ref", "HEAD", "refs/heads/other")
            assert command(root, "rev-parse", "HEAD").strip().decode() == expected.baseline.head_oid
        elif fault == "status":
            (root / "another-untracked.txt").write_bytes(b"new untracked file\n")
        elif fault == "logical-index":
            command(root, "add", "--", "unstaged.txt")
        elif fault == "config-value":
            names = command(root, "config", "--no-includes", "--null", "--name-only", "--list")
            command(root, "config", "user.name", "Changed Test Name")
            assert (
                command(root, "config", "--no-includes", "--null", "--name-only", "--list") == names
            )
        else:
            arguments = (
                "status",
                "--porcelain=v2",
                "--untracked-files=all",
                "--ignore-submodules=all",
                "-z",
            )
            status = command(root, *arguments)
            (root / scenario.selected_paths[0]).write_bytes(b"other\n")
            assert command(root, *arguments) == status
        if fault != "logical-index":
            assert (root / ".git/index").read_bytes() == index
        before = _readonly_state(scenario)
        with pytest.raises(KernelError):
            await _verify(scenario, expected, history)
        assert _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize(
    "fault",
    [
        "same-bytes-index",
        "corrupt-index",
        "missing-index",
        "index-directory",
        "index-symlink",
        "common-and-admin",
    ],
)
async def test_actual_native_index_and_git_directory_replacement_or_damage_is_rejected(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        index = scenario.root / ".git/index"
        body, identity = index.read_bytes(), native_identity(index)
        if fault == "same-bytes-index":
            replacement = tmp_path / "replacement-index"
            replacement.write_bytes(body)
            os.replace(replacement, index)
            assert index.read_bytes() == body and native_identity(index) != identity
        elif fault == "corrupt-index":
            index.write_bytes(b"broken physical index\n")
        elif fault == "common-and-admin":
            assert expected.common_directory_identity == expected.git_directory_identity
            retired = tmp_path / "retired-git-directory"
            index.parent.rename(retired)
            shutil.copytree(retired, index.parent)
            assert index.read_bytes() == body
        else:
            index.unlink()
            if fault == "index-directory":
                index.mkdir()
            elif fault == "index-symlink":
                outside = tmp_path / "outside-index"
                outside.write_bytes(body)
                index.symlink_to(outside)
        before = scenario.unchanged_state()
        with pytest.raises(KernelError):
            await _verify(scenario, expected, history)
        assert scenario.unchanged_state() == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("field", ["store_id", "key_id", "implementation_digest", "source-route"])
async def test_repaired_public_fingerprint_does_not_authenticate_foreign_identity_or_source(
    tmp_path, config, monkeypatch, field
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        if field == "source-route":
            source = expected.baseline.source
            patch = source.patches[0].model_copy(update={"route_fingerprint": "0" * 64})
            source = source.model_copy(update={"patches": (patch, *source.patches[1:])})
            source = source.model_copy(
                update={"digest": product_git_delivery_source_digest(source)}
            )
            baseline = expected.baseline.model_copy(update={"source": source})
            baseline = baseline.model_copy(update={"digest": product_git_baseline_digest(baseline)})
            forged = _refingerprint(expected, baseline=baseline)
        else:
            forged = _refingerprint(
                expected, **{field: "0" * 64 if field == "implementation_digest" else UUID(int=1)}
            )
        before = _readonly_state(scenario)
        with pytest.raises(KernelError):
            await _verify(scenario, forged, history)
        assert _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize(
    "fault",
    [
        "session-class",
        "unauthenticated-session",
        "readonly-store",
        "wrapped-write",
        "foreign-read",
        "reader-scope",
        "state-root",
        "reader-root",
    ],
)
async def test_original_host_and_resource_substitutes_are_rejected_before_git(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)

        async def forbidden(*_args, **_kwargs):
            pytest.fail("原宿主或根被替身后不得先查询Git")

        with (
            SQLiteWorkspaceTransactionStore(scenario.transactions._root, read_only=True) as other,
            monkeypatch.context() as context,
        ):
            context.setattr(scenario.reader, "_run_baseline", forbidden)
            kwargs = {}
            if fault == "session-class":
                kwargs["session"] = SimpleNamespace(path=scenario.session.path)
            elif fault == "unauthenticated-session":
                kwargs["session"] = SQLiteSessionStore(scenario.session.path)
            elif fault == "readonly-store":
                context.setattr(scenario, "transactions", other)
            elif fault in {"wrapped-write", "foreign-read"}:
                kwargs["snapshot_ports"] = WorkspaceSnapshotPorts(
                    (lambda *args: scenario.transactions.put_blob(*args))
                    if fault == "wrapped-write"
                    else scenario.transactions.put_blob,
                    other.blob if fault == "foreign-read" else scenario.transactions.blob,
                )
            elif fault == "reader-scope":
                context.setattr(scenario.reader, "_output_redaction", None)
            elif fault == "state-root":
                context.setattr(scenario.session, "path", scenario.state / "other/sessions.db")
            else:
                foreign = tmp_path / "foreign-workspace"
                shutil.copytree(scenario.root, foreign)
                context.setattr(scenario.reader, "_root", foreign)
            with pytest.raises(KernelError):
                await _verify(scenario, expected, history, **kwargs)

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize(
    "fault",
    [
        "publication",
        "events",
        "store-id",
        "key-id",
        "router-audit",
        "router-plans",
        "ports",
        "reader-root",
        "implementation",
    ],
)
async def test_original_host_and_implementation_remain_bound_after_real_query(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        original, entered = scenario.reader._run_baseline, []
        with monkeypatch.context() as context:

            async def changed(*args, **kwargs):
                result = await original(*args, **kwargs)
                if not entered:
                    publication = scenario.session._publication
                    if fault == "implementation":
                        context.setattr(
                            observation_module,
                            "git_user_observation_implementation_digest",
                            lambda: "0" * 64,
                        )
                    else:
                        owner, attribute, replacement = {
                            "publication": (scenario.session, "_publication", SimpleNamespace()),
                            "events": (publication, "_events", SimpleNamespace()),
                            "store-id": (publication, "_store_id", UUID(int=1)),
                            "key-id": (publication, "_key_id", UUID(int=1)),
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
                            "ports": (
                                scenario.router,
                                "_snapshot_ports",
                                WorkspaceSnapshotPorts(
                                    scenario.transactions.put_blob, scenario.transactions.blob
                                ),
                            ),
                            "reader-root": (scenario.reader, "_root", tmp_path / "other-root"),
                        }[fault]
                        context.setattr(owner, attribute, replacement)
                    entered.append(True)
                return result

            context.setattr(scenario.reader, "_run_baseline", changed)
            with pytest.raises(KernelError):
                await _verify(scenario, expected, history)
        assert entered

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("kind", [RuntimeError, OSError, asyncio.CancelledError])
@pytest.mark.parametrize("stage", ["entry", "middle", "final"])
async def test_entry_middle_final_callback_errors_preserve_exact_original_object(
    tmp_path, config, monkeypatch, kind, stage
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        checks = 0

        def counted():
            nonlocal checks
            checks += 1

        assert await _verify(scenario, expected, history, checkpoint=counted) is None
        total, checks = checks, 0
        assert total > 3
        target = 1 if stage == "entry" else total // 2 if stage == "middle" else total
        marker = kind("原控制回调异常")
        before = _readonly_state(scenario)

        def controlled():
            nonlocal checks
            checks += 1
            if checks == target:
                raise marker

        with pytest.raises(kind) as caught:
            await _verify(scenario, expected, history, checkpoint=controlled)
        assert caught.value is marker and checks == target
        assert _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect, deep=True)


@pytest.mark.parametrize("fault", ["cancel", "expired-budget"])
async def test_original_cancel_and_exhausted_budget_are_not_refreshed_at_entry(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        token, budget = CancelToken(), GitOperationBudget(60.0)
        if fault == "cancel":
            token.cancel()
        else:
            budget._deadline = time.monotonic() - 1.0
        deadline, before = budget._deadline, _readonly_state(scenario)

        async def forbidden(*_args, **_kwargs):
            pytest.fail("取消或原预算耗尽后不得查询Git")

        with monkeypatch.context() as context:
            context.setattr(scenario.reader, "_run_baseline", forbidden)
            with pytest.raises(TurnCancelled if fault == "cancel" else KernelError) as caught:
                await _verify(scenario, expected, history, cancel=token, budget=budget)
        if fault == "expired-budget":
            assert caught.value.code == "git_process_timeout"
        assert budget._deadline == deadline and _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["cancel", "budget", "parent-task"])
async def test_original_control_reclaims_inflight_real_read_and_does_not_grant_new_budget(
    tmp_path, config, monkeypatch, fault
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        started, reclaimed = asyncio.Event(), asyncio.Event()
        token, budget = CancelToken(), GitOperationBudget(0.5 if fault == "budget" else 60.0)
        deadline = budget._deadline
        original, tasks_before = scenario.reader._run_baseline, asyncio.all_tasks()
        before = _readonly_state(scenario)

        async def waiting(*args, **kwargs):
            result = await original(*args, **kwargs)
            started.set()
            try:
                await asyncio.Event().wait()
                return result
            finally:
                reclaimed.set()

        with monkeypatch.context() as context:
            context.setattr(scenario.reader, "_run_baseline", waiting)
            task = asyncio.create_task(
                _verify(scenario, expected, history, cancel=token, budget=budget)
            )
            try:
                # 未实现时直接传播RED，不把API缺失误报为等待超时。
                entrance = asyncio.create_task(started.wait())
                try:
                    done, _ = await asyncio.wait(
                        {task, entrance}, timeout=5, return_when=asyncio.FIRST_COMPLETED
                    )
                    if task in done:
                        await task
                    assert entrance in done
                finally:
                    entrance.cancel()
                    await asyncio.gather(entrance, return_exceptions=True)
                if fault == "cancel":
                    token.cancel()
                elif fault == "parent-task":
                    task.cancel()
                kind = (
                    TurnCancelled
                    if fault == "cancel"
                    else asyncio.CancelledError
                    if fault == "parent-task"
                    else KernelError
                )
                with pytest.raises(kind) as caught:
                    await asyncio.wait_for(task, 2)
                if fault == "budget":
                    assert caught.value.code in {"git_baseline_timeout", "git_process_timeout"}
                assert reclaimed.is_set() and budget._deadline == deadline
                await asyncio.sleep(0)
                assert not {
                    child for child in asyncio.all_tasks() - tasks_before if not child.done()
                }
                assert _readonly_state(scenario) == before
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("fault", ["cancel", "budget"])
@pytest.mark.parametrize("stage", ["entry", "final"])
async def test_normally_returning_callback_cannot_hide_stop_signals(
    tmp_path, config, monkeypatch, fault, stage
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        total = 0

        def counted():
            nonlocal total
            total += 1

        await _verify(scenario, expected, history, checkpoint=counted)
        target = 1 if stage == "entry" else total
        token, budget, count = CancelToken(), GitOperationBudget(60.0), 0
        before = _readonly_state(scenario)

        def controlled():
            nonlocal count
            count += 1
            if count == target:
                if fault == "cancel":
                    token.cancel()
                else:
                    budget._deadline = time.monotonic() - 1.0

        with pytest.raises(TurnCancelled if fault == "cancel" else KernelError) as caught:
            await _verify(
                scenario, expected, history, cancel=token, budget=budget, checkpoint=controlled
            )
        if fault == "budget":
            assert caught.value.code == "git_process_timeout"
        assert count == target and _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


@pytest.mark.parametrize("binding", ["store-check", "audit-check", "audit-blob"])
async def test_operation_time_read_callback_substitution_is_rejected(
    tmp_path, config, monkeypatch, binding
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        original, changed = scenario.reader._run_baseline, []
        owner, attribute = {
            "store-check": (scenario.transactions, "_checkpoint"),
            "audit-check": (scenario.router._audit, "_checkpoint"),
            "audit-blob": (scenario.router._audit, "_read_blob"),
        }[binding]
        before = _readonly_state(scenario)
        with monkeypatch.context() as context:

            async def mutate(*args, **kwargs):
                result = await original(*args, **kwargs)
                if not changed:
                    context.setattr(owner, attribute, lambda *_args: None)
                    changed.append(True)
                return result

            context.setattr(scenario.reader, "_run_baseline", mutate)
            with pytest.raises(KernelError) as caught:
                await _verify(scenario, expected, history)
            assert caught.value.code == "git_user_observation_host_invalid"
        assert changed and _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)


async def test_private_snapshot_does_not_reuse_mutable_caller_observation(
    tmp_path, config, monkeypatch
):
    async def inspect(scenario):
        expected, history = await _ready(scenario)
        original, changed = scenario.reader._run_baseline, []
        before = _readonly_state(scenario)
        with monkeypatch.context() as context:

            async def mutate(*args, **kwargs):
                result = await original(*args, **kwargs)
                if not changed:
                    object.__setattr__(expected, "config_sha256", "0" * 64)
                    changed.append(True)
                return result

            context.setattr(scenario.reader, "_run_baseline", mutate)
            assert await _verify(scenario, expected, history) is None
        assert changed and expected.config_sha256 == "0" * 64
        assert _readonly_state(scenario) == before

    await run_authenticated_observation(tmp_path, config, monkeypatch, inspect)
