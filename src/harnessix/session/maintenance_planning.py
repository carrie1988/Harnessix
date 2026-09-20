"""维护候选规划：构造禁删集合并按依赖顺序选择有界Item。"""

from __future__ import annotations

from collections import Counter

import aiosqlite

from harnessix.agent.errors import KernelError
from harnessix.session.capacity import ThreadFact, thread_has_uncertain_effect
from harnessix.session.maintenance_contracts import RetentionPolicy
from harnessix.session.maintenance_records import (
    PlanItem,
    artifact_precondition,
    parse_time,
    request_precondition,
    thread_precondition,
)


def build_candidates(
    policy: RetentionPolicy,
    *,
    facts: tuple[ThreadFact, ...],
    artifacts: list[aiosqlite.Row],
    requests: list[aiosqlite.Row],
) -> tuple[list[PlanItem], Counter[str]]:
    protected: Counter[str] = Counter()
    accepted = sum(row["state"] == "accepted" for row in requests)
    artifact_candidates, artifacts_by_thread = _artifact_candidates(
        policy, facts, artifacts, accepted, protected
    )
    groups = _thread_groups(
        policy,
        facts,
        artifacts_by_thread,
        artifact_candidates,
        accepted,
        protected,
    )
    protocol = _protocol_candidates(policy, requests, protected)
    return _select(policy.max_items, groups, artifact_candidates, protocol, protected), protected


def _artifact_candidates(
    policy: RetentionPolicy,
    facts: tuple[ThreadFact, ...],
    rows: list[aiosqlite.Row],
    accepted_requests: int,
    protected: Counter[str],
) -> tuple[dict[str, PlanItem], dict[str, list[aiosqlite.Row]]]:
    by_thread = {str(fact.thread.thread_id): fact for fact in facts}
    candidates: dict[str, PlanItem] = {}
    grouped: dict[str, list[aiosqlite.Row]] = {}
    for row in rows:
        grouped.setdefault(row["thread_id"], []).append(row)
        if not policy.expire_artifact_bodies or row["state"] != "published":
            continue
        fact = by_thread.get(row["thread_id"])
        if fact is None:
            raise KernelError("artifact_corrupt", "Artifact维护发现归属Thread缺失")
        reason = _artifact_protection_reason(policy, row, fact, accepted_requests)
        if reason is not None:
            protected[reason] += 1
            continue
        candidates[row["artifact_id"]] = PlanItem(
            "artifact_body",
            {"artifact_id": row["artifact_id"]},
            artifact_precondition(row),
        )
    return candidates, grouped


def _artifact_protection_reason(
    policy: RetentionPolicy,
    row: aiosqlite.Row,
    fact: ThreadFact,
    accepted_requests: int,
) -> str | None:
    if accepted_requests:
        return "accepted_protocol_global"
    if fact.thread.active_turn_id is not None:
        return "active_artifact_owner"
    if thread_has_uncertain_effect(fact.thread):
        return "uncertain_artifact_owner"
    if parse_time(row["expires_at"], code="artifact_corrupt") > policy.cutoff:
        return "artifact_retention"
    return None


def _thread_groups(
    policy: RetentionPolicy,
    facts: tuple[ThreadFact, ...],
    artifacts_by_thread: dict[str, list[aiosqlite.Row]],
    artifact_candidates: dict[str, PlanItem],
    accepted_requests: int,
    protected: Counter[str],
) -> list[tuple[list[PlanItem], PlanItem]]:
    fork_sources = {
        str(fact.thread.fork_snapshot.source_thread_id)
        for fact in facts
        if fact.thread.fork_snapshot is not None
    }
    groups: list[tuple[list[PlanItem], PlanItem]] = []
    for fact in facts:
        if not policy.delete_archived_threads:
            continue
        reason = _thread_protection_reason(
            policy,
            fact,
            fork_sources,
            artifacts_by_thread.get(str(fact.thread.thread_id), []),
            accepted_requests,
        )
        if reason is not None:
            protected[reason] += 1
            continue
        owned = artifacts_by_thread.get(str(fact.thread.thread_id), [])
        required = [
            artifact_candidates[row["artifact_id"]]
            for row in owned
            if row["state"] == "published" and row["artifact_id"] in artifact_candidates
        ]
        groups.append(
            (
                required,
                PlanItem(
                    "session_thread",
                    {"thread_id": str(fact.thread.thread_id)},
                    thread_precondition(fact),
                ),
            )
        )
    return groups


def _thread_protection_reason(
    policy: RetentionPolicy,
    fact: ThreadFact,
    fork_sources: set[str],
    artifacts: list[aiosqlite.Row],
    accepted_requests: int,
) -> str | None:
    thread = fact.thread
    if thread.archive is None:
        return "session_not_archived"
    if thread.archive.archived_at > policy.cutoff:
        return "session_retention"
    if thread.active_turn_id is not None:
        return "active_session"
    if thread_has_uncertain_effect(thread):
        return "uncertain_session"
    if str(thread.thread_id) in fork_sources:
        return "fork_source_session"
    if accepted_requests:
        return "accepted_protocol_global"
    if any(
        row["state"] == "published"
        and parse_time(row["expires_at"], code="artifact_corrupt") > policy.cutoff
        for row in artifacts
    ):
        return "session_artifact_retained"
    return None


def _protocol_candidates(
    policy: RetentionPolicy,
    rows: list[aiosqlite.Row],
    protected: Counter[str],
) -> list[PlanItem]:
    if not policy.delete_terminal_protocol_requests:
        return []
    candidates: list[PlanItem] = []
    for row in rows:
        if row["state"] == "accepted":
            protected["accepted_protocol_request"] += 1
        elif parse_time(row["updated_at"], code="request_corrupt") > policy.cutoff:
            protected["protocol_retention"] += 1
        else:
            candidates.append(
                PlanItem(
                    "protocol_request",
                    {
                        "client_instance_id": row["client_instance_id"],
                        "request_id": row["request_id"],
                    },
                    request_precondition(row),
                )
            )
    return candidates


def _select(
    limit: int,
    groups: list[tuple[list[PlanItem], PlanItem]],
    artifacts: dict[str, PlanItem],
    protocol: list[PlanItem],
    protected: Counter[str],
) -> list[PlanItem]:
    selected: list[PlanItem] = []
    selected_artifacts: set[str] = set()
    for required, thread_item in groups:
        missing = [item for item in required if item.key["artifact_id"] not in selected_artifacts]
        if len(selected) + len(missing) + 1 > limit:
            protected["plan_capacity"] += 1
            continue
        selected.extend(missing)
        selected_artifacts.update(item.key["artifact_id"] for item in missing)
        selected.append(thread_item)
    for artifact_id, item in artifacts.items():
        if artifact_id in selected_artifacts:
            continue
        if len(selected) >= limit:
            protected["plan_capacity"] += 1
        else:
            selected.append(item)
    for item in protocol:
        if len(selected) >= limit:
            protected["plan_capacity"] += 1
        else:
            selected.append(item)
    return selected
