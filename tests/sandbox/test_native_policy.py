"""原生进程沙箱策略推导（ADR 0111第2.2–2.4节）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.sandbox.native_contracts import NativeSandboxPolicy
from harnessix.sandbox.native_policy import (
    NativeSandboxFacts,
    build_native_sandbox_policy,
    environment_proxy_urls,
    host_environment,
    loopback_proxy_ports,
    macos_system_proxy_urls,
    sandbox_environment,
    sandbox_reserved_environment_problem,
    workspace_git_directories,
)

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="原生进程沙箱只在Linux与macOS上提供，策略与参数全是POSIX路径"
)

HOME = Path("/Users/dev")
WORKSPACE = HOME / "src/app"
CACHE = HOME / "Library/Caches/harnessix/sandbox/abc"


def facts(**overrides: object) -> NativeSandboxFacts:
    values: dict[str, object] = {
        "backend": "seatbelt",
        "home": HOME,
        "workspace": WORKSPACE,
        "workspace_mode": "read_write",
        "private_tmp": CACHE / "tmp/owner",
        "private_cache": CACHE / "cache",
        "git_directories": (WORKSPACE / ".git",),
    }
    return NativeSandboxFacts(**(values | overrides))  # type: ignore[arg-type]


@posix_only
def test_read_write_policy_confines_writes_and_protects_git() -> None:
    policy = build_native_sandbox_policy(facts())
    assert policy.writable_roots == tuple(
        sorted((str(WORKSPACE), str(CACHE / "tmp/owner"), str(CACHE / "cache")))
    )
    assert policy.read_only_roots == (str(WORKSPACE / ".git"),)
    assert policy.deny_workspace_env_files
    for denied in (".ssh", ".aws", ".npmrc", ".zsh_history", ".harnessix", "Library/Keychains"):
        assert str(HOME / denied) in policy.denied_paths
    assert str(WORKSPACE / ".harnessix") in policy.denied_paths
    assert NativeSandboxPolicy.model_validate_json(policy.model_dump_json()) == policy


@posix_only
def test_read_only_mode_binds_the_workspace_read_only() -> None:
    policy = build_native_sandbox_policy(facts(workspace_mode="read_only"))
    assert str(WORKSPACE) not in policy.writable_roots
    # 工作区显式只读挂载（位于/tmp等私有挂载下也可见）；.git不在可写目录下，无需单独列出。
    assert policy.read_only_roots == (str(WORKSPACE),)


@posix_only
def test_linux_policy_uses_platform_deny_list_and_private_loopback() -> None:
    policy = build_native_sandbox_policy(
        facts(backend="bubblewrap", blocked_loopback_ports=(7890,))
    )
    assert str(HOME / ".local/share/keyrings") in policy.denied_paths
    assert str(HOME / "Library/Keychains") not in policy.denied_paths
    assert policy.blocked_loopback_ports == ()


@posix_only
def test_seatbelt_keeps_sorted_unique_blocked_proxy_ports() -> None:
    policy = build_native_sandbox_policy(facts(blocked_loopback_ports=(7892, 7890, 7890)))
    assert policy.blocked_loopback_ports == (7890, 7892)


@posix_only
def test_protected_harnessix_paths_and_env_override_are_honoured(tmp_path: Path) -> None:
    state = Path("/opt/state/harnessix")
    policy = build_native_sandbox_policy(
        facts(protected_paths=(state,), allow_workspace_env_files=True)
    )
    assert str(state) in policy.denied_paths
    assert not policy.deny_workspace_env_files


@pytest.mark.parametrize(
    "overrides",
    [
        {"workspace": HOME},
        {"workspace": Path("/Users")},
        {"workspace": HOME / ".ssh/keys"},
        {"write_roots": (Path("/"),)},
        {
            "write_roots": (HOME / ".aws/cli",),
        },
        {"write_roots": (HOME,)},
        {"workspace": HOME / "src/../app"},
    ],
)
def test_overlapping_or_unsafe_roots_are_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(KernelError) as error:
        build_native_sandbox_policy(facts(**overrides))
    assert error.value.code in {"sandbox_root_overlaps_denied", "sandbox_path_invalid"}


@posix_only
def test_write_root_may_contain_denied_paths_which_stay_masked() -> None:
    policy = build_native_sandbox_policy(facts(write_roots=(HOME / ".config",)))
    assert str(HOME / ".config") in policy.writable_roots
    assert str(HOME / ".config/gh") in policy.denied_paths


@posix_only
def test_policy_contract_rejects_tampering() -> None:
    policy = build_native_sandbox_policy(facts())
    payload = policy.model_dump(mode="json")
    with pytest.raises(ValidationError):
        NativeSandboxPolicy.model_validate(
            payload | {"writable_roots": ["/", *payload["writable_roots"]]}
        )
    with pytest.raises(ValidationError):
        NativeSandboxPolicy.model_validate(payload | {"deny_workspace_env_files": False})


def test_worktree_git_directories_include_gitdir_and_common_dir(tmp_path: Path) -> None:
    main = tmp_path / "main/.git"
    gitdir = main / "worktrees/feature"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "feature"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert workspace_git_directories(worktree) == tuple(sorted((gitdir.resolve(), main.resolve())))
    plain = tmp_path / "plain"
    (plain / ".git").mkdir(parents=True)
    assert workspace_git_directories(plain) == ((plain / ".git").resolve(),)
    assert workspace_git_directories(tmp_path) == ()


def test_loopback_proxy_ports_from_environment_and_system_settings() -> None:
    environ = {
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "all_proxy": "socks5://localhost:7891",
        "HTTP_PROXY": "http://proxy.corp.example:3128",
        "NO_PROXY": "localhost",
        "http_proxy": "not a url:::",
    }
    scutil = "\n".join(
        (
            "<dictionary> {",
            "  HTTPEnable : 1",
            "  HTTPPort : 7892",
            "  HTTPProxy : 127.0.0.1",
            "  SOCKSEnable : 0",
            "  SOCKSPort : 9999",
            "  SOCKSProxy : 127.0.0.1",
            "}",
        )
    )
    urls = (*environment_proxy_urls(environ), *macos_system_proxy_urls(scutil))
    assert loopback_proxy_ports(urls) == (7890, 7891, 7892)


def test_host_commands_get_common_tool_settings_but_never_tokens() -> None:
    """沙箱外命令带上pnpm、Playwright等工具的非机密设置（Beta Issue #20）；
    登录令牌一类的npm变量不带。

    0.10.0b33 Windows：没有这些设置时，获批的测试命令先重装依赖、重新下载浏览器，
    以process_timeout结束。
    """
    settings = {
        "PNPM_CONFIG_VERIFY_DEPS_BEFORE_RUN": "false",
        "NPM_CONFIG_STORE_DIR": "D:/pnpm-store",
        "PLAYWRIGHT_BROWSERS_PATH": "D:/ms-playwright",
        "CI": "1",
    }
    tokens = {
        "NPM_CONFIG__AUTH": "secret",
        "npm_config_//registry.npmjs.org/:_authToken": "secret",
        "NPM_TOKEN": "secret",
    }
    host = host_environment({**settings, **tokens})
    assert {name: host.get(name) for name in settings} == settings
    assert not set(tokens) & set(host)
    # 沙箱内命令的缓存由私有目录承担，不继承这些设置。
    sandboxed = sandbox_environment(
        {**settings, **tokens}, private_tmp=CACHE / "tmp/o", private_cache=CACHE / "cache"
    )
    assert not (set(settings) | set(tokens)) & set(sandboxed)


def test_sandbox_environment_inherits_allowlist_without_proxies_or_secrets() -> None:
    environ = {
        "PATH": "/opt/homebrew/bin:/usr/bin",
        "HOME": str(HOME),
        "LC_ALL": "en_US.UTF-8",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "MODEL_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
        "HARNESSIX_PRODUCT_CONFIG": "/x",
        "GOPROXY": "https://goproxy.cn",
    }
    environment = sandbox_environment(
        environ,
        private_tmp=CACHE / "tmp/owner",
        private_cache=CACHE / "cache",
        presets=(("LANG", "C.UTF-8"),),
        secret_names=("MODEL_API_KEY",),
    )
    assert environment["PATH"] == environ["PATH"]
    assert environment["GOPROXY"] == "https://goproxy.cn"
    assert environment["LANG"] == "C.UTF-8"
    assert environment["TMPDIR"] == str(CACHE / "tmp/owner")
    assert environment["PIP_CACHE_DIR"] == str(CACHE / "cache/pip")
    assert not {"HTTPS_PROXY", "MODEL_API_KEY", "OPENAI_API_KEY", "HARNESSIX_PRODUCT_CONFIG"} & set(
        environment
    )
    host = host_environment(environ, secret_names=("MODEL_API_KEY",))
    assert host["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert "MODEL_API_KEY" not in host and "TMPDIR" not in host


def test_the_jvm_temporary_directory_is_the_private_tmpdir_under_seatbelt() -> None:
    """Beta Issue #25：macOS上的JVM不读TMPDIR，默认临时目录在Seatbelt写入范围之外。"""
    tmp = CACHE / "tmp/owner"
    plain = sandbox_environment({}, private_tmp=tmp, private_cache=CACHE / "cache")
    assert "JAVA_TOOL_OPTIONS" not in plain  # 只在Seatbelt下设置
    seatbelt = sandbox_environment(
        {"JAVA_TOOL_OPTIONS": "-Xmx1g"},  # 用户环境里的值本来就不继承
        private_tmp=tmp,
        private_cache=CACHE / "cache",
        presets=(("JAVA_TOOL_OPTIONS", "-Dfile.encoding=UTF-8"),),
        jvm_temporary_directory=True,
    )
    # 追加在预设值之后，后出现的-D生效。
    assert seatbelt["JAVA_TOOL_OPTIONS"] == f"-Dfile.encoding=UTF-8 -Djava.io.tmpdir={tmp}"
    spaced = sandbox_environment(
        {}, private_tmp=Path("/Users/a b/tmp"), private_cache=CACHE, jvm_temporary_directory=True
    )
    assert spaced["JAVA_TOOL_OPTIONS"] == '-Djava.io.tmpdir="/Users/a b/tmp"'


@pytest.mark.parametrize("name", ["HOME", "TMPDIR", "XDG_CACHE_HOME", "xdg_config_home", "GOCACHE"])
def test_harnessix_managed_variables_are_reserved(name: str) -> None:
    assert sandbox_reserved_environment_problem(name) is not None
    assert sandbox_reserved_environment_problem("RUST_LOG") is None
