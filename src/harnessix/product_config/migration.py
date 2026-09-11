"""产品配置：以CAS、备份和原子替换迁移旧版配置。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.file_lock import acquire_exclusive_file_lock
from harnessix.product_config.codec import (
    canonical_product_config_bytes,
    decode_product_config_bytes,
    read_product_config_bytes,
)
from harnessix.product_config.contracts import (
    ConfigMigrationReceipt,
    EnvironmentSecretSourceConfig,
    ProductConfigV1,
    ProductConfigV2,
    ProviderDefinition,
    SecretReference,
    build_migration_receipt,
)


def migrate_v1(config: ProductConfigV1) -> ProductConfigV2:
    sources: list[EnvironmentSecretSourceConfig] = []
    providers: list[ProviderDefinition] = []
    secrets_by_environment: dict[str, SecretReference] = {}
    for provider in config.providers:
        secret = secrets_by_environment.get(provider.api_key_env)
        if secret is None:
            secret = SecretReference(name=f"{provider.provider_id}-api-key", version="env-v1")
            secrets_by_environment[provider.api_key_env] = secret
            sources.append(
                EnvironmentSecretSourceConfig(
                    secret=secret,
                    environment_variable=provider.api_key_env,
                )
            )
        providers.append(
            ProviderDefinition(
                provider_id=provider.provider_id,
                kind=provider.kind,
                base_url=provider.base_url,
                credential=secret,
                output_token_parameter=provider.output_token_parameter,
            )
        )
    return ProductConfigV2(
        active_profile=config.active_profile,
        secret_sources=tuple(sorted(sources, key=lambda item: item.secret.name)),
        providers=tuple(sorted(providers, key=lambda item: item.provider_id)),
        profiles=config.profiles,
    )


def _write_new(path: Path, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name == "posix":
        path.chmod(0o600)


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _lock(path: Path) -> int:
    if path.exists() or path.is_symlink():
        try:
            info = path.lstat()
        except OSError:
            raise KernelError("product_config_lock", "配置迁移锁不可用") from None
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and info.st_mode & 0o077):
            raise KernelError("product_config_lock", "配置迁移锁不安全")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        linked = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
            or opened.st_nlink != 1
            or (
                os.name == "posix"
                and (opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600)
            )
        ):
            raise OSError
        acquire_exclusive_file_lock(descriptor)
        return descriptor
    except BlockingIOError:
        if descriptor is not None:
            os.close(descriptor)
        raise KernelError("product_config_busy", "产品配置正在迁移") from None
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise KernelError("product_config_lock", "配置迁移锁不可用") from None


def migrate_product_config_file(
    path: str | Path,
    *,
    expected_source_sha256: str,
    fault: Callable[[str], None] | None = None,
) -> ConfigMigrationReceipt:
    if len(expected_source_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_source_sha256
    ):
        raise KernelError("product_config_migration_invalid", "迁移期望摘要无效")
    target = Path(path).absolute()
    try:
        parent = target.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise KernelError("product_config_unavailable", "产品配置目录不可用") from None
    target = parent / target.name
    lock_descriptor = _lock(parent / f".{target.name}.migration.lock")
    temporary = parent / f".{target.name}.{uuid4().hex}.tmp"
    trigger = fault or (lambda _: None)
    try:
        source = read_product_config_bytes(target)
        source_sha256 = hashlib.sha256(source).hexdigest()
        if source_sha256 != expected_source_sha256:
            raise KernelError("product_config_conflict", "产品配置源摘要已变化")
        document = decode_product_config_bytes(source, allow_legacy=True)
        if isinstance(document, ProductConfigV2):
            return build_migration_receipt(
                from_version="v2",
                source_sha256=source_sha256,
                target_sha256=source_sha256,
                backup_sha256=None,
                occurred_at=utc_now(),
            )
        migrated = migrate_v1(document)
        body = canonical_product_config_bytes(migrated)
        target_sha256 = hashlib.sha256(body).hexdigest()
        backup = parent / f"{target.name}.v1.{source_sha256}.bak"
        _write_new(temporary, body)
        trigger("migration.after_temporary_fsync")
        if backup.exists() or backup.is_symlink():
            if hashlib.sha256(read_product_config_bytes(backup)).hexdigest() != source_sha256:
                raise KernelError("product_config_backup_conflict", "配置迁移备份发生冲突")
        else:
            _write_new(backup, source)
            _sync_directory(parent)
        trigger("migration.after_backup_fsync")
        # 锁内再次核对源字节，避免外部非协作写入覆盖新版本。
        if hashlib.sha256(read_product_config_bytes(target)).hexdigest() != source_sha256:
            raise KernelError("product_config_conflict", "产品配置在迁移期间发生变化")
        os.replace(temporary, target)
        _sync_directory(parent)
        trigger("migration.after_replace_fsync")
        return build_migration_receipt(
            from_version="v1",
            source_sha256=source_sha256,
            target_sha256=target_sha256,
            backup_sha256=source_sha256,
            occurred_at=utc_now(),
        )
    except KernelError:
        raise
    except (OSError, ValueError, TypeError):
        raise KernelError("product_config_migration_failed", "产品配置迁移未完成") from None
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
        os.close(lock_descriptor)
