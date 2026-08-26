"""Mocked OpenRouter chat-completions adapter contracts with no live-provider dependency."""

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

NOW = datetime(2026, 8, 26, 10, tzinfo=UTC)
API_KEY = "unit-test-openrouter-key"
MODEL = "nvidia/nemotron-ultra-253b-v1:free"


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
        run_id=RunId("run-openrouter-adapter"),
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
    """Build the raw non-streaming OpenRouter chat-completions shape with one JSON message."""
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(output)},
            }
        ]
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


def test_openrouter_adapter_enforces_native_output_schema_without_provider_fallbacks_and_audits_metadata() -> (
    None
):
    """A valid JSON result is validated canonically while routing stays pinned to the chosen model."""
    from enterprise_agent.adapters.openrouter import OpenRouterChatCompletionsAdapter

    transport = RecordingTransport(
        completed_response(
            {
                "outcome": "MANUAL_REVIEW",
                "reason": "Supplier timing evidence is inconclusive.",
            }
        )
    )
    audit = RecordingAudit()
    result = OpenRouterChatCompletionsAdapter(
        api_key=API_KEY,
        model=MODEL,
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
    assert request["model"] == MODEL
    assert request["stream"] is False
    assert request["usage"] == {"include": True}
    assert request["provider"] == {"allow_fallbacks": False, "require_parameters": True}
    messages = cast(list[dict[str, Any]], request["messages"])
    assert "untrusted data" in messages[0]["content"]
    assert "Return only JSON" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    payload = json.loads(cast(str, messages[1]["content"]))
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
    response_format = cast(dict[str, Any], request["response_format"])
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "scenario_a_recommendation_v1"
    assert response_format["json_schema"]["strict"] is True
    output_schema = response_format["json_schema"]["schema"]
    assert output_schema["discriminator"]["propertyName"] == "outcome"
    assert output_schema["$defs"]["ManualReviewRecommendation"]["additionalProperties"] is False
    assert API_KEY not in json.dumps(request)
    assert audit.events[0].event_type == "llm.completed"
    assert audit.events[0].payload == {
        "provider": "openrouter",
        "model": MODEL,
        "status": "succeeded",
        "response_schema": "scenario_a_recommendation:v1",
    }
    assert API_KEY not in repr(audit.events[0])


def test_openrouter_adapter_preserves_provider_reported_metering_cost_in_safe_audit_facts() -> None:
    """OpenRouter's provider-reported cost is stored as exact metering, never as a raw response."""
    from enterprise_agent.adapters.openrouter import OpenRouterChatCompletionsAdapter
    from enterprise_agent.ports import LLMCostSource

    raw_response = completed_response(
        {"outcome": "MANUAL_REVIEW", "reason": "A person must resolve this exception."}
    )
    raw_response["usage"] = {
        "prompt_tokens": 1000,
        "completion_tokens": 500,
        "total_tokens": 1500,
        "cost": "0.000321",
    }
    audit = RecordingAudit()

    result = OpenRouterChatCompletionsAdapter(
        api_key=API_KEY,
        model=MODEL,
        transport=RecordingTransport(raw_response),
        audit=audit,
        clock=FixedClock(),
    ).generate(prompt())

    assert result.usage is not None
    assert result.usage.cost_source is LLMCostSource.PROVIDER_REPORTED
    assert result.usage.cost_usd == Decimal("0.000321")
    assert audit.events[0].payload["cost_usd"] == "0.000321"
    assert audit.events[0].payload["cost_source"] == "provider_reported"


def test_openrouter_adapter_uses_the_declared_scenario_b_schema() -> None:
    """The common adapter selects the other application-owned output contract."""
    from enterprise_agent.adapters.openrouter import OpenRouterChatCompletionsAdapter

    transport = RecordingTransport(
        completed_response(
            {
                "outcome": "MANUAL_REVIEW",
                "reason": "Quality evidence needs a human decision.",
            }
        )
    )
    result = OpenRouterChatCompletionsAdapter(
        api_key=API_KEY,
        model=MODEL,
        transport=transport,
        audit=RecordingAudit(),
        clock=FixedClock(),
    ).generate(replace(prompt(), response_schema="scenario_b_recommendation:v1"))

    request = transport.requests[0]
    messages = cast(list[dict[str, Any]], request["messages"])
    payload = json.loads(cast(str, messages[1]["content"]))
    output_schema = cast(dict[str, Any], request["response_format"])["json_schema"]["schema"]
    assert result.status is LLMGenerationStatus.SUCCEEDED
    assert payload["response_schema"] == "scenario_b_recommendation:v1"
    assert output_schema["discriminator"]["propertyName"] == "outcome"
    assert output_schema["$defs"]["ReallocateLotInput"]["additionalProperties"] is False
    assert cast(dict[str, Any], request["response_format"])["json_schema"]["strict"] is True


def test_openrouter_adapter_rejects_an_undeclared_schema_without_calling_the_provider() -> None:
    """An unowned response schema cannot reach OpenRouter or trigger a fallback recommendation."""
    from enterprise_agent.adapters.openrouter import OpenRouterChatCompletionsAdapter

    transport = RecordingTransport(
        completed_response({"outcome": "NO_ACTION", "rationale": "ignored"})
    )
    audit = RecordingAudit()
    result = OpenRouterChatCompletionsAdapter(
        api_key=API_KEY,
        model=MODEL,
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
        (
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": None, "refusal": "denied"},
                    }
                ]
            },
            LLMGenerationStatus.REFUSAL,
        ),
        (
            {"choices": [{"finish_reason": "content_filter", "message": {}}]},
            LLMGenerationStatus.REFUSAL,
        ),
        (
            {"choices": [{"finish_reason": "length", "message": {}}]},
            LLMGenerationStatus.PROVIDER_FAILURE,
        ),
        ({"error": {"message": "upstream unavailable"}}, LLMGenerationStatus.PROVIDER_FAILURE),
        (["not a response object"], LLMGenerationStatus.INVALID_RESPONSE),
        ({}, LLMGenerationStatus.INVALID_RESPONSE),
        ({"choices": []}, LLMGenerationStatus.INVALID_RESPONSE),
        ({"choices": [{}, {}]}, LLMGenerationStatus.INVALID_RESPONSE),
        ({"choices": [None]}, LLMGenerationStatus.INVALID_RESPONSE),
        ({"choices": [{"finish_reason": "stop"}]}, LLMGenerationStatus.INVALID_RESPONSE),
        (
            {"choices": [{"finish_reason": "stop", "message": None}]},
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (
            {"choices": [{"finish_reason": "stop", "message": {"role": "user", "content": "{}"}}]},
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": None}}
                ]
            },
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "not-json"},
                    }
                ]
            },
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "[]"}}
                ]
            },
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (TimeoutError("provider timed out"), LLMGenerationStatus.TIMEOUT),
        (OSError("provider unavailable"), LLMGenerationStatus.PROVIDER_FAILURE),
    ),
)
def test_openrouter_adapter_normalizes_invalid_refused_timeout_and_provider_failures(
    response: object,
    expected_status: LLMGenerationStatus,
) -> None:
    """Provider-specific raw responses and errors cannot enter output or audit payloads."""
    from enterprise_agent.adapters.openrouter import OpenRouterChatCompletionsAdapter

    audit = RecordingAudit()
    result = OpenRouterChatCompletionsAdapter(
        api_key=API_KEY,
        model=MODEL,
        transport=RecordingTransport(response),
        audit=audit,
        clock=FixedClock(),
    ).generate(prompt())

    assert result.status is expected_status
    assert result.output is None
    assert audit.events[0].payload == {
        "provider": "openrouter",
        "model": MODEL,
        "status": expected_status.value,
        "response_schema": "scenario_a_recommendation:v1",
    }
    assert audit.events[0].failure_category == expected_status.value
    assert "provider timed out" not in repr(audit.events[0])
    assert "provider unavailable" not in repr(audit.events[0])
    assert "upstream unavailable" not in repr(audit.events[0])


