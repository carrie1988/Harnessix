from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from harnessix.agent.errors import KernelError
from harnessix.agent.models import (
    ToolResultContent,
    TrustedActionApprovalRequestContent,
    TurnStatus,
)
from harnessix.agent.runtime import AgentRuntime
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.delivery.store import SQLiteWorkspaceTransactionStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.models.contracts import ResponseCompleted, ResponseStarted, ToolCallCompleted
from harnessix.models.scripted import ScriptedProvider
from harnessix.processes.supervision_contracts import ProcessLease
from harnessix.processes.supervisor import PosixProcessSupervisor
from harnessix.processes.trusted_output import parse_trusted_process_output
from harnessix.product_config.action_composition import (
    build_fixed_product_action_environment,
    build_product_action_composition,
)
from harnessix.product_config.action_contracts import (
    ProductProcessProfile,
    build_product_action_config,
    build_product_process_profile,
)
from harnessix.product_config.process_action import (
    decode_run_command,
    decode_run_profile,
    process_profile_descriptor,
    product_process_binding,
)
from harnessix.product_config.process_profile import (
    ProductProcessProfileProbeResult,
    VerifiedProductProcessProfile,
    probe_product_process_profile,
    process_tool_name,
)
from harnessix.sandbox.contracts import ContainerExecutionSpec
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.tools.runtime import CodingToolRuntime
from harnessix.trusted_actions.public_outcomes import failure_family
from harnessix.trusted_actions.router import ActionPlanningContext, TrustedActionRouter
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.leases import WorkspaceLeaseStore
from tests.agent.helpers import answer
from tests.product_config.test_process_action import (
    _REPOSITORY_IMAGE,
    _CancellableRuntime,
    _fake_engine,
    _NoSecrets,
    _probe_runner,
    _TerminalRuntime,
)


def _command_profile(engine: Path, **overrides: Any) -> ProductProcessProfile:
    values = {
        "profile_id": "workspace-command",
        "version": "2026.10.1",
        "description": "运行批准的只读工作区命令",
        "container_engine": str(engine),
        "image": _REPOSITORY_IMAGE,
        "program": "/bin/sh",
        "arguments": ("-c",),
        "selector_policy": "shell_command",
    }
    return build_product_process_profile(**(values | overrides))


class _RecordingRuntime(_TerminalRuntime):
    """保留真实Executor传入的合同，仅模拟容器Owner的终态。"""

    execution: ContainerExecutionSpec | None = None
    launch_kwargs: dict[str, Any]

    async def run(self, *args: Any, **kwargs: Any) -> ProcessLease:
        self.execution = args[3]
        self.launch_kwargs = kwargs
        return await super().run(*args, **kwargs)


@dataclass
class _CommandCase:
    agent: AgentRuntime
    router: TrustedActionRouter
    artifacts: SQLiteArtifactStore
    runtime: _TerminalRuntime
    owner: VerifiedProductProcessProfile
    workspace: Path
    workspace_scope: str
    thread_id: UUID
    turn_id: UUID
    approval: TrustedActionApprovalRequestContent

    def context(self) -> ActionPlanningContext:
        return ActionPlanningContext(
            workspace_root=self.workspace,
            sandbox=self.owner.sandbox,
            capabilities=self.owner.capabilities,
            environment=self.owner.environment,
            secrets=self.owner.secret_bindings,
        )

    async def approve(self) -> None:
        await self.agent.reply_approval(
            self.thread_id,
            self.turn_id,
            self.approval.approval_id,
            fingerprint=self.approval.request_fingerprint,
            decision=ApprovalDecision(outcome=ApprovalOutcome.APPROVED, actor="command-reviewer"),
        )

    async def resume(self) -> Any:
        return await self.agent.resume_turn(self.thread_id, self.turn_id)


