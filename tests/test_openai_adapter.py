"""Mocked OpenAI Responses adapter contracts with no live-provider dependency."""

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
from enterprise_agent.ports import (
    LLMGenerationStatus,
    LLMMessage,
    PromptEnvelope,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 25, 9, tzinfo=UTC)
API_KEY = "unit-test-openai-key"


def prompt() -> PromptEnvelope:
    """Build one authorized Scenario A prompt with JSON-safe and typed evidence values."""
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
        run_id=RunId("run-openai-adapter"),
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
    """Build the raw Responses API shape containing one output-text JSON object."""
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": json.dumps(output)}],
            }
        ],
    }


@dataclass
class RecordingTransport:
    """Return a configured raw response or exception and retain only the adapter request."""

    response: object
    requests: list[dict[str, object]] = field(default_factory=list)

    def create(self, request: dict[str, object]) -> dict[str, object]:
        """Emulate one injected provider boundary without making an HTTP request."""
        self.requests.append(request)
        if isinstance(self.response, BaseException):
            raise self.response
        return cast(dict[str, object], self.response)


@dataclass
class RecordingAudit:
    """Retain the metadata event that the real ledger adapter would sanitize and persist."""

    events: list[Any] = field(default_factory=list)

    def append(self, event: Any) -> None:
        """Store the immutable event for direct assertions."""
        self.events.append(event)

    def events_for_run(self, run_id: RunId) -> tuple[Any, ...]:
        """Satisfy the audit read protocol without accessing a live ledger."""
        return tuple(event for event in self.events if event.run_id == run_id)


class FixedClock:
    """Provide deterministic timestamping for provider metadata."""

    def now(self) -> datetime:
        """Return the fixed time used by the adapter audit event."""
        return NOW


def test_openai_adapter_sends_only_authorized_evidence_requests_strict_json_and_audits_metadata() -> (
    None
):
    """A valid OpenAI response becomes canonical shared-schema output with no credential leakage."""
    from enterprise_agent.adapters.openai import OpenAIResponsesAdapter

    transport = RecordingTransport(
        completed_response(
            {
                "outcome": "MANUAL_REVIEW",
                "reason": "Supplier timing evidence is inconclusive.",
            }
        )
    )
    audit = RecordingAudit()
    adapter = OpenAIResponsesAdapter(
        api_key=API_KEY,
        model="gpt-5.6-luna",
        transport=transport,
        audit=audit,
        clock=FixedClock(),
    )

    result = adapter.generate(prompt())

    assert result.status is LLMGenerationStatus.SUCCEEDED
    assert result.require_output() == {
        "outcome": "MANUAL_REVIEW",
        "reason": "Supplier timing evidence is inconclusive.",
    }
    request = transport.requests[0]
    assert request["model"] == "gpt-5.6-luna"
    assert request["store"] is False
    text = cast(dict[str, Any], request["text"])
    assert text == {
        "format": {
            "type": "json_schema",
            "name": "scenario_a_recommendation_v1",
            "schema": text["format"]["schema"],
            "strict": True,
        }
    }
    input_items = cast(list[dict[str, Any]], request["input"])
    content_items = cast(list[dict[str, Any]], input_items[0]["content"])
    payload = json.loads(cast(str, content_items[0]["text"]))
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
    assert API_KEY not in json.dumps(request)
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.event_type == "llm.completed"
    assert event.run_id == RunId("run-openai-adapter")
    assert event.actor_id == UserId("00000000-0000-0000-0000-000000000001")
    assert event.attention_id == AttentionId("00000000-0000-0000-0000-000000000601")
    assert event.evidence_ids == (EvidenceId("erp:inventory:inventory-x"),)
    assert event.payload == {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "status": "succeeded",
        "response_schema": "scenario_a_recommendation:v1",
    }
    assert API_KEY not in repr(event)


def test_openai_adapter_uses_the_declared_scenario_b_schema() -> None:
    """The common adapter selects and validates the other application-owned recommendation contract."""
    from enterprise_agent.adapters.openai import OpenAIResponsesAdapter

    transport = RecordingTransport(
        completed_response(
            {
                "outcome": "MANUAL_REVIEW",
                "reason": "Quality evidence needs a human decision.",
            }
        )
    )
    scenario_b_prompt = replace(prompt(), response_schema="scenario_b_recommendation:v1")
    result = OpenAIResponsesAdapter(
        api_key=API_KEY,
        model="gpt-5.6-luna",
        transport=transport,
        audit=RecordingAudit(),
        clock=FixedClock(),
    ).generate(scenario_b_prompt)

    text = cast(dict[str, Any], transport.requests[0]["text"])
    request_format = cast(dict[str, Any], text["format"])
    schema = cast(dict[str, Any], request_format["schema"])
    assert result.status is LLMGenerationStatus.SUCCEEDED
    assert request_format["name"] == "scenario_b_recommendation_v1"
    assert schema["discriminator"]["propertyName"] == "outcome"
    assert {"ManualReviewRecommendation", "ReallocateAndNotifyRecommendation"} <= schema[
        "$defs"
    ].keys()
    reallocate_lot = schema["$defs"]["ReallocateLotInput"]
    assert set(reallocate_lot["required"]) == set(reallocate_lot["properties"])
    assert {"type": "null"} in reallocate_lot["properties"]["from_production_order_id"]["anyOf"]


