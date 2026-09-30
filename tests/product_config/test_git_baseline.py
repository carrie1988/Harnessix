"""真实产品Patch、原SQLite账本及真实Git的只读交付基准验收。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.cancellation import CancelToken, TurnCancelled
from harnessix.product_config import git_baseline as baseline_module
from harnessix.product_config.git_baseline import collect_product_git_baseline
from harnessix.product_config.git_baseline_contracts import (
    ProductGitDeliveryBaseline,
    product_git_baseline_digest,
)
from harnessix.tools.contracts import ReadToolError
from harnessix.tools.git import GitReadRuntime
from tests.product_config.test_git_delivery_source import edit, raises_code
from tests.product_config.test_product_patch_rollback import product, publish
from tests.tools.test_git import _git


def command(root: Path, *arguments: str, input_body: bytes | None = None) -> bytes:
    """测试准备命令与受测只读端口分离；不继承宿主凭据或Git配置。"""
    executable = _git()
    environment = {
        "PATH": str(executable.parent) + os.pathsep + os.defpath,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
    }
    if os.name == "nt":
        environment.update(SystemRoot=os.environ["SystemRoot"], WINDIR=os.environ["SystemRoot"])
    return subprocess.run(
        (str(executable), "-c", "core.hooksPath=" + os.devnull, *arguments),
        cwd=root,
        check=True,
        timeout=10,
        stdin=subprocess.DEVNULL if input_body is None else None,
        input=input_body,
        capture_output=True,
        env=environment,
    ).stdout


def repository(root: Path, *, object_format: str = "sha1") -> None:
    command(root, "init", "-q", "--object-format=" + object_format)
    command(root, "config", "user.name", "Harnessix Test")
    command(root, "config", "user.email", "test@harnessix.invalid")
    command(root, "config", "core.autocrlf", "false")
    command(root, "add", "src", "tests")
    command(root, "commit", "-qm", "baseline")


def reader(root: Path, state: Path, *, for_delivery: bool = True) -> GitReadRuntime:
    return GitReadRuntime(root, _git(), state_directory=state, for_delivery=for_delivery)


async def collect(runtime, owner, target, router, transactions, port, *, cancel=None):
    thread = await runtime.store.get_thread(owner)
    return await collect_product_git_baseline(
        thread, (target,), router, transactions, port, cancel=cancel or CancelToken()
    )


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
async def test_three_operations_bind_original_head_without_writing_index_or_refs(
    tmp_path, object_format
):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root, object_format=object_format)
        original_head = command(root, "rev-parse", "HEAD").strip().decode()
        original_index = (root / ".git/index").read_bytes()
        owner, target = await publish(runtime, provider, root)
        routes, changes = router._audit.routes(), transactions._db.total_changes
        actual = await collect(
            runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
        )
        assert actual.head_oid == original_head
        assert len(actual.head_oid) == (40 if object_format == "sha1" else 64)
        assert [member.path for member in actual.members] == [
            "src/modified.py",
            "src/新增.py",
            "tests/deleted.txt",
        ]
        assert actual.members[1].oid is None and actual.members[1].mode is None
        assert actual.members[0].mode == actual.members[2].mode == "100644"
        assert ProductGitDeliveryBaseline.model_validate_json(actual.model_dump_json()) == actual
        assert (root / ".git/index").read_bytes() == original_index
        assert command(root, "rev-parse", "HEAD").strip().decode() == original_head
        assert transactions._db.total_changes == changes and router._audit.routes() == routes
        assert (root / "src/modified.py").read_bytes() == b"new\n"
        assert (
            str(root) not in actual.model_dump_json()
            and '"content"' not in actual.model_dump_json()
        )


async def test_unrelated_staged_and_dirty_paths_are_preserved_and_not_in_members(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        (root / "src/user.py").write_bytes(b"original\n")
        repository(root)
        (root / "src/user.py").write_bytes(b"staged\n")
        command(root, "add", "src/user.py")
        (root / "src/user.py").write_bytes(b"user worktree\n")
        owner, target = await publish(runtime, provider, root)
        index = (root / ".git/index").read_bytes()
        actual = await collect(
            runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
        )
        assert "src/user.py" not in {member.path for member in actual.members}
        assert (root / ".git/index").read_bytes() == index
        assert (root / "src/user.py").read_bytes() == b"user worktree\n"
        assert command(root, "show", ":src/user.py") == b"staged\n"


async def test_continuous_patches_use_first_before_and_final_after(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        owner, first = await publish(runtime, provider, root)
        second = await edit(
            runtime, provider, owner, before=b"new\n", after=b"final\n", request="second"
        )
        thread = await runtime.store.get_thread(owner)
        actual = await collect_product_git_baseline(
            thread,
            (second, first),
            router,
            transactions,
            reader(root, tmp_path / "git-state"),
            cancel=CancelToken(),
        )
        assert [patch.transaction_id for patch in actual.source.patches] == [first, second]
        assert actual.source.mutations[0].before.sha256 == hashlib.sha256(b"old\n").hexdigest()
        assert actual.source.mutations[0].after.sha256 == hashlib.sha256(b"final\n").hexdigest()


@pytest.mark.parametrize(
    "fault", ["user-before", "selected-staged", "assume", "skip", "new-staged"]
)
async def test_user_content_or_selected_index_overlap_is_rejected(tmp_path, fault):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        if fault == "user-before":
            (root / "src/modified.py").write_bytes(b"not source\n")
        repository(root)
        if fault == "user-before":
            (root / "src/modified.py").write_bytes(b"old\n")
        owner, target = await publish(runtime, provider, root)
        if fault in {"selected-staged", "new-staged"}:
            command(root, "add", "src/新增.py" if fault == "new-staged" else "src/modified.py")
        elif fault in {"assume", "skip"}:
            command(
                root,
                "update-index",
                "--assume-unchanged" if fault == "assume" else "--skip-worktree",
                "src/modified.py",
            )
        index = (root / ".git/index").read_bytes()
        with raises_code(
            "git_baseline_before_mismatch"
            if fault == "user-before"
            else "git_baseline_index_conflict"
        ):
            await collect(
                runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
            )
        assert (root / ".git/index").read_bytes() == index


@pytest.mark.parametrize("target_kind", ["unknown", "other-thread", "fork"])
async def test_invalid_ownership_never_queries_git(tmp_path, monkeypatch, target_kind):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        owner, target = await publish(runtime, provider, root)
        if target_kind == "unknown":
            target = uuid4()
        elif target_kind == "other-thread":
            owner = (await runtime.create_thread(str(root))).thread_id
        else:
            owner = (await runtime.fork_thread(owner, request_id="fork-baseline")).thread_id
        port = reader(root, tmp_path / "git-state")

        async def forbidden(*_args, **_kwargs):
            pytest.fail("来源归属拒绝之前不得查询Git")

        monkeypatch.setattr(port, "_run", forbidden)
        with raises_code("git_delivery_source_not_owned"):
            await collect(runtime, owner, target, router, transactions, port)


@pytest.mark.parametrize("wrong_port", ["default", "different-root"])
async def test_wrong_purpose_or_same_contents_in_other_repository_are_rejected_before_git(
    tmp_path, monkeypatch, wrong_port
):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        other = tmp_path / "other"
        shutil.copytree(root, other)
        owner, target = await publish(runtime, provider, root)
        port = reader(
            other if wrong_port == "different-root" else root,
            tmp_path / "git-state",
            for_delivery=wrong_port != "default",
        )

        async def forbidden(*_args, **_kwargs):
            pytest.fail("根或用途不匹配不能查询Git")

        monkeypatch.setattr(port, "_run", forbidden)
        with raises_code(
            "git_baseline_reader_required"
            if wrong_port == "default"
            else "git_baseline_workspace_mismatch"
        ):
            await collect(runtime, owner, target, router, transactions, port)


@pytest.mark.parametrize(
    "key",
    [
        "core.sparseCheckout",
        "extensions.partialClone",
        "remote.origin.promisor",
        "filter.bad.process",
        "include.path",
    ],
)
async def test_unsafe_configuration_is_rejected_without_running_external_helper(tmp_path, key):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        owner, target = await publish(runtime, provider, root)
        marker = tmp_path / "helper-ran"
        helper = tmp_path / "helper"
        helper.write_text("#!/bin/sh\necho ran > '" + str(marker) + "'\n", encoding="utf-8")
        helper.chmod(0o700)
        command(
            root,
            "config",
            key,
            "true" if key.endswith(("sparseCheckout", "promisor")) else str(helper),
        )
        with raises_code(
            "git_baseline_unavailable"
            if key.startswith(("filter.", "include."))
            else "git_baseline_config_unsupported"
        ):
            await collect(
                runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
            )
        assert not marker.exists()


@pytest.mark.parametrize("drift", ["head", "index", "status"])
async def test_repository_drift_between_observations_is_rejected(tmp_path, monkeypatch, drift):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        owner, target = await publish(runtime, provider, root)
        original = baseline_module._observe
        observed = 0

        async def changed(query):
            nonlocal observed
            observed += 1
            if observed == 2:
                if drift == "head":
                    command(root, "commit", "--allow-empty", "-qm", "external change")
                else:
                    (root / "user.txt").write_bytes(b"user change\n")
                    if drift == "index":
                        command(root, "add", "user.txt")
            return await original(query)

        monkeypatch.setattr(baseline_module, "_observe", changed)
        with raises_code("git_baseline_changed"):
            await collect(
                runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
            )


@pytest.mark.parametrize("fault", ["cancel-before", "cancel-during", "timeout", "io"])
async def test_cancel_and_query_failure_never_returns_a_baseline(tmp_path, monkeypatch, fault):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        owner, target = await publish(runtime, provider, root)
        port, cancel = reader(root, tmp_path / "git-state"), CancelToken()
        original = port._run

        async def interrupted(*args, **kwargs):
            if fault == "cancel-during":
                cancel.cancel()
                cancel.checkpoint()
            if fault == "timeout":
                raise TimeoutError
            if fault == "io":
                raise ReadToolError("io_failed")
            return await original(*args, **kwargs)

        if fault == "cancel-before":
            cancel.cancel()
        else:
            monkeypatch.setattr(port, "_run", interrupted)
        if fault.startswith("cancel"):
            with pytest.raises(TurnCancelled):
                await collect(runtime, owner, target, router, transactions, port, cancel=cancel)
        else:
            with raises_code(
                "git_baseline_timeout" if fault == "timeout" else "git_baseline_unavailable"
            ):
                await collect(runtime, owner, target, router, transactions, port, cancel=cancel)


@pytest.mark.parametrize("size", [1024 * 1024 + 17, 8 * 1024 * 1024])
async def test_large_original_blob_uses_complete_observed_digest_not_prefix(tmp_path, size):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        # 原before合法达到8MiB；新Patch输入保持原512KiB限制，不放大Tool输入。
        # 二进制before沿原Diff摘要合同；文本大改仍受原完整Review容量限制。
        body = b"\0old" + b"x" * (size - 4)
        (root / "src/modified.py").write_bytes(body)
        repository(root)
        thread = await runtime.create_thread(str(root))
        target = await edit(
            runtime,
            provider,
            thread.thread_id,
            before=body,
            after=b"new\n",
            request="large-original",
        )
        port = reader(root, tmp_path / "git-state")
        actual = await collect(runtime, thread.thread_id, target, router, transactions, port)
        assert actual.source.mutations[0].before.size == size
        result = await port._run(
            (*port._global_arguments, "cat-file", "blob", actual.members[0].oid), CancelToken()
        )
        assert result.stdout.eof and result.stdout.truncated
        assert result.stdout.captured_bytes == 1024 * 1024
        assert result.stdout.observed_bytes == size
        assert result.stdout.observed_sha256 == hashlib.sha256(body).hexdigest()
        assert result.stdout.observed_sha256 != hashlib.sha256(result.stdout.data()).hexdigest()
        if size == 8 * 1024 * 1024:
            normal = reader(root, tmp_path / "normal-state", for_delivery=False)
            with pytest.raises(ReadToolError) as caught:
                await normal._run(
                    (*normal._global_arguments, "cat-file", "blob", actual.members[0].oid),
                    CancelToken(),
                )
            assert caught.value.code == "io_failed"


@pytest.mark.skipif(os.name == "nt", reason="Windows原Workspace Patch不接受执行位")
async def test_original_executable_mode_is_bound_to_git_tree(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        (root / "src/modified.py").chmod(0o755)
        repository(root)
        owner, target = await publish(runtime, provider, root)
        actual = await collect(
            runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
        )
        assert actual.members[0].mode == "100755"
        assert actual.source.mutations[0].before.mode == 0o755


@pytest.mark.parametrize(
    "field",
    ["head_oid", "index_observation_sha256", "reader_binding", "digest", "head_ref", "extra"],
)
async def test_schema_rejects_tampered_or_extra_fields(tmp_path, field):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        owner, target = await publish(runtime, provider, root)
        actual = await collect(
            runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
        )
        data = actual.model_dump(mode="json")
        data[field] = (
            "refs/heads/../invalid" if field == "head_ref" else "0" * len(str(data.get(field, "0")))
        )
        if field == "head_ref":
            candidate = actual.model_copy(update={"head_ref": data["head_ref"]})
            data["digest"] = product_git_baseline_digest(candidate)
        with pytest.raises(ValidationError):
            ProductGitDeliveryBaseline.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("mode", ["120000", "160000"])
async def test_link_or_gitlink_tree_entry_is_not_a_regular_original_blob(tmp_path, mode):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        oid = (
            command(root, "rev-parse", "HEAD" if mode == "160000" else "HEAD:src/modified.py")
            .strip()
            .decode()
        )
        command(root, "update-index", "--cacheinfo", mode + "," + oid + ",src/modified.py")
        command(root, "commit", "-qm", "non-regular tree entry")
        owner, target = await publish(runtime, provider, root)
        with raises_code("git_baseline_before_mismatch"):
            await collect(
                runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
            )


async def test_selected_index_conflict_stage_is_rejected(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        owner, target = await publish(runtime, provider, root)
        oid = command(root, "rev-parse", "HEAD:src/modified.py").strip().decode()
        command(root, "update-index", "--force-remove", "src/modified.py")
        # 故障准备保留真实Index格式，不伪造Git输出。
        command(
            root,
            "update-index",
            "--index-info",
            input_body=f"100644 {oid} 1\tsrc/modified.py\n".encode(),
        )
        with raises_code("git_baseline_index_conflict"):
            await collect(
                runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
            )


async def test_detached_head_is_observed_without_switching_branch(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        command(root, "checkout", "--detach", "-q")
        owner, target = await publish(runtime, provider, root)
        actual = await collect(
            runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
        )
        assert actual.head_ref == "HEAD"
        assert (root / ".git/HEAD").read_text().strip() == actual.head_oid


@pytest.mark.parametrize("fault", ["whole-timeout", "parent-cancel"])
async def test_whole_deadline_or_parent_cancellation_drains_the_pending_query(
    tmp_path, monkeypatch, fault
):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        owner, target = await publish(runtime, provider, root)
        port = reader(root, tmp_path / "git-state")
        entered, drained = asyncio.Event(), asyncio.Event()

        async def blocked(*_args, **_kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                drained.set()

        monkeypatch.setattr(port, "_run", blocked)
        monkeypatch.setattr(
            baseline_module, "_BASELINE_TIMEOUT_SECONDS", 0.1 if fault == "whole-timeout" else 60
        )
        task = asyncio.create_task(collect(runtime, owner, target, router, transactions, port))
        await asyncio.wait_for(entered.wait(), timeout=2)
        if fault == "whole-timeout":
            with raises_code("git_baseline_timeout"):
                await task
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert drained.is_set() and task.done()


@pytest.mark.parametrize(
    "fault", ["invalid-oid", "truncated-metadata", "missing-blob", "wrong-digest"]
)
async def test_incomplete_or_wrong_object_evidence_is_not_accepted(tmp_path, monkeypatch, fault):
    from harnessix.processes.contracts import ProcessStream

    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        owner, target = await publish(runtime, provider, root)
        port, original = reader(root, tmp_path / "git-state"), baseline_module._Queries.result

        async def corrupt(query, *arguments):
            if fault == "missing-blob" and arguments[:2] == ("cat-file", "blob"):
                raise ReadToolError("io_failed")
            result = await original(query, *arguments)
            stream = result.stdout
            if fault == "invalid-oid" and arguments == ("rev-parse", "--verify", "HEAD^{commit}"):
                body = b"untrusted object identifier\n"
                import base64

                stream = ProcessStream(
                    data_base64=base64.b64encode(body).decode(),
                    captured_bytes=len(body),
                    observed_bytes=len(body),
                    observed_sha256=hashlib.sha256(body).hexdigest(),
                    truncated=False,
                    eof=True,
                )
            if fault == "truncated-metadata" and arguments[0] == "ls-tree":
                stream = stream.model_copy(update={"truncated": True})
            if fault == "wrong-digest" and arguments[:2] == ("cat-file", "blob"):
                stream = stream.model_copy(update={"observed_sha256": "0" * 64})
            return result.model_copy(update={"stdout": stream})

        monkeypatch.setattr(baseline_module._Queries, "result", corrupt)
        code = {
            "invalid-oid": "git_baseline_output_invalid",
            "truncated-metadata": "git_baseline_limit",
            "missing-blob": "git_baseline_unavailable",
            "wrong-digest": "git_baseline_before_mismatch",
        }[fault]
        with raises_code(code):
            await collect(runtime, owner, target, router, transactions, port)


async def test_non_ascii_whitespace_in_real_branch_name_is_not_stripped(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        command(root, "checkout", "-qb", "topic\u00a0")
        owner, target = await publish(runtime, provider, root)
        actual = await collect(
            runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
        )
        assert actual.head_ref == "refs/heads/topic\u00a0"


@pytest.mark.parametrize("fault", ["cancel", "deadline"])
async def test_final_synchronous_snapshot_cannot_cross_cancellation_or_deadline(
    tmp_path, monkeypatch, fault
):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        owner, target = await publish(runtime, provider, root)
        original, cancel, clock = baseline_module.verify_workspace_snapshot, CancelToken(), [0.0]
        monkeypatch.setattr(baseline_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

        def cancelled_at_final_snapshot(*args, **kwargs):
            result = original(*args, **kwargs)
            if fault == "cancel":
                cancel.cancel()
            else:
                clock[0] = 61.0
            return result

        monkeypatch.setattr(
            baseline_module, "verify_workspace_snapshot", cancelled_at_final_snapshot
        )
        port = reader(root, tmp_path / "git-state")
        if fault == "cancel":
            with pytest.raises(TurnCancelled):
                await collect(runtime, owner, target, router, transactions, port, cancel=cancel)
        else:
            with raises_code("git_baseline_timeout"):
                await collect(runtime, owner, target, router, transactions, port, cancel=cancel)


async def test_helper_configuration_added_after_initial_check_is_rejected(tmp_path, monkeypatch):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        repository(root)
        owner, target = await publish(runtime, provider, root)
        original = baseline_module._reject_git_helpers

        async def changed(port, cancel):
            await original(port, cancel)
            command(root, "config", "filter.demo.clean", "untrusted-helper")

        monkeypatch.setattr(baseline_module, "_reject_git_helpers", changed)
        with raises_code("git_baseline_config_unsupported"):
            await collect(
                runtime, owner, target, router, transactions, reader(root, tmp_path / "git-state")
            )


async def test_real_intent_to_add_matching_empty_head_blob_is_rejected(tmp_path):
    async with product(tmp_path) as (root, runtime, provider, router, transactions, _, _):
        (root / "src/modified.py").write_bytes(b"")
        repository(root)
        command(root, "update-index", "--force-remove", "src/modified.py")
        command(root, "add", "--intent-to-add", "src/modified.py")
        debug = command(root, "ls-files", "--debug", "-z", "--", "src/modified.py")
        assert b"flags: 20004000" in debug
        thread = await runtime.create_thread(str(root))
        target = await edit(
            runtime, provider, thread.thread_id, before=b"", after=b"new\n", request="intent"
        )
        with raises_code("git_baseline_index_conflict"):
            await collect(
                runtime,
                thread.thread_id,
                target,
                router,
                transactions,
                reader(root, tmp_path / "git-state"),
            )
