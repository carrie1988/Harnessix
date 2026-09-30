"""Git交付来源以真实产品Patch和原账本为证，不执行提交或覆盖用户文件。"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.agent.errors import KernelError
from harnessix.agent.models import ItemStatus, ToolCallContent, ToolResultContent
from harnessix.delivery.contracts import new_transaction_record
from harnessix.delivery.trusted_action_contracts import WorkspacePatchFile, WorkspacePatchInput
from harnessix.product_config.git_delivery_source import collect_git_delivery_source
from harnessix.product_config.workspace_patch_source_contracts import ProductGitDeliverySource
from tests.agent.helpers import answer
from tests.delivery.test_trusted_action_patch import _action_step, _approval
from tests.product_config.test_product_patch_rollback import approve, product, publish


@contextmanager
def raises_code(expected):
    """错误码是正式契约，中文异常消息不是测试匹配接口。"""
    with pytest.raises(KernelError) as caught:
        yield
    assert caught.value.code == expected


async def edit(runtime, provider, thread_id, *, before, after, request):
    proposal = WorkspacePatchInput(
        files=(
            WorkspacePatchFile(
                operation="replace",
                path="src/modified.py",
                expected_sha256=hashlib.sha256(before).hexdigest(),
                content=after.decode(),
                mode=0o644,
            ),
        )
    )
    provider.steps = (_action_step(proposal), answer("修改完成"))
    waiting = await runtime.run_turn(thread_id, "修改文件", request_id=request)
    await approve(runtime, thread_id, waiting)
    return _approval(waiting).plan_id


async def source(runtime, thread_id, targets, router, transactions, checkpoint=None):
    thread = await runtime.store.get_thread(thread_id)
    return collect_git_delivery_source(
        thread, targets, router, transactions, checkpoint=checkpoint or CancelToken().checkpoint
    )


async def test_source_freezes_three_operations_without_new_route_or_file_writes(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        routes = router._audit.routes()
        rows = transactions._db.execute("SELECT * FROM workspace_transaction_events").fetchall()
        before = transactions._db.total_changes
        actual = await source(runtime, owner, (target,), router, transactions)
        assert actual.spec_version == "harnessix.product-git-delivery-source/v1"
        assert actual.thread_id == owner and actual.patches[0].transaction_id == target
        assert (
            actual.patches[0].transaction_fingerprint == transactions.load(target).plan.fingerprint
        )
        assert actual.patches[0].route_fingerprint == router.status(target).plan.fingerprint
        assert [item.path for item in actual.mutations] == [
            "src/modified.py",
            "src/新增.py",
            "tests/deleted.txt",
        ]
        assert (root / "src/modified.py").read_bytes() == b"new\n"
        assert (root / "src/新增.py").read_bytes() == "print('新增')\n".encode()
        assert not (root / "tests/deleted.txt").exists()
        assert transactions._db.total_changes == before
        assert (
            transactions._db.execute("SELECT * FROM workspace_transaction_events").fetchall()
            == rows
        )
        assert router._audit.routes() == routes
        assert ProductGitDeliverySource.model_validate_json(actual.model_dump_json()) == actual
        text = actual.model_dump_json()
        assert str(root) not in text and '"content"' not in text


async def test_multiple_patches_merge_in_session_order_not_requested_uuid_order(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, first = await publish(runtime, provider, root)
        second = await edit(
            runtime, provider, owner, before=b"new\n", after=b"final\n", request="second"
        )
        actual = await source(runtime, owner, (second, first), router, transactions)
        assert tuple(item.transaction_id for item in actual.patches) == (first, second)
        modified = actual.mutations[0]
        assert modified.before.sha256 == hashlib.sha256(b"old\n").hexdigest()
        assert modified.after.sha256 == hashlib.sha256(b"final\n").hexdigest()
        assert (root / "src/modified.py").read_bytes() == b"final\n"
        assert await source(runtime, owner, (first, second), router, transactions) == actual


@pytest.mark.parametrize("target_kind", ["unknown", "other-thread", "fork"])
async def test_unowned_source_is_rejected_before_route_transaction_or_blob_reads(
    tmp_path, monkeypatch, target_kind
):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        if target_kind == "unknown":
            selected, target = owner, uuid4()
        elif target_kind == "fork":
            selected = (await runtime.fork_thread(owner, request_id="fork-source")).thread_id
        else:
            selected = (await runtime.create_thread(str(root))).thread_id

        def forbidden(*_args):
            pytest.fail("未授权来源不得读取Route、Transaction或Blob")

        monkeypatch.setattr(router, "status", forbidden)
        monkeypatch.setattr(transactions, "load", forbidden)
        monkeypatch.setattr(transactions, "blob", forbidden)
        with raises_code("git_delivery_source_not_owned"):
            await source(runtime, selected, (target,), router, transactions)


@pytest.mark.parametrize("fault", ["call", "result", "outcome", "fingerprint", "effect-state"])
async def test_session_success_pair_is_mandatory(tmp_path, fault):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        thread = (await runtime.store.get_thread(owner)).model_copy(deep=True)
        items = list(thread.turns[0].items)
        for index, item in enumerate(items):
            content = item.content
            if fault == "call" and isinstance(content, ToolCallContent):
                items[index] = item.model_copy(update={"status": ItemStatus.FAILED})
            if isinstance(content, ToolResultContent):
                if fault == "result":
                    items[index] = item.model_copy(update={"status": ItemStatus.FAILED})
                elif fault == "outcome":
                    items[index] = item.model_copy(
                        update={"content": content.model_copy(update={"outcome": "unknown"})}
                    )
                elif fault in {"fingerprint", "effect-state"}:
                    assert content.trusted_action is not None
                    effect = content.trusted_action.model_copy(
                        update={"plan_fingerprint": "0" * 64}
                        if fault == "fingerprint"
                        else {"state": "unknown"}
                    )
                    items[index] = item.model_copy(
                        update={"content": content.model_copy(update={"trusted_action": effect})}
                    )
        turn = thread.turns[0].model_copy(update={"items": tuple(items)})
        thread = thread.model_copy(update={"turns": (turn, *thread.turns[1:])})
        with raises_code("git_delivery_source_not_owned"):
            collect_git_delivery_source(
                thread, (target,), router, transactions, checkpoint=CancelToken().checkpoint
            )


async def test_published_transaction_is_mandatory_even_with_successful_session(
    tmp_path, monkeypatch
):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        original = transactions.load(target)
        monkeypatch.setattr(transactions, "load", lambda _: new_transaction_record(original.plan))
        with raises_code("git_delivery_source_not_published"):
            await source(runtime, owner, (target,), router, transactions)


@pytest.mark.parametrize("change", ["file", "created", "deleted", "mode", "root"])
async def test_third_content_and_root_replacement_are_preserved(tmp_path, change):
    if change == "mode" and os.name == "nt":
        pytest.skip("Windows不模拟POSIX文件模式")
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        leaf = root / (
            "src/新增.py"
            if change == "created"
            else "tests/deleted.txt"
            if change == "deleted"
            else "src/modified.py"
        )
        if change == "root":
            root.rename(tmp_path / "original-workspace")
            (root / "src").mkdir(parents=True)
            (root / "tests").mkdir()
            leaf.write_bytes(b"new\n")
        elif change == "mode":
            leaf.chmod(0o755)
        else:
            leaf.write_bytes(b"third-content\n")
        before = leaf.read_bytes(), leaf.stat().st_mode
        with raises_code("git_delivery_source_changed"):
            await source(runtime, owner, (target,), router, transactions)
        assert (leaf.read_bytes(), leaf.stat().st_mode) == before


async def test_missing_intermediate_patch_is_not_silently_adopted(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, first = await publish(runtime, provider, root)
        await edit(runtime, provider, owner, before=b"new\n", after=b"middle\n", request="middle")
        last = await edit(
            runtime, provider, owner, before=b"middle\n", after=b"final\n", request="last"
        )
        with raises_code("git_delivery_source_chain_broken"):
            await source(runtime, owner, (first, last), router, transactions)


async def test_unrelated_user_dirty_file_is_not_in_source_or_read(tmp_path, monkeypatch):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        note = root / "user-note.txt"
        note.write_bytes(b"private-unrelated-change\n")
        original = Path.read_bytes

        def guarded(path):
            assert path != note, "不得读取未选中的用户文件正文"
            return original(path)

        monkeypatch.setattr(Path, "read_bytes", guarded)
        actual = await source(runtime, owner, (target,), router, transactions)
        assert "user-note.txt" not in {item.path for item in actual.mutations}
        assert original(note) == b"private-unrelated-change\n"


async def test_net_zero_members_are_verified_but_not_exported(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, first = await publish(runtime, provider, root)
        last = await edit(
            runtime, provider, owner, before=b"new\n", after=b"old\n", request="restore-text"
        )
        actual = await source(runtime, owner, (first, last), router, transactions)
        assert "src/modified.py" not in {item.path for item in actual.mutations}
        (root / "src/modified.py").write_bytes(b"third\n")
        with raises_code("git_delivery_source_changed"):
            await source(runtime, owner, (first, last), router, transactions)


@pytest.mark.parametrize("phase", ["entry", "after-read"])
async def test_cancel_returns_no_source_and_does_not_write(tmp_path, monkeypatch, phase):
    import harnessix.product_config.git_delivery_source as module

    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        token = CancelToken()
        if phase == "entry":
            token.cancel()
        else:
            original = module._read_existing

            def cancel_after_read(*args):
                value = original(*args)
                token.cancel()
                return value

            monkeypatch.setattr(module, "_read_existing", cancel_after_read)
        before = transactions._db.total_changes
        with pytest.raises(TurnCancelled):
            await source(runtime, owner, (target,), router, transactions, token.checkpoint)
        assert transactions._db.total_changes == before
        assert (root / "src/modified.py").read_bytes() == b"new\n"


@pytest.mark.parametrize("targets", [(), ("not-a-uuid",), tuple(uuid4() for _ in range(257))])
async def test_invalid_selection_rejected_before_store_access(tmp_path, targets):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, _ = await publish(runtime, provider, root)
        with raises_code("git_delivery_source_selection_invalid"):
            await source(runtime, owner, targets, router, transactions)


async def test_duplicate_selection_and_forged_digest_rejected(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        with raises_code("git_delivery_source_selection_invalid"):
            await source(runtime, owner, (target, target), router, transactions)
        actual = await source(runtime, owner, (target,), router, transactions)
        body = actual.model_dump(mode="json")
        body["digest"] = "0" * 64
        import json

        with pytest.raises(ValidationError, match="摘要"):
            ProductGitDeliverySource.model_validate_json(json.dumps(body))


async def test_real_git_keeps_head_and_index_and_original_clean_source_contract(tmp_path):
    from harnessix.delivery.git import GitDeliveryRuntime
    from harnessix.delivery.git_store import SQLiteGitDeliveryStore
    from harnessix.workspace.leases import WorkspaceLeaseStore
    from tests.delivery.test_git import _git, _run

    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        _run(root, "init", "-q")
        _run(root, "config", "user.name", "Harnessix Test")
        _run(root, "config", "user.email", "test@harnessix.invalid")
        _run(root, "add", "--", "src/modified.py", "tests/deleted.txt")
        _run(root, "commit", "-q", "-m", "baseline")
        head, index = _run(root, "rev-parse", "HEAD"), _run(root, "write-tree")
        owner, target = await publish(runtime, provider, root)
        dirty = _run(root, "status", "--porcelain=v2", "-z")
        actual = await source(runtime, owner, (target,), router, transactions)
        assert len(actual.mutations) == 3
        with (
            SQLiteGitDeliveryStore(tmp_path / "git-component") as git_store,
            WorkspaceLeaseStore(tmp_path / "git-leases.db") as leases,
        ):
            delivery = GitDeliveryRuntime(transactions, git_store, leases, _git())
            with raises_code("delivery_dirty_conflict"):
                delivery.plan_worktree(target, root)
        assert _run(root, "rev-parse", "HEAD") == head
        assert _run(root, "write-tree") == index
        assert _run(root, "status", "--porcelain=v2", "-z") == dirty


async def test_post_read_drift_does_not_return_stale_source(tmp_path, monkeypatch):
    import harnessix.product_config.git_delivery_source as module

    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        original = module._read_existing

        def drift(*args):
            value = original(*args)
            (root / "src/modified.py").write_bytes(b"changed-during-observation\n")
            return value

        monkeypatch.setattr(module, "_read_existing", drift)
        with pytest.raises(KernelError):
            await source(runtime, owner, (target,), router, transactions)
        assert (root / "src/modified.py").read_bytes() == b"changed-during-observation\n"


async def test_whole_selection_preflight_does_not_read_valid_member_before_unknown(
    tmp_path, monkeypatch
):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)

        def forbidden(*_args):
            pytest.fail("完整选择预检必须先于所有原账本读取")

        monkeypatch.setattr(router, "status", forbidden)
        monkeypatch.setattr(transactions, "load", forbidden)
        with raises_code("git_delivery_source_not_owned"):
            await source(runtime, owner, (target, uuid4()), router, transactions)


async def test_all_net_zero_is_not_an_empty_commit_source(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner = (await runtime.create_thread(str(root))).thread_id
        first = await edit(
            runtime, provider, owner, before=b"old\n", after=b"new\n", request="first"
        )
        last = await edit(runtime, provider, owner, before=b"new\n", after=b"old\n", request="last")
        with raises_code("git_delivery_source_no_change"):
            await source(runtime, owner, (first, last), router, transactions)
        assert (root / "src/modified.py").read_bytes() == b"old\n"


async def test_snapshot_and_final_metadata_must_agree_even_with_recomputed_digest(tmp_path):
    from harnessix.product_config.workspace_patch_source_contracts import (
        product_git_delivery_source_digest,
    )

    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        actual = await source(runtime, owner, (target,), router, transactions)
        first = actual.mutations[0]
        changed = first.model_copy(
            update={"after": first.after.model_copy(update={"sha256": "1" * 64})}
        )
        candidate = actual.model_copy(update={"mutations": (changed, *actual.mutations[1:])})
        import json

        body = candidate.model_dump(mode="json")
        body["digest"] = product_git_delivery_source_digest(candidate)
        with pytest.raises(ValidationError, match="最终版本"):
            ProductGitDeliverySource.model_validate_json(json.dumps(body))


@pytest.mark.parametrize("phase", ["entry", "after-read"])
async def test_original_deadline_callback_propagates_without_stale_source(
    tmp_path, monkeypatch, phase
):
    import harnessix.product_config.git_delivery_source as module
    from harnessix.session.maintenance_io import MaintenanceIOControl

    now = [100.0]
    monkeypatch.setattr("harnessix.session.maintenance_io.monotonic", lambda: now[0])
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        control = MaintenanceIOControl(30)
        if phase == "entry":
            now[0] = 131.0
        else:
            original = module._read_existing

            def expire(*args):
                value = original(*args)
                now[0] = 131.0
                return value

            monkeypatch.setattr(module, "_read_existing", expire)
        before = transactions._db.total_changes
        with raises_code("maintenance_io_timeout"):
            await source(runtime, owner, (target,), router, transactions, control.checkpoint)
        assert transactions._db.total_changes == before
        assert (root / "src/modified.py").read_bytes() == b"new\n"


async def test_successful_session_cannot_replace_failed_original_route(tmp_path, monkeypatch):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        original = router.status(target)
        monkeypatch.setattr(
            router, "status", lambda _: original.model_copy(update={"state": "unknown"})
        )
        with raises_code("git_delivery_source_not_owned"):
            await source(runtime, owner, (target,), router, transactions)


async def test_selected_metadata_capacity_is_bounded_before_workspace_observation(tmp_path):
    from harnessix.product_config.git_delivery_source import _merge_versions
    from harnessix.product_config.workspace_patch_source import load_owned_workspace_patch

    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        owner, target = await publish(runtime, provider, root)
        original = load_owned_workspace_patch(
            await runtime.store.get_thread(owner), target, router, transactions
        )
        first = original.record.plan.mutations[0]
        # 此负对照只检验合并后的容量护栏，不声称合成元数据具备会话授权或实际大文件证明。
        mutations = tuple(
            first.model_copy(update={"path": f"member-{index}.txt"}) for index in range(256)
        )
        from dataclasses import replace

        record = original.record.model_copy(
            update={"plan": original.record.plan.model_copy(update={"mutations": mutations})}
        )
        with raises_code("git_delivery_source_limit"):
            _merge_versions((replace(original, record=record),), CancelToken().checkpoint)
        version = first.after.model_copy(update={"size": 8 * 1024 * 1024})
        large = tuple(
            first.model_copy(
                update={"path": f"member-{index}.txt", "before": version, "after": version}
            )
            for index in range(3)
        )
        record = original.record.model_copy(
            update={"plan": original.record.plan.model_copy(update={"mutations": large})}
        )
        with raises_code("git_delivery_source_limit"):
            _merge_versions((replace(original, record=record),), CancelToken().checkpoint)
