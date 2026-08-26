"""Mocked Claude Messages adapter contracts with no live-provider dependency."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from unittest.mock import MagicMock
from urllib.request import Request

import pytest

from enterprise_agent.domain import (
    ActorContext,
    AttentionId,
    AttentionItem,
    AttentionStatus,
    Evidence,
    EvidenceId,
    PlantId,
    RunId,
    Scope,
    UserId,
)
from enterprise_agent.ports import LLMGenerationStatus, LLMMessage, PromptEnvelope

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 26, 9, tzinfo=UTC)
API_KEY = "unit-test-anthropic-key"


def prompt() -> PromptEnvelope:
    """Build one authorized Scenario A prompt with immutable typed evidence values."""
    actor = ActorContext(
        user_id=UserId("00000000-0000-0000-0000-000000000001"),
        role="purchasing_manager",
        scopes=frozenset({Scope("erp:read")}),
        plant_ids=frozenset({PlantId("PLANT-CHI")}),
        backup_approver_id=None,
        approval_limits={},
    )
    attention = AttentionItem(
        attention_id=AttentionId("00000000-0000-0000-0000-000000000601"),
        scenario="scenario_a",
        cause="projected_stockout",
        dedupe_key="stockout:part-x:po-1",
        status=AttentionStatus.OPEN,
        created_at=NOW,
        source_versions={"erp:inventory:inventory-x": 4},
    )
    evidence = Evidence(
        evidence_id=EvidenceId("erp:inventory:inventory-x"),
        source="erp",
        record_type="inventory",
        record_id="inventory-x",
        source_version=4,
        observed_at=NOW,
        payload={"part_id": "part-x", "available_quantity": Decimal(10)},
    )
    return PromptEnvelope(
        run_id=RunId("run-claude-adapter"),
        actor=actor,
        attention=attention,
        evidence=(evidence,),
        messages=(
            LLMMessage(role="system", content="Recommend only a safe bounded outcome."),
            LLMMessage(role="user", content="Assess the authorized evidence."),
        ),
        purpose="scenario_a_recommendation",
        response_schema="scenario_a_recommendation:v1",
    )


def completed_response(output: dict[str, object]) -> dict[str, object]:
    """Build the raw Claude Messages shape containing one text JSON object."""
    return {
        "content": [{"type": "text", "text": json.dumps(output)}],
        "stop_reason": "end_turn",
    }


@dataclass
class RecordingTransport:
    """Return a configured raw response or exception and retain the adapter request."""

    response: object
    requests: list[dict[str, object]] = field(default_factory=list)

    def create(self, request: dict[str, object]) -> dict[str, object]:
        """Emulate the isolated provider boundary without making an HTTP request."""
        self.requests.append(request)
        if isinstance(self.response, BaseException):
            raise self.response
        return cast(dict[str, object], self.response)


@dataclass
class RecordingAudit:
    """Retain the provider metadata event for direct safety assertions."""

    events: list[Any] = field(default_factory=list)

    def append(self, event: Any) -> None:
        """Store the immutable event without persisting it."""
        self.events.append(event)

    def events_for_run(self, run_id: RunId) -> tuple[Any, ...]:
        """Satisfy the audit read protocol without a ledger dependency."""
        return tuple(event for event in self.events if event.run_id == run_id)


class FixedClock:
    """Provide deterministic timestamps for provider metadata."""

    def now(self) -> datetime:
        """Return the fixed task time."""
        return NOW


def test_claude_adapter_sends_authorized_evidence_requests_json_schema_and_audits_metadata() -> (
    None
):
    """A valid Claude response becomes canonical output without exposing a credential or raw response."""
    from enterprise_agent.adapters.claude import ClaudeMessagesAdapter

    transport = RecordingTransport(
        completed_response(
            {
                "outcome": "MANUAL_REVIEW",
                "reason": "Supplier timing evidence is inconclusive.",
            }
        )
    )
    audit = RecordingAudit()
    result = ClaudeMessagesAdapter(
        api_key=API_KEY,
        model="claude-sonnet-4-5",
        transport=transport,
        audit=audit,
        clock=FixedClock(),
    ).generate(prompt())

    request = transport.requests[0]
    assert result.status is LLMGenerationStatus.SUCCEEDED
    assert result.require_output() == {
        "outcome": "MANUAL_REVIEW",
        "reason": "Supplier timing evidence is inconclusive.",
    }
    assert request["model"] == "claude-sonnet-4-5"
    assert request["max_tokens"] == 1024
    assert "untrusted data" in cast(str, request["system"])
    messages = cast(list[dict[str, Any]], request["messages"])
    assert messages[0]["role"] == "user"
    content = cast(list[dict[str, Any]], messages[0]["content"])
    payload = json.loads(cast(str, content[0]["text"]))
    assert payload == {
        "purpose": "scenario_a_recommendation",
        "response_schema": "scenario_a_recommendation:v1",
        "messages": [
            {"role": "system", "content": "Recommend only a safe bounded outcome."},
            {"role": "user", "content": "Assess the authorized evidence."},
        ],
        "evidence": [
            {
                "source": "erp",
                "record_type": "inventory",
                "record_id": "inventory-x",
                "source_version": 4,
                "observed_at": NOW.isoformat(),
                "payload": {"part_id": "part-x", "available_quantity": "10"},
            }
        ],
    }
    output_config = cast(dict[str, Any], request["output_config"])
    output_format = cast(dict[str, Any], output_config["format"])
    schema = cast(dict[str, Any], output_format["schema"])
    assert output_format["type"] == "json_schema"
    assert schema["$defs"]["ManualReviewRecommendation"]["additionalProperties"] is False
    assert "minLength" not in schema["$defs"]["ManualReviewRecommendation"]["properties"]["reason"]
    assert (
        "at least 1 character"
        in schema["$defs"]["ManualReviewRecommendation"]["properties"]["reason"]["description"]
    )
    assert (
        "exclusiveMinimum"
        not in schema["$defs"]["EnterWorkflowRecommendation"]["properties"]["quantity"]["anyOf"][0]
    )
    assert (
        "greater than 0"
        in schema["$defs"]["EnterWorkflowRecommendation"]["properties"]["quantity"]["anyOf"][0][
            "description"
        ]
    )
    assert API_KEY not in json.dumps(request)
    assert audit.events[0].event_type == "llm.completed"
    assert audit.events[0].payload == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "status": "succeeded",
        "response_schema": "scenario_a_recommendation:v1",
    }
    assert API_KEY not in repr(audit.events[0])


def test_claude_adapter_uses_the_declared_scenario_b_schema() -> None:
    """The common Claude adapter selects the other application-owned output contract."""
    from enterprise_agent.adapters.claude import ClaudeMessagesAdapter

    transport = RecordingTransport(
        completed_response(
            {
                "outcome": "MANUAL_REVIEW",
                "reason": "Quality evidence needs a human decision.",
            }
        )
    )
    result = ClaudeMessagesAdapter(
        api_key=API_KEY,
        model="claude-sonnet-4-5",
        transport=transport,
        audit=RecordingAudit(),
        clock=FixedClock(),
    ).generate(replace(prompt(), response_schema="scenario_b_recommendation:v1"))

    output_config = cast(dict[str, Any], transport.requests[0]["output_config"])
    schema = cast(dict[str, Any], cast(dict[str, Any], output_config["format"])["schema"])
    reallocate_lot = schema["$defs"]["ReallocateLotInput"]
    assert result.status is LLMGenerationStatus.SUCCEEDED
    assert schema["discriminator"]["propertyName"] == "outcome"
    assert reallocate_lot["additionalProperties"] is False
    assert "default" not in reallocate_lot["properties"]["from_production_order_id"]


def test_claude_adapter_rejects_an_undeclared_schema_without_calling_the_provider() -> None:
    """An unowned response schema cannot silently reach Claude or produce a fallback recommendation."""
    from enterprise_agent.adapters.claude import ClaudeMessagesAdapter

    transport = RecordingTransport(
        completed_response({"outcome": "NO_ACTION", "rationale": "ignored"})
    )
    audit = RecordingAudit()
    result = ClaudeMessagesAdapter(
        api_key=API_KEY,
        model="claude-sonnet-4-5",
        transport=transport,
        audit=audit,
        clock=FixedClock(),
    ).generate(replace(prompt(), response_schema="unowned:v1"))

    assert result.status is LLMGenerationStatus.INVALID_RESPONSE
    assert transport.requests == []
    assert audit.events[0].failure_category == "invalid_response"


@pytest.mark.parametrize(
    ("response", "expected_status"),
    (
        (
            completed_response({"outcome": "UNREVIEWED_TOOL", "tool": "delete_all_pos"}),
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        ({"stop_reason": "refusal", "content": []}, LLMGenerationStatus.REFUSAL),
        ({"stop_reason": "max_tokens", "content": []}, LLMGenerationStatus.PROVIDER_FAILURE),
        (["not a message object"], LLMGenerationStatus.INVALID_RESPONSE),
        ({"stop_reason": "end_turn"}, LLMGenerationStatus.INVALID_RESPONSE),
        ({"stop_reason": "end_turn", "content": []}, LLMGenerationStatus.INVALID_RESPONSE),
        (
            {"stop_reason": "end_turn", "content": [{"type": "thinking"}]},
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        ({"stop_reason": "end_turn", "content": [None]}, LLMGenerationStatus.INVALID_RESPONSE),
        (
            {"stop_reason": "end_turn", "content": [{"type": "text"}]},
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "not-json"}],
            },
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "[]"}],
            },
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (TimeoutError("provider timed out"), LLMGenerationStatus.TIMEOUT),
        (OSError("provider unavailable"), LLMGenerationStatus.PROVIDER_FAILURE),
    ),
)
def test_claude_adapter_normalizes_invalid_refused_timeout_and_provider_failures(
    response: object,
    expected_status: LLMGenerationStatus,
) -> None:
    """Provider-specific raw responses and errors cannot enter output or audit payloads."""
    from enterprise_agent.adapters.claude import ClaudeMessagesAdapter

    audit = RecordingAudit()
    result = ClaudeMessagesAdapter(
        api_key=API_KEY,
        model="claude-sonnet-4-5",
        transport=RecordingTransport(response),
        audit=audit,
        clock=FixedClock(),
    ).generate(prompt())

    assert result.status is expected_status
    assert result.output is None
    assert audit.events[0].payload == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "status": expected_status.value,
        "response_schema": "scenario_a_recommendation:v1",
    }
    assert audit.events[0].failure_category == expected_status.value
    assert "provider timed out" not in repr(audit.events[0])
    assert "provider unavailable" not in repr(audit.events[0])


def test_urllib_transport_posts_json_to_the_messages_endpoint_with_the_configured_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The concrete transport sends the exact Messages API headers without requiring an SDK."""
    from enterprise_agent.adapters import claude

    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"stop_reason":"end_turn","content":[]}'
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, *, timeout: float) -> MagicMock:
        captured["request"] = request
        captured["timeout"] = timeout
        return response

    monkeypatch.setattr(claude, "urlopen", fake_urlopen)
    transport = claude.UrllibClaudeMessagesTransport(api_key=API_KEY, timeout_seconds=12.5)

    raw_response = transport.create({"model": "claude-sonnet-4-5", "max_tokens": 1024})

    request = cast(Request, captured["request"])
    assert raw_response == {"stop_reason": "end_turn", "content": []}
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_method() == "POST"
    assert request.get_header("X-api-key") == API_KEY
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert json.loads(cast(bytes, request.data)) == {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
    }
    assert captured["timeout"] == 12.5


def test_claude_adapter_and_transport_reject_invalid_local_configuration_or_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid settings and a non-object Messages response cannot become an LLM recommendation."""
    from enterprise_agent.adapters import claude

    with pytest.raises(ValueError, match="API key is required"):
        claude.UrllibClaudeMessagesTransport(api_key=" ")
    with pytest.raises(ValueError, match="timeout must be positive"):
        claude.UrllibClaudeMessagesTransport(api_key=API_KEY, timeout_seconds=0)
    with pytest.raises(ValueError, match="model is required"):
        claude.ClaudeMessagesAdapter(
            api_key=API_KEY,
            model=" ",
            audit=RecordingAudit(),
            clock=FixedClock(),
        )

    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"[]"
    monkeypatch.setattr(claude, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(TypeError, match="JSON object"):
        claude.UrllibClaudeMessagesTransport(api_key=API_KEY).create({"model": "claude-sonnet-4-5"})