@asynccontextmanager
async def _command_case(
    root: Path,
    cmd: str,
    *,
    stdout: bytes = b"command completed\n",
    returncode: int = 0,
    cancellable: bool = False,
) -> AsyncIterator[_CommandCase]:
    workspace = root / "workspace"
    workspace.mkdir()
    profile = _command_profile(_fake_engine(root))
    secrets = _NoSecrets()
    async with PosixProcessSupervisor(root / "probe-owner") as supervisor:
        probe = probe_product_process_profile(
            profile, supervisor, secrets, probe_runner=_probe_runner
        )
        assert probe.reason_code == "verified" and probe.verified is not None
        capability = probe.verified.runtime.capability
        runtime = (
            _CancellableRuntime(capability)
            if cancellable
            else _RecordingRuntime(capability, stdout=stdout, returncode=returncode)
        )
        owner = replace(probe.verified, runtime=cast(Any, runtime))

    with ExitStack() as cleanup:
        sessions = SQLiteSessionStore(root / "sessions.db")
        artifacts = SQLiteArtifactStore(sessions)
        environment = build_fixed_product_action_environment(workspace)
        plans = SQLiteExecutionPlanStore(root / "plans.db")
        audit = SQLiteActionAuditStore(root / "audit.db")
        transactions = SQLiteWorkspaceTransactionStore(root / "delivery")
        leases = WorkspaceLeaseStore(root / "leases.db")
        for store in (plans, audit, transactions, leases):
            cleanup.callback(store.close)
        router = TrustedActionRouter(
            plans=plans, audit=audit, workspace_root=environment.workspace_root
        )
        # ScriptedProvider只模拟模型提案；批准、规划、执行和输出发布均走产品链路。
        provider = ScriptedProvider(
            [
                [
                    ResponseStarted(response_id="command-response"),
                    ToolCallCompleted(
                        call_id="command-call",
                        tool="exec_command.workspace-command",
                        arguments={"profile": profile.profile_id, "cmd": cmd},
                    ),
                    ResponseCompleted(finish_reason="tool_calls"),
                ],
                answer("命令执行完成"),
            ]
        )
        async with CodingToolRuntime(workspace, artifacts=artifacts) as tools:
            composition = build_product_action_composition(
                build_product_action_config(
                    workspace_patch_enabled=False, process_profiles=(profile,)
                ),
                environment,
                router,
                transactions,
                leases,
                artifacts,
                artifact_workspace_scope=tools.workspace_scope,
                process_probes=(
                    ProductProcessProfileProbeResult(
                        profile=profile, reason_code="verified", verified=owner
                    ),
                ),
                secrets=secrets,
            )
            assert composition.gateway is not None
            assert [item.name for item in composition.catalog.definitions()] == [
                "exec_command.workspace-command"
            ]
            capability_report = next(
                item
                for item in composition.report.capabilities
                if item.capability_id == "exec_command.workspace-command"
            )
            assert capability_report.status == "verified"
            async with AgentRuntime(
                sessions,
                provider,
                scoped_tools=tools,
                artifacts=artifacts,
                trusted_actions=composition.gateway,
            ) as agent:
                thread = await agent.create_thread(str(workspace))
                waiting = await agent.run_turn(thread.thread_id, "运行命令", request_id="command")
                approval = next(
                    item.content
                    for item in waiting.items
                    if isinstance(item.content, TrustedActionApprovalRequestContent)
                )
                assert waiting.status is TurnStatus.WAITING_APPROVAL
                assert approval.presentation == "process" and approval.diff_artifact is None
                assert runtime.run_calls == 0
                yield _CommandCase(
                    agent,
                    router,
                    artifacts,
                    runtime,
                    owner,
                    workspace,
                    tools.workspace_scope,
                    thread.thread_id,
                    waiting.turn_id,
                    approval,
                )


def test_command_profile_may_be_omitted_because_the_tool_name_binds_it() -> None:
    """v2合同：工具名已确定Profile，省略profile有效；传入时仍须等于固定值。"""
    checked = decode_run_command("workspace-command", {"cmd": "echo ok"})
    assert checked.profile is None
    # 规范化参数可再次解码：省略的profile不会序列化成显式null。
    assert checked.model_dump(mode="json") == {"cmd": "echo ok"}
    assert decode_run_command("workspace-command", checked.model_dump(mode="json")) == checked
    with pytest.raises(ValueError):
        decode_run_command("workspace-command", {"profile": "other", "cmd": "echo ok"})


