"""No-network normalized LLM usage, cost, and immutable-ledger summary contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from enterprise_agent.domain import AuditEvent, AuditEventId, RunId

pytestmark = pytest.mark.unit


def test_usage_normalization_estimates_reviewed_direct_provider_rates_and_tracks_cached_tokens() -> (
    None
):
    """Direct-provider usage is normalized without retaining a provider payload and clearly marked estimated."""
    from enterprise_agent.llm_usage import usage_from_response
    from enterprise_agent.ports import LLMCostSource

    openai_usage = usage_from_response(
        "openai",
        "gpt-5.6-luna",
        {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 1500,
                "input_tokens_details": {"cached_tokens": 200},
            }
        },
    )
    claude_usage = usage_from_response(
        "claude",
        "claude-sonnet-5",
        {
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_read_input_tokens": 100,
            }
        },
    )

    assert openai_usage is not None
    assert openai_usage.input_tokens == 1000
    assert openai_usage.cached_input_tokens == 200
    assert openai_usage.output_tokens == 500
    assert openai_usage.total_tokens == 1500
    assert openai_usage.cost_usd == Decimal("0.000764")
    assert openai_usage.cost_source is LLMCostSource.ESTIMATED
    assert claude_usage is not None
    assert claude_usage.cached_input_tokens == 100
    assert claude_usage.total_tokens == 1500
    assert claude_usage.cost_usd == Decimal("0.007020")
    assert claude_usage.cost_source is LLMCostSource.ESTIMATED


def test_usage_normalization_keeps_a_provider_reported_openrouter_cost() -> None:
    """OpenRouter's response cost takes precedence over an application estimate."""
    from enterprise_agent.llm_usage import usage_from_response
    from enterprise_agent.ports import LLMCostSource

    usage = usage_from_response(
        "openrouter",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
                "cost": "0.000321",
            }
        },
    )

    assert usage is not None
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 500
    assert usage.cost_usd == Decimal("0.000321")
    assert usage.cost_source is LLMCostSource.PROVIDER_REPORTED


@pytest.mark.parametrize(
    "response",
    (
        {},
        {"usage": "not-an-object"},
        {"usage": {"input_tokens": "100", "output_tokens": 20}},
        {"usage": {"input_tokens": -1, "output_tokens": 20}},
    ),
)
def test_usage_normalization_discards_missing_or_malformed_metering(response: dict[str, object]) -> None:
    """A malformed metering block cannot turn into invented accounting or a provider failure."""
    from enterprise_agent.llm_usage import usage_from_response

    assert usage_from_response("openai", "gpt-5.6-luna", response) is None


def test_usage_summary_groups_safe_audit_metadata_and_exposes_unknown_costs() -> None:
    """The read-only summary uses only ledger facts and identifies calls whose cost cannot be priced."""
    from enterprise_agent.llm_usage import summarize_llm_usage

    events = (
        _usage_event(
            "event-usage-1",
            {
                "provider": "openai",
                "model": "gpt-5.6-luna",
                "status": "succeeded",
                "input_tokens": 1000,
                "cached_input_tokens": 200,
                "output_tokens": 500,
                "total_tokens": 1500,
                "cost_usd": "0.000764",
                "cost_source": "estimated",
            },
        ),
        _usage_event(
            "event-usage-2",
            {
                "provider": "openai",
                "model": "unpriced-custom-model",
                "status": "provider_failure",
                "input_tokens": 25,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 25,
                "cost_source": "unavailable",
            },
        ),
    )

    summary = summarize_llm_usage(events)

    assert [(line.provider, line.model, line.request_count) for line in summary.lines] == [
        ("openai", "gpt-5.6-luna", 1),
        ("openai", "unpriced-custom-model", 1),
    ]
    assert summary.lines[0].input_tokens == 1000
    assert summary.lines[0].cached_input_tokens == 200
    assert summary.lines[0].output_tokens == 500
    assert summary.lines[0].total_tokens == 1500
    assert summary.lines[0].cost_usd == Decimal("0.000764")
    assert summary.lines[0].estimated_request_count == 1
    assert summary.lines[1].unknown_cost_request_count == 1
    assert summary.total_cost_usd == Decimal("0.000764")


def _usage_event(event_id: str, payload: dict[str, object]) -> AuditEvent:
    """Create one immutable event containing only the safe usage facts an adapter is permitted to append."""
    return AuditEvent(
        event_id=AuditEventId(event_id),
        occurred_at=datetime(2026, 8, 26, 12, tzinfo=UTC),
        event_type="llm.completed",
        run_id=RunId("run-usage"),
        actor_id=None,
        attention_id=None,
        workflow_id=None,
        plan_id=None,
        evidence_ids=(),
        payload=payload,
        policy_version=None,
        plan_hash=None,
        idempotency_key=None,
        failure_category=None,
    )
