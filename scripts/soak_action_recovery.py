"""以固定故障矩阵驱动真实Trusted Action主链并测量恢复扫描时延的Soak。"""

from __future__ import annotations

import asyncio
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from harnessix.agent.errors import KernelError
from harnessix.agent.ids import new_id
from harnessix.agent.models import EventDraft, ThreadCreated
from harnessix.artifacts.sqlite import SQLiteArtifactStore
from harnessix.domain.models import ApprovalDecision, ApprovalOutcome, EffectClass, RiskLevel
from harnessix.execution.contracts import canonical_digest
from harnessix.execution.store import SQLiteExecutionPlanStore
from harnessix.product_config.action_recovery import scan_product_action_recovery
from harnessix.session.sqlite import SQLiteSessionStore
from harnessix.trusted_actions.contracts import (
    ActionExecutionOutcome,
    CodingActionInvocation,
    TrustedToolBinding,
    build_trusted_tool_binding,
)
from harnessix.trusted_actions.router import (
    ActionPlanningContext,
    ResolvedAction,
    TrustedActionDefinition,
    TrustedActionRouter,
    canonical_action_resource,
)
from harnessix.trusted_actions.store import SQLiteActionAuditStore
from harnessix.workspace.contracts import WorkspaceResourceRequest
from scripts.soak_action_proof import SoakActionCycle, SoakActionFault, SoakActionProof
from scripts.soak_attempt import attempt_scope
from scripts.soak_environment import read_environment
from scripts.soak_manifest import (
    SoakFaultCounts,
    SoakFileWatermarks,
    SoakLoad,
    SoakManifestV7,
    SoakProfileReference,
)
from scripts.soak_provider import SoakProvider
from scripts.soak_rss import read_peak_rss
from scripts.soak_run_common import (
    check_release_revision,
    file_bytes,
    latency_sample,
    publish_measured_run,
    rss_sample,
)
from scripts.soak_samples import SoakSample

FAULT_MATRIX_VERSION = "action-recovery-v1"
CRASH_EXIT_CODE = 73
_MARKER_BODY = b"effect\n"
_UNKNOWN_TOOL = "soak_action.unknown_write"
_CRASH_TOOL = "soak_action.crash_write"
_CHILD = Path(__file__).with_name("soak_action_child.py")