def test_command_profile_has_distinct_opt_in_identity_and_strict_schema(tmp_path: Path) -> None:
    profile = _command_profile(tmp_path / "docker")
    descriptor = process_profile_descriptor(profile)
    binding = product_process_binding(profile)
    assert process_tool_name(profile.profile_id) == "run_profile.workspace-command"
    assert process_tool_name(profile.profile_id, "shell_command") == descriptor.name
    assert descriptor.name == "exec_command.workspace-command"
    assert descriptor.version == f"harnessix.product-command/v2:{profile.profile_sha256[:24]}"
    assert binding.executor_id == "product.command-profile.workspace-command"
    assert descriptor.requires_approval and descriptor.requires_idempotency
    assert descriptor.supports_reconciliation and not descriptor.supports_parallel_calls
    assert set(descriptor.input_schema["properties"]) == {"profile", "cmd"}
    assert set(descriptor.input_schema["required"]) == {"cmd"}
    assert descriptor.input_schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("program", "arguments"),
    [("/usr/bin/python", ("-c",)), ("/bin/sh", ()), ("/bin/sh", ("-c", "echo fixed"))],
)
def test_command_profile_rejects_non_shell_or_prebound_command(
    tmp_path: Path, program: str, arguments: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        _command_profile(tmp_path / "docker", program=program, arguments=arguments)


@pytest.mark.parametrize("cmd", ["printf 'a|b'; echo done\n", "x" * 16384, "中" * 5461 + "x"])
def test_command_decoder_preserves_exact_text_and_utf8_boundary(cmd: str) -> None:
    checked = decode_run_command("workspace-command", {"profile": "workspace-command", "cmd": cmd})
    assert checked.cmd == cmd
    assert checked.model_dump(mode="json") == {"profile": "workspace-command", "cmd": cmd}


@pytest.mark.parametrize(
    "arguments",
    [
        {"profile": "workspace-command", "cmd": ""},
        {"profile": "workspace-command", "cmd": " \t\n"},
        {"profile": "workspace-command", "cmd": "echo\x00unsafe"},
        {"profile": "workspace-command", "cmd": "x" * 16385},
        {"profile": "workspace-command", "cmd": "中" * 5462},
        {"profile": "workspace-command", "cmd": "\ud800"},
        {"profile": "other", "cmd": "echo ok"},
        {"profile": "workspace-command"},
        {"profile": None, "cmd": "echo ok"},
        {"profile": "workspace-command", "cmd": 123},
        {"profile": "workspace-command", "cmd": "echo ok", "selectors": []},
        {"profile": "workspace-command", "cmd": "echo ok", "cwd": "/tmp"},
    ],
)
def test_command_decoder_rejects_invalid_or_unfrozen_input(arguments: Any) -> None:
    with pytest.raises(ValueError):
        decode_run_command("workspace-command", arguments)


def test_fixed_profile_decoder_still_rejects_dynamic_command() -> None:
    for policy in ("none", "bounded_test_selector"):
        with pytest.raises(ValueError):
            decode_run_profile(
                "workspace-command", policy, {"profile": "workspace-command", "cmd": "echo ok"}
            )


@pytest.mark.skipif(os.name != "posix", reason="命令Profile使用POSIX Process Owner夹具")
async def test_approval_binds_exact_command_and_keeps_container_boundary(tmp_path: Path) -> None:
    cmd = "  printf 'alpha\\nbeta\\n' | sed -n '2p'; printf '%s' \"$HOME\"\n"
    async with _command_case(tmp_path, cmd) as case:
        plan_id = case.approval.plan_id
        route = case.router.status(plan_id)
        assert failure_family(route.plan) == "process"
        assert route.plan.invocation.arguments == {"profile": "workspace-command", "cmd": cmd}
        with pytest.raises(KernelError) as unapproved:
            await case.router.execute(plan_id)
        assert unapproved.value.code == "action_not_approved"
        assert case.runtime.run_calls == 0
        await case.approve()

        changed = route.plan.invocation.model_copy(
            update={"arguments": {"profile": "workspace-command", "cmd": "echo different"}}
        )
        with pytest.raises(KernelError) as conflict:
            case.router.plan(changed, case.context())
        assert conflict.value.code == "action_invocation_conflict"
        different = case.router.plan(
            changed.model_copy(update={"invocation_id": uuid4(), "idempotency_key": "different"}),
            case.context(),
        )
        assert different.state == "pending_approval"
        assert different.plan.execution.fingerprint != route.plan.execution.fingerprint
        assert different.plan.resources_sha256 != route.plan.resources_sha256
        assert case.router.approval(different.plan.execution.plan_id) is None
        with pytest.raises(KernelError) as missing_approval:
            await case.router.execute(different.plan.execution.plan_id)
        assert missing_approval.value.code == "action_not_approved"

        completed = await case.resume()
        assert completed.status is TurnStatus.COMPLETED
        runtime = cast(_RecordingRuntime, case.runtime)
        assert runtime.execution is not None
        assert runtime.execution.command.argv == ("/bin/sh", "-c", cmd)
        assert runtime.execution.process.argv == ("/bin/sh", "-c", cmd)
        assert runtime.execution.process.timeout_seconds == case.owner.profile.timeout_seconds
        assert runtime.launch_kwargs["environment"] == {"LANG": "C", "LC_ALL": "C"}
        assert runtime.launch_kwargs["intent_arguments"] == route.plan.invocation.arguments
        assert case.owner.sandbox_profile.workspace_mode == "read_only"
        assert case.owner.sandbox_profile.network.policy.mode == "none"
        assert route.plan.execution.workspace.cwd == "."
        assert runtime.run_calls == 1


@pytest.mark.skipif(os.name != "posix", reason="命令Profile使用POSIX Process Owner夹具")
@pytest.mark.parametrize("returncode", [0, 7])
async def test_command_terminal_output_uses_process_contract_and_archives_complete_stdout(
    tmp_path: Path, returncode: int
) -> None:
    payload = b"begin\n" + b"command output\n" * 1200 + b"end\n"
    async with _command_case(
        tmp_path, "printf output", stdout=payload, returncode=returncode
    ) as case:
        await case.approve()
        completed = await case.resume()
        assert completed.status is TurnStatus.COMPLETED
        result = next(
            item.content for item in completed.items if isinstance(item.content, ToolResultContent)
        )
        assert result.outcome == ("succeeded" if returncode == 0 else "failed")
        if returncode:
            assert result.error is not None and result.error.code == "process_nonzero_exit"
        assert isinstance(result.output, dict)
        assert result.output["version"] == "trusted-process-output/v3"
        assert result.output["profile"] == "workspace-command"
        assert result.output["process_id"] == str(case.approval.plan_id)
        reference = cast(dict[str, Any], result.output["artifact"])
        offset, pages = 0, []
        while True:
            page = await case.artifacts.read(
                case.thread_id,
                case.workspace_scope,
                reference["artifact_id"],
                offset=offset,
                limit=200,
            )
            pages.append(page.text.encode())
            if page.next_offset is None:
                break
            assert page.next_offset > offset
            offset = page.next_offset
        document = parse_trusted_process_output(b"".join(pages))
        assert document.summary.complete
        assert document.summary.stdout.archived_bytes == len(payload)
        assert b"".join(chunk.data() for chunk in document.chunks) == payload
        events = case.router.events(case.approval.plan_id)
        assert await case.resume() == completed
        assert case.router.events(case.approval.plan_id) == events
        assert case.runtime.run_calls == 1 and case.runtime.reconcile_calls == 0


@pytest.mark.skipif(os.name != "posix", reason="命令Profile使用POSIX Process Owner夹具")
async def test_cancelled_command_reconciles_without_replaying_command(tmp_path: Path) -> None:
    async with _command_case(tmp_path, "sleep 30", cancellable=True) as case:
        await case.approve()
        execution = asyncio.create_task(case.router.execute(case.approval.plan_id))
        await asyncio.wait_for(cast(_CancellableRuntime, case.runtime).started.wait(), timeout=1)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert case.router.status(case.approval.plan_id).state == "unknown"
        recovered = await case.router.reconcile(case.approval.plan_id)
        assert recovered.kind == "failed" and recovered.error_code == "process_cancelled"
        completed = await case.resume()
        assert completed.status is TurnStatus.COMPLETED
        result = next(
            item.content for item in completed.items if isinstance(item.content, ToolResultContent)
        )
        assert result.outcome == "failed" and result.error.code == "process_cancelled"
        assert isinstance(result.output, dict) and "artifact" in result.output
        assert case.runtime.run_calls == 1 and case.runtime.reconcile_calls == 1


def test_command_profile_workspace_path_is_opt_in_and_advertised(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path)
    legacy = _command_profile(engine)
    assert "workspace_path" not in legacy.model_dump(mode="json")
    assert "挂载在容器内" not in process_profile_descriptor(legacy).description
    profile = _command_profile(engine, workspace_path="/testbed")
    assert profile.model_dump(mode="json")["workspace_path"] == "/testbed"
    assert profile.profile_sha256 != legacy.profile_sha256
    assert "工作区源码挂载在容器内/testbed" in process_profile_descriptor(profile).description
    for unsafe in ("relative", "/tmp/work", "/harnessix", "/a/../b"):
        with pytest.raises(ValidationError):
            _command_profile(engine, workspace_path=unsafe)


async def test_command_profile_workspace_path_reaches_the_sandbox_profile(tmp_path: Path) -> None:
    profile = _command_profile(_fake_engine(tmp_path), workspace_path="/testbed")
    async with PosixProcessSupervisor(tmp_path / "probe-owner") as supervisor:
        probe = probe_product_process_profile(
            profile, supervisor, _NoSecrets(), probe_runner=_probe_runner
        )
    assert probe.verified is not None
    assert probe.verified.sandbox_profile.workspace_path == "/testbed"
    assert probe.verified.sandbox.profile_digest == probe.verified.sandbox_profile.digest


async def test_snapshot_profile_with_presets_reaches_the_sandbox_and_needs_a_root(
    tmp_path: Path,
) -> None:
    presets = (("HOME", "/tmp"), ("GOFLAGS", "-mod=mod"))
    profile = _command_profile(
        _fake_engine(tmp_path), workspace_mode="snapshot", environment=presets
    )
    assert profile.environment == (("GOFLAGS", "-mod=mod"), ("HOME", "/tmp"))  # 规范排序
    description = process_profile_descriptor(profile).description
    assert "一次性可写快照" in description and "预设环境变量：GOFLAGS、HOME" in description
    legacy = _command_profile(_fake_engine(tmp_path))
    assert "workspace_mode" not in legacy.model_dump(mode="json")
    assert "environment" not in legacy.model_dump(mode="json")

    async with PosixProcessSupervisor(tmp_path / "probe-owner") as supervisor:
        missing = probe_product_process_profile(
            profile, supervisor, _NoSecrets(), probe_runner=_probe_runner
        )
        probe = probe_product_process_profile(
            profile,
            supervisor,
            _NoSecrets(),
            probe_runner=_probe_runner,
            snapshot_root=tmp_path / "snapshots",
        )
    assert missing.verified is None and missing.reason_code == "snapshot_root_unavailable"
    assert probe.verified is not None
    sandbox = probe.verified.sandbox_profile
    assert sandbox.workspace_mode == "snapshot" and sandbox.environment == profile.environment


def test_profile_environment_presets_are_validated(tmp_path: Path) -> None:
    for presets in ((("HTTPS_PROXY", "x"),), (("HOME", "a\nb"),), (("bad name", "x"),)):
        with pytest.raises(ValidationError):
            _command_profile(_fake_engine(tmp_path), environment=presets)
