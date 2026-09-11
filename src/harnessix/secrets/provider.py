"""Secret解析与脱敏：按名称和版本解析Secret并管理明文生命周期。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import SecretVersionBinding
from harnessix.workspace.contracts import PlatformKind

MAX_SECRET_BYTES = 64 * 1024


@dataclass(slots=True)
class SecretMaterial:
    name: str
    version: str
    value: bytearray = field(repr=False)

    def clear(self) -> None:
        self.value[:] = b"\0" * len(self.value)


class SecretProvider(Protocol):
    def resolve(self, name: str) -> SecretMaterial: ...


@dataclass(frozen=True, slots=True)
class EnvironmentSecretSource:
    name: str
    version: str
    environment_variable: str


class EnvironmentSecretProvider:
    """宿主白名单映射；领域配置只保存环境变量名，不保存值。"""

    def __init__(
        self,
        sources: Sequence[EnvironmentSecretSource],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self._sources = {source.name: source for source in sources}
        if (
            not self._sources
            or len(self._sources) != len(sources)
            or any(
                re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", source.name) is None
                or not source.version.strip()
                or len(source.version) > 128
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", source.environment_variable)
                is None
                for source in sources
            )
        ):
            raise KernelError("secret_provider_invalid", "Secret Provider配置无效")

    def resolve(self, name: str) -> SecretMaterial:
        source = self._sources.get(name)
        if source is None:
            raise KernelError("secret_unavailable", "Secret引用不存在")
        value = self._environment.get(source.environment_variable)
        if value is None:
            raise KernelError("secret_unavailable", "Secret值不可用")
        try:
            encoded = value.encode("utf-8")
        except UnicodeError:
            raise KernelError("secret_unavailable", "Secret值编码无效") from None
        if not encoded or b"\0" in encoded or len(encoded) > MAX_SECRET_BYTES:
            raise KernelError("secret_unavailable", "Secret值大小或内容无效")
        return SecretMaterial(name=source.name, version=source.version, value=bytearray(encoded))


class ResolvedSecretEnvironment:
    """短生命周期明文；关闭时尽力清零可变字节副本。"""

    def __init__(self, values: dict[str, SecretMaterial]) -> None:
        self._values = values
        self._closed = False

    def as_text(self) -> dict[str, str]:
        if self._closed:
            raise KernelError("secret_scope_closed", "Secret作用域已经关闭")
        try:
            return {
                target: bytes(material.value).decode("utf-8")
                for target, material in self._values.items()
            }
        except UnicodeError:
            raise KernelError("secret_unavailable", "Secret不能注入文本环境") from None

    def raw_values(self) -> tuple[bytes, ...]:
        if self._closed:
            raise KernelError("secret_scope_closed", "Secret作用域已经关闭")
        return tuple(bytes(material.value) for material in self._values.values())

    def bindings(self) -> tuple[tuple[str, str, str], ...]:
        if self._closed:
            raise KernelError("secret_scope_closed", "Secret作用域已经关闭")
        return tuple(
            sorted(
                (target, material.name, material.version)
                for target, material in self._values.items()
            )
        )

    def close(self) -> None:
        if not self._closed:
            for material in self._values.values():
                material.clear()
            self._values.clear()
            self._closed = True

    def __enter__(self) -> ResolvedSecretEnvironment:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def resolve_secret_environment(
    bindings: Sequence[SecretVersionBinding],
    provider: SecretProvider,
    *,
    platform: PlatformKind,
) -> ResolvedSecretEnvironment:
    if len(bindings) > 32:
        raise KernelError("secret_limit_exceeded", "Secret引用超过数量上限")
    values: dict[str, SecretMaterial] = {}
    comparison_targets: set[str] = set()
    total = 0
    try:
        for binding in bindings:
            target_key = binding.target.casefold() if platform == "windows" else binding.target
            if target_key in comparison_targets:
                raise KernelError("secret_target_conflict", "Secret注入目标重复")
            comparison_targets.add(target_key)
            material = provider.resolve(binding.name)
            if material.name != binding.name:
                material.clear()
                raise KernelError("secret_provider_invalid", "Secret Provider返回错误引用")
            if material.version != binding.version:
                material.clear()
                raise KernelError("secret_version_changed", "Secret版本已经变化")
            total += len(material.value)
            if total > MAX_SECRET_BYTES:
                material.clear()
                raise KernelError("secret_limit_exceeded", "Secret总字节超过上限")
            values[binding.target] = material
        return ResolvedSecretEnvironment(values)
    except BaseException:
        for material in values.values():
            material.clear()
        raise
