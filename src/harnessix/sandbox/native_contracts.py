"""原生进程沙箱（ADR 0111）：探测结果与单次装配的沙箱策略合同。"""

from __future__ import annotations

import posixpath
from typing import Literal, Self

from pydantic import Field, model_validator

from harnessix.execution.contracts import canonical_digest
from harnessix.sandbox.contracts import SandboxContract
from harnessix.tools.contracts import Revision

NativeSandboxBackend = Literal["seatbelt", "bubblewrap"]
NativeSandboxPlatform = Literal["macos", "linux", "other"]
NativeWorkspaceMode = Literal["read_only", "read_write"]
# 保证清单的条目；每个后端实际生效的条目见 NATIVE_GUARANTEES。
NativeGuarantee = Literal[
    "credential_paths_denied",
    "writes_confined",
    "git_metadata_read_only",
    "workspace_env_files_denied",
    "workspace_env_files_masked_at_start",
    "external_network_blocked",
    "host_loopback_isolated",
    "configured_proxy_ports_blocked",
    "host_unix_sockets_blocked",
    "common_host_unix_sockets_hidden",
    "harnessix_process_isolated",
    "descendants_terminated",
    "process_group_terminated",
]

NATIVE_GUARANTEES: dict[NativeSandboxBackend, tuple[NativeGuarantee, ...]] = {
    "seatbelt": (
        "configured_proxy_ports_blocked",
        "credential_paths_denied",
        "external_network_blocked",
        "git_metadata_read_only",
        "harnessix_process_isolated",
        "host_unix_sockets_blocked",
        "process_group_terminated",
        "workspace_env_files_denied",
        "writes_confined",
    ),
    "bubblewrap": (
        "common_host_unix_sockets_hidden",
        "credential_paths_denied",
        "descendants_terminated",
        "external_network_blocked",
        "git_metadata_read_only",
        "harnessix_process_isolated",
        "host_loopback_isolated",
        "workspace_env_files_masked_at_start",
        "writes_confined",
    ),
}
_PLATFORM_BACKEND: dict[NativeSandboxPlatform, NativeSandboxBackend | None] = {
    "macos": "seatbelt",
    "linux": "bubblewrap",
    "other": None,
}
_REASON_CODE = r"^[a-z][a-z0-9_.-]{0,127}$"
_MAX_PATH_BYTES = 4096


def native_backend_for_platform(platform: NativeSandboxPlatform) -> NativeSandboxBackend | None:
    return _PLATFORM_BACKEND[platform]


def canonical_absolute_path_problem(path: str) -> str | None:
    """路径必须是规范绝对路径：无`.`/`..`段、无重复或结尾斜杠、无NUL或换行。"""

    if (
        type(path) is not str
        or not path.startswith("/")
        or len(path.encode("utf-8", "surrogatepass")) > _MAX_PATH_BYTES
        or any(char in path for char in "\0\r\n")
        or posixpath.normpath(path) != path
        or path.startswith("//")
    ):
        return "沙箱路径必须是规范的绝对路径"
    return None


def path_within(path: str, root: str) -> bool:
    """path等于root或位于root之下（两者均为规范绝对路径）。"""

    return path == root or root == "/" or path.startswith(root + "/")


class HostSandboxProbeV2(SandboxContract):
    """一次探测的结果：后端是否可用、身份、原因码与实际生效的保证项。"""

    spec_version: Literal["harnessix.host-sandbox-probe/v2"] = "harnessix.host-sandbox-probe/v2"
    platform: NativeSandboxPlatform
    backend: NativeSandboxBackend | None
    available: bool
    executable: str | None = None
    executable_identity: Revision | None = None
    backend_version: str | None = Field(default=None, min_length=1, max_length=128)
    reason_code: str = Field(pattern=_REASON_CODE)
    guarantees: tuple[NativeGuarantee, ...] = ()
    digest: Revision

    @model_validator(mode="after")
    def consistent_probe(self) -> Self:
        if self.backend != native_backend_for_platform(self.platform):
            raise ValueError("沙箱后端与平台不一致")
        identified = (
            self.executable is not None
            and self.executable_identity is not None
            and self.backend_version is not None
        )
        if self.available:
            if (
                not identified
                or self.reason_code != "verified"
                or self.backend is None
                or self.guarantees != NATIVE_GUARANTEES[self.backend]
            ):
                raise ValueError("可用的沙箱探测必须带完整身份与该后端的保证清单")
        elif self.guarantees or self.reason_code == "verified":
            raise ValueError("不可用的沙箱探测不能声明保证项")
        if self.executable is not None and canonical_absolute_path_problem(self.executable):
            raise ValueError("沙箱可执行文件路径不规范")
        if self.digest != host_sandbox_probe_v2_digest(self):
            raise ValueError("沙箱探测摘要不一致")
        return self


