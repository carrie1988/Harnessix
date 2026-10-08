"""Review 分层与真实 Task 单测；原 Owner SQL 不替代 SDK 历史/发布验收。"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.git_authentication_control import GitAuthenticationControl
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import utc_now
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config import git_delivery_review as review
from harnessix.product_config import git_delivery_review_host as hosts
from harnessix.product_config.git_delivery_core_store import ProductGitDeliveryCoreStore
from harnessix.product_config.git_native_control import (
    native_git_checkpoint as _native_checkpointer,
)
from harnessix.secrets.publication import SecretPublicationScope
from harnessix.session.publication_seal import EventPublicationAuthority
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.session.store_publication import SessionPublicationBinding
from harnessix.tools.git import GitReadRuntime, _git_arguments
from harnessix.trusted_actions.router import TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.native_observation_io import UpstreamCheckpointError
from harnessix.workspace.snapshot_ports import WorkspaceSnapshotPorts


class ProbeDone(Exception):
    """在首个生产端口终止，不能把控制单测当成已发布 Review。"""


@pytest.fixture
def host_case(tmp_path):
    # 只准备身份字段；不加载凭据、签发 Seal、调用模型或运行 Git。
    protection = object.__new__(SecretPublicationScope)
    protection._closed = False
    events = object.__new__(EventPublicationAuthority)
    events._closed, events._protection, events._key_id = False, protection, uuid4()
    publication = object.__new__(SessionPublicationBinding)
    publication._closed, publication._events = False, events
    publication._key_id, publication._store_id = events._key_id, uuid4()
    session = SQLiteSessionStore(tmp_path / "sessions.db", publication=publication)
    session._runtime_owner_token = object()
    reader = object.__new__(GitReadRuntime)
    reader._root, reader._executable, reader._state_directory = tmp_path, Path("/usr/bin/git"), None
    reader._output_redaction, reader._for_delivery = protection, True
    reader._binding_fingerprint = "0" * 64
    reader._global_arguments = _git_arguments(for_delivery=True)
    with (
        SQLiteWorkspaceTransactionStore(tmp_path / "workspace-transactions") as transactions,
        SQLiteExecutionPlanStore(tmp_path / "execution-plans.db") as plans,
        SQLiteActionAuditStore(tmp_path / "action-audit.db", require_runtime_owner=True) as audit,
        audit.runtime_owner(),
    ):
        ports = WorkspaceSnapshotPorts(transactions.put_blob, transactions.blob)
        router = TrustedActionRouter(
            plans=plans, audit=audit, workspace_root=lambda _: tmp_path, snapshot_ports=ports
        )
        artifacts = SQLiteArtifactStore(session, public_output_protection=protection)
        core = ProductGitDeliveryCoreStore(transactions)
        arguments = (router, core, artifacts, reader, ports, "a" * 64)
        provider = review.ProductGitReviewProvider(
            *arguments[:4], snapshot_ports=ports, workspace_scope=arguments[-1]
        )
        yield SimpleNamespace(
            args=arguments,
            provider=provider,
            audit=audit,
            session=session,
            publication=publication,
            events=events,
            protection=protection,
            transactions=transactions,
            plans=plans,
            artifacts=artifacts,
            reader=reader,
        )


@pytest.fixture
def frozen():
    directory = os.environ.get("HARNESSIX_REVIEW_FROZEN_DIR")
    if directory is None:
        pytest.skip("冻结 oracle 仅由本次隔离验证显式注入")
    modules = []
    for name in ("git_delivery_review_host", "git_delivery_review"):
        path = Path(directory) / "src" / "harnessix" / "product_config" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_review_before_{name}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    modules[1].require_git_review_host = modules[0].require_git_review_host
    return modules


def test_bound_has_no_owner_sql_or_path_io(host_case, monkeypatch):
    bound, full = hosts._git_review_host_checks(*host_case.args)
    statements = []
    host_case.audit._db.set_trace_callback(statements.append)

    def forbidden(*_args, **_kwargs):
        pytest.fail("bound 不能读取 Owner、SQL 或路径")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(SQLiteActionAuditStore, "_read_runtime_owner", forbidden)
            patch.setattr(hosts, "readonly_database", forbidden)
            patch.setattr(Path, "lstat", forbidden)
            patch.setattr(Path, "stat", forbidden)
            patch.setattr(Path, "resolve", forbidden)
            for _ in range(20):
                bound()
        assert statements == []
        full()
        assert len(statements) == 1 and statements[0].startswith("SELECT ")
    finally:
        host_case.audit._db.set_trace_callback(None)


@pytest.mark.parametrize("existing", [False, True])
def test_metadata_never_calls_reader_contract_even_if_already_shadowed(
    host_case, monkeypatch, existing
):
    calls = []

    def contract():
        calls.append("contract")
        return GitReadRuntime.contract(host_case.reader)

    with monkeypatch.context() as patch:
        if existing:
            patch.setattr(host_case.reader, "contract", contract)
        observer, full = hosts._git_review_host_checks(*host_case.args)
        if not existing:
            patch.setattr(host_case.reader, "contract", contract)
        calls.clear()
        if existing:
            observer()
        else:
            with pytest.raises(KernelError) as caught:
                observer()
            assert caught.value.code == "git_user_observation_host_invalid"
        assert calls == []
        full()
        assert calls == ["contract"] * 4


@pytest.mark.parametrize(
    "target,name,code",
    [
        ("transactions", "put_blob", "git_user_observation_host_invalid"),
        ("transactions", "blob", "git_user_observation_host_invalid"),
        ("audit", "_read_runtime_owner", "git_action_review_host_invalid"),
        ("transactions", "_checkpoint", "git_user_observation_host_invalid"),
        ("audit", "_checkpoint", "git_user_observation_host_invalid"),
        ("audit", "_read_blob", "git_user_observation_host_invalid"),
    ],
)
def test_metadata_rejects_changed_callback_before_calling_it(
    host_case, monkeypatch, target, name, code
):
    observer, _full = hosts._git_review_host_checks(*host_case.args)
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("callback")
        raise AssertionError("metadata executed callback")

    with monkeypatch.context() as patch:
        patch.setattr(getattr(host_case, target), name, forbidden)
        with pytest.raises(KernelError) as caught:
            observer()
        assert caught.value.code == code and not calls


@pytest.mark.parametrize(
    "target,name",
    [
        ("session", "path"),
        ("transactions", "_root"),
        ("audit", "_path"),
        ("plans", "_path"),
        ("reader", "_root"),
        ("reader", "_executable"),
        ("reader", "_state_directory"),
        ("reader", "_global_arguments"),
        ("reader", "_binding_fingerprint"),
        ("reader", "_for_delivery"),
        ("publication", "_closed"),
        ("events", "_closed"),
        ("protection", "_closed"),
        ("audit", "_closed"),
        ("plans", "_closed"),
        ("transactions", "_closed"),
        ("publication", "_store_id"),
        ("publication", "_key_id"),
        ("events", "_key_id"),
    ],
)
def test_metadata_rejects_magic_value_without_equality_bool_or_attribute_callback(
    host_case, target, name
):
    observer, _full = hosts._git_review_host_checks(*host_case.args)
    calls = []

    class Magic:
        def forbidden(self, *_args):
            calls.append("magic")
            raise AssertionError("metadata executed magic")

        __eq__ = __ne__ = __bool__ = __getattr__ = forbidden

    subject = getattr(host_case, target)
    original = object.__getattribute__(subject, name)
    object.__setattr__(subject, name, Magic())
    try:
        with pytest.raises(KernelError) as caught:
            observer()
        assert caught.value.code == "git_user_observation_host_invalid" and not calls
    finally:
        object.__setattr__(subject, name, original)


@pytest.mark.parametrize(
    "target,code",
    [
        ("session", "git_user_observation_host_invalid"),
        ("reader", "git_user_observation_host_invalid"),
        ("publication", "git_user_observation_host_invalid"),
        ("events", "git_user_observation_host_invalid"),
        ("protection", "git_user_observation_host_invalid"),
        ("transactions", "git_user_observation_host_invalid"),
        ("audit", "git_user_observation_host_invalid"),
        ("artifacts", "git_action_review_host_invalid"),
    ],
)
@pytest.mark.parametrize("mapping", ["sparse-key", "dict-subclass"])
def test_metadata_rejects_sparse_keys_and_dict_subclass_before_magic(
    host_case, target, code, mapping
):
    observer, _full = hosts._git_review_host_checks(*host_case.args)
    subject, calls = getattr(host_case, target), []
    original = object.__getattribute__(subject, "__dict__")
    replacement = dict(original)
    armed = [False]

    class Key(str):
        def __hash__(self):
            if armed[0]:
                calls.append("hash")
                raise AssertionError("metadata rehashed sparse key")
            return 37

        def __eq__(self, _other):
            if armed[0]:
                calls.append("eq")
                raise AssertionError("metadata compared sparse key")
            return False

    class Mapping(dict):
        def items(self):
            calls.append("items")
            raise AssertionError("metadata executed dict subclass")

    if mapping == "sparse-key":
        for index in range(80):
            replacement[f"unbound-cache-{index}"] = index
        replacement[Key("unbound-magic")] = object()
        for index in range(80):
            del replacement[f"unbound-cache-{index}"]
    else:
        replacement = Mapping(replacement)
    object.__setattr__(subject, "__dict__", replacement)
    armed[0] = True
    try:
        with pytest.raises(KernelError) as caught:
            observer()
        assert caught.value.code == code and calls == []
    finally:
        object.__setattr__(subject, "__dict__", original)


def test_metadata_ignores_unbound_cache_fields(host_case):
    observer, full = hosts._git_review_host_checks(*host_case.args)
    for subject in [
        host_case.reader,
        host_case.audit,
        host_case.transactions,
        host_case.publication,
        host_case.artifacts,
    ]:
        fields = object.__getattribute__(subject, "__dict__")
        fields["unbound_cache"] = object()
        observer()
        fields["unbound_cache"] = object()
        observer()
        del fields["unbound_cache"]
        observer()
    full()


def test_metadata_checks_native_connection_alive_without_sql_and_keeps_full_error(host_case):
    observer, full = hosts._git_review_host_checks(*host_case.args)
    host_case.audit._db.close()
    with pytest.raises(KernelError) as caught:
        observer()
    assert caught.value.code == "git_action_review_host_invalid"
    with pytest.raises(sqlite3.ProgrammingError):
        full()  # 原bound的连接错误不改写为新的metadata分类。


@pytest.mark.parametrize(
    "slot,code",
    [
        ("store", "git_action_review_host_invalid"),
        ("write_blob", "git_user_observation_host_invalid"),
        ("read_blob", "git_user_observation_host_invalid"),
    ],
)
def test_metadata_reads_exact_slots_without_callback_attribute_lookup(host_case, slot, code):
    observer, _full = hosts._git_review_host_checks(*host_case.args)
    subject = host_case.args[1] if slot == "store" else host_case.args[4]
    original, calls = object.__getattribute__(subject, slot), []

    class Magic:
        def __getattr__(self, _name):
            calls.append("attribute")
            raise AssertionError("metadata executed slotted callback")

    object.__setattr__(subject, slot, Magic())
    try:
        with pytest.raises(KernelError) as caught:
            observer()
        assert caught.value.code == code and calls == []
    finally:
        object.__setattr__(subject, slot, original)


def test_full_bound_and_owner_check_bodies_keep_frozen_ast(frozen):
    def bodies(path, outer):
        tree = ast.parse(Path(path).read_text())
        function = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == outer
        )
        return {
            node.name: [ast.dump(item) for item in node.body]
            for node in function.body
            if isinstance(node, ast.FunctionDef)
        }

    current = bodies(hosts.__file__, "_git_review_host_checks")
    before = bodies(frozen[0].__file__, "require_git_review_host")
    assert current["bound"] == before["bound"]
    assert current["full"] == before["check"]


@pytest.mark.parametrize(
    "change",
    [
        "cancel-method",
        "event-method",
        "budget-method",
        "event-bool",
        "budget-value",
        "provider-dict",
    ],
)
async def test_review_local_progress_never_executes_instance_shadow_or_magic(
    host_case, monkeypatch, change
):
    token, budgets, calls = CancelToken(), [], []
    original_budget = review.GitOperationBudget

    def make_budget(seconds):
        result = original_budget(seconds)
        budgets.append(result)
        return result

    class Magic:
        def forbidden(self, *_args):
            calls.append("magic")
            raise AssertionError("Review local executed shadow or magic")

        __call__ = __bool__ = __rsub__ = __eq__ = __getattr__ = forbidden

    class Mapping(dict):
        def items(self):
            calls.append("items")
            raise AssertionError("Review local executed dict subclass")

    def snapshot(_value, _model, control):
        with control.pure() as local:
            with monkeypatch.context() as patch:
                target, name, value = {
                    "cancel-method": (token, "checkpoint", Magic()),
                    "event-method": (token._event, "is_set", Magic()),
                    "budget-method": (budgets[0], "remaining", Magic()),
                    "event-bool": (token._event, "_value", Magic()),
                    "budget-value": (budgets[0], "_deadline", Magic()),
                    "provider-dict": (
                        host_case.provider,
                        "__dict__",
                        Mapping(vars(host_case.provider)),
                    ),
                }[change]
                patch.setattr(target, name, value)
                local()
        pytest.fail("被改写的局部控制不能交付")

    monkeypatch.setattr(review, "GitOperationBudget", make_budget)
    monkeypatch.setattr(review, "_snapshot", snapshot)
    with pytest.raises(KernelError) as caught:
        await host_case.provider.review(None, None, None, None, token)
    assert caught.value.code == "git_action_review_host_invalid" and calls == []


def test_review_provider_class_stays_within_readability_limit():
    for module in (hosts, review):
        tree = ast.parse(Path(module.__file__).read_text())
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.end_lineno - node.lineno + 1 <= 100, node.name


@pytest.mark.parametrize("field", ["owner_generation", "owner_token_sha256"])
def test_local_is_not_authority_and_full_reads_fresh_owner(host_case, field):
    bound, full = hosts._git_review_host_checks(*host_case.args)
    audit = host_case.audit
    row = audit._db.execute(
        "SELECT value FROM action_audit_metadata WHERE key=?", (field,)
    ).fetchone()
    cursor = audit._db.execute("SELECT key,value FROM action_audit_metadata ORDER BY key")
    assert cursor.fetchone() is not None
    try:
        with closing(sqlite3.connect(audit._path)) as external, external:
            external.execute(
                "UPDATE action_audit_metadata SET value='changed' WHERE key=?", (field,)
            )
        assert not audit._db.in_transaction
        assert (
            audit._db.execute(
                "SELECT value FROM action_audit_metadata WHERE key=?", (field,)
            ).fetchone()
            == row
        )
        bound()  # 内存身份仍有效，不能据此推断持久 Owner 有效。
        with pytest.raises(KernelError) as caught:
            full()
        assert caught.value.code == "action_runtime_fence_lost"
        with pytest.raises(KernelError) as caught:
            hosts.require_git_review_host(*host_case.args)
        assert caught.value.code == "action_runtime_fence_lost"
    finally:
        cursor.close()
        audit._db.execute("UPDATE action_audit_metadata SET value=? WHERE key=?", (row[0], field))
    full()


@pytest.mark.parametrize("layer", ["bound", "full"])
@pytest.mark.parametrize(
    "change",
    [
        "session_owner",
        "artifact_session",
        "guard",
        "guard_binding",
        "guard_protection",
        "fence_copy",
        "fence_generation",
        "owner_optional",
        "connection",
        "transaction",
        "audit_closed",
        "plans_closed",
        "cas_closed",
        "publication_closed",
        "events_closed",
        "protection_closed",
        "reader_binding",
        "reader_implementation",
        "reader_root",
    ],
)
def test_original_identity_drift_rejected_in_both_layers(host_case, monkeypatch, layer, change):
    bound, full = hosts._git_review_host_checks(*host_case.args)
    case, audit = host_case, host_case.audit
    guard = case.artifacts._publication
    replacements = {
        "session_owner": (case.session, "_runtime_owner_token", object()),
        "artifact_session": (case.artifacts, "_session", object()),
        "guard": (case.artifacts, "_publication", object()),
        "guard_binding": (guard, "binding", object()),
        "guard_protection": (guard, "protection", object()),
        "fence_copy": (audit, "_runtime_fence", audit._runtime_fence.model_copy(deep=True)),
        "fence_generation": (
            audit._runtime_fence,
            "generation",
            audit._runtime_fence.generation + 1,
        ),
        "owner_optional": (audit, "_require_runtime_owner", False),
        "connection": (audit, "_db", sqlite3.connect(":memory:")),
        "audit_closed": (audit, "_closed", True),
        "plans_closed": (case.plans, "_closed", True),
        "cas_closed": (case.transactions, "_closed", True),
        "publication_closed": (case.publication, "_closed", True),
        "events_closed": (case.events, "_closed", True),
        "protection_closed": (case.protection, "_closed", True),
        "reader_binding": (case.reader, "_binding_fingerprint", "1" * 64),
        "reader_implementation": (case.reader, "_for_delivery", False),
        "reader_root": (case.reader, "_root", Path("/different-root")),
    }
    try:
        with monkeypatch.context() as patch:
            if change == "transaction":
                audit._db.execute("BEGIN")
            elif change == "fence_generation":
                target, name, value = replacements[change]
                original = getattr(target, name)
                object.__setattr__(target, name, value)
            else:
                patch.setattr(*replacements[change])
            with pytest.raises(KernelError) as caught:
                (bound if layer == "bound" else full)()
            original_authority_first = change.endswith("_closed") or change.startswith("reader_")
            assert caught.value.code == (
                "git_user_observation_host_invalid"
                if original_authority_first
                else "git_action_review_host_invalid"
            )
    finally:
        replacements["connection"][2].close()
        if audit._db.in_transaction:
            audit._db.execute("ROLLBACK")
        if change == "fence_generation":
            object.__setattr__(target, name, original)
    full()


def test_public_host_initial_and_recheck_trace_matches_frozen(host_case, frozen, monkeypatch):
    def run(module):
        trace = []
        authority = module.require_git_user_authority
        owner, fresh, identity = (
            host_case.audit._read_runtime_owner,
            module.readonly_database,
            module._audit_file_identity,
        )

        def require(*args):
            trace.append("authority_init")
            original = authority(*args)

            def bound():
                trace.append("bound")
                original()

            return bound

        def read(*, database=None):
            trace.append("owner_original" if database is None else "owner_fresh")
            return owner(database=database)

        def observe(path):
            trace.append("open_readonly")
            return fresh(path)

        def path_identity(path):
            trace.append("path_identity")
            return identity(path)

        with monkeypatch.context() as patch:
            patch.setattr(module, "require_git_user_authority", require)
            patch.setattr(module, "readonly_database", observe)
            patch.setattr(module, "_audit_file_identity", path_identity)
            patch.setattr(host_case.audit, "_read_runtime_owner", read)
            module.require_git_review_host(*host_case.args)()
        return trace

    assert run(hosts) == run(frozen[0])


async def test_control_created_in_managed_child_with_parent_closures(host_case, monkeypatch):
    parent, delivered = asyncio.current_task(), []
    original = hosts._git_review_host_checks

    def require(*args):
        assert asyncio.current_task() is parent
        return original(*args)

    def snapshot(value, _model, check):
        assert type(check) is GitAuthenticationControl
        origin = GitAuthenticationControl._binding(check)
        assert origin[2] is asyncio.current_task() and origin[2] is not parent
        check()  # 普通 _produce 检查仍走 full。
        with check.pure() as local:
            statements = []
            host_case.audit._db.set_trace_callback(statements.append)
            try:
                for _ in range(20):
                    local()
                assert not statements
            finally:
                host_case.audit._db.set_trace_callback(None)
        assert check._segment is None
        delivered.append(check)
        raise ProbeDone

    monkeypatch.setattr(review, "_git_review_host_checks", require)
    monkeypatch.setattr(review, "_snapshot", snapshot)
    with pytest.raises(ProbeDone):
        await host_case.provider.review(None, None, None, None, CancelToken())
    assert len(delivered) == 1


@pytest.mark.parametrize("segment", ["pure", "io_progress"])
@pytest.mark.parametrize("phase", ["entry", "exit"])
@pytest.mark.parametrize("field", ["owner_generation", "owner_token_sha256"])
async def test_segment_boundaries_keep_independent_fresh_owner(
    host_case, monkeypatch, segment, phase, field
):
    audit, observers, locals_seen = host_case.audit, [], []
    row = audit._db.execute(
        "SELECT value FROM action_audit_metadata WHERE key=?", (field,)
    ).fetchone()
    cursor = audit._db.execute("SELECT key,value FROM action_audit_metadata ORDER BY key")
    assert cursor.fetchone() is not None
    factory = hosts.readonly_database

    def observe(path):
        result = factory(path)
        observers.append(result)
        return result

    def drift():
        with closing(sqlite3.connect(audit._path)) as external, external:
            external.execute(
                "UPDATE action_audit_metadata SET value='changed' WHERE key=?", (field,)
            )
        assert not audit._db.in_transaction
        assert (
            audit._db.execute(
                "SELECT value FROM action_audit_metadata WHERE key=?", (field,)
            ).fetchone()
            == row
        )

    def snapshot(_value, _model, check):
        if phase == "entry":
            drift()
        with getattr(check, segment)() as local:
            local()
            count = len(observers)
            drift()
            local()
            assert len(observers) == count
            locals_seen.append(True)
        pytest.fail("失效的持久 Owner 不能穿过 full 出口")

    monkeypatch.setattr(hosts, "readonly_database", observe)
    monkeypatch.setattr(review, "_snapshot", snapshot)
    try:
        with pytest.raises(KernelError) as caught:
            await host_case.provider.review(None, None, None, None, CancelToken())
        assert caught.value.code == "action_runtime_fence_lost"
        assert len(observers) == (3 if phase == "entry" else 4)
        assert locals_seen == ([] if phase == "entry" else [True])
        for observer in observers:
            with pytest.raises(sqlite3.ProgrammingError):
                observer.execute("SELECT 1")
    finally:
        cursor.close()
        audit._db.execute("UPDATE action_audit_metadata SET value=? WHERE key=?", (row[0], field))


@pytest.mark.parametrize("surface", ["task", "thread"])
async def test_external_task_or_thread_only_gets_full(host_case, monkeypatch, surface):
    calls, saved = [], []
    original = hosts._git_review_host_checks

    def require(*args):
        bound, authenticate = original(*args)

        def local():
            calls.append("local")
            bound()

        def full():
            calls.append("full")
            authenticate()

        return local, full

    async def history(*args):
        check = args[-1]
        assert type(check) is GitAuthenticationControl
        origin = GitAuthenticationControl._binding(check)
        with check.pure() as active:
            calls.clear()

            async def use():
                assert type(_native_checkpointer(check)) is not GitAuthenticationControl
                active()
                with check.pure() as foreign:
                    foreign()

            if surface == "task":
                await asyncio.create_task(use())
                assert calls == ["full"] * 4
            else:
                # 不给 SQLite 原连接跨线程权限；仍先走 full 而不是 local。
                with pytest.raises(sqlite3.ProgrammingError):
                    await asyncio.to_thread(lambda: asyncio.run(use()))
                assert calls == ["full"]
            assert GitAuthenticationControl._binding(check) is origin
            saved.append((check, active))
        raise ProbeDone

    monkeypatch.setattr(review, "_git_review_host_checks", require)
    monkeypatch.setattr(review, "_snapshot", lambda value, _model, _check: value)
    monkeypatch.setattr(review, "_history", history)
    with pytest.raises(ProbeDone):
        await host_case.provider.review(None, None, None, None, CancelToken())
    assert saved[0][0]._segment is None
    calls.clear()
    saved[0][1]()  # 受管 Task 结算后的保存检查点也必须撤销。
    assert calls == ["full"]


@pytest.mark.parametrize("phase", ["entry", "local", "exit"])
@pytest.mark.parametrize("kind", ["cancel", "deadline", "kernel", "upstream", "nested"])
async def test_first_failure_identity_not_masked_by_exit(host_case, monkeypatch, phase, kind):
    marker = {
        "cancel": TurnCancelled(),
        "deadline": KernelError("git_process_timeout", "first deadline"),
        "kernel": KernelError("git_action_review_host_invalid", "first same-code failure"),
        "upstream": UpstreamCheckpointError(ValueError("first")),
        "nested": UpstreamCheckpointError(UpstreamCheckpointError(ValueError("first"))),
    }[kind]
    armed, events = [False], []
    original_checks, original_owner = (
        hosts._git_review_host_checks,
        host_case.audit._read_runtime_owner,
    )

    def require(*args):
        observer, full = original_checks(*args)

        def local():
            observer()
            if armed[0] and phase == "local":
                events.append("first")
                raise marker

        return local, full

    def owner(*, database=None):
        if armed[0] and phase != "local":
            events.append("first")
            raise marker
        return original_owner(database=database)

    def snapshot(_value, _model, check):
        assert type(check) is GitAuthenticationControl
        if phase == "entry":
            armed[0] = True
        with check.pure() as local:
            armed[0] = True
            local()
        pytest.fail("失败后不能交付")

    monkeypatch.setattr(review, "_git_review_host_checks", require)
    monkeypatch.setattr(host_case.audit, "_read_runtime_owner", owner)
    monkeypatch.setattr(review, "_snapshot", snapshot)
    with pytest.raises(type(marker)) as caught:
        await host_case.provider.review(None, None, None, None, CancelToken())
    assert caught.value is marker and events == ["first"]


async def test_parent_cancel_reaches_child_local(host_case, monkeypatch):
    parent = asyncio.current_task()
    observed = []

    def snapshot(_value, _model, check):
        with check.pure() as local:
            parent.cancel()
            with pytest.raises(asyncio.CancelledError):
                local()
            observed.append(True)
        raise ProbeDone

    monkeypatch.setattr(review, "_snapshot", snapshot)
    try:
        with pytest.raises(asyncio.CancelledError):
            await host_case.provider.review(None, None, None, None, CancelToken())
    finally:
        parent.uncancel()
    assert observed == [True]


@pytest.mark.parametrize("layer", ["local", "full"])
@pytest.mark.parametrize(
    "field",
    [
        "_router",
        "_core_store",
        "_artifacts",
        "_reader",
        "_ports",
        "_workspace_scope",
    ],
)
async def test_provider_frozen_references_checked_at_original_stage(
    host_case, monkeypatch, layer, field
):
    sql = []

    def snapshot(_value, _model, check):
        with check.pure() as local:
            with monkeypatch.context() as patch:
                patch.setattr(host_case.provider, field, object())
                host_case.audit._db.set_trace_callback(sql.append)
                try:
                    (local if layer == "local" else check)()
                finally:
                    host_case.audit._db.set_trace_callback(None)
        pytest.fail("替换 Provider 原引用不能交付")

    monkeypatch.setattr(review, "_snapshot", snapshot)
    with pytest.raises(KernelError) as caught:
        await host_case.provider.review(None, None, None, None, CancelToken())
    assert caught.value.code == "git_action_review_host_invalid"
    assert len(sql) == (0 if layer == "local" else 1)


@pytest.mark.parametrize("change", ["cancel", "deadline"])
async def test_real_cancel_or_shared_budget_precedes_host_and_exit(host_case, monkeypatch, change):
    token, budgets, observed = CancelToken(), [], []
    original_budget = review.GitOperationBudget

    def budget(seconds):
        assert seconds == 60.0
        result = original_budget(seconds)
        budgets.append(result)
        return result

    def snapshot(_value, _model, check):
        with check.pure() as local:
            if change == "cancel":
                token.cancel()
            else:
                budgets[0]._deadline = time.monotonic() - 1
            with monkeypatch.context() as patch:
                patch.setattr(
                    host_case.audit, "_read_runtime_owner", lambda **_: observed.append(True)
                )
                patch.setattr(host_case.audit, "_closed", True)
                local()
        pytest.fail("取消或耗尽同一期限不能交付")

    monkeypatch.setattr(review, "GitOperationBudget", budget)
    monkeypatch.setattr(review, "_snapshot", snapshot)
    with pytest.raises(TurnCancelled if change == "cancel" else KernelError) as caught:
        await host_case.provider.review(None, None, None, None, token)
    if change == "deadline":
        assert caught.value.code == "git_process_timeout"
    assert len(budgets) == 1 and observed == []


@pytest.mark.parametrize("failure", [False, True])
async def test_unknown_provider_preserves_frozen_trace(host_case, frozen, monkeypatch, failure):
    marker = KernelError("git_action_review_host_invalid", "original failure")

    async def run(module):
        trace = []

        class Unknown(module.ProductGitReviewProvider):
            def __getattribute__(self, name):
                if name in {
                    "_router",
                    "_core_store",
                    "_artifacts",
                    "_reader",
                    "_ports",
                    "_workspace_scope",
                }:
                    trace.append(("provider_read", name))
                return super().__getattribute__(name)

        provider = Unknown(
            *host_case.args[:4], snapshot_ports=host_case.args[4], workspace_scope=host_case.args[5]
        )
        original_host = module.require_git_review_host

        def require(*args):
            trace.append("host_init")
            original = original_host(*args)

            def full():
                trace.append("full")
                original()

            return full

        async def produce(*args, **kwargs):
            check = args[-1]
            assert type(check) is not GitAuthenticationControl
            trace.append("produce")
            check()
            await asyncio.sleep(0)
            check()
            if failure:
                raise marker
            return SimpleNamespace(
                diff_artifact=SimpleNamespace(expires_at=utc_now() + timedelta(1))
            )

        with monkeypatch.context() as patch:
            patch.setattr(module, "require_git_review_host", require)
            patch.setattr(module, "_produce", produce)
            if failure:
                with pytest.raises(KernelError) as caught:
                    await provider.review(None, None, None, None, CancelToken())
                assert caught.value is marker
            else:
                await provider.review(None, None, None, None, CancelToken())
        return trace

    assert await run(review) == await run(frozen[1])


async def test_native_adapter_preserves_exact_same_task_and_nested_error():
    marker = UpstreamCheckpointError(UpstreamCheckpointError(ValueError("original")))
    calls = []

    def local():
        calls.append("local")
        raise marker

    control = GitAuthenticationControl(local, lambda: calls.append("full"))
    protected = _native_checkpointer(control)
    assert type(protected) is GitAuthenticationControl
    with pytest.raises(UpstreamCheckpointError) as caught:
        with protected.io_progress() as check:
            check()
    assert caught.value.error is marker and calls == ["full", "local"]
