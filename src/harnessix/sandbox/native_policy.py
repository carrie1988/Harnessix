"""原生进程沙箱（ADR 0111）：从本机事实推导文件视图、拦截端口与命令环境。

本模块只做确定性推导；读取文件系统的事实采集函数单独列出，便于在探测与测试中替换。
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from harnessix.agent.errors import KernelError
from harnessix.sandbox.native_contracts import (
    NativeSandboxBackend,
    NativeSandboxPolicy,
    NativeWorkspaceMode,
    canonical_absolute_path_problem,
    native_sandbox_policy_digest,
    path_within,
)

# 相对HOME的拒读路径（ADR 0111第2.3节）：凭据、命令历史与Harnessix自身。
_DENIED_HOME_PATHS: tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    ".netrc",
    ".git-credentials",
    ".config/gh",
    ".npmrc",
    ".yarnrc.yml",
    ".pypirc",
    ".cargo/credentials",
    ".cargo/credentials.toml",
    ".docker",
    ".m2/settings.xml",
    ".gradle/gradle.properties",
    ".aws",
    ".config/gcloud",
    ".azure",
    ".kube",
    ".terraform.d/credentials.tfrc.json",
    ".zsh_history",
    ".bash_history",
    ".python_history",
    ".node_repl_history",
    ".psql_history",
    ".mysql_history",
    ".harnessix",
)
_DENIED_HOME_PATHS_BY_BACKEND: dict[NativeSandboxBackend, tuple[str, ...]] = {
    "seatbelt": (
        "Library/Keychains",
        "Library/Cookies",
        "Library/Safari",
        "Library/Application Support/Google/Chrome",
        "Library/Application Support/Microsoft Edge",
        "Library/Application Support/BraveSoftware",
        "Library/Application Support/Firefox",
    ),
    "bubblewrap": (
        ".local/share/keyrings",
        ".mozilla",
        ".config/google-chrome",
        ".config/chromium",
        ".config/microsoft-edge",
        ".config/BraveSoftware",
    ),
}
# 工作区内与读工具一致的拒读目录；`.env*`由deny_workspace_env_files单独处理。
_DENIED_WORKSPACE_PATHS = (".harnessix",)

# 命令可从启动环境继承的变量（ADR 0111第2.2节）；其余一律不传。
_INHERITED_NAMES = frozenset(
    {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PATH",
        "LANG",
        "TERM",
        "TZ",
        "JAVA_HOME",
        "GOPATH",
        "GOROOT",
        "GOPROXY",
        "GOFLAGS",
        "GOPRIVATE",
        "GONOSUMDB",
        "GONOPROXY",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "PYENV_ROOT",
        "NVM_DIR",
        "PNPM_HOME",
        "DEVELOPER_DIR",
        "SDKROOT",
    }
)
_INHERITED_PREFIXES = ("LC_",)
# 只交给沙箱外命令（ADR 0113）；沙箱内不能借本机代理联网。
PROXY_ENVIRONMENT_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)
# 由Harnessix按私有目录设置；Profile预设不得覆盖（ADR 0111第2.2节）。
_PRIVATE_CACHE_VARIABLES = {
    "XDG_CACHE_HOME": "xdg",
    "PIP_CACHE_DIR": "pip",
    "UV_CACHE_DIR": "uv",
    "npm_config_cache": "npm",
    "YARN_CACHE_FOLDER": "yarn",
    "GOCACHE": "go-build",
}
_TEMPORARY_VARIABLES = ("TMPDIR", "TMP", "TEMP")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DEFAULT_PROXY_PORTS = {
    "http": 80,
    "https": 443,
    "socks": 1080,
    "socks4": 1080,
    "socks5": 1080,
    "socks5h": 1080,
}


def sandbox_reserved_environment_problem(name: str) -> str | None:
    """沙箱与沙箱外命令的Profile预设不得覆盖由Harnessix设置的变量。"""

    if name in {
        "HOME",
        *_TEMPORARY_VARIABLES,
        *_PRIVATE_CACHE_VARIABLES,
    } or name.upper().startswith("XDG_"):
        return "环境预设不能覆盖由Harnessix管理的HOME、临时目录或缓存变量"
    return None


@dataclass(frozen=True, slots=True)
class NativeSandboxFacts:
    """推导一份沙箱策略所需的全部本机事实；路径必须已解析符号链接。"""

    backend: NativeSandboxBackend
    home: Path
    workspace: Path
    workspace_mode: NativeWorkspaceMode
    private_tmp: Path
    private_cache: Path
    git_directories: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()
    protected_paths: tuple[Path, ...] = ()
    allow_workspace_env_files: bool = False
    blocked_loopback_ports: tuple[int, ...] = ()


def denied_paths(
    backend: NativeSandboxBackend,
    home: Path,
    workspace: Path,
    protected_paths: Iterable[Path] = (),
) -> tuple[str, ...]:
    """拒读路径的候选全集（存在与否由后端翻译时处理）。"""

    candidates = [
        *(home / item for item in _DENIED_HOME_PATHS),
        *(home / item for item in _DENIED_HOME_PATHS_BY_BACKEND[backend]),
        *(workspace / item for item in _DENIED_WORKSPACE_PATHS),
        *protected_paths,
    ]
    return tuple(sorted({str(path) for path in candidates}))


def build_native_sandbox_policy(facts: NativeSandboxFacts) -> NativeSandboxPolicy:
    """推导策略；工作区或可写目录与拒读路径冲突时报sandbox_root_overlaps_denied。"""

    home = str(facts.home)
    workspace = str(facts.workspace)
    denied = denied_paths(facts.backend, facts.home, facts.workspace, facts.protected_paths)
    writable = {str(facts.private_tmp), str(facts.private_cache), *map(str, facts.write_roots)}
    if facts.workspace_mode == "read_write":
        writable.add(workspace)
    for path in (home, workspace, *writable, *map(str, facts.git_directories), *denied):
        if canonical_absolute_path_problem(path):
            raise KernelError("sandbox_path_invalid", "沙箱路径必须是规范的绝对路径")
    for root in (*writable, workspace):
        if (
            root == "/"
            or path_within(home, root)
            or any(path_within(root, item) for item in denied)
        ):
            raise KernelError(
                "sandbox_root_overlaps_denied",
                "工作区或可写目录不能是HOME及其上级，也不能位于拒读路径之内",
            )
    read_only = tuple(
        sorted(
            {
                str(path)
                for path in facts.git_directories
                if any(path_within(str(path), root) for root in writable)
            }
        )
    )
    ports = tuple(sorted(set(facts.blocked_loopback_ports)))
    candidate = NativeSandboxPolicy.model_construct(
        backend=facts.backend,
        home=home,
        workspace=workspace,
        workspace_mode=facts.workspace_mode,
        private_tmp=str(facts.private_tmp),
        private_cache=str(facts.private_cache),
        writable_roots=tuple(sorted(writable)),
        read_only_roots=read_only,
        denied_paths=denied,
        deny_workspace_env_files=not facts.allow_workspace_env_files,
        blocked_loopback_ports=ports if facts.backend == "seatbelt" else (),
        digest="0" * 64,
    )
    return NativeSandboxPolicy(
        **candidate.model_dump(exclude={"digest"}),
        digest=native_sandbox_policy_digest(candidate),
    )


def workspace_git_directories(workspace: Path) -> tuple[Path, ...]:
    """工作区的Git目录：普通仓库为`.git`；worktree或子模块另含gitdir与common dir。"""

    marker = workspace / ".git"
    try:
        if marker.is_dir() and not marker.is_symlink():
            return (marker.resolve(strict=True),)
        if not marker.is_file():
            return ()
        text = marker.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    match = re.fullmatch(r"gitdir: (.+)\n?", text)
    if match is None:
        return ()
    try:
        gitdir = (workspace / match.group(1)).resolve(strict=True)
    except OSError:
        return ()
    directories = {gitdir}
    try:
        common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        directories.add((gitdir / common).resolve(strict=True))
    except (OSError, UnicodeError):
        pass
    return tuple(sorted(directories))


def loopback_proxy_ports(proxy_urls: Iterable[str]) -> tuple[int, ...]:
    """从代理地址中取出指向本机回环的端口；无法解析或非回环的地址忽略。"""

    ports: set[int] = set()
    for value in proxy_urls:
        text = value.strip()
        if not text:
            continue
        parts = urlsplit(text if "://" in text else "http://" + text)
        try:
            host = parts.hostname
            port = parts.port
        except ValueError:
            continue
        if host is None or not _is_loopback(host):
            continue
        port = port or _DEFAULT_PROXY_PORTS.get(parts.scheme.lower())
        if port is not None and 1 <= port <= 65535:
            ports.add(port)
    return tuple(sorted(ports))


def environment_proxy_urls(environ: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        environ[name]
        for name in sorted(PROXY_ENVIRONMENT_NAMES)
        if name.lower() != "no_proxy" and name in environ
    )


def macos_system_proxy_urls(output: str) -> tuple[str, ...]:
    """解析`scutil --proxy`输出中已启用的HTTP、HTTPS与SOCKS代理。"""

    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.strip().partition(" : ")
        if separator:
            values[key] = value.strip()
    return tuple(
        f"{scheme}://{values[f'{kind}Proxy']}:{values[f'{kind}Port']}"
        for kind, scheme in (("HTTP", "http"), ("HTTPS", "http"), ("SOCKS", "socks5"))
        if values.get(f"{kind}Enable") == "1"
        and f"{kind}Proxy" in values
        and f"{kind}Port" in values
    )


def sandbox_environment(
    environ: Mapping[str, str],
    *,
    private_tmp: Path,
    private_cache: Path,
    presets: Iterable[tuple[str, str]] = (),
    secret_names: Iterable[str] = (),
) -> Mapping[str, str]:
    """沙箱内命令的环境：继承白名单、无代理、临时与缓存目录指向私有位置。"""

    environment = _inherited(environ, secret_names, include_proxies=False)
    environment.update(dict(presets))
    for name in _TEMPORARY_VARIABLES:
        environment[name] = str(private_tmp)
    for name, directory in _PRIVATE_CACHE_VARIABLES.items():
        environment[name] = str(private_cache / directory)
    return MappingProxyType(dict(sorted(environment.items())))


def host_environment(
    environ: Mapping[str, str],
    *,
    presets: Iterable[tuple[str, str]] = (),
    secret_names: Iterable[str] = (),
) -> Mapping[str, str]:
    """沙箱外命令的环境：继承白名单并额外继承代理变量（ADR 0113第2.2节）。"""

    environment = _inherited(environ, secret_names, include_proxies=True)
    environment.update(dict(presets))
    return MappingProxyType(dict(sorted(environment.items())))


def _inherited(
    environ: Mapping[str, str], secret_names: Iterable[str], *, include_proxies: bool
) -> dict[str, str]:
    excluded = set(secret_names)
    return {
        name: value
        for name, value in environ.items()
        if name not in excluded
        and not name.upper().startswith("HARNESSIX_")
        and "\0" not in value
        and (
            name in _INHERITED_NAMES
            or name.startswith(_INHERITED_PREFIXES)
            or (include_proxies and name in PROXY_ENVIRONMENT_NAMES)
        )
    }


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolved_directory(path: str | os.PathLike[str]) -> Path:
    """解析符号链接后的真实目录；不存在或不是目录时报sandbox_path_invalid。"""

    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise KernelError("sandbox_path_invalid", "沙箱目录不存在或无法解析") from None
    if not resolved.is_dir():
        raise KernelError("sandbox_path_invalid", "沙箱目录不是目录")
    return resolved