def host_sandbox_probe_v2_digest(probe: HostSandboxProbeV2) -> str:
    return canonical_digest(probe.model_dump(mode="json", exclude={"digest"}, warnings="error"))


class NativeSandboxPolicy(SandboxContract):
    """一个工作区、一个后端的完整文件与网络边界；后端只把它翻译成各自的参数。

    读：整机可读，denied_paths优先；写：只限writable_roots，read_only_roots优先于写。
    """

    spec_version: Literal["harnessix.native-sandbox-policy/v1"] = (
        "harnessix.native-sandbox-policy/v1"
    )
    backend: NativeSandboxBackend
    home: str
    workspace: str
    workspace_mode: NativeWorkspaceMode
    private_tmp: str
    private_cache: str
    writable_roots: tuple[str, ...] = Field(min_length=2, max_length=16)
    read_only_roots: tuple[str, ...] = Field(default=(), max_length=8)
    denied_paths: tuple[str, ...] = Field(max_length=128)
    deny_workspace_env_files: bool
    # 仅Seatbelt：放行宿主回环时单独拒绝的已配置代理端口（ADR 0111第2.4节）。
    blocked_loopback_ports: tuple[int, ...] = Field(default=(), max_length=16)
    digest: Revision

    @model_validator(mode="after")
    def consistent_policy(self) -> Self:
        paths = (
            self.home,
            self.workspace,
            self.private_tmp,
            self.private_cache,
            *self.writable_roots,
            *self.read_only_roots,
            *self.denied_paths,
        )
        if any(canonical_absolute_path_problem(path) for path in paths):
            raise ValueError("沙箱策略路径必须是规范的绝对路径")
        for values in (self.writable_roots, self.read_only_roots, self.denied_paths):
            if values != tuple(sorted(set(values))):
                raise ValueError("沙箱策略路径集合必须唯一且有序")
        expected_writable = {self.private_tmp, self.private_cache}
        if self.workspace_mode == "read_write":
            expected_writable.add(self.workspace)
        if not expected_writable.issubset(self.writable_roots):
            raise ValueError("沙箱可写目录缺少私有目录或工作区")
        if self.workspace_mode == "read_only" and self.workspace in self.writable_roots:
            raise ValueError("只读工作区不能出现在可写目录中")
        for root in self.writable_roots:
            if root == "/" or path_within(self.home, root):
                raise ValueError("可写目录不能是根目录、HOME或HOME的上级")
            if any(path_within(root, denied) for denied in self.denied_paths):
                raise ValueError("可写目录不能位于拒读路径之内")
        if self.backend == "bubblewrap" and self.blocked_loopback_ports:
            raise ValueError("bubblewrap使用私有回环，不拦截宿主端口")
        if self.blocked_loopback_ports != tuple(sorted(set(self.blocked_loopback_ports))) or any(
            type(port) is not int or not 1 <= port <= 65535 for port in self.blocked_loopback_ports
        ):
            raise ValueError("拦截端口必须唯一有序且有效")
        if self.digest != native_sandbox_policy_digest(self):
            raise ValueError("沙箱策略摘要不一致")
        return self


def native_sandbox_policy_digest(policy: NativeSandboxPolicy) -> str:
    return canonical_digest(policy.model_dump(mode="json", exclude={"digest"}, warnings="error"))