class SoakActionInput(BaseModel):
    """受控故障Executor的唯一参数；Schema摘要在两个进程间保持稳定。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str = Field(min_length=1, max_length=256)


@dataclass
class UnknownOutcomeExecutor:
    """模拟效果已发生但返回丢失：只报告UNKNOWN，对账观察既成事实。"""

    calls: int = 0
    reconciliations: int = 0

    async def execute(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        self.calls += 1
        SoakActionInput.model_validate(arguments)
        external = getattr(plan, "external_action_id", None)
        return ActionExecutionOutcome(
            kind="unknown",
            external_action_id=external,
            error_code="write_effect_response_lost",
        )

    async def reconcile(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        self.reconciliations += 1
        SoakActionInput.model_validate(arguments)
        return ActionExecutionOutcome(kind="succeeded", output={"reconciled": True})


@dataclass
class CrashWriteExecutor:
    """崩溃子进程内的一次性效果写入；写入后立即硬退出，不结算Operation。"""

    marker: Path

    async def execute(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        SoakActionInput.model_validate(arguments)
        import os

        try:
            with self.marker.open("xb") as stream:
                stream.write(_MARKER_BODY)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            os._exit(74)
        os._exit(CRASH_EXIT_CODE)
        raise AssertionError("unreachable")

    async def reconcile(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        raise AssertionError("崩溃子进程不执行对账")


@dataclass
class CrashReconcileExecutor:
    """主进程只对账执行器：观察崩溃子进程留下的一次性效果标记。"""

    marker: Path | None = None
    reconciliations: int = 0

    async def execute(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        raise AssertionError("主进程不重放崩溃Route的效果")

    async def reconcile(self, plan: object, arguments: BaseModel) -> ActionExecutionOutcome:
        self.reconciliations += 1
        SoakActionInput.model_validate(arguments)
        if (
            self.marker is not None
            and self.marker.is_file()
            and self.marker.read_bytes() == (_MARKER_BODY)
        ):
            return ActionExecutionOutcome(kind="succeeded", output={"reconciled": True})
        return ActionExecutionOutcome(kind="manual_intervention", error_code="effect_fact_missing")


def _binding(tool: str, executor_id: str) -> TrustedToolBinding:
    return build_trusted_tool_binding(
        source="builtin",
        source_id="harnessix.product",
        tool=tool,
        tool_version="1",
        tool_fingerprint=canonical_digest({"tool": tool}),
        input_schema_sha256=canonical_digest(SoakActionInput.model_json_schema()),
        effect_class=EffectClass.NON_IDEMPOTENT_WRITE,
        risk_level=RiskLevel.HIGH,
        recovery_mode="durable_ledger",
        executor_id=executor_id,
    )


def _definition(tool: TrustedToolBinding, executor: object) -> TrustedActionDefinition:
    def resolve(arguments: BaseModel, _: ActionPlanningContext) -> ResolvedAction:
        parsed = SoakActionInput.model_validate(arguments)
        return ResolvedAction(
            resources=(
                canonical_action_resource(
                    kind="workspace",
                    access="write",
                    identifier={"location": "workspace", "path": parsed.path},
                ),
            ),
            workspace_resources=(WorkspaceResourceRequest(path=parsed.path, access="write"),),
        )

    return TrustedActionDefinition(tool, SoakActionInput, resolve, executor)  # type: ignore[arg-type]


def unknown_definition(executor: UnknownOutcomeExecutor) -> TrustedActionDefinition:
    return _definition(_binding(_UNKNOWN_TOOL, "soak_action.unknown"), executor)


def crash_definition(executor: object) -> TrustedActionDefinition:
    return _definition(_binding(_CRASH_TOOL, "soak_action.crash"), executor)


def _planning_context(root: Path) -> ActionPlanningContext:
    from harnessix.execution.contracts import SandboxBindingV2
    from harnessix.execution.planner import build_capability_evidence_v2

    capabilities = build_capability_evidence_v2(
        platform="windows" if sys.platform.startswith("win") else "posix",
        provider="native",
        provider_version="1",
        sandbox_levels=("host_guarded",),
        network_modes=("none",),
        supports_pty=False,
        supports_background=False,
        supports_process_tree=True,
        provider_evidence_digest=canonical_digest("soak-native-provider"),
    )
    sandbox = SandboxBindingV2(
        level="host_guarded",
        backend="host",
        backend_version="1",
        network="none",
        capability_digest=capabilities.evidence_digest,
        profile_digest=canonical_digest("soak-profile"),
    )
    return ActionPlanningContext(
        workspace_root=root,
        sandbox=sandbox,
        capabilities=capabilities,
    )


def _invocation(tool_name: str, ordinal: int, kind: str) -> CodingActionInvocation:
    tool = _binding(tool_name, f"soak_action.{kind}")
    return CodingActionInvocation(
        invocation_id=uuid4(),
        source=tool.source,
        source_id=tool.source_id,
        tool=tool.tool,
        tool_version=tool.tool_version,
        tool_fingerprint=tool.tool_fingerprint,
        arguments={"path": "file.txt"},
        idempotency_key=f"soak-action-{kind}-{ordinal}",
    )


def _approval() -> ApprovalDecision:
    return ApprovalDecision(
        outcome=ApprovalOutcome.APPROVED,
        actor="soak-action-recovery",
        reason="fixed-fault-matrix",
    )


async def _insert_orphan_artifact(
    sessions: SQLiteSessionStore, thread_id: UUID, ordinal: int
) -> None:
    """构造Artifact引用窗口：目的为action_review但Session无对应调用。"""

    now = "2099-01-01T00:00:00+00:00"
    async with sessions._connection() as database:  # noqa: SLF001 - 与恢复测试相同的受控植入
        await database.execute(
            "INSERT INTO agent_artifacts "
            "(artifact_id, thread_id, turn_id, call_id, workspace_scope, manifest_json, "
            "size_bytes, expires_at, state, body, purpose, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, 'action_review', ?)",
            (
                str(uuid4()),
                str(thread_id),
                str(uuid4()),
                str(uuid4()),
                "0" * 64,
                "{}",
                1,
                now,
                b"x",
                now,
            ),
        )
        await database.commit()


def _state_bytes(root: Path) -> tuple[int, int]:
    names = ("plans.db", "audit.db", "sessions.db")
    db = sum(file_bytes(root / name) for name in names)
    wal = sum(file_bytes(root / f"{name}-wal") for name in names)
    return db, wal


async def run_action_recovery(
    evidence_root: Path,
    *,
    code_revision: str,
    cycle_count: int,
    warmup_count: int,
    seed: int = 0,
    scan_timeout_seconds: float = 30.0,
    threshold_profile_ref: SoakProfileReference | None = None,
) -> tuple[Path, SoakManifestV7]:
    """执行固定故障矩阵循环，逐轮计时恢复扫描并发布v7证据。"""

    if (
        not isinstance(evidence_root, Path)
        or not isinstance(code_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", code_revision) is None
        or type(cycle_count) is not int
        or not 1 <= cycle_count <= 100
        or type(warmup_count) is not int
        or not 0 <= warmup_count <= 4
        or type(seed) is not int
        or not 0 <= seed <= 100_000
        or type(scan_timeout_seconds) not in (int, float)
        or not math.isfinite(scan_timeout_seconds)
        or not 1 <= scan_timeout_seconds <= 120
        or (cycle_count >= 20 and warmup_count != 2)
        or (threshold_profile_ref is not None and cycle_count < 20)
    ):
        raise KernelError("soak_load_invalid", "Action恢复Soak负载参数无效")
    formal = cycle_count >= 20
    if formal:
        check_release_revision(code_revision)

    run_id = uuid4().hex
    total = warmup_count + cycle_count
    with attempt_scope(
        evidence_root,
        run_id=run_id,
        code_revision=code_revision,
        scenario_id="action_recovery",
        threshold_profile_ref=threshold_profile_ref,
    ) as attempt:
        environment = read_environment()
        started_at = datetime.now(UTC)
        provider = SoakProvider()
        samples: list[SoakSample] = []
        cycles: list[SoakActionCycle] = []
        attempt.phase = "warming"
        with TemporaryDirectory(prefix="harnessix-action-soak-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "file.txt").write_text("content", encoding="utf-8")
            fixture = root / "fixture"
            fixture.mkdir()
            sessions = SQLiteSessionStore(root / "sessions.db")
            await sessions.initialize()
            thread_id = new_id()
            await sessions.append(
                thread_id,
                (EventDraft(payload=ThreadCreated(workspace=str(workspace))),),
                expected_sequence=0,
            )
            artifacts = SQLiteArtifactStore(sessions)
            plans = SQLiteExecutionPlanStore(root / "plans.db")
            audit = SQLiteActionAuditStore(root / "audit.db")
            try:
                unknown_executor = UnknownOutcomeExecutor()
                crash_reconciler = CrashReconcileExecutor()
                router = TrustedActionRouter(
                    plans=plans,
                    audit=audit,
                    workspace_root=lambda _: workspace,
                )
                router.register(unknown_definition(unknown_executor))
                router.register(crash_definition(crash_reconciler))
                before_db, before_wal = _state_bytes(root)
                cumulative_repaired = 0
                for ordinal in range(1, total + 1):
                    phase = "warmup" if ordinal <= warmup_count else "measure"
                    attempt.phase = "warming" if phase == "warmup" else "measuring"
                    context = _planning_context(workspace)
                    unknown_route = router.plan(
                        _invocation(_UNKNOWN_TOOL, ordinal, "unknown"), context
                    )
                    unknown_plan_id = unknown_route.plan.execution.plan_id
                    router.decide(unknown_plan_id, _approval())
                    outcome = await router.execute(unknown_plan_id)
                    if outcome.kind != "unknown" or audit.load(unknown_plan_id).state != "unknown":
                        raise KernelError("soak_action_fault_invalid", "UNKNOWN故障未进入未知效果")
                    calls_before = unknown_executor.calls
                    reconciled = await router.reconcile(unknown_plan_id)
                    if (
                        reconciled.kind != "succeeded"
                        or unknown_executor.calls != calls_before
                        or audit.load(unknown_plan_id).state != "succeeded"
                    ):
                        raise KernelError("soak_action_fault_invalid", "UNKNOWN对账重放了效果")
                    crash_route = router.plan(_invocation(_CRASH_TOOL, ordinal, "crash"), context)
                    crash_plan_id = crash_route.plan.execution.plan_id
                    router.decide(crash_plan_id, _approval())
                    marker = fixture / f"marker-{ordinal}.bin"
                    completed = await asyncio.to_thread(
                        subprocess.run,
                        [
                            sys.executable,
                            str(_CHILD),
                            str(root / "plans.db"),
                            str(root / "audit.db"),
                            str(workspace),
                            str(crash_plan_id),
                            str(marker),
                        ],
                        capture_output=True,
                        timeout=120,
                    )
                    if completed.returncode != CRASH_EXIT_CODE or not marker.is_file():
                        raise KernelError("soak_action_crash_invalid", "受控崩溃子进程合同失败")
                    crash_reconciler.marker = marker
                    with audit.runtime_owner() as fence:
                        recovered = router.recover_interrupted()
                        if set(recovered) != {crash_plan_id}:
                            raise KernelError(
                                "soak_action_recovery_invalid", "宿主中断未收敛为唯一UNKNOWN"
                            )
                        outcome_crash = await router.reconcile(crash_plan_id)
                        if (
                            outcome_crash.kind != "succeeded"
                            or marker.read_bytes() != _MARKER_BODY
                            or audit.load(crash_plan_id).state != "succeeded"
                        ):
                            raise KernelError(
                                "soak_action_recovery_invalid", "崩溃Route对账未观察一次性效果"
                            )
                        plans._db.execute(  # noqa: SLF001 - 与恢复测试相同的受控崩溃窗口
                            "DELETE FROM execution_approvals WHERE plan_id = ?",
                            (str(unknown_plan_id),),
                        )
                        plans._db.execute(  # noqa: SLF001
                            "DELETE FROM execution_plans WHERE plan_id = ?",
                            (str(unknown_plan_id),),
                        )
                        plans._db.commit()  # noqa: SLF001
                        await _insert_orphan_artifact(sessions, thread_id, ordinal)
                        start = perf_counter_ns()
                        try:
                            async with asyncio.timeout(scan_timeout_seconds):
                                report = await scan_product_action_recovery(
                                    plans=plans,
                                    audit=audit,
                                    sessions=sessions,
                                    artifacts=artifacts,
                                    supervisor=None,
                                    fence=fence,
                                )
                        except TimeoutError:
                            raise KernelError("soak_action_timeout", "Action恢复扫描超时") from None
                        elapsed = perf_counter_ns() - start
                    cumulative_repaired += report.repaired_execution_plans
                    if (
                        report.scanned_routes != ordinal * 2
                        or cumulative_repaired != ordinal
                        or report.invalid_execution_plans != 0
                        or report.session_orphan_references != 0
                        or report.routes_without_session_reference != ordinal * 2
                        or report.artifact_orphans != ordinal
                        or report.process_orphan_leases != 0
                        or report.owner_generation != fence.generation
                    ):
                        raise KernelError(
                            "soak_action_recovery_invalid", "Action恢复扫描报告与轮次不一致"
                        )
                    samples.append(
                        latency_sample(
                            run_id,
                            "action_recovery",
                            len(samples) + 1,
                            phase,
                            "recovery_scan",
                            elapsed,
                        )
                    )
                    cycles.append(
                        SoakActionCycle(
                            ordinal=ordinal,
                            phase=phase,
                            faults=(
                                SoakActionFault(
                                    kind="unknown_outcome",
                                    execute_calls=1,
                                    reconcile_calls=1,
                                    terminal_state="succeeded",
                                ),
                                SoakActionFault(
                                    kind="host_crash",
                                    execute_calls=1,
                                    reconcile_calls=1,
                                    terminal_state="succeeded",
                                ),
                                SoakActionFault(
                                    kind="plan_orphan",
                                    execute_calls=0,
                                    reconcile_calls=0,
                                    terminal_state="none",
                                ),
                                SoakActionFault(
                                    kind="artifact_orphan",
                                    execute_calls=0,
                                    reconcile_calls=0,
                                    terminal_state="none",
                                ),
                            ),
                            owner_generation=fence.generation,
                            scanned_routes=report.scanned_routes,
                            repaired_execution_plans=cumulative_repaired,
                            artifact_orphans=report.artifact_orphans,
                            scan_report_sha256=report.report_sha256,
                            scan_sample_index=len(samples),
                        )
                    )

                attempt.phase = "reconciling"
                if (
                    unknown_executor.calls != total
                    or unknown_executor.reconciliations != total
                    or crash_reconciler.reconciliations != total
                ):
                    raise KernelError("soak_action_fault_invalid", "Action执行或对账计数漂移")
                after_db, after_wal = _state_bytes(root)
            finally:
                # Windows下失败路径也必须先释放SQLite句柄，临时目录才能清理。
                plans.close()
                audit.close()

        rss = read_peak_rss()
        samples.append(rss_sample(run_id, "action_recovery", len(samples) + 1, rss))
        proof = SoakActionProof(
            spec_version="harnessix.soak-action-proof/v1",
            run_id=run_id,
            cycles=tuple(cycles),
            unknown_resolved=total * 2,
            duplicate_effects=0,
            crash_exits=total,
        )
        attempt.phase = "publishing"
        run_directory, manifest = publish_measured_run(
            evidence_root,
            run_id=run_id,
            code_revision=code_revision,
            scenario_id="action_recovery",
            seed=seed,
            environment=environment,
            started_at=started_at,
            load=SoakLoad(
                turn_count=0,
                thread_count=0,
                artifact_count=0,
                warmup_count=warmup_count,
                fault_matrix_version=FAULT_MATRIX_VERSION,
            ),
            samples=tuple(samples),
            provider=provider,
            rss=rss,
            file_watermarks=SoakFileWatermarks(
                db_before_bytes=before_db,
                db_after_bytes=after_db,
                wal_before_bytes=before_wal,
                wal_after_bytes=after_wal,
                artifact_before_bytes=0,
                artifact_after_bytes=0,
            ),
            baseline=formal and threshold_profile_ref is None,
            threshold_profile_ref=threshold_profile_ref,
            action_proof=proof,
            fault_counts=SoakFaultCounts(
                cancelled=0,
                timed_out=0,
                eof=0,
                unknown_effect=total * 2,
                duplicate_effect=0,
                orphan=0,
            ),
        )
        if not isinstance(manifest, SoakManifestV7):
            raise KernelError("soak_run_invalid", "Action恢复Run证据版本无效")
        attempt.commit(run_directory)
        return run_directory, manifest