def test_openai_adapter_rejects_an_undeclared_response_schema_without_calling_the_provider() -> (
    None
):
    """An unowned schema cannot silently reach the provider or become a fallback recommendation."""
    from enterprise_agent.adapters.openai import OpenAIResponsesAdapter

    transport = RecordingTransport(
        completed_response({"outcome": "NO_ACTION", "rationale": "ignored"})
    )
    audit = RecordingAudit()
    result = OpenAIResponsesAdapter(
        api_key=API_KEY,
        model="gpt-5.6-luna",
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
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "Cannot comply."}],
                    }
                ],
            },
            LLMGenerationStatus.REFUSAL,
        ),
        (["not a response object"], LLMGenerationStatus.INVALID_RESPONSE),
        ({"status": "completed"}, LLMGenerationStatus.INVALID_RESPONSE),
        (
            {"status": "completed", "output": [None]},
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (
            {
                "status": "completed",
                "output": [{"type": "message", "content": "not a content list"}],
            },
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (
            {
                "status": "completed",
                "output": [{"type": "message", "content": [None]}],
            },
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text"}],
                    }
                ],
            },
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        ({"status": "completed", "output": []}, LLMGenerationStatus.INVALID_RESPONSE),
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "not-json"}],
                    }
                ],
            },
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        (
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "[]"}],
                    }
                ],
            },
            LLMGenerationStatus.INVALID_RESPONSE,
        ),
        ({"status": "in_progress", "output": []}, LLMGenerationStatus.PROVIDER_FAILURE),
        (TimeoutError("provider timed out"), LLMGenerationStatus.TIMEOUT),
        (OSError("provider unavailable"), LLMGenerationStatus.PROVIDER_FAILURE),
    ),
)
def test_openai_adapter_normalizes_invalid_refused_timeout_and_provider_failures(
    response: object,
    expected_status: LLMGenerationStatus,
) -> None:
    """Provider-specific failure details do not become structured output, errors, or audit payload."""
    from enterprise_agent.adapters.openai import OpenAIResponsesAdapter

    audit = RecordingAudit()
    result = OpenAIResponsesAdapter(
        api_key=API_KEY,
        model="gpt-5.6-luna",
        transport=RecordingTransport(response),
        audit=audit,
        clock=FixedClock(),
    ).generate(prompt())

    assert result.status is expected_status
    assert result.output is None
    assert audit.events[0].payload == {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "status": expected_status.value,
        "response_schema": "scenario_a_recommendation:v1",
    }
    assert audit.events[0].failure_category == expected_status.value
    assert "provider timed out" not in repr(audit.events[0])
    assert "provider unavailable" not in repr(audit.events[0])


def test_urllib_transport_posts_json_to_the_responses_endpoint_with_the_configured_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The concrete transport is ready for a selected OpenAI profile without an SDK dependency."""
    from enterprise_agent.adapters import openai

    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"status":"completed","output":[]}'
    captured: dict[str, object] = {}

    def fake_urlopen(request: object, *, timeout: float) -> MagicMock:
        captured["request"] = request
        captured["timeout"] = timeout
        return response

    monkeypatch.setattr(openai, "urlopen", fake_urlopen)
    transport = openai.UrllibOpenAIResponsesTransport(api_key=API_KEY, timeout_seconds=12.5)

    raw_response = transport.create({"model": "gpt-5.6-luna", "store": False})

    request = cast(Request, captured["request"])
    assert raw_response == {"status": "completed", "output": []}
    assert request.full_url == "https://api.openai.com/v1/responses"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {API_KEY}"
    assert json.loads(cast(bytes, request.data)) == {"model": "gpt-5.6-luna", "store": False}
    assert captured["timeout"] == 12.5


def test_openai_adapter_and_transport_reject_invalid_local_configuration_or_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid local settings and non-object provider JSON fail before producing a recommendation."""
    from enterprise_agent.adapters import openai

    with pytest.raises(ValueError, match="API key is required"):
        openai.UrllibOpenAIResponsesTransport(api_key=" ")
    with pytest.raises(ValueError, match="timeout must be positive"):
        openai.UrllibOpenAIResponsesTransport(api_key=API_KEY, timeout_seconds=0)
    with pytest.raises(ValueError, match="model is required"):
        openai.OpenAIResponsesAdapter(
            api_key=API_KEY,
            model=" ",
            audit=RecordingAudit(),
            clock=FixedClock(),
        )

    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"[]"
    monkeypatch.setattr(openai, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(TypeError, match="JSON object"):
        openai.UrllibOpenAIResponsesTransport(api_key=API_KEY).create({"model": "gpt-5.6-luna"})
