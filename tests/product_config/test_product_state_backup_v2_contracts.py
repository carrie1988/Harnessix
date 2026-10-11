"""Backup v2合同：Git业务剖面的固定成员、身份和摘要必须全有或全无。"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.domain.models import utc_now
from harnessix.product_config.state_backup_contracts import (
    DATABASES,
    KEY_FILE,
    ProductStateBackupManifest,
)
from harnessix.product_config.state_backup_v2_contracts import (
    GIT_BUSINESS_SNAPSHOT,
    GIT_DATABASE,
    ProductStateBackupManifestV2,
    ProductStateBackupReceiptV2,
    ProductStateFileV2,
    ProductStateGitBackupProfile,
    StateFileKindV2,
    ephemeral_state_file_v2,
    state_directory_allowed_v2,
    state_file_kind_v2,
)


def _file(path: str, kind: StateFileKindV2, digest: str = "1" * 64) -> ProductStateFileV2:
    return ProductStateFileV2(path=path, kind=kind, size_bytes=1, sha256=digest)


def _manifest_files(snapshot_digest: str = "2" * 64) -> tuple[ProductStateFileV2, ...]:
    entries = [_file(path, "database") for path in DATABASES]
    entries.extend(
        (
            _file(KEY_FILE, "key"),
            _file(GIT_DATABASE, "database"),
            _file(GIT_BUSINESS_SNAPSHOT, "business_snapshot", snapshot_digest),
        )
    )
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _profile(
    capture_id: UUID | None = None, snapshot_digest: str = "2" * 64
) -> ProductStateGitBackupProfile:
    return ProductStateGitBackupProfile(
        capture_id=capture_id or uuid4(),
        store_genesis_epoch=uuid4(),
        prefix_revision=7,
        prefix_anchor_sha256="3" * 64,
        business_snapshot_sha256=snapshot_digest,
    )


def _manifest(**changes: object) -> ProductStateBackupManifestV2:
    backup_id = changes.get("backup_id", uuid4())
    assert isinstance(backup_id, UUID)
    values: dict[str, object] = {
        "backup_id": backup_id,
        "created_at": utc_now(),
        "platform": "posix",
        "store_id": uuid4(),
        "key_id": uuid4(),
        "captured_state_root_identity": (1, 2),
        "business_profiles": (_profile(backup_id),),
        "files": _manifest_files(),
    }
    values.update(changes)
    return ProductStateBackupManifestV2.model_validate(values)


def test_git_backup_profile_is_a_closed_versioned_manifest() -> None:
    manifest = _manifest()

    assert manifest.spec_version == "harnessix.product-state-backup/v2"
    assert manifest.business_profiles[0].profile == "git-delivery"
    assert manifest.business_profiles[0].git_store_schema_version == "2"
    assert {entry.path for entry in manifest.files} >= {
        *DATABASES,
        KEY_FILE,
        GIT_DATABASE,
        GIT_BUSINESS_SNAPSHOT,
    }


def test_git_profile_revision_uses_the_same_range_as_the_authenticated_tail_anchor() -> None:
    values = _profile().model_dump()
    values["prefix_revision"] = 2**63 - 1
    profile = ProductStateGitBackupProfile.model_validate(values)

    assert profile.prefix_revision == 2**63 - 1
    values["prefix_revision"] = 2**63
    with pytest.raises(ValidationError):
        ProductStateGitBackupProfile.model_validate(values)


def test_v1_reader_rejects_v2_manifest_instead_of_guessing_layout() -> None:
    with pytest.raises(ValidationError):
        ProductStateBackupManifest.model_validate_json(_manifest().model_dump_json())


def test_v2_receipt_binds_exact_manifest_and_logical_store_identity() -> None:
    manifest = _manifest()
    receipt = ProductStateBackupReceiptV2(
        backup_id=manifest.backup_id,
        manifest_sha256="4" * 64,
        store_id=manifest.store_id,
        key_id=manifest.key_id,
    )

    assert receipt.spec_version == "harnessix.product-state-backup-receipt/v2"
    assert receipt.backup_id == manifest.backup_id
    assert receipt.store_id == manifest.store_id
    assert receipt.key_id == manifest.key_id


@pytest.mark.parametrize("missing", [GIT_DATABASE, GIT_BUSINESS_SNAPSHOT])
def test_git_profile_rejects_missing_fixed_member(missing: str) -> None:
    files = tuple(entry for entry in _manifest_files() if entry.path != missing)

    with pytest.raises(ValidationError, match="Git业务剖面"):
        _manifest(files=files)


def test_git_profile_rejects_snapshot_digest_mismatch() -> None:
    manifest = _manifest()
    with pytest.raises(ValidationError, match="业务快照摘要"):
        _manifest(
            backup_id=manifest.backup_id,
            business_profiles=(_profile(manifest.backup_id, "4" * 64),),
        )


def test_git_profile_rejects_capture_identity_mismatch() -> None:
    with pytest.raises(ValidationError, match="捕获身份"):
        _manifest(business_profiles=(_profile(),))


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        (GIT_DATABASE, "database"),
        (GIT_BUSINESS_SNAPSHOT, "business_snapshot"),
    ],
)
def test_v2_path_contract_only_adds_two_fixed_git_members(path: str, kind: str) -> None:
    assert state_file_kind_v2(path) == kind
    assert state_directory_allowed_v2("git-delivery")


@pytest.mark.parametrize(
    "path",
    [
        "git-delivery/worktrees/managed/file.txt",
        "git-delivery/source-anchors/managed/.git",
        "git-delivery/tmp/index",
        "git-delivery/foreign.db",
    ],
)
def test_v2_path_contract_rejects_runtime_or_unknown_git_members(path: str) -> None:
    with pytest.raises(ValueError, match="未受管"):
        state_file_kind_v2(path)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_git_database_sidecars_are_ephemeral(suffix: str) -> None:
    assert ephemeral_state_file_v2(GIT_DATABASE + suffix)


def test_manifest_rejects_unsorted_or_duplicate_members() -> None:
    files = _manifest_files()
    with pytest.raises(ValidationError, match="顺序"):
        _manifest(files=tuple(reversed(files)))
    with pytest.raises(ValidationError, match="顺序"):
        _manifest(files=tuple(sorted((*files, files[0]), key=lambda entry: entry.path)))


@pytest.mark.parametrize("identity", [(1, 0), (-1, 1)])
def test_manifest_rejects_invalid_captured_root_identity(identity: tuple[int, int]) -> None:
    with pytest.raises(ValidationError):
        _manifest(captured_state_root_identity=identity)
