"""只读加载内置Task Pack，并把固定检查定义转换为产品Container Profile。"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.evals.task_pack_contracts import CodingEvalTaskPack
from harnessix.product_config.action_contracts import (
    ProductProcessProfile,
    build_product_process_profile,
)

_MAX_TASK_PACK_MANIFEST_BYTES = 1024 * 1024
_TASK_PACKS = {
    ("harnessix-engineering", 1): "engineering-v1",
    ("harnessix-engineering", 2): "engineering-v2",
    ("harnessix-seed", 1): "v1",
}
_TASK_PACK_VERSIONS = {
    "harnessix-engineering": (1, 2),
    "harnessix-seed": (1,),
}


@dataclass(frozen=True, slots=True)
class LoadedCodingEvalTaskPack:
    """已经通过严格合同和资源根身份校验的内置Task Pack。"""

    manifest: CodingEvalTaskPack
    resource_root: Path


def _read_regular(path: Path, max_bytes: int, *, code: str, label: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= max_bytes:
            raise OSError
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) != info.st_size or len(body) > max_bytes:
            raise OSError
        return body
    except OSError:
        raise KernelError(code, f"{label}缺失、类型无效或超过上限") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _resource(root: Path, relative: str, *, code: str, label: str) -> Path:
    try:
        selected = (root / relative).resolve(strict=True)
    except (OSError, RuntimeError):
        raise KernelError(code, f"{label}资源不可用") from None
    if selected == root or root not in selected.parents:
        raise KernelError(code, f"{label}资源逃逸Task Pack目录")
    try:
        info = (root / relative).lstat()
    except OSError:
        raise KernelError(code, f"{label}资源不可用") from None
    if selected != root / relative or not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise KernelError(code, f"{label}资源类型无效")
    return selected


def _verify_resources(root: Path, manifest: CodingEvalTaskPack) -> None:
    for repository in manifest.repositories:
        archive = _resource(
            root,
            repository.archive_file,
            code="eval_task_pack_archive_invalid",
            label="Task Pack Archive",
        )
        body = _read_regular(
            archive,
            64 * 1024 * 1024,
            code="eval_task_pack_archive_invalid",
            label="Task Pack Archive",
        )
        if (
            len(body) != repository.archive_bytes
            or hashlib.sha256(body).hexdigest() != repository.archive_sha256
        ):
            raise KernelError("eval_task_pack_archive_invalid", "Task Pack Archive摘要或大小不一致")


def _load_builtin_directory(directory: str) -> LoadedCodingEvalTaskPack:
    try:
        catalog_root = Path(__file__).resolve(strict=True).with_name("taskpacks")
        candidate = catalog_root / directory
        info = candidate.lstat()
    except (OSError, RuntimeError):
        raise KernelError("eval_task_pack_not_found", "内置Task Pack资源不存在") from None
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise KernelError("eval_task_pack_invalid", "内置Task Pack资源根类型无效")
    try:
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise KernelError("eval_task_pack_invalid", "内置Task Pack资源根不可用") from None
    if root != candidate:
        raise KernelError("eval_task_pack_invalid", "内置Task Pack资源根不能是符号链接")
    manifest_path = _resource(
        root,
        "manifest.json",
        code="eval_task_pack_invalid",
        label="Task Pack Manifest",
    )
    body = _read_regular(
        manifest_path,
        _MAX_TASK_PACK_MANIFEST_BYTES,
        code="eval_task_pack_invalid",
        label="Task Pack Manifest",
    )
    try:
        manifest = CodingEvalTaskPack.model_validate_json(body, strict=True)
    except (ValidationError, ValueError):
        raise KernelError("eval_task_pack_invalid", "Task Pack Manifest损坏或不受支持") from None
    _verify_resources(root, manifest)
    return LoadedCodingEvalTaskPack(manifest=manifest, resource_root=root)


def builtin_coding_eval_task_pack(
    pack_id: str = "harnessix-seed",
    pack_version: int | None = None,
) -> LoadedCodingEvalTaskPack:
    """只按代码内置ID与版本加载Task Pack，不接受外部路径或URL。"""

    try:
        versions = _TASK_PACK_VERSIONS[pack_id]
        version = versions[-1] if pack_version is None else pack_version
        directory = _TASK_PACKS[(pack_id, version)]
    except (KeyError, TypeError):
        raise KernelError("eval_task_pack_not_found", "内置Task Pack不存在") from None
    loaded = _load_builtin_directory(directory)
    if loaded.manifest.pack_id != pack_id or loaded.manifest.pack_version != version:
        raise KernelError("eval_task_pack_invalid", "Task Pack Catalog与Manifest身份不一致")
    return loaded


def builtin_coding_eval_task_pack_ids() -> tuple[str, ...]:
    """返回稳定排序的内置Task Pack ID。"""

    return tuple(sorted(_TASK_PACK_VERSIONS))


def builtin_coding_eval_task_pack_versions(pack_id: str) -> tuple[int, ...]:
    """返回指定内置Task Pack的不可变版本集合。"""

    try:
        return _TASK_PACK_VERSIONS[pack_id]
    except (KeyError, TypeError):
        raise KernelError("eval_task_pack_not_found", "内置Task Pack不存在") from None


def _verified_builtin_task_pack(
    loaded: LoadedCodingEvalTaskPack,
) -> LoadedCodingEvalTaskPack:
    """重新核验调用方对象确实等同于代码目录中的内置Task Pack。"""

    try:
        manifest = CodingEvalTaskPack.model_validate_json(
            loaded.manifest.model_dump_json(), strict=True
        )
        directory = _TASK_PACKS[(manifest.pack_id, manifest.pack_version)]
        expected = _load_builtin_directory(directory)
        candidate_root = loaded.resource_root.resolve(strict=True)
    except (KeyError, OSError, RuntimeError, ValidationError, ValueError, AttributeError):
        raise KernelError("eval_task_pack_invalid", "Task Pack不是受信内置资源") from None
    if candidate_root != loaded.resource_root or candidate_root != expected.resource_root:
        raise KernelError("eval_task_pack_invalid", "Task Pack资源根身份不一致")
    if manifest != expected.manifest:
        raise KernelError("eval_task_pack_invalid", "Task Pack Manifest与内置版本不一致")
    return expected


def build_task_pack_product_profile(
    loaded: LoadedCodingEvalTaskPack,
    profile_id: str,
    container_engine: Path,
) -> ProductProcessProfile:
    """把已校验Pack Profile绑定到当前宿主Engine，保持镜像、命令和资源不可改写。"""

    verified = _verified_builtin_task_pack(loaded)
    try:
        profile = verified.manifest.profile(profile_id)
        engine = container_engine.resolve(strict=True)
        engine_info = engine.stat()
    except KeyError:
        raise KernelError(
            "eval_task_pack_profile_not_found", "Task Pack检查Profile不存在"
        ) from None
    except (OSError, RuntimeError, ValidationError, ValueError):
        raise KernelError(
            "eval_task_pack_profile_invalid", "Task Pack检查Profile或Engine无效"
        ) from None
    if not stat.S_ISREG(engine_info.st_mode) or not os.access(engine, os.X_OK):
        raise KernelError("eval_task_pack_profile_invalid", "Task Pack检查Profile或Engine无效")
    return build_product_process_profile(
        profile_id=profile.profile_id,
        version=f"{profile.version}+taskpack.{profile.profile_sha256[:16]}",
        description=profile.description,
        container_engine=str(engine),
        image=profile.image,
        program=profile.program,
        arguments=profile.arguments,
        selector_policy="none",
        timeout_seconds=profile.timeout_seconds,
        max_output_bytes=profile.max_output_bytes,
        network_mode="none",
        cpu_limit=profile.cpu_limit,
        memory_bytes=profile.memory_bytes,
        process_limit=profile.process_limit,
    )
