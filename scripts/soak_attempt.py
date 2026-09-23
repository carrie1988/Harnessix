"""记录Soak尝试的开始与终态；硬退出保留不可判PASS的未完成事实。"""

from __future__ import annotations

import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from harnessix.agent.errors import KernelError
from harnessix.domain.models import ContractModel
from scripts.soak_evidence import (
    _ensure_private_root,
    _read_file,
    _sync_directory,
    _write_file,
    read_published_run,
)
from scripts.soak_manifest import SoakManifest
from scripts.soak_samples import ScenarioId

STARTED_FILENAME = "STARTED.json"
FINAL_FILENAME = "FINAL.json"
MAX_ATTEMPT_BYTES = 2048
AttemptPhase = Literal["prepared", "warming", "measuring", "reconciling", "publishing"]


class SoakAttemptStart(ContractModel):
    """只保存一次Run的低敏身份和启动时间。"""

    spec_version: Literal["harnessix.soak-attempt-start/v1"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    code_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    scenario_id: ScenarioId
    started_at: datetime

    @model_validator(mode="after")
    def utc_time(self) -> Self:
        if self.started_at.utcoffset() != UTC.utcoffset(None):
            raise ValueError("Attempt启动时间必须为UTC")
        return self


class SoakAttemptFinal(ContractModel):
    """明确失败或成功提交的独占终态，不收集异常正文。"""

    spec_version: Literal["harnessix.soak-attempt-final/v1"]
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    outcome: Literal["failed", "committed"]
    phase: AttemptPhase
    finished_at: datetime
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def terminal_fields(self) -> Self:
        if self.finished_at.utcoffset() != UTC.utcoffset(None):
            raise ValueError("Attempt终态时间必须为UTC")
        if (self.outcome == "committed") != (self.manifest_sha256 is not None):
            raise ValueError("Attempt终态与Manifest摘要不匹配")
        if self.outcome == "committed" and self.phase != "publishing":
            raise ValueError("已提交Attempt必须完成发布阶段")
        return self


def _canonical(value: ContractModel) -> bytes:
    return (value.model_dump_json() + "\n").encode("utf-8")


def begin_attempt(
    evidence_root: Path, *, run_id: str, code_revision: str, scenario_id: ScenarioId
) -> Path:
    """排他创建Attempt目录；STARTED落盘后才允许真实负载。"""

    started = SoakAttemptStart(
        spec_version="harnessix.soak-attempt-start/v1",
        run_id=run_id,
        code_revision=code_revision,
        scenario_id=scenario_id,
        started_at=datetime.now(UTC),
    )
    _ensure_private_root(evidence_root)
    attempts_root = evidence_root / "attempts"
    _ensure_private_root(attempts_root)
    attempt_directory = attempts_root / run_id
    try:
        attempt_directory.mkdir(mode=0o700, exist_ok=False)
        _sync_directory(attempts_root)
    except OSError:
        raise KernelError("soak_attempt_exists", "Soak Attempt目录已存在或不可创建") from None
    _write_file(attempt_directory, STARTED_FILENAME, _canonical(started), MAX_ATTEMPT_BYTES)
    return attempt_directory


def _started(attempt_directory: Path) -> SoakAttemptStart:
    try:
        body = _read_file(attempt_directory, STARTED_FILENAME, MAX_ATTEMPT_BYTES)
        started = SoakAttemptStart.model_validate_json(body)
        if body != _canonical(started) or started.run_id != attempt_directory.name:
            raise ValueError
    except (KernelError, ValueError):
        raise KernelError("soak_attempt_invalid", "Soak Attempt启动证据无效") from None
    return started


def _published_manifest(attempt_directory: Path) -> tuple[SoakManifest, str] | None:
    run_directory = attempt_directory.parent.parent / attempt_directory.name
    try:
        return read_published_run(run_directory)
    except KernelError:
        return None


def finish_attempt(
    attempt_directory: Path,
    *,
    outcome: Literal["failed", "committed"],
    phase: AttemptPhase,
    manifest_sha256: str | None = None,
) -> None:
    """终态单写；已提交Run必须能由磁盘独立重读。"""

    started = _started(attempt_directory)
    published = _published_manifest(attempt_directory)
    if outcome == "committed":
        if published is None:
            raise KernelError("soak_attempt_invalid", "已提交Attempt缺少有效Run")
        manifest, digest = published
        if (
            digest != manifest_sha256
            or manifest.run_id != started.run_id
            or manifest.scenario_id != started.scenario_id
            or manifest.code_revision != started.code_revision
        ):
            raise KernelError("soak_attempt_invalid", "Attempt与已提交Run不一致")
    elif published is not None:
        raise KernelError("soak_attempt_invalid", "已发布Run不能标记为失败")
    final = SoakAttemptFinal(
        spec_version="harnessix.soak-attempt-final/v1",
        run_id=started.run_id,
        outcome=outcome,
        phase=phase,
        finished_at=datetime.now(UTC),
        manifest_sha256=manifest_sha256,
    )
    _write_file(attempt_directory, FINAL_FILENAME, _canonical(final), MAX_ATTEMPT_BYTES)


def read_attempt(attempt_directory: Path) -> tuple[SoakAttemptStart, SoakAttemptFinal | None]:
    """严格读取；只有STARTED表示硬退出或终态提交窗口中断。"""

    try:
        if not stat.S_ISDIR(attempt_directory.stat(follow_symlinks=False).st_mode):
            raise OSError
        files = {path.name for path in attempt_directory.iterdir()}
        if files not in ({STARTED_FILENAME}, {STARTED_FILENAME, FINAL_FILENAME}):
            raise ValueError
        started = _started(attempt_directory)
        if FINAL_FILENAME not in files:
            return started, None
        body = _read_file(attempt_directory, FINAL_FILENAME, MAX_ATTEMPT_BYTES)
        final = SoakAttemptFinal.model_validate_json(body)
        published = _published_manifest(attempt_directory)
        if (
            body != _canonical(final)
            or final.run_id != started.run_id
            or final.finished_at < started.started_at
            or (
                final.outcome == "committed"
                and (published is None or published[1] != final.manifest_sha256)
            )
            or (
                final.outcome == "committed"
                and published is not None
                and (
                    published[0].scenario_id != started.scenario_id
                    or published[0].code_revision != started.code_revision
                )
            )
            or (final.outcome == "failed" and published is not None)
        ):
            raise ValueError
    except (OSError, ValueError, KernelError):
        raise KernelError("soak_attempt_invalid", "Soak Attempt证据无效") from None
    return started, final


class AttemptHandle:
    """只持有当前Run的Attempt目录及低敏阶段，不接触业务状态。"""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.phase: AttemptPhase = "prepared"
        self._committed = False

    def commit(self, run_directory: Path) -> None:
        manifest, digest = read_published_run(run_directory)
        if run_directory.name != self.directory.name or manifest.run_id != self.directory.name:
            raise KernelError("soak_attempt_invalid", "Attempt与Run身份不匹配")
        self.phase = "publishing"
        finish_attempt(
            self.directory,
            outcome="committed",
            phase=self.phase,
            manifest_sha256=digest,
        )
        self._committed = True


@contextmanager
def attempt_scope(
    evidence_root: Path, *, run_id: str, code_revision: str, scenario_id: ScenarioId
) -> Iterator[AttemptHandle]:
    """任意异常保留失败事实；硬退出至少保留STARTED。"""

    handle = AttemptHandle(
        begin_attempt(
            evidence_root,
            run_id=run_id,
            code_revision=code_revision,
            scenario_id=scenario_id,
        )
    )
    try:
        yield handle
    except BaseException:
        if not handle._committed and _published_manifest(handle.directory) is None:
            finish_attempt(handle.directory, outcome="failed", phase=handle.phase)
        raise
    else:
        if not handle._committed:
            if _published_manifest(handle.directory) is None:
                finish_attempt(handle.directory, outcome="failed", phase=handle.phase)
            raise KernelError("soak_attempt_uncommitted", "Soak Attempt未提交Run")
