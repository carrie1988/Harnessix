"""认证宿主的真实临时 SQLite／合成 Scope 回归；不请求模型、网络或容器。"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import os
import socket
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    Budget,
    EventDraft,
    ItemFinished,
    ItemStarted,
    ItemStatus,
    TextContent,
    ThreadCreated,
    ToolCallContent,
    ToolResultContent,
    TurnStarted,
    TurnStateChanged,
    TurnStatus,
    Usage,
    UsageRecorded,
)
from harnessix.artifacts.contracts import ArtifactToolResult
from harnessix.domain.models import EffectClass
from harnessix.evals import provider_suite_execution, task_pack_publication, task_pack_trial
from harnessix.evals.provider_suite_execution import TaskPackOpenAIChatProviderFactory
from harnessix.evals.task_pack_execution import _completed_trial
from harnessix.evals.task_pack_publication import (
    PROVIDER_SECRET,
    TaskPackHistoryReadControl,
    open_task_pack_publication,
    provider_publication_scope,
)
from harnessix.product_config.session_key_codec import OwnedSessionKey
from harnessix.product_config.state_owner import product_state_owner
from harnessix.session.sqlite import SQLiteSessionStore
from tests.evals.provider_suite_helpers import provider_suite_config
from tests.evals.test_task_pack_evidence_stop import _write_fixture_trial
from tests.evals.test_task_pack_execution import _case_and_campaign


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("此验证只允许本地 SQLite、合成材料与固定输入")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def scope():
    with provider_publication_scope("FIXTURE_KEY", "fixture-publication-credential") as scoped:
        yield scoped


def run_root(tmp_path):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    return root


def key_facts(root):
    path = root / "session-auth/key.v1"
    info = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), info.st_mtime_ns, info.st_ino


async def test_new_run_owns_same_binding_scope_and_stable_original_key(
    tmp_path, scope, monkeypatch
):
    root, tid = run_root(tmp_path), uuid4()
    async with open_task_pack_publication(root, scope) as owner:
        assert owner.scope is scope
        assert owner.sessions._publication is owner.binding
        assert owner.artifacts.session is owner.sessions
        assert owner.artifacts._publication.binding is owner.binding
        assert owner.artifacts._publication.protection is scope
        owner.root_owner.require_ready(root)
        header = owner.binding.header()
        key_copy = owner.binding._key
        await owner.sessions.append(
            tid,
            [EventDraft(payload=ThreadCreated(workspace=str(root / "workspace")))],
            expected_sequence=0,
        )
        with sqlite3.connect(owner.sessions.path) as db:
            assert db.execute("SELECT seal FROM agent_publication_store").fetchone() == (header,)
            assert db.execute("SELECT COUNT(*) FROM agent_event_publications").fetchone() == (1,)
            assert db.execute("SELECT COUNT(*) FROM agent_projection_publications").fetchone() == (
                1,
            )
    assert not any(key_copy)
    before = key_facts(root)

    def forbidden_create(*args):
        pytest.fail("恢复不得调用 create-or-load")

    monkeypatch.setattr(task_pack_publication, "open_product_session_binding", forbidden_create)
    async with open_task_pack_publication(root, scope, existing_only=True) as owner:
        assert owner.binding.header() == header
        assert (await owner.sessions.get_thread(tid)).sequence == 1
        assert len(await owner.sessions.events(tid)) == 1
    assert key_facts(root) == before


@pytest.mark.parametrize(
    "present",
    ["session.sqlite", "session.sqlite-wal", "report.json", "run-state.json", "session-auth"],
)
async def test_lost_key_never_creates_or_falls_back(tmp_path, scope, monkeypatch, present):
    root = run_root(tmp_path)
    path = root / present
    if present == "session-auth":
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(b"isolated-missing-key-fixture")
        path.chmod(0o600)
    before = path.stat().st_mtime_ns
    monkeypatch.setattr(
        task_pack_publication,
        "open_product_session_binding",
        lambda *args: pytest.fail("丢 Key 不得创建替代身份"),
    )
    with pytest.raises(KernelError) as denied:
        async with open_task_pack_publication(root, scope):
            pytest.fail("缺 Key 的旧状态不得开放")
    assert denied.value.code == "publication_key_unavailable"
    assert not (root / "session-auth/key.v1").exists()
    assert path.stat().st_mtime_ns == before


@pytest.mark.parametrize("kind", ["missing", "corrupt", "wide", "link"])
async def test_original_key_failures_never_replace_material(tmp_path, scope, kind):
    root = run_root(tmp_path)
    async with open_task_pack_publication(root, scope):
        pass
    key = root / "session-auth/key.v1"
    if kind == "missing":
        key.unlink()
    elif kind == "corrupt":
        key.write_bytes(b"invalid-original-key")
    elif kind == "wide":
        key.chmod(0o644)
    else:
        copied = root / "fixture-original-key"
        key.rename(copied)
        key.symlink_to(copied)
    with pytest.raises(KernelError) as denied:
        async with open_task_pack_publication(root, scope, existing_only=True):
            pytest.fail("原 Key 无效不得恢复")
    assert denied.value.code == "publication_key_unavailable"
    if kind == "corrupt":
        assert key.read_bytes() == b"invalid-original-key"
    elif kind == "link":
        assert key.is_symlink()


@pytest.mark.parametrize("nonempty", [False, True])
async def test_legacy_missing_mac_is_not_enrolled_even_with_fixture_key(tmp_path, scope, nonempty):
    root, tid = run_root(tmp_path), uuid4()
    async with open_task_pack_publication(root, scope):
        pass
    path = root / "session.sqlite"
    path.unlink()
    legacy = SQLiteSessionStore(path)
    await legacy.initialize()
    if nonempty:
        await legacy.append(
            tid,
            [EventDraft(payload=ThreadCreated(workspace=str(root / "workspace")))],
            expected_sequence=0,
        )
    with pytest.raises(KernelError) as denied:
        async with open_task_pack_publication(root, scope, existing_only=True):
            pytest.fail("无 MAC 历史不能补签")
    assert denied.value.code == "publication_history_unproven"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM agent_publication_store").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM agent_event_publications").fetchone() == (0,)


async def fixture_trial(tmp_path, scope):
    loaded, expected, campaign = _case_and_campaign()
    runs = tmp_path / "runs"
    runs.mkdir(mode=0o700)
    run_id = campaign.run_ids[0]
    trial = await _write_fixture_trial(
        loaded, runs, expected.case_id, run_id, campaign.environment, publication_scope=scope
    )
    return loaded, expected, campaign, runs, run_id, trial


async def test_completed_and_terminal_paths_share_run_owner_without_provider(tmp_path, scope):
    loaded, expected, campaign, runs, run_id, trial = await fixture_trial(tmp_path, scope)
    root = runs / str(run_id)
    async with open_task_pack_publication(root, scope, existing_only=True) as owner:
        loaded_trial = await task_pack_trial._load_completed_run(
            root, loaded.manifest.case(expected.case_id), campaign.environment, run_id, owner=owner
        )
        terminal = await task_pack_trial._completed_session_turn(owner, trial.workspace, run_id)
        assert loaded_trial.turn == trial.turn
        assert terminal == (trial.state.thread_id, trial.turn)
    reopened = await _completed_trial(
        tmp_path, loaded.manifest.case(expected.case_id), campaign, run_id, scope
    )
    assert reopened.turn == trial.turn


@pytest.mark.parametrize("window", ["completed", "terminal"])
async def test_trial_recovers_both_report_windows_with_original_scope(
    tmp_path, scope, monkeypatch, window
):
    loaded, expected, campaign, runs, run_id, trial = await fixture_trial(tmp_path, scope)
    root = runs / str(run_id)
    materialized = SimpleNamespace(
        case=loaded.manifest.case(expected.case_id),
        run_root=root,
        workspace=trial.workspace,
        manifest=SimpleNamespace(
            run_id=run_id,
            baseline_revision=trial.state.baseline_revision,
            baseline_tree_sha256=trial.state.baseline_tree_sha256,
        ),
    )
    monkeypatch.setattr(task_pack_trial, "materialize_task_pack_case", lambda *args: materialized)
    if window == "terminal":
        (root / "run-state.json").unlink()
        (root / "report.json").unlink()

        async def git(*args, **kwargs):
            return trial.report.git

        monkeypatch.setattr(task_pack_trial, "collect_git_evidence", git)

    def no_provider(*args):
        pytest.fail("报告窗口恢复不得创建 Provider")

    result = await task_pack_trial.run_task_pack_coding_eval(
        loaded,
        runs,
        Path("/missing/git"),
        Path("/missing/docker"),
        expected.case_id,
        run_id,
        no_provider,
        campaign.environment,
        publication_scope=scope,
    )
    assert result.turn == trial.turn
    assert (root / "run-state.json").is_file() and (root / "report.json").is_file()


@pytest.mark.parametrize("fault", ["projection", "event_seal", "header", "key"])
async def test_trial_authentication_precedes_provider(tmp_path, scope, monkeypatch, fault):
    loaded, expected, campaign, runs, run_id, trial = await fixture_trial(tmp_path, scope)
    root = runs / str(run_id)
    for name in ("run-state.json", "report.json"):
        (root / name).unlink()
    if fault == "key":
        (root / "session-auth/key.v1").unlink()
    else:
        with sqlite3.connect(root / "session.sqlite") as db:
            statement = {
                "projection": "UPDATE agent_threads SET snapshot_json='{}'",
                "event_seal": "DELETE FROM agent_event_publications",
                "header": "DELETE FROM agent_publication_store",
            }[fault]
            db.execute(statement)
    materialized = SimpleNamespace(
        case=loaded.manifest.case(expected.case_id),
        run_root=root,
        workspace=trial.workspace,
        manifest=SimpleNamespace(run_id=run_id),
    )
    monkeypatch.setattr(task_pack_trial, "materialize_task_pack_case", lambda *args: materialized)

    def no_provider(*args):
        pytest.fail("认证拒绝必须先于 Provider")

    with pytest.raises(KernelError):
        await task_pack_trial.run_task_pack_coding_eval(
            loaded,
            runs,
            Path("/missing/git"),
            Path("/missing/docker"),
            expected.case_id,
            run_id,
            no_provider,
            campaign.environment,
            publication_scope=scope,
        )


async def test_agent_tool_action_wiring_borrows_identical_scope_and_owner(
    tmp_path, scope, monkeypatch
):
    loaded, expected, campaign = _case_and_campaign()
    root = run_root(tmp_path)
    engine = tmp_path / "fixture-engine"
    engine.write_text("fixture-executable-not-invoked")
    engine.chmod(0o700)
    materialized = SimpleNamespace(
        case=loaded.manifest.case(expected.case_id),
        run_root=root,
        workspace=root / "workspace",
        manifest=SimpleNamespace(run_id=uuid4()),
    )
    async with open_task_pack_publication(root, scope) as owner:
        calls = []

        @asynccontextmanager
        async def provider_factory(*args):
            assert await owner.sessions.thread_ids() == []
            calls.append("provider")
            yield object()

        @asynccontextmanager
        async def tools(workspace, **kwargs):
            assert kwargs["artifacts"] is owner.artifacts
            assert kwargs["git_output_redaction"] is scope
            assert kwargs["git_state_directory"] == root
            yield SimpleNamespace(workspace_scope="a" * 64, workspace_root=object())

        @asynccontextmanager
        async def actions(*args, **kwargs):
            assert kwargs["root_owner"] is owner.root_owner
            assert kwargs["output_redaction"] is scope
            yield SimpleNamespace(gateway=object())

        @asynccontextmanager
        async def agent(sessions, provider, **kwargs):
            assert sessions is owner.sessions
            assert kwargs["artifacts"] is owner.artifacts
            assert kwargs["public_output_protection"] is scope
            yield object()

        async def drive(runtime, borrowed_owner, *args):
            assert borrowed_owner is owner
            return "fixture-no-model-drive"

        monkeypatch.setattr(task_pack_trial, "CodingToolRuntime", tools)
        monkeypatch.setattr(task_pack_trial, "open_default_product_action_runtime", actions)
        monkeypatch.setattr(task_pack_trial, "AgentRuntime", agent)
        monkeypatch.setattr(
            task_pack_trial,
            "build_product_agent_context",
            lambda *args: SimpleNamespace(context=None, compaction=None),
        )
        monkeypatch.setattr(task_pack_trial, "_drive_turn", drive)
        result = await task_pack_trial._run_agent(
            loaded,
            materialized,
            Path("/missing/git"),
            engine,
            provider_factory,
            CancelToken(),
            object(),
            lambda _: None,
            owner,
        )
        assert result == "fixture-no-model-drive" and calls == ["provider"]


async def _publish_fixture_artifact(owner):
    tid, turn_id, item_id, user_id = uuid4(), uuid4(), uuid4(), uuid4()
    user = TextContent(kind="user_message", text="fixture-artifact-publication")
    call = ToolCallContent(
        call_id=uuid4(),
        provider_call_id="fixture",
        tool="grep",
        tool_version="1",
        arguments={"query": "safe"},
        effect_class=EffectClass.READ_ONLY,
    )
    payloads = [
        TurnStarted(request_id="fixture", request_fingerprint="a" * 64, budget=Budget()),
        ItemStarted(item_id=user_id, content=user),
        ItemFinished(item_id=user_id, content=user, status=ItemStatus.COMPLETED),
        TurnStateChanged(status=TurnStatus.PREPARING_CONTEXT),
        TurnStateChanged(status=TurnStatus.CALLING_MODEL),
        ItemStarted(item_id=item_id, content=call),
        ItemFinished(item_id=item_id, content=call, status=ItemStatus.COMPLETED),
        UsageRecorded(step=1, usage=Usage()),
        TurnStateChanged(status=TurnStatus.EXECUTING_TOOLS),
    ]
    thread = await owner.sessions.append(
        tid,
        [
            EventDraft(payload=ThreadCreated(workspace=str(owner.run_root / "workspace"))),
            *(EventDraft(turn_id=turn_id, payload=p) for p in payloads),
        ],
        expected_sequence=0,
    )
    output = ArtifactToolResult(
        result=ToolResultContent(call_id=call.call_id, outcome="succeeded", output={"count": 1}),
        body=b'{"text":"safe fixture"}\n',
        workspace_scope="a" * 64,
        complete=True,
        publisher=owner.artifacts,
    )
    async with owner.sessions.runtime_owner():
        thread = await owner.artifacts.publish(
            tid, turn_id, call, output, expected_sequence=thread.sequence, max_output_chars=10000
        )
    result = thread.turns[0].items[-1].content
    return tid, result.output["artifact"]


@pytest.mark.parametrize("change", [None, "seal", "body"])
async def test_artifact_persists_original_mac_and_rejects_tampering(tmp_path, scope, change):
    from uuid import UUID

    root = run_root(tmp_path)
    async with open_task_pack_publication(root, scope) as owner:
        tid, ref = await _publish_fixture_artifact(owner)
    with sqlite3.connect(root / "session.sqlite") as db:
        original = db.execute("SELECT publication_seal FROM agent_artifacts").fetchone()[0]
        assert isinstance(original, bytes)
        if change == "seal":
            db.execute("UPDATE agent_artifacts SET publication_seal=NULL")
        elif change == "body":
            db.execute("UPDATE agent_artifacts SET body=?", (b'{"text":"evil fixture"}\n',))
    async with open_task_pack_publication(root, scope, existing_only=True) as owner:
        if change is None:
            page = await owner.artifacts.read(tid, "a" * 64, UUID(ref["artifact_id"]))
            assert page.text == '{"text":"safe fixture"}\n'
        else:
            with pytest.raises(KernelError) as denied:
                await owner.artifacts.read(tid, "a" * 64, UUID(ref["artifact_id"]))
            assert denied.value.code == "artifact_publication_unproven"


async def test_output_cannot_issue_secret_from_same_scope(tmp_path, scope):
    root = run_root(tmp_path)
    async with open_task_pack_publication(root, scope) as owner:
        with pytest.raises(KernelError) as denied:
            await owner.artifacts._publication.check_body(
                b'{"text":"fixture-publication-credential"}\n'
            )
        assert denied.value.code == "public_output_secret_leak"


async def test_default_suite_freezes_nonempty_credential_scope_before_provider(
    tmp_path, monkeypatch
):
    config = provider_suite_config(tmp_path)
    config = config.model_copy(
        update={
            "provider_config": config.provider_config.model_copy(
                update={"api_key_env": "R3_FIXTURE_KEY"}
            )
        }
    )
    fake_executor = SimpleNamespace(publication_scope=None, provider_factory=None)
    monkeypatch.setenv(config.provider_config.api_key_env, "fixture-first-key")
    monkeypatch.setattr(provider_suite_execution, "_require_scope", lambda *args: fake_executor)
    created = []

    def native(config, *, api_key):
        created.append(api_key)
        return object()

    monkeypatch.setattr(provider_suite_execution, "OpenAIChatProvider", native)

    async def run(suite, executor, **kwargs):
        assert executor.publication_scope.publication_context()["bindings"]
        assert executor.provider_factory.publication_scope is executor.publication_scope
        monkeypatch.setenv(config.provider_config.api_key_env, "fixture-second-key")
        executor.provider_factory(None, uuid4())
        return "fixture-no-suite-request"

    monkeypatch.setattr(provider_suite_execution, "run_coding_eval_suite", run)
    result = await provider_suite_execution.run_task_pack_provider_suite(config, allow_network=True)
    assert result == "fixture-no-suite-request" and created == ["fixture-first-key"]
    with pytest.raises(KernelError):
        fake_executor.publication_scope.resolve(PROVIDER_SECRET.name)


@pytest.mark.parametrize("provided", ["missing", "empty"])
async def test_real_custom_factory_requires_explicit_nonempty_scope(
    tmp_path, monkeypatch, provided
):
    config = provider_suite_config(tmp_path)
    monkeypatch.setattr(
        provider_suite_execution,
        "_require_scope",
        lambda *args, **kwargs: pytest.fail("无 Scope 不得进入执行宿主"),
    )
    async with open_task_pack_publication(run_root(tmp_path)) as owner:
        with pytest.raises(KernelError) as denied:
            await provider_suite_execution.run_task_pack_provider_suite(
                config,
                allow_network=True,
                provider_factory=lambda *args: None,
                provider_binding_sha256="a" * 64,
                publication_scope=None if provided == "missing" else owner.scope,
            )
    assert denied.value.code == "publication_scope_unavailable"


def test_default_factory_cannot_use_environment_without_scope(monkeypatch):
    monkeypatch.setattr(
        provider_suite_execution,
        "OpenAIChatProvider",
        lambda *args, **kwargs: pytest.fail("不得创建无保护的 Provider"),
    )
    with pytest.raises(KernelError) as denied:
        TaskPackOpenAIChatProviderFactory(None)(None, uuid4())
    assert denied.value.code == "publication_scope_unavailable"


async def test_exception_and_owner_competition_release_key_and_lock(tmp_path, scope):
    root = run_root(tmp_path)
    with pytest.raises(RuntimeError, match="fixture-exit"):
        async with open_task_pack_publication(root, scope) as owner:
            key_copy = owner.binding._key
            with pytest.raises(KernelError) as denied:
                with product_state_owner(root):
                    pytest.fail("同一 Root 不得出现第二 Owner")
            assert denied.value.code == "product_state_busy"
            raise RuntimeError("fixture-exit")
    assert not any(key_copy)
    with product_state_owner(root):
        pass


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_existing_load_settles_and_clears_late_material(tmp_path, scope, monkeypatch, stop):
    root = run_root(tmp_path)
    async with open_task_pack_publication(root, scope):
        pass
    started, release = Event(), Event()
    material = OwnedSessionKey(uuid4(), uuid4(), bytearray(range(32)))
    calls = []

    def held(root):
        calls.append(root)
        started.set()
        assert release.wait(5)
        return material

    monkeypatch.setattr(task_pack_publication, "_existing_material", held)
    if stop == "timeout":
        monkeypatch.setattr(task_pack_publication, "KEY_LOAD_TIMEOUT_SECONDS", 0.05)

    async def opening():
        async with open_task_pack_publication(root, scope, existing_only=True):
            pytest.fail("被取消的加载不得开放 Owner")

    task = asyncio.create_task(opening())
    async with asyncio.timeout(3):
        assert await asyncio.to_thread(started.wait, 3)
        if stop == "cancel":
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
        else:
            await asyncio.sleep(0.08)
        assert not task.done()
        release.set()
        if stop == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(KernelError) as denied:
                await task
            assert denied.value.code == "publication_key_timeout"
    assert calls == [root] and not any(material.key)
    with product_state_owner(root):
        pass


def test_budget_entry_only_forwards_scope_and_keeps_guard_owner():
    # 静态核对实际入口，不调用费用入口、账本、凭据读取或 Provider。
    tree = ast.parse(
        (
            Path(__file__).parents[2] / "scripts" / "run_engineering_provider_suite_budgeted.py"
        ).read_text()
    )
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_budgeted_suite"
    )
    calls = [n for n in ast.walk(function) if isinstance(n, ast.Call)]
    suite = next(
        n
        for n in calls
        if isinstance(n.func, ast.Name) and n.func.id == "run_task_pack_provider_suite"
    )
    assert (
        ast.unparse(next(k.value for k in suite.keywords if k.arg == "publication_scope"))
        == "scope"
    )
    assert any(
        isinstance(n.func, ast.Name) and n.func.id == "GuardedVerificationProvider" for n in calls
    )
    scopes = [
        n
        for n in calls
        if isinstance(n.func, ast.Name) and n.func.id == "provider_publication_scope"
    ]
    assert len(scopes) == 1 and ast.unparse(scopes[0].args[1]) == "key"


def fixture_file_snapshot(root):
    """仅合成旧 Fixture 的文件摘要，不保存正文或材料值。"""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


@pytest.mark.parametrize("failure", ["missing_key", "missing_header", "missing_event_seal"])
async def test_rejected_existing_fixture_keeps_all_file_sha_and_zero_provider_requests(
    tmp_path, scope, monkeypatch, failure
):
    loaded, expected, campaign, runs, run_id, trial = await fixture_trial(tmp_path, scope)
    root = runs / str(run_id)
    if failure == "missing_key":
        (root / "session-auth/key.v1").unlink()
    else:
        with sqlite3.connect(root / "session.sqlite") as db:
            db.execute(
                "DELETE FROM "
                + (
                    "agent_publication_store"
                    if failure == "missing_header"
                    else "agent_event_publications"
                )
            )
        # sqlite3.Connection 上下文只提交、不关闭；冻结旧夹具前结束构造连接。
        db.close()
    before = fixture_file_snapshot(root)
    materialized = SimpleNamespace(
        case=loaded.manifest.case(expected.case_id),
        run_root=root,
        workspace=trial.workspace,
        manifest=SimpleNamespace(run_id=run_id),
    )
    monkeypatch.setattr(task_pack_trial, "materialize_task_pack_case", lambda *args: materialized)
    providers = []

    def forbidden_provider(*args):
        providers.append(args)
        pytest.fail("认证失败不能调用 Provider")

    with pytest.raises(KernelError):
        await task_pack_trial.run_task_pack_coding_eval(
            loaded,
            runs,
            Path("/missing/git"),
            Path("/missing/docker"),
            expected.case_id,
            run_id,
            forbidden_provider,
            campaign.environment,
            publication_scope=scope,
        )
    assert providers == [] and fixture_file_snapshot(root) == before
    if failure == "missing_key":
        assert not (root / "session-auth/key.v1").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX FD 下验证同址目录替换；Windows 原生另验")
@pytest.mark.parametrize("stage", ["completed_read", "terminal_read", "execution", "report"])
async def test_root_identity_is_checked_at_every_borrowed_stage(
    tmp_path, scope, monkeypatch, stage
):
    loaded, expected, campaign = _case_and_campaign()
    case, root = loaded.manifest.case(expected.case_id), run_root(tmp_path)
    materialized = SimpleNamespace(
        case=case,
        run_root=root,
        workspace=root / "workspace",
        manifest=SimpleNamespace(run_id=campaign.run_ids[0]),
    )
    providers, git_reads = [], []

    def no_provider(*args):
        providers.append(args)
        pytest.fail("身份漂移不能创建 Provider")

    async def no_git(*args, **kwargs):
        git_reads.append(args)
        pytest.fail("身份漂移不能收集并发布报告")

    monkeypatch.setattr(task_pack_trial, "collect_git_evidence", no_git)
    with pytest.raises(KernelError):
        async with open_task_pack_publication(root, scope) as owner:
            root.rename(tmp_path / "original-root-fixture")
            root.mkdir(mode=0o700)
            if stage == "completed_read":
                await task_pack_trial._load_completed_run(
                    root, case, campaign.environment, campaign.run_ids[0], owner=owner
                )
            elif stage == "terminal_read":
                await task_pack_trial._completed_session_turn(
                    owner, materialized.workspace, campaign.run_ids[0]
                )
            elif stage == "execution":
                await task_pack_trial._run_agent(
                    loaded,
                    materialized,
                    Path("/missing/git"),
                    Path("/missing/docker"),
                    no_provider,
                    CancelToken(),
                    object(),
                    lambda _: None,
                    owner,
                )
            else:
                await task_pack_trial._grade_and_publish(
                    materialized,
                    Path("/missing/git"),
                    campaign.environment,
                    uuid4(),
                    None,
                    CancelToken(),
                    lambda _: None,
                    owner,
                )
    assert providers == [] and git_reads == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX 目录替换故障注入")
async def test_run_creation_post_load_checks_original_root_identity(tmp_path, scope, monkeypatch):
    root = run_root(tmp_path)
    original = task_pack_publication.open_product_session_binding

    @asynccontextmanager
    async def swapped(root, scope):
        async with original(root, scope) as binding:
            root.rename(tmp_path / "original-root-fixture")
            root.mkdir(mode=0o700)
            yield binding

    monkeypatch.setattr(task_pack_publication, "open_product_session_binding", swapped)
    with pytest.raises(KernelError):
        async with open_task_pack_publication(root, scope):
            pytest.fail("加载期间替换 Root 不能创建 Session")
    assert not (root / "session.sqlite").exists()


async def test_valid_but_other_store_key_rejects_without_resigning(tmp_path, scope):
    root = run_root(tmp_path)
    other = tmp_path / "other-run"
    other.mkdir(mode=0o700)
    async with open_task_pack_publication(root, scope):
        pass
    async with open_task_pack_publication(other, scope):
        pass
    (root / "session-auth/key.v1").write_bytes((other / "session-auth/key.v1").read_bytes())
    before = fixture_file_snapshot(root)
    with pytest.raises(KernelError) as denied:
        async with open_task_pack_publication(root, scope, existing_only=True):
            pytest.fail("另一 Store 的合法 Key 不得认证原 Header")
    assert denied.value.code == "publication_history_unproven"
    assert fixture_file_snapshot(root) == before


async def test_session_symlink_is_rejected_before_any_sqlite_reader(tmp_path, scope):
    root = run_root(tmp_path)
    async with open_task_pack_publication(root, scope):
        pass
    db = root / "session.sqlite"
    original = root / "original-fixture.sqlite"
    db.rename(original)
    db.symlink_to(original)
    before = fixture_file_snapshot(root)
    with pytest.raises(KernelError) as denied:
        async with open_task_pack_publication(root, scope, existing_only=True):
            pytest.fail("不能跟随 Session 叶链接")
    assert denied.value.code == "eval_publication_session_invalid"
    assert db.is_symlink() and fixture_file_snapshot(root) == before


async def test_case_prefix_and_trial_forward_the_exact_scope(tmp_path, scope, monkeypatch):
    from harnessix.evals import task_pack_execution
    from harnessix.evals.task_pack_execution import TaskPackCaseExecutor

    loaded, expected, campaign = _case_and_campaign()
    seen = []

    async def trial(
        loaded, runs, git, container, case_id, run_id, provider, environment, token, **kwargs
    ):
        assert kwargs["publication_scope"] is scope
        if seen:
            raise KernelError("eval_baseline_invalid", "fixture-evidence-stop")
        seen.append(run_id)
        return await _write_fixture_trial(
            loaded, runs, case_id, run_id, environment, publication_scope=scope
        )

    original = task_pack_execution._completed_trial
    reads = []

    async def completed(case_root, case, campaign, run_id, publication_scope, **kwargs):
        assert publication_scope is scope
        reads.append(run_id)
        return await original(case_root, case, campaign, run_id, publication_scope, **kwargs)

    monkeypatch.setattr(task_pack_execution, "run_task_pack_coding_eval", trial)
    monkeypatch.setattr(task_pack_execution, "_completed_trial", completed)

    def no_provider(*args):
        pytest.fail("前缀 Fixture 不得调用 Provider")

    executor = TaskPackCaseExecutor(
        loaded, Path("/missing/git"), Path("/missing/docker"), no_provider, publication_scope=scope
    )
    case_root = tmp_path / "case"
    assert (await executor(expected, campaign, case_root, CancelToken())).reason == "cost_unknown"
    assert (await executor(expected, campaign, case_root, CancelToken())).reason == "cost_unknown"
    assert reads == [campaign.run_ids[0], campaign.run_ids[0]]


async def test_root_owner_is_held_during_materialization_and_report_recovery(
    tmp_path, scope, monkeypatch
):
    loaded, expected, campaign, runs, run_id, trial = await fixture_trial(tmp_path, scope)
    root = runs / str(run_id)
    visited = []

    def materialize(*args):
        with pytest.raises(KernelError) as denied:
            with product_state_owner(root):
                pytest.fail("固定物化读取期间必须持有原 Run Owner")
        assert denied.value.code == "product_state_busy"
        visited.append(root)
        return SimpleNamespace(
            case=loaded.manifest.case(expected.case_id),
            run_root=root,
            workspace=trial.workspace,
            manifest=SimpleNamespace(run_id=run_id),
        )

    monkeypatch.setattr(task_pack_trial, "materialize_task_pack_case", materialize)

    def no_provider(*args):
        pytest.fail("完成恢复不得调用 Provider")

    result = await task_pack_trial.run_task_pack_coding_eval(
        loaded,
        runs,
        Path("/missing/git"),
        Path("/missing/docker"),
        expected.case_id,
        run_id,
        no_provider,
        campaign.environment,
        publication_scope=scope,
    )
    assert result.turn == trial.turn and visited == [root]


async def test_closed_run_owner_cannot_be_borrowed(tmp_path, scope):
    root = run_root(tmp_path)
    async with open_task_pack_publication(root, scope) as owner:
        owner.require_ready(root)
    with pytest.raises(KernelError) as denied:
        owner.require_ready(root)
    assert denied.value.code == "product_state_owner_invalid"


async def test_completed_recovery_requires_original_event_body_mac(tmp_path, scope):
    loaded, expected, campaign, runs, run_id, _ = await fixture_trial(tmp_path, scope)
    root = runs / str(run_id)
    with sqlite3.connect(root / "session.sqlite") as db:
        db.execute("UPDATE agent_events SET event_json='{}' WHERE sequence=1")
    db.close()
    # 原投影及Seal均保持；不能仅依赖缓存投影而接受已篡改的原事件正文。
    with pytest.raises(KernelError) as denied:
        await task_pack_trial._load_completed_run(
            root,
            loaded.manifest.case(expected.case_id),
            campaign.environment,
            run_id,
            publication_scope=scope,
        )
    assert denied.value.code == "publication_history_unproven"


@pytest.mark.parametrize("window", ["completed", "terminal", "active"])
@pytest.mark.parametrize("sequence", [1, 3])
async def test_original_event_mac_precedes_provider_and_runtime(
    tmp_path, scope, monkeypatch, window, sequence
):
    loaded, expected, campaign, runs, run_id, trial = await fixture_trial(tmp_path, scope)
    root = runs / str(run_id)
    if window != "completed":
        (root / "run-state.json").unlink()
        (root / "report.json").unlink()
    if window == "active":
        # 添加新的开放 Turn 会破坏计划身份；用独立新认证夹具模拟原未完成窗口。
        runs = tmp_path / "active-runs"
        runs.mkdir(mode=0o700)
        root = runs / str(run_id)
        root.mkdir(mode=0o700)
        tid, turn_id = uuid4(), uuid4()
        async with open_task_pack_publication(root, scope) as owner:
            await owner.sessions.append(
                tid,
                [
                    EventDraft(payload=ThreadCreated(workspace=str(root / "workspace"))),
                    EventDraft(
                        turn_id=turn_id,
                        payload=TurnStarted(
                            request_id=f"coding-eval:{run_id}",
                            request_fingerprint="a" * 64,
                            budget=loaded.manifest.case(expected.case_id).task.budget,
                        ),
                    ),
                    EventDraft(
                        turn_id=turn_id,
                        payload=ItemStarted(
                            item_id=uuid4(),
                            content=TextContent(kind="user_message", text="fixture-active-history"),
                        ),
                    ),
                ],
                expected_sequence=0,
            )
    with sqlite3.connect(root / "session.sqlite") as db:
        db.execute("UPDATE agent_events SET event_json='{}' WHERE sequence=?", (sequence,))
    db.close()
    settle_fixture_database(root)
    before = fixture_file_snapshot(root)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("execution")
        pytest.fail("原事件验真失败不得进入 Provider、Action 或 AgentRuntime")

    monkeypatch.setattr(
        task_pack_trial,
        "materialize_task_pack_case",
        lambda *args: SimpleNamespace(
            case=loaded.manifest.case(expected.case_id),
            run_root=root,
            workspace=root / "workspace",
            manifest=SimpleNamespace(run_id=run_id),
        ),
    )
    monkeypatch.setattr(task_pack_trial, "AgentRuntime", forbidden)
    monkeypatch.setattr(task_pack_trial, "open_default_product_action_runtime", forbidden)
    with pytest.raises(KernelError) as denied:
        await task_pack_trial.run_task_pack_coding_eval(
            loaded,
            runs,
            Path("/missing/git"),
            Path("/missing/docker"),
            expected.case_id,
            run_id,
            forbidden,
            campaign.environment,
            publication_scope=scope,
        )
    assert denied.value.code == "publication_history_unproven"
    assert calls == [] and fixture_file_snapshot(root) == before
    assert trial.report.outcome != "passed"


async def test_shared_history_phase_uses_original_controls_and_owner(tmp_path, scope, monkeypatch):
    loaded, expected, campaign, runs, run_id, trial = await fixture_trial(tmp_path, scope)
    token = CancelToken()
    calls, callbacks = [], []
    original = SQLiteSessionStore.authenticated_thread_history
    control = TaskPackHistoryReadControl.begin(token)
    async with open_task_pack_publication(runs / str(run_id), scope, existing_only=True) as owner:

        async def read(store, thread_id, *, cancel, deadline, checkpoint):
            assert store is owner.sessions and cancel is token
            assert deadline == control.deadline

            def check():
                callbacks.append(owner)
                checkpoint()

            result = await original(
                store, thread_id, cancel=cancel, deadline=deadline, checkpoint=check
            )
            calls.append((thread_id, deadline, len(result.events)))
            return result

        monkeypatch.setattr(SQLiteSessionStore, "authenticated_thread_history", read)
        monkeypatch.setattr(
            owner.sessions, "get_thread", lambda *args: pytest.fail("不能拼缓存投影")
        )
        result = await task_pack_trial._load_completed_run(
            owner.run_root,
            loaded.manifest.case(expected.case_id),
            campaign.environment,
            run_id,
            owner=owner,
            history_read=control,
        )
        terminal = await task_pack_trial._completed_session_turn(
            owner,
            trial.workspace,
            run_id,
            history_read=control,
        )
        assert result.turn == trial.turn and terminal == (trial.state.thread_id, trial.turn)
    assert len(calls) == 2 and all(count > 3 for _, _, count in calls)
    assert len(callbacks) > 6


async def test_terminal_history_read_is_independent_of_expired_turn_budget(tmp_path, scope):
    loaded, expected, campaign, runs, run_id, trial = await fixture_trial(tmp_path, scope)
    assert task_pack_trial.remaining_seconds(trial.turn) < 0
    settle_fixture_database(runs / str(run_id))
    before = fixture_file_snapshot(runs / str(run_id))
    result = await task_pack_trial._load_completed_run(
        runs / str(run_id),
        loaded.manifest.case(expected.case_id),
        campaign.environment,
        run_id,
        publication_scope=scope,
        history_read=TaskPackHistoryReadControl.begin(CancelToken()),
    )
    assert result.turn == trial.turn
    assert result.turn.budget == loaded.manifest.case(expected.case_id).task.budget
    assert fixture_file_snapshot(runs / str(run_id)) == before


@pytest.mark.parametrize(
    "failure", ["cancel", "deadline", "binding_close", "owner_close", "owner_os"]
)
async def test_original_controls_interrupt_real_history_before_provider(
    tmp_path, scope, monkeypatch, failure
):
    loaded, expected, campaign, runs, run_id, _ = await fixture_trial(tmp_path, scope)
    root = runs / str(run_id)
    token, calls, checkpoints = CancelToken(), [], []
    original = SQLiteSessionStore.authenticated_thread_history
    os_error = OSError("fixture-owner-unavailable")
    root_context = product_state_owner(root)
    root_owner = root_context.__enter__()
    try:
        with pytest.raises((TurnCancelled, KernelError, OSError)) as denied:
            async with open_task_pack_publication(
                root, scope, existing_only=True, root_owner=root_owner
            ) as owner:
                control = TaskPackHistoryReadControl.begin(token)

                async def read(store, tid, *, cancel, deadline, checkpoint):
                    assert cancel is token and deadline == control.deadline

                    def mutate():
                        checkpoint()
                        checkpoints.append(tid)
                        if len(checkpoints) == 3:
                            if failure == "cancel":
                                token.cancel()
                            elif failure == "deadline":
                                monkeypatch.setattr(
                                    "harnessix.session.sqlite_history.monotonic",
                                    lambda: deadline + 1,
                                )
                            elif failure == "binding_close":
                                owner.binding.close()
                            elif failure == "owner_close":
                                root_context.__exit__(None, None, None)
                            else:
                                raise os_error

                    return await original(
                        store, tid, cancel=cancel, deadline=deadline, checkpoint=mutate
                    )

                monkeypatch.setattr(SQLiteSessionStore, "authenticated_thread_history", read)

                def forbidden(*args):
                    calls.append("provider")
                    pytest.fail("认证读取中断不得调用 Provider")

                await task_pack_trial._run_agent(
                    loaded,
                    SimpleNamespace(
                        case=loaded.manifest.case(expected.case_id),
                        run_root=root,
                        workspace=root / "workspace",
                        manifest=SimpleNamespace(run_id=run_id),
                    ),
                    Path("/missing/git"),
                    Path("/missing/docker"),
                    forbidden,
                    token,
                    object(),
                    lambda _: None,
                    owner,
                    history_read=control,
                )
        if failure == "cancel":
            assert isinstance(denied.value, TurnCancelled)
        elif failure == "owner_os":
            assert denied.value is os_error
        else:
            assert (
                denied.value.code
                == {
                    "deadline": "publication_history_timeout",
                    "binding_close": "publication_key_unavailable",
                    "owner_close": "product_state_owner_invalid",
                }[failure]
            )
        assert len(checkpoints) == 3 and calls == []
    finally:
        root_context.__exit__(None, None, None)


async def test_case_prefix_shares_one_deadline_and_original_cancel(tmp_path, scope, monkeypatch):
    from harnessix.evals import task_pack_execution

    loaded, expected, campaign = _case_and_campaign()
    root = tmp_path / "case"
    runs = root / "runs"
    root.mkdir(mode=0o700)
    runs.mkdir(mode=0o700)
    for rid in campaign.run_ids:
        await _write_fixture_trial(
            loaded, runs, expected.case_id, rid, campaign.environment, publication_scope=scope
        )
    token, controls, histories = CancelToken(), [], []
    original_trial = task_pack_execution._completed_trial
    original_read = SQLiteSessionStore.authenticated_thread_history

    async def completed(*args, **kwargs):
        controls.append(kwargs["history_read"])
        assert kwargs["history_read"].cancel is token
        assert args[4] is scope
        return await original_trial(*args, **kwargs)

    async def read(store, tid, **kwargs):
        assert kwargs["cancel"] is token and kwargs["deadline"] == controls[0].deadline
        result = await original_read(store, tid, **kwargs)
        histories.append(result)
        return result

    monkeypatch.setattr(task_pack_execution, "_completed_trial", completed)
    monkeypatch.setattr(SQLiteSessionStore, "authenticated_thread_history", read)
    context = task_pack_execution._CaseExecution(
        loaded,
        Path("/missing/git"),
        Path("/missing/docker"),
        lambda *args: pytest.fail("完成前缀不调用 Provider"),
        None,
        None,
        lambda _: None,
        expected,
        campaign,
        root,
        loaded.manifest.case(expected.case_id),
        scope,
    )
    state = task_pack_execution._load_state(context).model_copy(update={"status": "running"})
    state, completed, _, _ = await task_pack_execution._load_prefix(context, state, token)
    assert state.completed_run_ids == campaign.run_ids and len(completed) == len(campaign.run_ids)
    assert len(controls) == len(campaign.run_ids) == len(histories) > 1
    assert all(control is controls[0] for control in controls)
    assert all(len(history.events) > 3 for history in histories)


async def test_phase_deadline_is_captured_once_and_never_refreshed(tmp_path, scope, monkeypatch):
    root = run_root(tmp_path)
    async with open_task_pack_publication(root, scope) as owner:
        clock = [10.0]
        monkeypatch.setattr(task_pack_publication, "monotonic", lambda: clock[0])
        token = CancelToken()
        control = TaskPackHistoryReadControl.begin(token)
        assert control.deadline == 130.0 and control.cancel is token
        clock[0] = 129.0
        assert await owner.authenticated_single_thread(control) is None
        clock[0] = 130.0
        with pytest.raises(KernelError) as denied:
            await owner.authenticated_single_thread(control)
        assert denied.value.code == "publication_history_timeout" and control.deadline == 130.0
        new_operation = TaskPackHistoryReadControl.begin(token)
        assert new_operation.deadline == 250.0
        assert await owner.authenticated_single_thread(new_operation) is None

        started, settled = asyncio.Event(), []

        async def held_discovery():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                settled.append(True)

        monkeypatch.setattr(owner.sessions, "thread_ids", held_discovery)
        pending = asyncio.create_task(owner.authenticated_thread_ids(new_operation))
        await started.wait()
        token.cancel()
        with pytest.raises(TurnCancelled):
            await pending
        assert settled == [True]

        timed_read = TaskPackHistoryReadControl(CancelToken(), clock[0] + 0.01)
        with pytest.raises(KernelError) as denied:
            await owner.authenticated_thread_ids(timed_read)
        assert denied.value.code == "publication_history_timeout" and len(settled) == 2

        started.clear()
        parent_read = TaskPackHistoryReadControl.begin(CancelToken())
        pending = asyncio.create_task(owner.authenticated_thread_ids(parent_read))
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(settled) == 3


async def test_default_factory_rejects_unsupported_application_secret_version(
    tmp_path, monkeypatch
):
    from harnessix.product_config.contracts import SecretReference
    from harnessix.secrets.provider import EnvironmentSecretProvider, EnvironmentSecretSource
    from harnessix.secrets.publication import SecretPublicationScope

    config = provider_suite_config(tmp_path)
    monkeypatch.setattr(
        provider_suite_execution,
        "OpenAIChatProvider",
        lambda *args, **kwargs: pytest.fail("版本不匹配不得构造 Provider"),
    )
    with SecretPublicationScope(
        (SecretReference(name="eval.provider", version="2"),),
        EnvironmentSecretProvider(
            (EnvironmentSecretSource("eval.provider", "2", "FIXTURE_KEY"),),
            environment={"FIXTURE_KEY": "fixture-second-version"},
        ),
    ) as unsupported:
        with pytest.raises(KernelError) as denied:
            TaskPackOpenAIChatProviderFactory(config.provider_config, unsupported)(None, uuid4())
    assert denied.value.code == "publication_scope_unavailable"


def settle_fixture_database(root):
    """仅整理新建合成夹具的日志模式，以静止主库核对只读恢复前后 SHA。"""
    db = sqlite3.connect(root / "session.sqlite")
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    finally:
        db.close()


@pytest.mark.parametrize("corrupt", [False, True])
async def test_drive_turn_consumes_fresh_complete_history_without_cached_projection(
    tmp_path, scope, monkeypatch, corrupt
):
    loaded, expected, campaign, runs, run_id, trial = await fixture_trial(tmp_path, scope)
    root = runs / str(run_id)
    token, reads = CancelToken(), []
    original = SQLiteSessionStore.authenticated_thread_history
    async with open_task_pack_publication(root, scope, existing_only=True) as owner:
        # 入口前快照已取得；Runtime 恢复之后的驱动必须重新取得完整同读历史。
        await owner.authenticated_thread_history(
            trial.state.thread_id, TaskPackHistoryReadControl.begin(token)
        )
        if corrupt:
            with sqlite3.connect(root / "session.sqlite") as db:
                db.execute("UPDATE agent_events SET event_json='{}' WHERE sequence=3")
            db.close()

        async def read(store, tid, **kwargs):
            assert store is owner.sessions and kwargs["cancel"] is token
            reads.append(kwargs["deadline"])
            return await original(store, tid, **kwargs)

        monkeypatch.setattr(SQLiteSessionStore, "authenticated_thread_history", read)
        monkeypatch.setattr(
            owner.sessions, "get_thread", lambda *args: pytest.fail("驱动不得使用另一个缓存投影")
        )
        operation = task_pack_trial._drive_turn(
            object(),
            owner,
            loaded.manifest.case(expected.case_id),
            trial.workspace,
            run_id,
            token,
            lambda _: pytest.fail("终态只读不得恢复执行"),
        )
        if corrupt:
            with pytest.raises(KernelError) as denied:
                await operation
            assert denied.value.code == "publication_history_unproven"
        else:
            assert await operation == (trial.state.thread_id, trial.turn)
        assert len(reads) == 1


@pytest.mark.parametrize("thread_count", [0, 1, 2])
async def test_owner_single_thread_reads_original_controls_without_cached_projection(
    tmp_path, scope, monkeypatch, thread_count
):
    """认证宿主负责唯一身份；空库和多身份不能进入完整历史或执行恢复。"""
    root = run_root(tmp_path)
    async with open_task_pack_publication(root, scope) as owner:
        ids = [uuid4() for _ in range(thread_count)]
        for tid in ids:
            await owner.sessions.append(
                tid,
                [EventDraft(payload=ThreadCreated(workspace=str(root / "workspace")))],
                expected_sequence=0,
            )
        original = SQLiteSessionStore.authenticated_thread_history
        token, reads = CancelToken(), []
        control = TaskPackHistoryReadControl.begin(token)
        before = key_facts(root)

        async def read(store, tid, **kwargs):
            assert store is owner.sessions and kwargs["cancel"] is token
            assert kwargs["deadline"] == control.deadline
            reads.append(tid)
            return await original(store, tid, **kwargs)

        monkeypatch.setattr(SQLiteSessionStore, "authenticated_thread_history", read)
        monkeypatch.setattr(
            owner.sessions, "get_thread", lambda *args: pytest.fail("不能拼接缓存投影")
        )
        if thread_count > 1:
            with pytest.raises(KernelError) as denied:
                await owner.authenticated_single_thread(control)
            assert denied.value.code == "eval_run_projection_invalid"
            assert not reads
        else:
            thread = await owner.authenticated_single_thread(control)
            if ids:
                assert thread is not None and thread.thread_id == ids[0] and thread.sequence == 1
                assert reads == ids
            else:
                assert thread is None and not reads
        assert key_facts(root) == before
