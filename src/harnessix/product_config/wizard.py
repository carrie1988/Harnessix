"""产品配置：把非敏感草案构造成v2配置并以CAS原子提交。"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from harnessix.agent.errors import KernelError
from harnessix.domain.models import utc_now
from harnessix.file_lock import acquire_exclusive_file_lock
from harnessix.product_config.codec import (
    canonical_product_config_bytes,
    load_product_config,
    read_product_config_bytes,
)
from harnessix.product_config.contracts import (
    ConfigurationDraft,
    ConfigurationWriteReceipt,
    EnvironmentSecretSourceConfig,
    ModelProfile,
    ProductConfigSnapshot,
    ProductConfigV2,
    ProviderDefinition,
    SecretReference,
    configuration_write_receipt_digest,
    product_config_digest,
)


@dataclass(frozen=True, slots=True)
class ConfigurationWriteRequest:
    """一次明确的新建或替换请求；替换必须携带旧文件摘要。"""

    path: Path
    draft: ConfigurationDraft
    expected_source_sha256: str | None = None
    replace: bool = False


def build_product_config(draft: ConfigurationDraft) -> ProductConfigV2:
    """使用现有安全默认值生成单Provider、单Profile配置。"""

    checked = ConfigurationDraft.model_validate_json(draft.model_dump_json(), strict=True)
    secret = SecretReference(name=checked.secret_name, version=checked.secret_version)
    return ProductConfigV2(
        active_profile=checked.profile_id,
        secret_sources=(
            EnvironmentSecretSourceConfig(
                secret=secret,
                environment_variable=checked.environment_variable,
            ),
        ),
        providers=(
            ProviderDefinition(
                provider_id=checked.provider_id,
                kind=checked.provider_kind,
                base_url=checked.base_url,
                credential=secret,
                output_token_parameter=checked.output_token_parameter,
            ),
        ),
        profiles=(
            ModelProfile(
                profile_id=checked.profile_id,
                provider_id=checked.provider_id,
                model=checked.model,
            ),
        ),
    )


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _prepare_private_parent(path: Path) -> Path:
    parent = path.parent
    try:
        if parent.exists() or _is_link_or_junction(parent):
            info = parent.lstat()
        else:
            parent.mkdir(parents=True, mode=0o700)
            info = parent.lstat()
        if _is_link_or_junction(parent) or not stat.S_ISDIR(info.st_mode):
            raise OSError
        if os.name == "posix" and (
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise OSError
        return parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise KernelError(
            "product_config_permissions",
            "产品配置目录权限或身份不安全",
        ) from None


def _open_lock(path: Path) -> int:
    flags = os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        except FileExistsError:
            descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        linked = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
            or opened.st_nlink != 1
            or opened.st_size < 1
            or (
                os.name == "posix"
                and (opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600)
            )
        ):
            raise OSError
        os.lseek(descriptor, 0, os.SEEK_SET)
        acquire_exclusive_file_lock(descriptor)
        return descriptor
    except BlockingIOError:
        if descriptor is not None:
            os.close(descriptor)
        raise KernelError(
            "product_config_lock_timeout",
            "产品配置写入锁被占用",
            retryable=True,
        ) from None
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise KernelError("product_config_lock", "产品配置写入锁不安全") from None


def _write_private_file(path: Path, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        remaining = memoryview(body)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_expected_digest(value: str | None) -> None:
    if value is None:
        return
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise KernelError("product_config_write_invalid", "产品配置期望摘要无效")


def _receipt(
    *,
    operation: Literal["created", "replaced"],
    previous_source_sha256: str | None,
    source_sha256: str,
    config_sha256: str,
) -> ConfigurationWriteReceipt:
    candidate = ConfigurationWriteReceipt.model_construct(
        operation=operation,
        previous_source_sha256=previous_source_sha256,
        source_sha256=source_sha256,
        config_sha256=config_sha256,
        occurred_at=utc_now(),
        receipt_sha256="0" * 64,
    )
    return ConfigurationWriteReceipt(
        **candidate.model_dump(exclude={"receipt_sha256"}),
        receipt_sha256=configuration_write_receipt_digest(candidate),
    )


def write_product_config(
    request: ConfigurationWriteRequest,
    *,
    fault: Callable[[str], None] | None = None,
) -> ConfigurationWriteReceipt:
    """在私有目录中创建配置，或按源字节摘要CAS替换现有配置。"""

    _validate_expected_digest(request.expected_source_sha256)
    if request.replace and request.expected_source_sha256 is None:
        raise KernelError(
            "product_config_expected_digest_required",
            "替换产品配置必须提供源摘要",
        )
    if not request.replace and request.expected_source_sha256 is not None:
        raise KernelError("product_config_write_invalid", "新建产品配置不能提供源摘要")
    if not request.path.name or request.path.name in {".", ".."}:
        raise KernelError("product_config_write_invalid", "产品配置写入路径无效")

    config = ProductConfigV2.model_validate_json(
        build_product_config(request.draft).model_dump_json(),
        strict=True,
    )
    body = canonical_product_config_bytes(config)
    source_sha256 = hashlib.sha256(body).hexdigest()
    config_sha256 = product_config_digest(config)
    parent = _prepare_private_parent(request.path.absolute())
    target = parent / request.path.name
    lock_descriptor = _open_lock(parent / f".{target.name}.configure.lock")
    temporary = parent / f".{target.name}.{uuid4().hex}.tmp"
    trigger = fault or (lambda _: None)
    committed = False
    previous_source_sha256: str | None = None
    try:
        target_exists = target.exists() or _is_link_or_junction(target)
        if not request.replace and target_exists:
            raise KernelError("product_config_exists", "产品配置已存在")
        if request.replace and not target_exists:
            raise KernelError("product_config_conflict", "待替换产品配置不存在")
        if request.replace:
            previous_source_sha256 = hashlib.sha256(read_product_config_bytes(target)).hexdigest()
            if previous_source_sha256 != request.expected_source_sha256:
                raise KernelError("product_config_conflict", "产品配置源摘要已变化")

        _write_private_file(temporary, body)
        trigger("configuration.after_temporary_fsync")
        if request.replace:
            current_sha256 = hashlib.sha256(read_product_config_bytes(target)).hexdigest()
            if current_sha256 != request.expected_source_sha256:
                raise KernelError("product_config_conflict", "产品配置在写入期间发生变化")
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
            temporary.unlink()
        committed = True
        _sync_directory(parent)
        trigger("configuration.after_replace_fsync")

        reopened = load_product_config(target)
        if not isinstance(reopened, ProductConfigSnapshot) or (
            reopened.source_sha256 != source_sha256
            or reopened.config_sha256 != config_sha256
            or reopened.config != config
        ):
            raise KernelError("product_config_commit_unknown", "产品配置提交结果无法确认")
        return _receipt(
            operation="replaced" if request.replace else "created",
            previous_source_sha256=previous_source_sha256,
            source_sha256=source_sha256,
            config_sha256=config_sha256,
        )
    except KernelError:
        if committed:
            raise KernelError("product_config_commit_unknown", "产品配置提交结果无法确认") from None
        raise
    except FileExistsError:
        if committed:
            raise KernelError("product_config_commit_unknown", "产品配置提交结果无法确认") from None
        raise KernelError("product_config_exists", "产品配置已存在") from None
    except OSError:
        code = "product_config_commit_unknown" if committed else "product_config_write_failed"
        message = "产品配置提交结果无法确认" if committed else "产品配置写入未完成"
        raise KernelError(code, message) from None
    except Exception:
        code = "product_config_commit_unknown" if committed else "product_config_write_failed"
        message = "产品配置提交结果无法确认" if committed else "产品配置写入未完成"
        raise KernelError(code, message) from None
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
        try:
            os.close(lock_descriptor)
        except OSError:
            pass
