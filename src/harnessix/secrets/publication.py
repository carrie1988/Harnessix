"""显式版本化Secret内存快照；同一Provider材料同时约束执行与公开原生JSON。"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Self

from pydantic import JsonValue

from harnessix.agent.errors import KernelError
from harnessix.execution.contracts import SecretVersionBinding
from harnessix.secrets.provider import MAX_SECRET_BYTES, SecretMaterial, SecretProvider
from harnessix.secrets.redaction import secret_patterns

MAX_SCOPE_BINDINGS = 1024
MAX_SCOPE_NAMES = 32
MAX_PATTERN_BYTES = 2 * 1024 * 1024
MAX_SCAN_BYTES = 1024 * 1024
MAX_SCAN_NODES = 10256
MAX_SCAN_DEPTH = 64
MAX_SCAN_WORK = 64 * 1024 * 1024


def _unavailable() -> KernelError:
    return KernelError("trusted_action_secret_unavailable", "Action输出缺少匹配的Secret保护能力")


def _capture_material(
    provider: SecretProvider, binding: SecretVersionBinding, remaining: int
) -> SecretMaterial:
    raw = None
    try:
        raw = provider.resolve(binding.name)
        if (
            type(raw) is not SecretMaterial
            or (raw.name, raw.version) != (binding.name, binding.version)
            or type(raw.value) is not bytearray
            or not 4 <= len(raw.value) <= remaining
            or b"\0" in raw.value
        ):
            raise _unavailable()
        return SecretMaterial(raw.name, raw.version, bytearray(raw.value))
    except Exception:
        raise _unavailable() from None
    finally:
        if (
            type(raw) is SecretMaterial
            and type(raw.value) is bytearray
            and len(raw.value) <= MAX_SECRET_BYTES
        ):
            raw.clear()


@dataclass
class _ScanBudget:
    checkpoint: Callable[[], None]
    nodes: int = 0
    size: int = 0
    work: int = 0

    def step(self, depth: int) -> None:
        self.checkpoint()
        self.nodes += 1
        if self.nodes > MAX_SCAN_NODES or depth > MAX_SCAN_DEPTH:
            self.reject()

    @staticmethod
    def reject() -> None:
        raise KernelError("trusted_action_output_limit", "Action输出投影超过资源上限")

    def scan(self, text: str, patterns: tuple[bytes, ...], *, account_size: bool = True) -> None:
        remaining = MAX_SCAN_BYTES - self.size if account_size else MAX_SCAN_BYTES
        if len(text) > remaining:
            self.reject()
        try:
            encoded = text.encode("utf-8")
        except UnicodeError:
            raise _unavailable() from None
        if account_size:
            self.size += len(encoded)
        if len(encoded) > remaining or self.size > MAX_SCAN_BYTES:
            self.reject()
        for pattern in patterns:
            self.checkpoint()
            self.work += len(encoded) + len(pattern)
            if self.work > MAX_SCAN_WORK:
                self.reject()
            if pattern in encoded:
                raise KernelError("trusted_action_secret_leak", "Action输出包含受保护Secret")
        self.checkpoint()


def _scan_native(
    value: JsonValue, patterns: tuple[bytes, ...], checkpoint: Callable[[], None]
) -> None:
    """有界原生树扫描与快照生命周期分离，预算作用于全部键、标量和模式。"""
    budget = _ScanBudget(checkpoint)
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        budget.step(depth)
        if type(node) is str:
            budget.scan(node, patterns)
        elif type(node) is dict:
            if budget.nodes + len(stack) + len(node) * 2 > MAX_SCAN_NODES:
                budget.reject()
            for key, child in node.items():
                if type(key) is not str:
                    raise _unavailable()
                stack.extend(((key, depth + 1), (child, depth + 1)))
        elif type(node) is list:
            if budget.nodes + len(stack) + len(node) > MAX_SCAN_NODES:
                budget.reject()
            stack.extend((child, depth + 1) for child in node)
        elif node is None or type(node) is bool:
            budget.scan(json.dumps(node), patterns)
        elif type(node) is int and node.bit_length() <= 128:
            budget.scan(str(node), patterns)
        elif type(node) is float and math.isfinite(node):
            budget.scan(json.dumps(node, allow_nan=False), patterns)
        else:
            raise _unavailable()
    checkpoint()
    # 树已被原生预算验证；规范JSON再检查结构边界，避免数字或整段JSON凭据旁路。
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    budget.scan(serialized, patterns, account_size=False)
    checkpoint()


class SecretPublicationScope:
    """显式宿主作用域；不枚举环境、不持久化值，关闭后拒绝解析或公开检查。"""

    def __init__(self, bindings: Sequence[SecretVersionBinding], provider: SecretProvider) -> None:
        self._materials: dict[str, SecretMaterial] = {}
        self._patterns: tuple[bytes, ...] = ()
        self._closed = False
        if len(bindings) > MAX_SCOPE_BINDINGS:
            raise _unavailable()
        total = 0
        try:
            for binding in bindings:
                existing = self._materials.get(binding.name)
                if existing is not None:
                    if existing.version != binding.version:
                        raise _unavailable()
                    continue
                if len(self._materials) >= MAX_SCOPE_NAMES:
                    raise _unavailable()
                material = _capture_material(provider, binding, MAX_SECRET_BYTES - total)
                self._materials[binding.name] = material
                total += len(material.value)
            self._patterns = secret_patterns(
                tuple(bytes(m.value) for m in self._materials.values())
            )
            if sum(map(len, self._patterns)) > MAX_PATTERN_BYTES:
                raise _unavailable()
        except BaseException:
            self.close()
            raise

    def resolve(self, name: str) -> SecretMaterial:
        """返回执行用独立可清零副本，调用者无法修改公开保护快照。"""
        self._ensure_open()
        material = self._materials.get(name)
        if material is None:
            raise _unavailable()
        return SecretMaterial(material.name, material.version, bytearray(material.value))

    def require(self, bindings: Sequence[SecretVersionBinding]) -> None:
        """正文必须由原冻结name/version材料保护，不以当前新版本代替。"""
        self._ensure_open()
        if len(bindings) > MAX_SCOPE_BINDINGS or any(
            binding.name not in self._materials
            or self._materials[binding.name].version != binding.version
            for binding in bindings
        ):
            raise _unavailable()

    def assert_safe(
        self,
        value: JsonValue,
        bindings: Sequence[SecretVersionBinding],
        *,
        checkpoint: Callable[[], None],
    ) -> None:
        """扫描原生树与规范JSON；不执行用户钩子或替换正文，所有模式共用工作预算。"""
        self.require(bindings)
        _scan_native(value, self._patterns, checkpoint)

    def close(self) -> None:
        """幂等清零可变材料并丢弃模式；不承诺Python不可变副本已被完全擦除。"""
        if not self._closed:
            for material in self._materials.values():
                material.clear()
            self._materials.clear()
            self._patterns = ()
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise _unavailable()

    def __enter__(self) -> Self:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
