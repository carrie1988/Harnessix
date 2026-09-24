"""Soak场景共用的低敏采样、正式Revision核验和Run发布。"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from harnessix.agent.errors import KernelError
from scripts.soak_action_proof import ACTION_PROOF_FILENAME, SoakActionProof
from scripts.soak_artifact_proof import ARTIFACT_PROOF_FILENAME, SoakArtifactProof
from scripts.soak_context_proof import CONTEXT_PROOF_FILENAME, SoakContextProof
from scripts.soak_environment import SoakEnvironment
from scripts.soak_evidence import publish_run, read_published_run
from scripts.soak_manifest import (
    SCENARIO_BOUNDARY,
    SoakFaultCounts,
    SoakFileWatermarks,
    SoakLoad,
    SoakManifest,
    SoakManifestV2,
    SoakManifestV3,
    SoakManifestV4,
    SoakManifestV5,
    SoakManifestV6,
    SoakManifestV7,
    SoakProfileReference,
    SoakProviderEvidence,
    SoakRssEvidence,
)
from scripts.soak_provider import SoakProvider
from scripts.soak_restart_proof import RESTART_PROOF_FILENAME, SoakRestartProof
from scripts.soak_rss import RssObservation
from scripts.soak_sample_file import SAMPLE_FILENAME, sample_sha256
from scripts.soak_samples import ScenarioId, SoakSample, validate_sample_series
from scripts.soak_sdk_proof import SDK_PROOF_FILENAME, SoakSdkProof
from scripts.soak_thread_proof import THREAD_PROOF_FILENAME, SoakThreadProof


def file_bytes(path: Path) -> int:
    """缺失的WAL记0；其他文件仍由场景复核业务事实。"""

    return path.stat().st_size if path.exists() else 0


def check_release_revision(code_revision: str) -> None:
    """正式基线只允许绑定当前干净Git Revision，不能伪造来源。"""

    repository = Path(__file__).resolve().parents[1]
    try:
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=normal"),
            cwd=repository,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        raise KernelError("soak_revision_invalid", "正式Soak无法核对源码Revision") from None
    if head != code_revision or dirty:
        raise KernelError("soak_revision_invalid", "正式Soak源码Revision不一致或工作区未清洁")


def latency_sample(
    run_id: str,
    scenario_id: ScenarioId,
    index: int,
    phase: str,
    metric: str,
    elapsed_ns: int,
) -> SoakSample:
    """用统一单调时钟合同记录时延，不接收业务正文。"""

    return SoakSample.model_validate(
        {
            "spec_version": "harnessix.soak-sample/v1",
            "run_id": run_id,
            "scenario_id": scenario_id,
            "sample_index": index,
            "phase": phase,
            "metric": metric,
            "value": elapsed_ns,
            "unit": "ns",
            "clock": "monotonic_ns",
        }
    )


def rss_sample(run_id: str, scenario_id: ScenarioId, index: int, rss: RssObservation) -> SoakSample:
    """保留峰值原始单位及归一化依据。"""

    return SoakSample(
        spec_version="harnessix.soak-sample/v1",
        run_id=run_id,
        scenario_id=scenario_id,
        sample_index=index,
        phase="measure",
        metric="rss_peak",
        value=rss.rss_bytes,
        unit="bytes",
        rss_source=rss.source,
        rss_raw_unit=rss.raw_unit,
        rss_normalization=rss.normalization,
        rss_raw_value=rss.raw_value,
        rss_bytes=rss.rss_bytes,
    )


def publish_measured_run(
    evidence_root: Path,
    *,
    run_id: str,
    code_revision: str,
    scenario_id: ScenarioId,
    seed: int,
    environment: SoakEnvironment,
    started_at: datetime,
    load: SoakLoad,
    samples: tuple[SoakSample, ...],
    provider: SoakProvider | None,
    restart_proof: SoakRestartProof | None = None,
    thread_proof: SoakThreadProof | None = None,
    action_proof: SoakActionProof | None = None,
    rss: RssObservation,
    file_watermarks: SoakFileWatermarks,
    baseline: bool,
    threshold_profile_ref: SoakProfileReference | None = None,
    context_proof: SoakContextProof | None = None,
    artifact_proof: SoakArtifactProof | None = None,
    sdk_proof: SoakSdkProof | None = None,
    fault_counts: SoakFaultCounts | None = None,
    summary_request_count: int | None = None,
) -> tuple[
    Path,
    SoakManifest
    | SoakManifestV2
    | SoakManifestV3
    | SoakManifestV4
    | SoakManifestV5
    | SoakManifestV6
    | SoakManifestV7,
]:
    """按固定白名单组装Manifest，发布后独立重读。"""

    sample_counts: dict[str, int] = {}
    for sample in samples:
        if sample.phase == "measure":
            sample_counts[sample.metric] = sample_counts.get(sample.metric, 0) + 1
    proofs = (context_proof, artifact_proof, sdk_proof, restart_proof, thread_proof, action_proof)
    if sum(proof is not None for proof in proofs) > 1:
        raise KernelError("soak_evidence_invalid", "同一Run不能混用场景证明")
    if restart_proof is not None and (
        scenario_id != "restart" or summary_request_count is not None
    ):
        raise KernelError("soak_restart_proof_invalid", "重启证明与场景不匹配")
    if (restart_proof is None) == (provider is None):
        raise KernelError("soak_evidence_invalid", "模型场景与无Turn产品场景的Provider不匹配")
    if artifact_proof is not None and (
        scenario_id != "artifact_growth" or summary_request_count is not None
    ):
        raise KernelError("soak_artifact_proof_invalid", "Artifact证明与场景不匹配")
    if sdk_proof is not None and (
        scenario_id != "sdk_capacity" or summary_request_count is not None
    ):
        raise KernelError("soak_sdk_proof_invalid", "SDK证明与场景不匹配")
    if thread_proof is not None and (
        scenario_id != "many_threads" or summary_request_count is not None
    ):
        raise KernelError("soak_thread_proof_invalid", "多Thread证明与场景不匹配")
    if action_proof is not None and (
        scenario_id != "action_recovery" or summary_request_count is not None
    ):
        raise KernelError("soak_action_proof_invalid", "Action证明与场景不匹配")
    manifest_data: dict[str, object] = dict(
        spec_version="harnessix.soak-manifest/v1",
        run_id=run_id,
        code_revision=code_revision,
        scenario_version="harnessix.soak-scenario/v1",
        scenario_id=scenario_id,
        measurement_boundary=SCENARIO_BOUNDARY[scenario_id],
        seed=seed,
        provider=(
            SoakProviderEvidence(
                mode="product_no_turn_v1",
                script_version="harnessix.product-no-turn/v1",
                request_count=0,
            )
            if restart_proof is not None
            else SoakProviderEvidence(
                mode="deterministic_stateless_v1",
                script_version=SoakProvider.SCRIPT_VERSION,
                request_count=provider.request_count if provider is not None else 0,
            )
        ),
        platform=environment.platform,
        python_version=environment.python_version,
        cpu_count=environment.cpu_count,
        physical_memory_bytes=environment.physical_memory_bytes,
        hardware_class=environment.hardware_class,
        started_at=started_at,
        ended_at=datetime.now(UTC),
        status="baseline" if baseline else "unverified",
        load=load,
        sample_counts=sample_counts,
        quantile_method="nearest_rank_v1",
        statistics=validate_sample_series(
            samples,
            run_id=run_id,
            scenario_id=scenario_id,
            expected_measured=sample_counts,
        ),
        rss=SoakRssEvidence(
            source=rss.source,
            raw_unit=rss.raw_unit,
            normalization=rss.normalization,
            peak_bytes=rss.rss_bytes,
            unit_verified=rss.unit_verified,
        ),
        file_watermarks=file_watermarks,
        fault_counts=fault_counts
        or SoakFaultCounts(
            cancelled=0,
            timed_out=0,
            eof=0,
            unknown_effect=0,
            duplicate_effect=0,
            orphan=0,
        ),
        evidence_sha256={SAMPLE_FILENAME: sample_sha256(samples)},
        threshold_profile_ref=threshold_profile_ref,
    )
    manifest: (
        SoakManifest
        | SoakManifestV2
        | SoakManifestV3
        | SoakManifestV4
        | SoakManifestV5
        | SoakManifestV6
        | SoakManifestV7
    )
    if (
        artifact_proof is None
        and sdk_proof is None
        and restart_proof is None
        and thread_proof is None
        and action_proof is None
    ):
        manifest = SoakManifest.model_validate(manifest_data)
    elif action_proof is not None:
        proof_digest = sha256((action_proof.model_dump_json() + "\n").encode()).hexdigest()
        manifest = SoakManifestV7.model_validate(
            {
                **manifest_data,
                "spec_version": "harnessix.soak-manifest/v7",
                "scenario_version": "harnessix.soak-scenario/v7",
                "action_proof_sha256": proof_digest,
                "evidence_sha256": {
                    SAMPLE_FILENAME: sample_sha256(samples),
                    ACTION_PROOF_FILENAME: proof_digest,
                },
            }
        )
    elif thread_proof is not None:
        proof_digest = sha256((thread_proof.model_dump_json() + "\n").encode()).hexdigest()
        manifest = SoakManifestV6.model_validate(
            {
                **manifest_data,
                "spec_version": "harnessix.soak-manifest/v6",
                "scenario_version": "harnessix.soak-scenario/v6",
                "thread_proof_sha256": proof_digest,
                "evidence_sha256": {
                    SAMPLE_FILENAME: sample_sha256(samples),
                    THREAD_PROOF_FILENAME: proof_digest,
                },
            }
        )
    elif artifact_proof is not None:
        proof_digest = sha256((artifact_proof.model_dump_json() + "\n").encode()).hexdigest()
        manifest = SoakManifestV3.model_validate(
            {
                **manifest_data,
                "spec_version": "harnessix.soak-manifest/v3",
                "scenario_version": "harnessix.soak-scenario/v3",
                "artifact_proof_sha256": proof_digest,
                "evidence_sha256": {
                    SAMPLE_FILENAME: sample_sha256(samples),
                    ARTIFACT_PROOF_FILENAME: proof_digest,
                },
            }
        )
    elif sdk_proof is not None:
        assert sdk_proof is not None
        proof_digest = sha256((sdk_proof.model_dump_json() + "\n").encode()).hexdigest()
        manifest = SoakManifestV4.model_validate(
            {
                **manifest_data,
                "spec_version": "harnessix.soak-manifest/v4",
                "scenario_version": "harnessix.soak-scenario/v4",
                "sdk_proof_sha256": proof_digest,
                "evidence_sha256": {
                    SAMPLE_FILENAME: sample_sha256(samples),
                    SDK_PROOF_FILENAME: proof_digest,
                },
            }
        )
    else:
        assert restart_proof is not None
        proof_digest = sha256((restart_proof.model_dump_json() + "\n").encode()).hexdigest()
        manifest = SoakManifestV5.model_validate(
            {
                **manifest_data,
                "spec_version": "harnessix.soak-manifest/v5",
                "scenario_version": "harnessix.soak-scenario/v5",
                "restart_proof_sha256": proof_digest,
                "evidence_sha256": {
                    SAMPLE_FILENAME: sample_sha256(samples),
                    RESTART_PROOF_FILENAME: proof_digest,
                },
            }
        )
    if context_proof is not None:
        if summary_request_count is None:
            raise KernelError("soak_context_proof_invalid", "缺少摘要Provider请求计数")
        proof_digest = sha256((context_proof.model_dump_json() + "\n").encode()).hexdigest()
        manifest = SoakManifestV2.model_validate(
            {
                **manifest.model_dump(mode="json"),
                "spec_version": "harnessix.soak-manifest/v2",
                "scenario_version": "harnessix.soak-scenario/v2",
                "summary_request_count": summary_request_count,
                "context_proof_sha256": proof_digest,
                "evidence_sha256": {
                    SAMPLE_FILENAME: manifest.evidence_sha256[SAMPLE_FILENAME],
                    CONTEXT_PROOF_FILENAME: proof_digest,
                },
            }
        )
    elif summary_request_count is not None:
        raise KernelError("soak_context_proof_invalid", "v1 Run不得包含摘要Provider请求计数")
    run_directory, _ = publish_run(
        evidence_root,
        manifest,
        samples,
        context_proof=context_proof,
        artifact_proof=artifact_proof,
        sdk_proof=sdk_proof,
        restart_proof=restart_proof,
        thread_proof=thread_proof,
        action_proof=action_proof,
    )
    restored, _ = read_published_run(run_directory)
    if restored != manifest:
        raise KernelError("soak_run_invalid", "Soak发布复核失败")
    return run_directory, manifest