def test_urllib_transport_posts_json_to_chat_completions_with_the_configured_bearer_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The concrete transport sends only the documented HTTP boundary without requiring an SDK."""
    from enterprise_agent.adapters import openrouter

    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"choices":[]}'
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, *, timeout: float) -> MagicMock:
        captured["request"] = request
        captured["timeout"] = timeout
        return response

    monkeypatch.setattr(openrouter, "urlopen", fake_urlopen)
    transport = openrouter.UrllibOpenRouterChatCompletionsTransport(
        api_key=API_KEY, timeout_seconds=12.5
    )

    raw_response = transport.create({"model": MODEL, "stream": False})

    request = cast(Request, captured["request"])
    assert raw_response == {"choices": []}
    assert request.full_url == "https://openrouter.ai/api/v1/chat/completions"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {API_KEY}"
    assert json.loads(cast(bytes, request.data)) == {"model": MODEL, "stream": False}
    assert captured["timeout"] == 12.5


def test_openrouter_adapter_and_transport_reject_invalid_local_configuration_or_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid settings and a non-object response cannot become an LLM recommendation."""
    from enterprise_agent.adapters import openrouter

    with pytest.raises(ValueError, match="API key is required"):
        openrouter.UrllibOpenRouterChatCompletionsTransport(api_key=" ")
    with pytest.raises(ValueError, match="timeout must be positive"):
        openrouter.UrllibOpenRouterChatCompletionsTransport(api_key=API_KEY, timeout_seconds=0)
    with pytest.raises(ValueError, match="model is required"):
        openrouter.OpenRouterChatCompletionsAdapter(
            api_key=API_KEY,
            model=" ",
            audit=RecordingAudit(),
            clock=FixedClock(),
        )

    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"[]"
    monkeypatch.setattr(openrouter, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(TypeError, match="JSON object"):
        openrouter.UrllibOpenRouterChatCompletionsTransport(api_key=API_KEY).create(
            {"model": MODEL}
        )
