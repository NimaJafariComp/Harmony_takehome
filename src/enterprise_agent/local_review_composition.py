"""Local-only composition root for the optional actor-scoped review UI."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from enterprise_agent.adapters import (
    PostgresAttentionAdapter,
    PostgresAuditAdapter,
    PostgresCalendarAdapter,
    PostgresDemoClock,
    PostgresErpAdapter,
    PostgresIdentityAdapter,
    PostgresKnowledgeAdapter,
    PostgresLocalReviewAccessAdapter,
    PostgresMailAdapter,
    PostgresOperatorStatusAdapter,
    PostgresPlanApprovalAdapter,
    PostgresQualityAdapter,
    PostgresWorkflowStateAdapter,
)
from enterprise_agent.application.local_decisions import (
    CurrentPlanSourceVersionsService,
    LocalApprovalDecisionPort,
    LocalApprovalDecisionService,
    UnconfiguredLocalApprovalDecisionService,
)
from enterprise_agent.application.local_demo_controls import (
    LocalDemoClockControlPort,
    LocalDemoClockControlService,
    UnconfiguredLocalDemoClockControlService,
)
from enterprise_agent.application.local_review import (
    LocalReviewReadPort,
    LocalReviewReadService,
    UnconfiguredLocalReviewService,
)
from enterprise_agent.domain import UserId
from enterprise_agent.llm_setup import default_env_path, load_local_environment
from enterprise_agent.seed import ID_DANA, SeedSafetyError, _require_local_demo_database

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


def create_local_approval_decision_service() -> LocalApprovalDecisionPort:
    """Compose one local decision service without loading an LLM profile, key, or write tool adapter."""
    try:
        environment = load_local_environment(default_env_path())
    except ValueError:
        return UnconfiguredLocalApprovalDecisionService()
    database_url = environment.get("DATABASE_URL", "").strip()
    actor_id = _selected_actor_id(environment)
    if not database_url or actor_id is None:
        return UnconfiguredLocalApprovalDecisionService()

    identity = PostgresIdentityAdapter(database_url)
    attention_store = PostgresAttentionAdapter(database_url)
    audit = PostgresAuditAdapter(database_url)
    return LocalApprovalDecisionService(
        actor_id=actor_id,
        approvals=PostgresPlanApprovalAdapter(database_url),
        freshness=CurrentPlanSourceVersionsService(
            identity=identity,
            attentions=attention_store,
            erp=PostgresErpAdapter(database_url),
            quality=PostgresQualityAdapter(database_url),
            knowledge=PostgresKnowledgeAdapter(database_url),
            mail=PostgresMailAdapter(database_url),
            calendar=PostgresCalendarAdapter(database_url),
        ),
        clock=PostgresDemoClock(database_url),
        audit=audit,
        audit_runs=audit,
    )


def create_local_demo_clock_control_service() -> LocalDemoClockControlPort:
    """Build the one mutable local-demo control only for an explicit true local setting."""
    try:
        environment = load_local_environment(default_env_path())
    except ValueError:
        return UnconfiguredLocalDemoClockControlService()
    database_url = environment.get("DATABASE_URL", "").strip()
    demo_mode_enabled = environment.get("DEMO_MODE", "").strip().lower() == "true"
    if not database_url or not demo_mode_enabled:
        return UnconfiguredLocalDemoClockControlService()
    try:
        _require_local_demo_database(database_url, allow_test_database=False)
    except SeedSafetyError:
        return UnconfiguredLocalDemoClockControlService()
    return LocalDemoClockControlService(
        clock=PostgresDemoClock(database_url),
        demo_mode_enabled=True,
    )


def _selected_actor_id(environment: Mapping[str, str]) -> UserId | None:
    """Use Dana's seeded local demo identity unless a UUID-shaped local actor override is explicitly set."""
    value = environment.get(LOCAL_REVIEW_ACTOR_SETTING, str(_DEFAULT_LOCAL_REVIEW_ACTOR)).strip()
    try:
        return UserId(str(UUID(value)))
    except (AttributeError, TypeError, ValueError):
        return None
