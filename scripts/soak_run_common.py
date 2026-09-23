"""Soak场景共用的低敏采样、正式Revision核验和Run发布。"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from harnessix.agent.errors import KernelError
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
    SoakProviderEvidence,
    SoakRssEvidence,
)
from scripts.soak_provider import SoakProvider
from scripts.soak_rss import RssObservation
from scripts.soak_sample_file import SAMPLE_FILENAME, sample_sha256
from scripts.soak_samples import ScenarioId, SoakSample, validate_sample_series


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
    provider: SoakProvider,
    rss: RssObservation,
    file_watermarks: SoakFileWatermarks,
    baseline: bool,
    context_proof: SoakContextProof | None = None,
    summary_request_count: int | None = None,
) -> tuple[Path, SoakManifest | SoakManifestV2]:
    """按固定白名单组装Manifest，发布后独立重读。"""

    sample_counts: dict[str, int] = {}
    for sample in samples:
        if sample.phase == "measure":
            sample_counts[sample.metric] = sample_counts.get(sample.metric, 0) + 1
    manifest = SoakManifest(
        spec_version="harnessix.soak-manifest/v1",
        run_id=run_id,
        code_revision=code_revision,
        scenario_version="harnessix.soak-scenario/v1",
        scenario_id=scenario_id,
        measurement_boundary=SCENARIO_BOUNDARY[scenario_id],
        seed=seed,
        provider=SoakProviderEvidence(
            mode="deterministic_stateless_v1",
            script_version=SoakProvider.SCRIPT_VERSION,
            request_count=provider.request_count,
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
        fault_counts=SoakFaultCounts(
            cancelled=0,
            timed_out=0,
            eof=0,
            unknown_effect=0,
            duplicate_effect=0,
            orphan=0,
        ),
        evidence_sha256={SAMPLE_FILENAME: sample_sha256(samples)},
        threshold_profile_ref=None,
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
    run_directory, _ = publish_run(evidence_root, manifest, samples, context_proof=context_proof)
    restored, _ = read_published_run(run_directory)
    if restored != manifest:
        raise KernelError("soak_run_invalid", "Soak发布复核失败")
    return run_directory, manifest
