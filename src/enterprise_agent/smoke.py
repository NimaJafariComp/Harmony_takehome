"""Manual, fixed-input LLM smoke probe with no business-system dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from enterprise_agent.adapters import (
    ClaudeMessagesAdapter,
    OpenAIResponsesAdapter,
    OpenRouterChatCompletionsAdapter,
)
from enterprise_agent.config import ProviderConfiguration
from enterprise_agent.domain import (
    ActorContext,
    AttentionId,
    AttentionItem,
    AttentionStatus,
    AuditEvent,
    RunId,
    UserId,
)
from enterprise_agent.ports import (
    LLMGenerationResult,
    LLMMessage,
    LLMPort,
    PromptEnvelope,
)

_SMOKE_NOW: Final = datetime(2026, 8, 26, 12, tzinfo=UTC)
FIXED_SMOKE_MESSAGE: Final = (
    'Return only JSON with outcome "MANUAL_REVIEW" and reason '
    '"Non-sensitive structured-output smoke probe completed."'
)


@dataclass
class _SmokeAudit:
    """Keep adapter metadata in memory so the manual probe never writes an audit store."""

    events: list[AuditEvent] = field(default_factory=list)

    def append(self, event: AuditEvent) -> None:
        """Accept the adapter's sanitized metadata event without persistence."""
        self.events.append(event)

    def events_for_run(self, run_id: RunId) -> tuple[AuditEvent, ...]:
        """Satisfy the audit protocol without reading an external system."""
        return tuple(event for event in self.events if event.run_id == run_id)


class _SmokeClock:
    """Provide a fixed timestamp so the probe has no database or wall-clock dependency."""

    def now(self) -> datetime:
        """Return the deterministic smoke-probe instant."""
        return _SMOKE_NOW


def smoke_prompt() -> PromptEnvelope:
    """Return the static no-evidence probe sent by every manual provider smoke command."""
    return PromptEnvelope(
        run_id=RunId("llm-smoke-v1"),
        actor=ActorContext(
            user_id=UserId("llm-smoke-actor"),
            role="llm_smoke_probe",
            scopes=frozenset(),
            plant_ids=frozenset(),
            backup_approver_id=None,
            approval_limits={},
        ),
        attention=AttentionItem(
            attention_id=AttentionId("llm-smoke-attention"),
            scenario="llm_smoke",
            cause="structured_output_probe",
            dedupe_key="llm-smoke:v1",
            status=AttentionStatus.OPEN,
            created_at=_SMOKE_NOW,
            source_versions={},
        ),
        evidence=(),
        messages=(LLMMessage(role="user", content=FIXED_SMOKE_MESSAGE),),
        purpose="llm_smoke",
        response_schema="scenario_a_recommendation:v1",
    )


def create_smoke_adapter(configuration: ProviderConfiguration) -> LLMPort:
    """Compose exactly one selected live adapter; this is the sole profile-specific smoke branch."""
    audit = _SmokeAudit()
    clock = _SmokeClock()
    match configuration.profile:
        case "openai":
            return OpenAIResponsesAdapter(
                api_key=configuration.api_key,
                model=configuration.model,
                audit=audit,
                clock=clock,
            )
        case "claude":
            return ClaudeMessagesAdapter(
                api_key=configuration.api_key,
                model=configuration.model,
                audit=audit,
                clock=clock,
            )
        case "openrouter":
            return OpenRouterChatCompletionsAdapter(
                api_key=configuration.api_key,
                model=configuration.model,
                audit=audit,
                clock=clock,
            )
        case _:
            raise ValueError(f"unsupported LLM profile: {configuration.profile}")


def run_smoke(configuration: ProviderConfiguration) -> LLMGenerationResult:
    """Call only the configured provider with a fixed prompt and return its sanitized result."""
    return create_smoke_adapter(configuration).generate(smoke_prompt())
