"""Small application helper for material-event audit recording."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import uuid4

from enterprise_agent.domain import (
    AttentionId,
    AuditEvent,
    AuditEventId,
    EvidenceId,
    PlanId,
    RunId,
    UserId,
    WorkflowId,
)
from enterprise_agent.ports import AuditPort


def append_material_audit_event(
    audit: AuditPort,
    *,
    event_type: str,
    run_id: RunId,
    occurred_at: datetime,
    actor_id: UserId | None = None,
    attention_id: AttentionId | None = None,
    workflow_id: WorkflowId | None = None,
    plan_id: PlanId | None = None,
    payload: Mapping[str, object],
    evidence_ids: Sequence[EvidenceId] = (),
    policy_version: str | None = None,
    plan_hash: str | None = None,
    idempotency_key: str | None = None,
    failure_category: str | None = None,
) -> None:
    """Append one typed material fact through the only authorized audit persistence port."""
    audit.append(
        AuditEvent(
            event_id=AuditEventId(str(uuid4())),
            occurred_at=occurred_at,
            event_type=event_type,
            run_id=run_id,
            actor_id=actor_id,
            attention_id=attention_id,
            workflow_id=workflow_id,
            plan_id=plan_id,
            evidence_ids=tuple(evidence_ids),
            payload=payload,
            policy_version=policy_version,
            plan_hash=plan_hash,
            idempotency_key=idempotency_key,
            failure_category=failure_category,
        )
    )
