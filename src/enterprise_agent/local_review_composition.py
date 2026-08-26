"""Local-only composition root for the optional actor-scoped review UI."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from enterprise_agent.adapters import (
    PostgresAttentionAdapter,
    PostgresAuditAdapter,
    PostgresDemoClock,
    PostgresIdentityAdapter,
    PostgresLocalReviewAccessAdapter,
    PostgresOperatorStatusAdapter,
    PostgresPlanApprovalAdapter,
    PostgresWorkflowStateAdapter,
)
from enterprise_agent.application.local_review import (
    LocalReviewReadPort,
    LocalReviewReadService,
    UnconfiguredLocalReviewService,
)
from enterprise_agent.domain import UserId
from enterprise_agent.llm_setup import default_env_path, load_local_environment
from enterprise_agent.seed import ID_DANA

LOCAL_REVIEW_ACTOR_SETTING = "LOCAL_REVIEW_ACTOR_ID"
_DEFAULT_LOCAL_REVIEW_ACTOR = UserId(str(ID_DANA))


def create_local_review_service() -> LocalReviewReadPort:
    """Build the optional local read service from local settings without loading an LLM profile or key."""
    try:
        environment = load_local_environment(default_env_path())
    except ValueError:
        return UnconfiguredLocalReviewService()
    database_url = environment.get("DATABASE_URL", "").strip()
    actor_id = _selected_actor_id(environment)
    if not database_url or actor_id is None:
        return UnconfiguredLocalReviewService()
    return LocalReviewReadService(
        actor_id=actor_id,
        operator_status=PostgresOperatorStatusAdapter(database_url),
        attention_store=PostgresAttentionAdapter(database_url),
        approvals=PostgresPlanApprovalAdapter(database_url),
        workflows=PostgresWorkflowStateAdapter(database_url),
        audit_ledger=PostgresAuditAdapter(database_url),
        demo_clock_reader=PostgresDemoClock(database_url),
        identity=PostgresIdentityAdapter(database_url),
        attention_access=PostgresLocalReviewAccessAdapter(database_url),
    )


def _selected_actor_id(environment: Mapping[str, str]) -> UserId | None:
    """Use Dana's seeded local demo identity unless a UUID-shaped local actor override is explicitly set."""
    value = environment.get(LOCAL_REVIEW_ACTOR_SETTING, str(_DEFAULT_LOCAL_REVIEW_ACTOR)).strip()
    try:
        return UserId(str(UUID(value)))
    except (AttributeError, TypeError, ValueError):
        return None
