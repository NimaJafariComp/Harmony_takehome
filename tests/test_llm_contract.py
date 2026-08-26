"""Provider-neutral normalized LLM result contracts."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_successful_llm_result_exposes_only_immutable_structured_output() -> None:
    """A selected provider returns one safe structured result without provider-specific types."""
    from enterprise_agent.ports import LLMGenerationResult, LLMGenerationStatus

    result = LLMGenerationResult.succeeded(
        provider="openai",
        model="gpt-5.6-luna",
        output={"outcome": "MANUAL_REVIEW", "reason": "Evidence conflicts."},
    )

    assert result.status is LLMGenerationStatus.SUCCEEDED
    assert result.is_success is True
    assert result.output == {"outcome": "MANUAL_REVIEW", "reason": "Evidence conflicts."}
    assert result.require_output() == result.output
    with pytest.raises(TypeError):
        result.output["outcome"] = "ENTER_WORKFLOW"  # type: ignore[index]


@pytest.mark.parametrize(
    "status",
    (
        "invalid_response",
        "timeout",
        "provider_failure",
        "refusal",
    ),
)
def test_failed_llm_result_normalizes_every_safe_failure_without_raw_output(
    status: str,
) -> None:
    """Malformed, unavailable, and refused provider outcomes never become planner input."""
    from enterprise_agent.ports import LLMGenerationResult, LLMGenerationStatus

    result = LLMGenerationResult.failed(
        provider="openrouter",
        model="nvidia/nemotron-ultra-253b-v1:free",
        status=LLMGenerationStatus(status),
    )

    assert result.status is LLMGenerationStatus(status)
    assert result.is_success is False
    assert result.output is None
    with pytest.raises(ValueError, match=status):
        result.require_output()


def test_llm_result_rejects_mixed_or_incomplete_success_and_failure_states() -> None:
    """Adapters cannot label raw failure data as a structured success or vice versa."""
    from enterprise_agent.ports import LLMGenerationResult, LLMGenerationStatus

    with pytest.raises(ValueError, match="structured output"):
        LLMGenerationResult(
            provider="anthropic",
            model="claude-sonnet",
            status=LLMGenerationStatus.SUCCEEDED,
            output=None,
        )
    with pytest.raises(ValueError, match="must not include output"):
        LLMGenerationResult(
            provider="anthropic",
            model="claude-sonnet",
            status=LLMGenerationStatus.REFUSAL,
            output={"unsafe": "raw provider payload"},
        )
    with pytest.raises(ValueError, match="provider is required"):
        LLMGenerationResult.succeeded(
            provider=" ",
            model="claude-sonnet",
            output={"outcome": "MANUAL_REVIEW"},
        )
