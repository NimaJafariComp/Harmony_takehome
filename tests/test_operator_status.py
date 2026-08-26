"""Pure recovery-status contracts for the local operator read model."""

from datetime import UTC, datetime, timedelta

import pytest

from enterprise_agent.application.operator_status import RecoveryState, recovery_state_for
from enterprise_agent.domain import WorkflowStatus


pytestmark = pytest.mark.unit


def test_recovery_status_never_exposes_a_raw_error_and_distinguishes_reclaimable_work() -> None:
    """Operators get an actionable recovery state without being shown internal failure details."""
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)

    assert (
        recovery_state_for(
            WorkflowStatus.RUNNING,
            lease_expires_at=now - timedelta(minutes=1),
            now=now,
        )
        is RecoveryState.RECLAIMABLE
    )
    assert (
        recovery_state_for(
            WorkflowStatus.FAILED,
            lease_expires_at=None,
            now=now,
        )
        is RecoveryState.RECOVERY_REQUIRED
    )
    assert (
        recovery_state_for(
            WorkflowStatus.PENDING,
            lease_expires_at=None,
            now=now,
        )
        is RecoveryState.APPROVAL_REQUIRED
    )
