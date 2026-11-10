"""Seatbelt策略翻译（ADR 0111第2.3–2.5节）：结构、参数化与规则顺序。

真实执行只能在未被沙箱化的普通终端验证（见test_native_sandbox_action.py）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harnessix.agent.errors import KernelError
from harnessix.sandbox.native_policy import NativeSandboxFacts, build_native_sandbox_policy
from harnessix.sandbox.seatbelt import seatbelt_argv, seatbelt_policy

HOME = Path("/Users/dev")
WORKSPACE = HOME / "src/app"


def _policy(**overrides: object):
    values: dict[str, object] = {
        "backend": "seatbelt",
        "home": HOME,
        "workspace": WORKSPACE,
        "workspace_mode": "read_write",
        "private_tmp": HOME / "Library/Caches/harnessix/sandbox/x/tmp",
        "private_cache": HOME / "Library/Caches/harnessix/sandbox/x/cache",
        "git_directories": (WORKSPACE / ".git",),
        "blocked_loopback_ports": (7890, 7892),
    }
    return build_native_sandbox_policy(NativeSandboxFacts(**(values | overrides)))  # type: ignore[arg-type]


def test_paths_only_travel_as_parameters() -> None:
    hostile = Path('/Users/dev/src/we")(allow default)(x "')
    policy = _policy(write_roots=(hostile,))
    text, parameters = seatbelt_policy(policy)
    assert "/Users/dev" not in text and "(allow default)" not in text
    writable = [item for item in parameters if item.startswith("-DWRITABLE_")]
    assert writable == [
        f"-DWRITABLE_{index}={root}" for index, root in enumerate(policy.writable_roots)
    ]
    assert f"-DWRITABLE_{policy.writable_roots.index(str(hostile))}={hostile}" in parameters


def test_rule_order_lets_protections_override_grants() -> None:
    text, parameters = seatbelt_policy(_policy())
    lines = text.splitlines()

    def index(fragment: str) -> int:
        return next(number for number, line in enumerate(lines) if fragment in line)

    assert lines[:2] == ["(version 1)", "(deny default)"]
    assert index("(allow file-read*)") < index("(deny file-read* file-write* (subpath")
    assert index("(allow file-write* (subpath") < index("(deny file-write* (subpath")
    assert index('(allow network-outbound (remote ip "localhost:*"))') < index(
        '(deny network-outbound (remote ip "localhost:7890"))'
    )
    assert '(remote ip "localhost:7892")' in text
    assert "vnode-type REGULAR-FILE" in text
    assert "SecurityServer" not in text and "launchservicesd" not in text
    denied = [item for item in parameters if item.startswith("-DDENIED_")]
    assert any(item.endswith("/Users/dev/.ssh") for item in denied)
    assert any(item.endswith("/Users/dev/Library/Keychains") for item in denied)
    assert any(item.startswith("-DREAD_ONLY_0=") and item.endswith("/.git") for item in parameters)


def test_every_deny_rule_comes_after_every_allow_rule() -> None:
    """SBPL以最后匹配为准；任何放行规则排在拒绝之后都会覆盖拒绝（macOS实测）。"""

    text, _ = seatbelt_policy(_policy())
    lines = text.splitlines()
    allows = [number for number, line in enumerate(lines) if line.startswith("(allow ")]
    denies = [
        number
        for number, line in enumerate(lines)
        if line.startswith("(deny ") and line != "(deny default)"
    ]
    assert denies and max(allows) < min(denies)
    assert lines[-1] == '(deny network-outbound (remote ip "localhost:7892"))'


def test_env_override_and_read_only_mode_change_rules() -> None:
    text, parameters = seatbelt_policy(_policy(allow_workspace_env_files=True))
    assert "vnode-type REGULAR-FILE" not in text
    _, read_only = seatbelt_policy(_policy(workspace_mode="read_only"))
    assert f"-DWRITABLE_0={WORKSPACE}" not in read_only
    assert [item for item in read_only if item.startswith("-DREAD_ONLY_")] == [
        f"-DREAD_ONLY_0={WORKSPACE}"
    ]


def test_argv_shape_and_limits() -> None:
    argv = seatbelt_argv(_policy(), command=("/bin/zsh", "-lc", "echo hi"))
    assert argv[:2] == ("/usr/bin/sandbox-exec", "-p")
    assert argv[-3:] == ("/bin/zsh", "-lc", "echo hi")
    assert len(argv) <= 128
    with pytest.raises(KernelError):
        seatbelt_argv(_policy(backend="bubblewrap", blocked_loopback_ports=()), command=("x",))
