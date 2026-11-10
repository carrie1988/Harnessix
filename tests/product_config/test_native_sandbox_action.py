"""本机沙箱原生命令经产品Action链路端到端执行（ADR 0111第6节A、B、D类）。

- Linux：bubblewrap，在能运行它的宿主上执行（如247以nobody身份运行，basetemp不放在/tmp下）。
- macOS：Seatbelt，必须在未被沙箱化的普通终端执行（Claude会话内嵌套会被系统拒绝，自动跳过）。
HOME、工作区与私有目录都在tmp_path内，不触碰真实用户目录。
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from harnessix.agent.models import TurnStatus
from harnessix.processes.supervisor_capabilities import probe_posix_process_capability
from harnessix.product_config.native_process import attest_native_process_profile
from harnessix.product_config.native_sandbox import NativeSandboxSite, prepare_sandboxed_argv
from harnessix.sandbox.native_probe import probe_native_sandbox
from tests.product_config.test_native_process_action import _native_case, _result
from tests.product_config.test_native_process_profile import native_profile

_PROBE = probe_native_sandbox()
BACKEND = _PROBE.backend if _PROBE.available else None
pytestmark = pytest.mark.skipif(BACKEND is None, reason=f"本机沙箱不可用：{_PROBE.reason_code}")
_SHELL = {"seatbelt": "/bin/zsh", "bubblewrap": "/bin/bash"}


def _environ(home: Path, proxy_port: int | None = None) -> dict[str, str]:
    environ = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "HARNESSIX_TEST_MODEL_KEY": "must-not-leak",
    }
    if proxy_port is not None:
        environ["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy_port}"
    return environ


def _case(
    tmp_path: Path,
    cmd: str,
    *,
    proxy_port: int | None = None,
    state_root: Path | None = None,
    **profile: Any,
):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True, exist_ok=True)
    (home / ".ssh/id_rsa").write_text("PRIVATE-KEY\n")
    workspace = tmp_path / "workspace"
    (workspace / ".git/hooks").mkdir(parents=True, exist_ok=True)
    (workspace / ".env").write_text("TOKEN=workspace-secret\n")
    return _native_case(
        tmp_path,
        cmd,
        native_profile(
            backend=BACKEND,
            program=_SHELL[str(BACKEND)],
            environment=(("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),),
            **profile,
        ),
        environ=_environ(home, proxy_port),
        site=lambda root: NativeSandboxSite(
            workspace_root=root, state_root=state_root or tmp_path / "state"
        ),
        secret_names=("HARNESSIX_TEST_MODEL_KEY",),
    )


def _report(case: Any, name: str) -> str:
    path = case.workspace / name
    return path.read_text() if path.exists() else "<missing>"


async def test_sandboxed_command_writes_workspace_but_not_protected_paths(
    tmp_path: Path,
) -> None:
    cmd = "; ".join(
        (
            "printf 'changed\\n' > source.txt",
            '{ cat "$HOME/.ssh/id_rsa" || echo ssh-denied; } > report.txt 2>&1',
            "{ cat .env || echo env-denied; } >> report.txt 2>&1",
            "{ echo x > .git/hooks/pre-commit || echo git-denied; } >> report.txt 2>&1",
            '{ echo x > "$HOME/.bashrc" || echo home-denied; } >> report.txt 2>&1',
            '{ echo x > "$TMPDIR/t" && echo tmpdir-ok; } >> report.txt 2>&1',
            "{ echo x > /dev/null && echo devnull-ok; } >> report.txt 2>&1",
            "{ git status --short > /dev/null && echo git-status-ok; } >> report.txt 2>&1",
            "env | sort > env.txt",
        )
    )
    async with _case(tmp_path, cmd) as case:
        completed = await case.approve_and_resume()
        report = _report(case, "report.txt")
        assert completed.status is TurnStatus.COMPLETED, report
        assert _result(completed).outcome == "succeeded", report
        assert (case.workspace / "source.txt").read_text() == "changed\n"
        for marker in (
            "ssh-denied",
            "env-denied",
            "git-denied",
            "home-denied",
            "tmpdir-ok",
            "devnull-ok",
        ):
            assert marker in report, report
        assert "PRIVATE-KEY" not in report and "workspace-secret" not in report, report
        assert not (case.workspace / ".git/hooks/pre-commit").exists()
        assert not (tmp_path / "home/.bashrc").exists()
        names = {
            line.split("=", 1)[0] for line in _report(case, "env.txt").splitlines() if "=" in line
        }
        assert not {"HTTPS_PROXY", "HARNESSIX_TEST_MODEL_KEY"} & names
        assert {"HOME", "PATH", "TMPDIR"} <= names
        plan = case.router.status(case.approval.plan_id).plan
        sandbox = plan.execution.sandbox
        expected_network = "none" if BACKEND == "bubblewrap" else "full"
        assert (sandbox.level, sandbox.backend, sandbox.network) == (
            "host_sandboxed",
            BACKEND,
            expected_network,
        )
        # 真实后端下同样由宿主以系统身份自动批准，不需要人工（ADR 0113）。
        decision = case.approval.decision
        assert decision is not None and decision.actor == "harnessix:sandbox-policy"


async def test_default_state_directory_inside_home_still_starts_and_stays_hidden(
    tmp_path: Path,
) -> None:
    """产品默认状态目录在~/.harnessix之下：两层拒读路径嵌套时命令仍能启动，状态仍不可读。"""
    state = tmp_path / "home/.harnessix/workspaces/abc/runtime"
    state.mkdir(parents=True)
    (state / "session.db").write_text("STATE-SECRET\n")
    secret = "$HOME/.harnessix/workspaces/abc/runtime/session.db"
    cmd = f'{{ cat "{secret}" || echo state-denied; }} > report.txt 2>&1'
    async with _case(tmp_path, cmd, state_root=state) as case:
        completed = await case.approve_and_resume()
        report = _report(case, "report.txt")
        assert completed.status is TurnStatus.COMPLETED, report
        assert _result(completed).outcome == "succeeded", report
        assert "state-denied" in report and "STATE-SECRET" not in report, report


async def test_network_boundary_matches_backend(tmp_path: Path) -> None:
    script = (
        "import socket, sys\n"
        "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen()\n"
        "socket.create_connection(s.getsockname(),timeout=2); print('loopback-ok')\n"
        "for name, port in (('host', int(sys.argv[1])), ('proxy', int(sys.argv[2]))):\n"
        "    try:\n"
        "        socket.create_connection(('127.0.0.1',port),timeout=2); print(name+'-open')\n"
        "    except OSError: print(name+'-blocked')\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1',443),timeout=3); print('egress-open')\n"
        "except OSError: print('egress-blocked')\n"
    )
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace/net.py").write_text(script)
    listeners = []
    for _ in range(2):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listeners.append(listener)
    host_port, proxy_port = (item.getsockname()[1] for item in listeners)
    try:
        async with _case(
            tmp_path,
            f"python3 net.py {host_port} {proxy_port} > net.txt 2>&1",
            proxy_port=proxy_port,
        ) as case:
            result = _result(await case.approve_and_resume())
            report = _report(case, "net.txt")
    finally:
        for listener in listeners:
            listener.close()
    assert result.outcome == "succeeded", report
    assert "loopback-ok" in report and "egress-blocked" in report, report
    if BACKEND == "bubblewrap":
        assert "host-blocked" in report and "proxy-blocked" in report, report
    else:
        # macOS放行宿主回环（用户决策），但已配置的代理端口被拦截。
        assert "host-open" in report and "proxy-blocked" in report, report


async def test_read_only_mode_rejects_workspace_writes(tmp_path: Path) -> None:
    async with _case(tmp_path, "echo x > new.txt", workspace_mode="read_only") as case:
        result = _result(await case.approve_and_resume())
        assert result.outcome == "failed"
        assert result.error is not None and result.error.code == "process_nonzero_exit"
        assert not (case.workspace / "new.txt").exists()


async def test_many_env_files_are_all_protected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    for index in range(40):
        (workspace / f"svc{index}").mkdir(parents=True)
        (workspace / f"svc{index}/.env").write_text("X=secret\n")
    async with _case(tmp_path, "cat svc7/.env > out.txt 2>&1; touch ran.txt") as case:
        result = _result(await case.approve_and_resume())
        if BACKEND == "bubblewrap":
            # 遮盖数量超出参数上限：启动前拒绝执行。
            assert result.outcome == "failed"
            assert result.error is not None and result.error.code == "process_preflight_failed"
            assert not (case.workspace / "ran.txt").exists()
        else:
            # Seatbelt按规则拒读任意数量的.env。
            assert (case.workspace / "ran.txt").exists()
            assert "secret" not in _report(case, "out.txt")


def test_generated_policy_is_accepted_by_the_system(tmp_path: Path) -> None:
    """策略被系统拒绝时，这里给出后端的原始报错，便于调整。"""

    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    reason, attested = attest_native_process_profile(
        native_profile(backend=BACKEND, program=_SHELL[str(BACKEND)]),
        probe_posix_process_capability(),
        environ=_environ(home),
        site=NativeSandboxSite(workspace_root=workspace),
    )
    if attested is None or attested.native_sandbox is None:
        pytest.fail(f"证明失败：{reason}")
    argv = prepare_sandboxed_argv(attested.native_sandbox, ("/bin/sh", "-c", "echo sandboxed"))
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=workspace,
        env=dict(attested.environment),
    )
    assert completed.returncode == 0 and completed.stdout == "sandboxed\n", completed.stderr
