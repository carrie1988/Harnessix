"""Sandbox与网络隔离：定义版本化数据合同及其跨字段一致性校验。"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from harnessix.domain.models import ContractModel
from harnessix.execution.contracts import NetworkMode, canonical_digest
from harnessix.processes.supervision_contracts import ProcessSpec
from harnessix.tools.contracts import Revision

DestinationKind = Literal["domain", "cidr"]
DestinationProtocol = Literal["https", "tcp"]
WorkspaceMountMode = Literal["read_only", "read_write"]

_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class SandboxContract(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


def canonical_domain(value: str) -> str:
    if type(value) is not str or not value or value.endswith("."):
        raise ValueError("域名不是规范形式")
    try:
        normalized = value.encode("idna").decode("ascii").lower()
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    except UnicodeError:
        raise ValueError("域名编码无效") from None
    else:
        raise ValueError("IP地址必须使用CIDR规则")
    labels = normalized.split(".")
    if (
        len(normalized) > 253
        or len(labels) < 2
        or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError("域名不是规范形式")
    return normalized


class NetworkDestination(SandboxContract):
    kind: DestinationKind
    value: str = Field(min_length=1, max_length=253)
    protocol: DestinationProtocol
    ports: tuple[int, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def canonical_destination(self) -> Self:
        if self.ports != tuple(sorted(set(self.ports))) or any(
            type(port) is not int or not 1 <= port <= 65535 for port in self.ports
        ):
            raise ValueError("网络端口必须唯一有序且位于有效范围")
        if self.kind == "domain":
            if self.value != canonical_domain(self.value) or self.protocol != "https":
                raise ValueError("域名规则仅支持规范HTTPS目标")
        else:
            try:
                network = ipaddress.ip_network(self.value, strict=True)
            except ValueError:
                raise ValueError("CIDR不是规范网络") from None
            if self.value != network.with_prefixlen:
                raise ValueError("CIDR必须使用规范网络地址")
        return self


class NetworkPolicy(SandboxContract):
    spec_version: Literal["harnessix.network-policy/v1"] = "harnessix.network-policy/v1"
    mode: NetworkMode
    destinations: tuple[NetworkDestination, ...] = Field(default=(), max_length=64)
    allow_private_addresses: bool = False
    resolution_ttl_seconds: int = Field(default=60, ge=1, le=300)

    @model_validator(mode="after")
    def policy_shape(self) -> Self:
        keys = [(item.kind, item.value, item.protocol, item.ports) for item in self.destinations]
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            raise ValueError("网络目标必须唯一并按规范顺序排列")
        if self.mode in {"none", "full"} and self.destinations:
            raise ValueError("none/full网络模式不能携带选择性目标")
        if self.mode in {"limited", "restricted"} and not self.destinations:
            raise ValueError("选择性网络模式必须声明目标")
        if self.mode != "restricted" and self.allow_private_addresses:
            raise ValueError("私有地址只能在restricted模式显式授权")
        return self


class NetworkResolution(SandboxContract):
    hostname: str = Field(min_length=1, max_length=253)
    addresses: tuple[str, ...] = Field(min_length=1, max_length=16)
    resolved_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def canonical_resolution(self) -> Self:
        try:
            addresses = tuple(
                sorted({ipaddress.ip_address(value).compressed for value in self.addresses})
            )
        except ValueError:
            raise ValueError("DNS解析包含非法IP地址") from None
        if (
            self.hostname != canonical_domain(self.hostname)
            or self.addresses != addresses
            or self.resolved_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.expires_at <= self.resolved_at
        ):
            raise ValueError("DNS解析快照不规范")
        return self


class NetworkPolicySnapshot(SandboxContract):
    spec_version: Literal["harnessix.network-policy-snapshot/v1"] = (
        "harnessix.network-policy-snapshot/v1"
    )
    policy: NetworkPolicy
    resolutions: tuple[NetworkResolution, ...] = Field(default=(), max_length=64)
    digest: Revision

    @model_validator(mode="after")
    def complete_snapshot(self) -> Self:
        hosts = [item.hostname for item in self.resolutions]
        expected = sorted(item.value for item in self.policy.destinations if item.kind == "domain")
        if hosts != expected or len(set(hosts)) != len(hosts):
            raise ValueError("网络策略没有绑定完整且唯一的DNS解析")
        if self.digest != network_policy_snapshot_digest(self):
            raise ValueError("网络策略快照摘要不一致")
        return self


class SandboxResourceLimits(SandboxContract):
    cpus: float = Field(default=2.0, gt=0, le=32)
    memory_bytes: int = Field(default=1024 * 1024 * 1024, ge=64 * 1024 * 1024, le=64 * 1024**3)
    pids: int = Field(default=256, ge=16, le=4096)
    tmpfs_bytes: int = Field(default=256 * 1024 * 1024, ge=16 * 1024 * 1024, le=4 * 1024**3)


class ManagedEgressBinding(SandboxContract):
    network_name: str = Field(pattern=r"^harnessix-internal-[a-z0-9]{12,64}$")
    proxy_url: str = Field(pattern=r"^http://harnessix-egress:[1-9][0-9]{0,4}$")
    policy_digest: Revision
    gateway_digest: Revision
    internal_network_attestation: Revision

    @model_validator(mode="after")
    def valid_proxy_port(self) -> Self:
        if int(self.proxy_url.rsplit(":", 1)[1]) > 65535:
            raise ValueError("受管出口代理端口超出有效范围")
        return self


class ContainerSandboxProfile(SandboxContract):
    spec_version: Literal["harnessix.container-sandbox-profile/v1"] = (
        "harnessix.container-sandbox-profile/v1"
    )
    image: str = Field(min_length=71, max_length=512)
    workspace_mode: WorkspaceMountMode
    run_as: str = Field(default="65532:65532", pattern=r"^[1-9][0-9]{0,9}:[1-9][0-9]{0,9}$")
    limits: SandboxResourceLimits = Field(default_factory=SandboxResourceLimits)
    network: NetworkPolicySnapshot
    egress_gateway_digest: Revision | None = None
    digest: Revision

    @model_validator(mode="after")
    def immutable_image_and_digest(self) -> Self:
        image_digest = self.image.rsplit("@", 1)[-1]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
            raise ValueError("容器镜像必须固定到SHA-256摘要")
        selective = self.network.policy.mode in {"limited", "restricted"}
        if selective != (self.egress_gateway_digest is not None):
            raise ValueError("选择性网络必须绑定出口网关实现")
        if self.digest != container_sandbox_profile_digest(self):
            raise ValueError("容器Sandbox Profile摘要不一致")
        return self


class ContainerCommandSpec(SandboxContract):
    spec_version: Literal["harnessix.container-command/v1"] = "harnessix.container-command/v1"
    argv: tuple[str, ...] = Field(min_length=1, max_length=128)
    profile_digest: Revision
    digest: Revision

    @model_validator(mode="after")
    def command_digest(self) -> Self:
        try:
            size = sum(len(value.encode("utf-8")) + 1 for value in self.argv)
        except UnicodeError:
            raise ValueError("容器命令不是有效UTF-8") from None
        if any(not value or "\0" in value for value in self.argv) or size > 65536:
            raise ValueError("容器命令为空、包含NUL或超过字节上限")
        if self.digest != container_command_digest(self):
            raise ValueError("容器命令摘要不一致")
        return self


class ContainerExecutionSpec(SandboxContract):
    spec_version: Literal["harnessix.container-execution/v1"] = "harnessix.container-execution/v1"
    command: ContainerCommandSpec
    process: ProcessSpec
    owner_capability_digest: Revision
    digest: Revision

    @model_validator(mode="after")
    def complete_execution(self) -> Self:
        if (
            self.process.invocation != "argv"
            or self.process.argv != self.command.argv
            or self.process.terminal != "pipe"
        ):
            raise ValueError("Container执行只能绑定相同argv的非终端Process")
        if self.digest != container_execution_digest(self):
            raise ValueError("Container执行摘要不一致")
        return self


def network_policy_snapshot_digest(snapshot: NetworkPolicySnapshot) -> str:
    return canonical_digest(snapshot.model_dump(mode="json", exclude={"digest"}, warnings="error"))


def container_sandbox_profile_digest(profile: ContainerSandboxProfile) -> str:
    return canonical_digest(profile.model_dump(mode="json", exclude={"digest"}, warnings="error"))


def container_command_digest(command: ContainerCommandSpec) -> str:
    return canonical_digest(command.model_dump(mode="json", exclude={"digest"}, warnings="error"))


def container_execution_digest(execution: ContainerExecutionSpec) -> str:
    return canonical_digest(execution.model_dump(mode="json", exclude={"digest"}, warnings="error"))
