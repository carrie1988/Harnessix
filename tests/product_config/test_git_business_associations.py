"""真实产品认证链上的 Git 业务快照跨库验真。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

from harnessix.agent.cancellation import CancelToken
from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.product_config.git_business_associations import (
    validate_git_business_associations,
)
from harnessix.product_config.git_business_snapshot_contracts import (
    GitBusinessLinkSnapshot,
    GitDeliveryBusinessSnapshot,
)
from harnessix.product_config.git_business_snapshot_wire import (
    encode_git_delivery_business_snapshot,
)
from harnessix.product_config.git_decision_link_ledger import ProductGitDecisionLinkLedger
from harnessix.product_config.git_prefix_catalog import encode_git_prefix_catalog
from harnessix.product_config.git_prefix_reader import read_git_prefix_catalog
from harnessix.product_config.git_prefix_sql import git_prefix_sql_window
from harnessix.product_config.git_prepared_runtime_thread import prepared_git_commit_scope
from harnessix.product_config.state_backup import _copy_database
from harnessix.product_config.state_backup_contracts import KEY_FILE, PROCESS_DATABASE
from harnessix.product_config.state_backup_files import (
    PrivateStateTree,
    copy_file,
    create_private_tree,
    file_digest,
    write_new,
)
from harnessix.product_config.state_backup_v2_contracts import (
    GIT_BUSINESS_SNAPSHOT,
    V2_DATABASES,
    ProductStateBackupManifestV2,
    ProductStateFileV2,
    ProductStateGitBackupProfile,
    ephemeral_state_file_v2,
    state_directory_allowed_v2,
    state_file_kind_v2,
)
from harnessix.product_config.state_backup_v2_validation import validate_product_state_v2
from harnessix.product_config.state_backup_validation import verification_publication_binding
from harnessix.session.maintenance_io import MaintenanceIOControl
from tests.product_config.test_git_prepared_approval_history import _decide
from tests.product_config.test_git_prepared_link_ledger import (
    _case,
    _database,
    _genesis,
    _ledger,
)


def _validate_associations(
    state: Path,
    snapshot: GitDeliveryBusinessSnapshot,
    control: MaintenanceIOControl | None = None,
) -> None:
    with PrivateStateTree(state) as tree, verification_publication_binding(tree) as binding:
        validate_git_business_associations(
            tree, snapshot, binding, control or MaintenanceIOControl(timeout=60)
        )


def _copy_complete_candidate(
    source_path: Path, candidate_path: Path, snapshot: GitDeliveryBusinessSnapshot
) -> None:
    """从运行态构造关闭的DELETE-journal候选，避免把运行目录冒充备份成员。"""
    control = MaintenanceIOControl(timeout=60)
    create_private_tree(candidate_path)
    with PrivateStateTree(source_path) as source, PrivateStateTree(candidate_path) as candidate:
        for directory in (
            "git-delivery",
            "process-owner",
            "process-owner/runs",
            "session-auth",
            "workspace-transactions",
            "workspace-transactions/blobs",
        ):
            candidate.directory(directory)

        process_database = (PROCESS_DATABASE,) if (source_path / PROCESS_DATABASE).is_file() else ()
        databases = (*V2_DATABASES, *process_database)
        for path in databases:
            _copy_database(source, candidate, path, control)
        copy_file(source, candidate, KEY_FILE, control)

        for blob in sorted((source_path / "workspace-transactions/blobs").iterdir()):
            copy_file(source, candidate, blob.relative_to(source_path).as_posix(), control)
        for run in sorted((source_path / "process-owner/runs").iterdir()):
            if not run.is_dir():
                continue
            relative = run.relative_to(source_path).as_posix()
            candidate.directory(relative)
            for name in ("receipt.json", "stdout.bin", "stderr.bin"):
                if (run / name).is_file():
                    copy_file(source, candidate, f"{relative}/{name}", control)

        write_new(
            candidate,
            GIT_BUSINESS_SNAPSHOT,
            encode_git_delivery_business_snapshot(snapshot, checkpoint=control.checkpoint),
        )


def _validate_complete_v2(state: Path, snapshot: GitDeliveryBusinessSnapshot) -> None:
    control = MaintenanceIOControl(timeout=60)
    with PrivateStateTree(state) as tree:
        body = (state / GIT_BUSINESS_SNAPSHOT).read_bytes()
        paths = tuple(
            path
            for path in tree.files(
                control,
                directories=state_directory_allowed_v2,
                transient=ephemeral_state_file_v2,
            )
            if not ephemeral_state_file_v2(path)
        )
        files = tuple(
            ProductStateFileV2(
                path=path,
                kind=state_file_kind_v2(path),
                size_bytes=(measured := file_digest(tree, path, control))[0],
                sha256=measured[1],
            )
            for path in paths
        )
        identity = tree.path.stat()
        manifest = ProductStateBackupManifestV2(
            backup_id=snapshot.capture_id,
            created_at=utc_now(),
            platform=os.name,
            store_id=snapshot.prefix.store_id,
            key_id=snapshot.prefix.key_id,
            captured_state_root_identity=(identity.st_dev, identity.st_ino),
            business_profiles=(
                ProductStateGitBackupProfile(
                    capture_id=snapshot.capture_id,
                    store_genesis_epoch=snapshot.prefix.genesis_epoch,
                    prefix_revision=snapshot.prefix.revision,
                    prefix_anchor_sha256=hashlib.sha256(
                        encode_git_prefix_catalog(snapshot.prefix)
                    ).hexdigest(),
                    business_snapshot_sha256=hashlib.sha256(body).hexdigest(),
                ),
            ),
            files=files,
        )
        assert validate_product_state_v2(tree, manifest, control) == (
            manifest.store_id,
            manifest.key_id,
        )


@pytest.mark.parametrize(
    ("damage", "decided"),
    [
        (None, False),
        (None, True),
        ("external-workspace", False),
        ("core-cas", False),
        ("cancelled", False),
    ],
)
async def test_actual_prepared_link_is_bound_to_every_original_store(
    tmp_path, config, monkeypatch, damage, decided
) -> None:
    async def inspect(actual) -> None:
        publication = actual.scenario.session._publication
        assert publication is not None
        async with _database(actual) as database:
            await _genesis(actual, database)
            database.execute("BEGIN IMMEDIATE")
            prepared = await _ledger(actual, database).prepare(
                actual.route.plan.execution.plan_id,
                cancel=CancelToken(),
                checkpoint=lambda: None,
            )
            with prepared_git_commit_scope(database):
                database.execute("COMMIT")

        decision = None
        if decided:
            await _decide(actual, monkeypatch, "approved")
            async with _database(actual) as database:
                database.execute("BEGIN IMMEDIATE")
                decision = await ProductGitDecisionLinkLedger(
                    _ledger(actual, database)
                ).append_decision(
                    actual.route.plan.execution.plan_id,
                    cancel=CancelToken(),
                    checkpoint=lambda: None,
                )
                with prepared_git_commit_scope(database):
                    database.execute("COMMIT")

        async with _database(actual) as database:
            with git_prefix_sql_window(database, checkpoint=lambda: None):
                database.execute("BEGIN")
                catalog = read_git_prefix_catalog(
                    database,
                    publication.git_verifier,
                    publication.git_prefix_verifier,
                    checkpoint=lambda: None,
                )
                database.execute("ROLLBACK")

        snapshot = GitDeliveryBusinessSnapshot(
            capture_id=uuid4(),
            prefix=catalog,
            links=(GitBusinessLinkSnapshot(prepared=prepared, decision=decision),),
        )
        state = actual.scenario.state
        candidate = state.parent / f"backup-candidate-{damage or 'valid'}"
        await asyncio.to_thread(_copy_complete_candidate, state, candidate, snapshot)
        if damage == "external-workspace":
            workspace = actual.scenario.root
            unavailable = workspace.with_name(workspace.name + "-unavailable")
            workspace.rename(unavailable)
            try:
                await asyncio.to_thread(_validate_complete_v2, candidate, snapshot)
            finally:
                unavailable.rename(workspace)
        else:
            await asyncio.to_thread(_validate_complete_v2, candidate, snapshot)

        if damage == "core-cas":
            core = candidate / "workspace-transactions" / "blobs" / prepared.plan.core.fingerprint
            core.write_bytes(b"corrupt-cross-store-core")
            with pytest.raises(KernelError) as caught:
                await asyncio.to_thread(_validate_associations, candidate, snapshot)
            assert caught.value.code == "git_business_association_invalid"
        elif damage == "cancelled":
            failure = KernelError("maintenance_io_cancelled", "原受控停止")
            control = MaintenanceIOControl()

            def stop() -> None:
                raise failure

            monkeypatch.setattr(control, "checkpoint", stop)
            with pytest.raises(KernelError) as caught:
                await asyncio.to_thread(_validate_associations, candidate, snapshot, control)
            assert caught.value is failure

    await _case(tmp_path, config, monkeypatch, inspect)
