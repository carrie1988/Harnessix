from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path = Path(".harnessix/harnessix.db")
    database_url: str | None = None
    demo_database_path: Path = Path(".harnessix/demo-external.db")
    host: str = "127.0.0.1"
    port: int = 8787
    lease_seconds: int = 30
    execution_mode: str = "inline"
    worker_poll_seconds: float = 0.5
    worker_heartbeat_seconds: float = 10.0
    recovery_interval_seconds: float = 5.0
    service_name: str = "harnessix"
    log_level: str = "INFO"
    log_format: str = "json"
    otel_endpoint: str | None = None
    otel_export_interval_millis: int = 10_000

    def __post_init__(self) -> None:
        if self.execution_mode not in {"inline", "queued"}:
            raise ValueError("execution_mode 只能是 inline 或 queued")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        if (
            self.worker_poll_seconds <= 0
            or self.worker_heartbeat_seconds <= 0
            or self.recovery_interval_seconds <= 0
        ):
            raise ValueError("Worker 时间间隔必须大于 0")
        if self.worker_heartbeat_seconds >= self.lease_seconds:
            raise ValueError("worker_heartbeat_seconds 必须小于 lease_seconds")
        if not self.service_name:
            raise ValueError("service_name 不能为空")
        if self.log_format not in {"json", "console"}:
            raise ValueError("log_format 只能是 json 或 console")
        if self.otel_export_interval_millis <= 0:
            raise ValueError("otel_export_interval_millis 必须大于 0")

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            database_path=Path(os.getenv("HARNESSIX_DATABASE_PATH", ".harnessix/harnessix.db")),
            database_url=os.getenv("HARNESSIX_DATABASE_URL") or None,
            demo_database_path=Path(
                os.getenv("HARNESSIX_DEMO_DATABASE_PATH", ".harnessix/demo-external.db")
            ),
            host=os.getenv("HARNESSIX_HOST", "127.0.0.1"),
            port=int(os.getenv("HARNESSIX_PORT", "8787")),
            lease_seconds=int(os.getenv("HARNESSIX_LEASE_SECONDS", "30")),
            execution_mode=os.getenv("HARNESSIX_EXECUTION_MODE", "inline"),
            worker_poll_seconds=float(os.getenv("HARNESSIX_WORKER_POLL_SECONDS", "0.5")),
            worker_heartbeat_seconds=float(os.getenv("HARNESSIX_WORKER_HEARTBEAT_SECONDS", "10")),
            recovery_interval_seconds=float(os.getenv("HARNESSIX_RECOVERY_INTERVAL_SECONDS", "5")),
            service_name=os.getenv("HARNESSIX_SERVICE_NAME", "harnessix"),
            log_level=os.getenv("HARNESSIX_LOG_LEVEL", "INFO"),
            log_format=os.getenv("HARNESSIX_LOG_FORMAT", "json"),
            otel_endpoint=(
                os.getenv("HARNESSIX_OTEL_ENDPOINT")
                or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
                or None
            ),
            otel_export_interval_millis=int(
                os.getenv("HARNESSIX_OTEL_EXPORT_INTERVAL_MILLIS", "10000")
            ),
        )
