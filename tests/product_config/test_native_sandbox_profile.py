"""本机沙箱原生Profile的证明、计划绑定与资源声明（ADR 0111第2.6节）。

探测用可替换的假执行器，因此在任何POSIX平台都能验证装配逻辑；真实bubblewrap执行见
test_native_sandbox_action.py。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from harnessix.agent.errors import KernelError
from harnessix.agent.models import TrustedActionApprovalRequestContent
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.processes.supervisor_capabilities import probe_posix_process_capability
from harnessix.product_config.native_process import (
    attest_native_process_profile,
    native_process_descriptor,
    probe_native_process_profile,
    resolve_native_command,
)
from harnessix.product_config.native_sandbox import (
    NativeSandboxSite,
    attest_native_sandbox,
    prepare_sandboxed_argv,
)
from harnessix.product_config.process_command import RunCommandInput
from harnessix.sandbox.native_contracts import HostSandboxProbeV2
from harnessix.sandbox.native_probe import probe_native_sandbox
from harnessix.trusted_actions.router import ActionPlanningContext
from tests.product_config.test_native_process_profile import native_profile

pytestmark = pytest.mark.skipif(os.name != "posix", reason="原生沙箱只支持POSIX")


def _fake_probe(tmp_path: Path, platform: str = "linux", code: int = 0):
    executable = tmp_path / "bin" / ("bwrap" if platform == "linux" else "sandbox-exec")
    executable.parent.mkdir(exist_ok=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)

    def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        output = "bubblewrap 0.6.1\n" if "--version" in argv else ""
        return subprocess.CompletedProcess(argv, 0 if "--version" in argv else code, output, "")

    def probe() -> HostSandboxProbeV2:
        return probe_native_sandbox(
            platform=platform,  # type: ignore[arg-type]
            runner=run,
            nested=lambda: False,
            bubblewrap_candidates=(executable,),
            seatbelt_executable=executable,
        )

    return probe


@pytest.fixture
def sandbox_home(tmp_path: Path) -> dict[str, Any]:
    home = tmp_path / "home"
    workspace = home / "src/app"
    (workspace / ".git").mkdir(parents=True)
    environ = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "XDG_CACHE_HOME": str(home / ".cache"),
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "MODEL_API_KEY": "secret",
    }
    return {"home": home, "workspace": workspace, "environ": environ}


def _attest(tmp_path: Path, sandbox_home: dict[str, Any], **profile: Any):
    capability = probe_posix_process_capability()
    return attest_native_process_profile(
        native_profile(backend="bubblewrap", **profile),
        capability,
        environ=sandbox_home["environ"],
        site=NativeSandboxSite(
            workspace_root=sandbox_home["workspace"], state_root=tmp_path / "state"
        ),
        secret_names=("MODEL_API_KEY",),
        sandbox_probe=_fake_probe(tmp_path),
    )


def test_bubblewrap_attestation_binds_host_sandboxed_plan(
    tmp_path: Path, sandbox_home: dict[str, Any]
) -> None:
    reason, attested = _attest(tmp_path, sandbox_home)
    assert reason == "verified" and attested is not None
    sandbox = attested.sandbox
    assert (sandbox.level, sandbox.backend, sandbox.network) == (
        "host_sandboxed",
        "bubblewrap",
        "none",
    )
    assert attested.capabilities.provider == "bubblewrap"
    assert attested.capabilities.network_modes == ("none",)
    binding = attested.native_sandbox
    assert binding is not None
    policy = binding.policy
    assert policy.workspace == str(sandbox_home["workspace"].resolve())
    assert str((tmp_path / "state").resolve()) in policy.denied_paths
    environment = attested.environment
    assert environment["TMPDIR"] == policy.private_tmp
    assert "HTTPS_PROXY" not in environment and "MODEL_API_KEY" not in environment
    # Profile预设优先于继承的同名变量。
    assert environment["PATH"] == "/opt/env/bin:/usr/bin:/bin"


def test_profile_or_policy_changes_change_the_binding(
    tmp_path: Path, sandbox_home: dict[str, Any]
) -> None:
    _, base = _attest(tmp_path, sandbox_home)
    _, read_only = _attest(tmp_path, sandbox_home, workspace_mode="read_only")
    assert base is not None and read_only is not None
    assert base.sandbox.profile_digest != read_only.sandbox.profile_digest
    assert base.executor_evidence_sha256 != read_only.executor_evidence_sha256


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"probe_code": 1}, "bubblewrap_preflight_failed"),
        ({"backend": "seatbelt"}, "native_backend_platform_mismatch"),
        ({"site": None}, "native_sandbox_site_unavailable"),
        ({"workspace_is_home": True}, "sandbox_root_overlaps_denied"),
    ],
)
def test_attestation_failures_omit_the_tool_with_reasons(
    tmp_path: Path, sandbox_home: dict[str, Any], change: dict[str, Any], reason: str
) -> None:
    workspace = (
        sandbox_home["home"] if change.get("workspace_is_home") else sandbox_home["workspace"]
    )
    site = (
        None
        if "site" in change
        else NativeSandboxSite(workspace_root=workspace, state_root=tmp_path / "state")
    )
    result = attest_native_process_profile(
        native_profile(backend=change.get("backend", "bubblewrap")),
        probe_posix_process_capability(),
        environ=sandbox_home["environ"],
        site=site,
        sandbox_probe=_fake_probe(tmp_path, code=change.get("probe_code", 0)),
    )
    assert result == (reason, None)


_SCUTIL_OUTPUT = """<dictionary> {
  HTTPEnable : 1
  HTTPPort : 7892
  HTTPProxy : 127.0.0.1
}
"""


def _seatbelt_runner(code: int = 0):
    calls: list[tuple[str, ...]] = []

    def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        if argv[0] == "/usr/sbin/scutil":
            return subprocess.CompletedProcess(argv, 0, _SCUTIL_OUTPUT, "")
        return subprocess.CompletedProcess(argv, code, "", "sandbox-exec: syntax error")

    return run, calls


def test_seatbelt_records_full_network_and_blocks_configured_proxy_ports(
    tmp_path: Path, sandbox_home: dict[str, Any]
) -> None:
    run, calls = _seatbelt_runner()
    reason, attested = attest_native_sandbox(
        native_profile(backend="seatbelt"),
        probe_posix_process_capability(),
        environ=sandbox_home["environ"],
        site=NativeSandboxSite(workspace_root=sandbox_home["workspace"]),
        probe=_fake_probe(tmp_path, platform="macos"),
        runner=run,
    )
    assert reason == "verified" and attested is not None
    assert (attested.sandbox.level, attested.sandbox.backend, attested.sandbox.network) == (
        "host_sandboxed",
        "seatbelt",
        "full",
    )
    assert attested.capabilities.network_modes == ("full",)
    # 环境变量中的7890与系统设置中的7892都被拦截。
    assert attested.binding.policy.blocked_loopback_ports == (7890, 7892)
    preflight = calls[-1]
    assert preflight[1] == "-p" and preflight[-1] == "/usr/bin/true"


def test_seatbelt_policy_rejected_by_the_system_omits_the_tool(
    tmp_path: Path, sandbox_home: dict[str, Any]
) -> None:
    run, _ = _seatbelt_runner(code=65)
    assert attest_native_sandbox(
        native_profile(backend="seatbelt"),
        probe_posix_process_capability(),
        environ=sandbox_home["environ"],
        site=NativeSandboxSite(workspace_root=sandbox_home["workspace"]),
        probe=_fake_probe(tmp_path, platform="macos"),
        runner=run,
    ) == ("seatbelt_preflight_failed", None)


async def test_resources_follow_workspace_mode_and_write_roots(
    tmp_path: Path, sandbox_home: dict[str, Any]
) -> None:
    extra = tmp_path / "shared-cache"
    extra.mkdir()
    profile = native_profile(
        backend="bubblewrap", workspace_mode="read_only", write_roots=(str(extra),)
    )
    async with PosixProcessSupervisor(tmp_path / "owner") as supervisor:
        probe = probe_native_process_profile(
            profile,
            supervisor,
            environ=sandbox_home["environ"],
            site=NativeSandboxSite(workspace_root=sandbox_home["workspace"]),
            sandbox_probe=_fake_probe(tmp_path),
        )
        owner = probe.verified
        assert owner is not None
        resolved = resolve_native_command(
            owner,
            RunCommandInput(profile=profile.profile_id, cmd="ls"),
            ActionPlanningContext(
                workspace_root=sandbox_home["workspace"],
                sandbox=owner.sandbox,
                capabilities=owner.capabilities,
                environment=owner.environment,
            ),
        )
    kinds = {(item.kind, item.access) for item in resolved.resources}
    assert kinds == {("process", "execute"), ("workspace", "read"), ("external", "write")}
    assert resolved.workspace_resources[0].access == "read"


def test_sandbox_descriptor_states_limits_and_boundary_text_is_unchanged() -> None:
    sandboxed = native_process_descriptor(native_profile(backend="bubblewrap"))
    assert "本机沙箱" in sandboxed.description and "没有网络" in sandboxed.description
    assert ".git只读" in sandboxed.description
    boundary = native_process_descriptor(native_profile())
    assert "Harnessix不另加沙箱" in boundary.description


def test_backend_identity_drift_is_a_preflight_failure(
    tmp_path: Path, sandbox_home: dict[str, Any]
) -> None:
    _, attested = _attest(tmp_path, sandbox_home)
    assert attested is not None and attested.native_sandbox is not None
    executable = Path(attested.native_sandbox.probe.executable or "")
    executable.write_text("#!/bin/sh\n# replaced\n")
    with pytest.raises(Exception) as error:
        prepare_sandboxed_argv(attested.native_sandbox, ("/bin/sh", "-c", "true"))
    assert getattr(error.value, "code", None) == "sandbox_backend_changed"


_PASSTHROUGH = {
    # 只剥掉后端参数后直接运行命令：不提供隔离，用来验证产品链路的计划与启动绑定一致。
    "macos": "shift; shift\n"
    'while [ $# -gt 0 ]; do case "$1" in -D*) shift;; *) break;; esac; done\n'
    'exec "$@"\n',
    "linux": 'if [ "$1" = "--version" ]; then echo "bubblewrap 0.6.1"; exit 0; fi\n'
    'while [ $# -gt 0 ]; do if [ "$1" = "--" ]; then shift; break; fi; shift; done\n'
    # 预检运行/bin/true；macOS上没有该路径。
    'if [ "$1" = /bin/true ]; then exit 0; fi\n'
    'exec "$@"\n',
}


@pytest.mark.parametrize("platform", ["macos", "linux"])
async def test_launch_binding_matches_plan_through_product_chain(
    tmp_path: Path, sandbox_home: dict[str, Any], platform: str
) -> None:
    from tests.product_config.test_native_process_action import _native_case, _result

    executable = tmp_path / "bin" / ("sandbox-exec" if platform == "macos" else "bwrap")
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n" + _PASSTHROUGH[platform])
    executable.chmod(0o755)

    def probe() -> HostSandboxProbeV2:
        def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
            return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)

        return probe_native_sandbox(
            platform=platform,  # type: ignore[arg-type]
            runner=run,
            nested=lambda: False,
            bubblewrap_candidates=(executable,),
            seatbelt_executable=executable,
        )

    backend = "seatbelt" if platform == "macos" else "bubblewrap"
    async with _native_case(
        tmp_path,
        "echo ran > ran.txt",
        native_profile(backend=backend),
        environ=sandbox_home["environ"],
        site=lambda root: NativeSandboxSite(workspace_root=root),
        sandbox_probe=probe,
    ) as case:
        result = _result(await case.approve_and_resume())
        assert (result.outcome, result.error) == ("succeeded", None)
        assert (case.workspace / "ran.txt").read_text() == "ran\n"
        sandbox = case.router.status(case.approval.plan_id).plan.execution.sandbox
        assert (sandbox.level, sandbox.backend) == ("host_sandboxed", backend)
        # 沙箱内命令由宿主以系统身份当场批准，用户不会看到待审批（ADR 0113）。
        decision = case.approval.decision
        assert decision is not None and decision.actor == "harnessix:sandbox-policy"
        assert case.approval.policy_id == "default.approve-sandboxed-command"
        # 客户端（自动批准模式或人工）晚到的同结论回复幂等成功，审批只记一次；相反结论失败关闭。
        late = ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="harnessix-exec")
        again = await case.agent.reply_approval(
            case.thread_id,
            case.turn_id,
            case.approval.approval_id,
            fingerprint=case.approval.request_fingerprint,
            decision=late,
        )
        approvals = [
            item.content
            for item in again.items
            if isinstance(item.content, TrustedActionApprovalRequestContent)
        ]
        assert len(approvals) == 1 and approvals[0].decision == decision
        with pytest.raises(KernelError) as conflict:
            await case.agent.reply_approval(
                case.thread_id,
                case.turn_id,
                case.approval.approval_id,
                fingerprint=case.approval.request_fingerprint,
                decision=ApprovalDecision(outcome=ApprovalOutcome.REJECTED, actor="human"),
            )
        assert conflict.value.code == "approval_conflict"


async def test_pending_sandbox_approval_is_filled_in_on_resume(
    tmp_path: Path, sandbox_home: dict[str, Any]
) -> None:
    """崩溃恢复：审批请求已落盘、系统决定未落盘时，重启后resume_turn补批并继续执行。"""

    from harnessix.agent.runtime import AgentRuntime
    from tests.product_config.test_native_process_action import _native_case, _result

    executable = tmp_path / "bin" / "bwrap"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n" + _PASSTHROUGH["linux"])
    executable.chmod(0o755)

    def probe() -> HostSandboxProbeV2:
        def run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
            return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)

        return probe_native_sandbox(
            platform="linux", runner=run, bubblewrap_candidates=(executable,)
        )

    original = AgentRuntime.__init__

    def without_responder(self: AgentRuntime, *args: Any, **kwargs: Any) -> None:
        # 模拟进程在系统决定落盘前退出：第一次运行没有应答器。
        responder = kwargs.pop("approval_responder", None)
        original(self, *args, **kwargs)
        self._held_responder = responder  # type: ignore[attr-defined]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AgentRuntime, "__init__", without_responder)
        async with _native_case(
            tmp_path,
            "echo resumed > resumed.txt",
            native_profile(backend="bubblewrap"),
            environ=sandbox_home["environ"],
            site=lambda root: NativeSandboxSite(workspace_root=root),
            sandbox_probe=probe,
        ) as case:
            assert case.approval.decision is None
            case.agent._approval_responder = case.agent._held_responder  # type: ignore[attr-defined]
            resumed = await case.agent.resume_turn(case.thread_id, case.turn_id)
            assert _result(resumed).outcome == "succeeded"
            assert (case.workspace / "resumed.txt").read_text() == "resumed\n"
