from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from harnessix.domain.models import ActionSnapshot, utc_now
from harnessix.observability import SpanKind, bind_log_context, trace_log_fields
from harnessix.runtime import ActionService

logger = logging.getLogger(__name__)


class WorkerLeaseLostError(RuntimeError):
    """Worker 在 Action 执行完成前失去租约。"""


class ActionWorker:
    def __init__(
        self,
        service: ActionService,
        *,
        poll_seconds: float = 0.5,
        heartbeat_seconds: float = 10.0,
        recovery_interval_seconds: float = 5.0,
    ) -> None:
        if poll_seconds <= 0 or heartbeat_seconds <= 0 or recovery_interval_seconds <= 0:
            raise ValueError("Worker 时间间隔必须大于 0")
        if heartbeat_seconds >= service.lease_seconds:
            raise ValueError("heartbeat_seconds 必须小于 lease_seconds")
        self.service = service
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.recovery_interval_seconds = recovery_interval_seconds

    async def run_once(self) -> ActionSnapshot | None:
        snapshot = await self.service.journal.claim_next_ready(
            worker_id=self.service.worker_id,
            lease_expires_at=utc_now() + timedelta(seconds=self.service.lease_seconds),
        )
        if snapshot is None:
            return None
        self.service.observability.increment(
            "harnessix.worker.claims", attributes={"tool": snapshot.request.tool}
        )
        with self.service.observability.span(
            "harnessix.worker.consume",
            kind=SpanKind.CONSUMER,
            trace_context=snapshot.trace_context,
            attributes={"tool": snapshot.request.tool},
        ):
            trace_context = self.service.observability.current_trace_context()
            log_fields = trace_log_fields(
                trace_context.traceparent if trace_context is not None else None
            )
            with bind_log_context(
                action_id=snapshot.request.action_id,
                tenant_id=snapshot.request.principal.tenant_id,
                tool=snapshot.request.tool,
                worker_id=self.service.worker_id,
                **log_fields,
            ):
                completed = await self._execute_with_heartbeat(snapshot)
        await self.record_operational_metrics()
        return completed

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        loop = asyncio.get_running_loop()
        next_recovery_at = 0.0
        while not stop.is_set():
            try:
                now = loop.time()
                if now >= next_recovery_at:
                    recovered = await self.service.journal.recover_expired()
                    if recovered:
                        self.service.observability.increment(
                            "harnessix.lease.recoveries", len(recovered)
                        )
                        logger.info("已恢复 %d 个过期租约", len(recovered))
                    await self.record_operational_metrics()
                    next_recovery_at = now + self.recovery_interval_seconds

                snapshot = await self.run_once()
                if snapshot is None:
                    await self._wait_or_stop(stop, self.poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker 执行循环发生异常")
                await self._wait_or_stop(stop, self.poll_seconds)

    async def _execute_with_heartbeat(self, snapshot: ActionSnapshot) -> ActionSnapshot:
        execution = asyncio.create_task(
            self.service.execute_leased(snapshot),
            name=f"harnessix-action-{snapshot.request.action_id}",
        )
        try:
            while True:
                done, _ = await asyncio.wait({execution}, timeout=self.heartbeat_seconds)
                if execution in done:
                    return execution.result()

                renewed = await self.service.journal.renew_lease(
                    snapshot.request.action_id,
                    worker_id=self.service.worker_id,
                    lease_expires_at=utc_now() + timedelta(seconds=self.service.lease_seconds),
                )
                if not renewed:
                    self.service.observability.increment(
                        "harnessix.worker.lease_renewal_failures",
                        attributes={"tool": snapshot.request.tool},
                    )
                    if execution.done():
                        return execution.result()
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                    if not execution.cancelled():
                        return execution.result()
                    raise WorkerLeaseLostError(
                        f"Action {snapshot.request.action_id} 的执行租约已经丢失"
                    )
        except BaseException:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            raise

    async def record_operational_metrics(self) -> None:
        try:
            stats = await self.service.journal.operational_stats()
            self.service.observability.set_gauge("harnessix.queue.ready", stats.ready_count)
            self.service.observability.set_gauge(
                "harnessix.actions.pending_approval", stats.pending_approval_count
            )
            self.service.observability.set_gauge("harnessix.actions.unknown", stats.unknown_count)
            oldest_age = 0.0
            if stats.oldest_ready_at is not None:
                oldest_age = max(0.0, (utc_now() - stats.oldest_ready_at).total_seconds())
            self.service.observability.set_gauge("harnessix.queue.oldest_ready_age", oldest_age)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("采集队列运行指标失败")

    @staticmethod
    async def _wait_or_stop(stop: asyncio.Event, delay: float) -> bool:
        try:
            async with asyncio.timeout(delay):
                await stop.wait()
        except TimeoutError:
            return False
        return True
