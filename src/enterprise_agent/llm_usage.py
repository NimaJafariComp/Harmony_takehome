"""Safe normalized LLM metering, reviewed rate estimates, and immutable-ledger reporting."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from enterprise_agent.domain import AuditEvent
from enterprise_agent.ports import LLMCostSource, LLMUsage

_TOKENS_PER_MILLION = Decimal(1000000)
REVIEWED_RATE_CARD_AS_OF = "2026-08-26"


@dataclass(frozen=True, slots=True)
class ModelRate:
    """One versioned public price card for a reviewed direct-provider model, in USD per million tokens."""

    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    input_includes_cached_tokens: bool


REVIEWED_MODEL_RATES: Mapping[tuple[str, str], ModelRate] = MappingProxyType(
    {
        (
            "openai",
            "gpt-5.6-luna",
        ): ModelRate(
            input_usd_per_million=Decimal("0.20"),
            cached_input_usd_per_million=Decimal("0.02"),
            output_usd_per_million=Decimal("1.20"),
            input_includes_cached_tokens=True,
        ),
        (
            "openai",
            "gpt-5.6-terra",
        ): ModelRate(
            input_usd_per_million=Decimal("2.00"),
            cached_input_usd_per_million=Decimal("0.20"),
            output_usd_per_million=Decimal("12.00"),
            input_includes_cached_tokens=True,
        ),
        (
            "claude",
            "claude-sonnet-5",
        ): ModelRate(
            input_usd_per_million=Decimal("2.00"),
            cached_input_usd_per_million=Decimal("0.20"),
            output_usd_per_million=Decimal("10.00"),
            input_includes_cached_tokens=False,
        ),
        (
            "openrouter",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
        ): ModelRate(
            input_usd_per_million=Decimal(0),
            cached_input_usd_per_million=Decimal(0),
            output_usd_per_million=Decimal(0),
            input_includes_cached_tokens=True,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class LLMUsageLine:
    """One provider/model aggregate reconstructed solely from sanitized immutable audit events."""

    provider: str
    model: str
    request_count: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: Decimal
    estimated_cost_usd: Decimal
    provider_reported_cost_usd: Decimal
    estimated_request_count: int
    provider_reported_request_count: int
    unknown_cost_request_count: int
    unmetered_request_count: int


@dataclass(frozen=True, slots=True)
class LLMUsageSummary:
    """Read-only aggregate suitable for a compact operator-facing CLI report."""

    lines: tuple[LLMUsageLine, ...]
    total_cost_usd: Decimal


def usage_from_response(
    provider: str,
    model: str,
    response: Mapping[str, object],
) -> LLMUsage | None:
    """Extract normal provider metering and return `None` for missing or malformed telemetry.

    This function never preserves the input mapping; it only returns scalar facts needed for audit and
    reporting.  A missing telemetry block cannot alter an otherwise valid provider outcome.
    """
    usage_data = response.get("usage")
    if not isinstance(usage_data, Mapping):
        return None

    if provider == "openrouter":
        input_tokens = _token_count(usage_data.get("prompt_tokens"))
        output_tokens = _token_count(usage_data.get("completion_tokens"))
        cached_input_tokens = (
            _token_count(usage_data.get("prompt_tokens_details", {}).get("cached_tokens"))
            if isinstance(usage_data.get("prompt_tokens_details"), Mapping)
            else 0
        )
    elif provider == "claude":
        input_tokens = _token_count(usage_data.get("input_tokens"))
        output_tokens = _token_count(usage_data.get("output_tokens"))
        cached_input_tokens = _token_count(usage_data.get("cache_read_input_tokens", 0))
    else:
        input_tokens = _token_count(usage_data.get("input_tokens"))
        output_tokens = _token_count(usage_data.get("output_tokens"))
        details = usage_data.get("input_tokens_details")
        cached_input_tokens = (
            _token_count(details.get("cached_tokens", 0)) if isinstance(details, Mapping) else 0
        )

    if input_tokens is None or output_tokens is None or cached_input_tokens is None:
        return None
    total_tokens = _total_token_count(
        usage_data.get("total_tokens"), input_tokens=input_tokens, output_tokens=output_tokens
    )
    if total_tokens is None:
        return None

    reported_cost = _cost(usage_data.get("cost")) if provider == "openrouter" else None
    if reported_cost is not None:
        return LLMUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=reported_cost,
            cost_source=LLMCostSource.PROVIDER_REPORTED,
        )

    estimated_cost = _estimate_cost(
        provider,
        model,
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
    )
    if estimated_cost is None:
        return LLMUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=None,
            cost_source=LLMCostSource.UNAVAILABLE,
        )
    return LLMUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=estimated_cost,
        cost_source=LLMCostSource.ESTIMATED,
    )


def usage_audit_payload(usage: LLMUsage) -> dict[str, object]:
    """Convert normalized scalar metering to JSON-safe audit facts with no response/prompt/key content."""
    payload: dict[str, object] = {
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cost_source": usage.cost_source.value,
    }
    if usage.cost_usd is not None:
        payload["cost_usd"] = str(usage.cost_usd)
    return payload


def summarize_llm_usage(events: Sequence[AuditEvent]) -> LLMUsageSummary:
    """Group only well-formed safe `llm.completed` audit facts, skipping malformed legacy payload fields."""
    grouped: defaultdict[tuple[str, str], _MutableUsageLine] = defaultdict(_MutableUsageLine)
    for event in events:
        if event.event_type != "llm.completed":
            continue
        provider = event.payload.get("provider")
        model = event.payload.get("model")
        if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
            continue
        line = grouped[(provider, model)]
        line.request_count += 1
        usage = _usage_from_audit_payload(event.payload)
        if usage is None:
            line.unmetered_request_count += 1
            continue
        line.input_tokens += usage.input_tokens
        line.cached_input_tokens += usage.cached_input_tokens
        line.output_tokens += usage.output_tokens
        line.total_tokens += usage.total_tokens
        if usage.cost_source is LLMCostSource.ESTIMATED:
            line.estimated_request_count += 1
            if usage.cost_usd is not None:
                line.estimated_cost_usd += usage.cost_usd
        elif usage.cost_source is LLMCostSource.PROVIDER_REPORTED:
            line.provider_reported_request_count += 1
            if usage.cost_usd is not None:
                line.provider_reported_cost_usd += usage.cost_usd
        else:
            line.unknown_cost_request_count += 1
        if usage.cost_usd is not None:
            line.cost_usd += usage.cost_usd

    lines = tuple(
        _usage_line(provider, model, aggregate)
        for (provider, model), aggregate in sorted(grouped.items())
    )
    return LLMUsageSummary(
        lines=lines, total_cost_usd=sum((line.cost_usd for line in lines), Decimal())
    )


def render_llm_usage(summary: LLMUsageSummary) -> str:
    """Render a small read-only report that makes estimated and unknown cost coverage explicit."""
    lines = [
        "LLM usage (immutable audit ledger)",
        f"Direct-provider estimates use reviewed public rates as of {REVIEWED_RATE_CARD_AS_OF}; billing is authoritative.",
    ]
    if not summary.lines:
        return "\n".join((*lines, "No LLM calls have been recorded."))
    for line in summary.lines:
        lines.extend(
            (
                f"{line.provider} / {line.model}: {line.request_count} requests",
                (
                    "  "
                    f"input={line.input_tokens} (cached={line.cached_input_tokens}), "
                    f"output={line.output_tokens}, total={line.total_tokens}"
                ),
            )
        )
        if line.estimated_request_count:
            lines.append(f"  estimated cost: ${line.estimated_cost_usd}")
        if line.provider_reported_request_count:
            lines.append(f"  provider-reported cost: ${line.provider_reported_cost_usd}")
        if line.unknown_cost_request_count:
            lines.append(f"  cost unavailable for {line.unknown_cost_request_count} requests")
        if line.unmetered_request_count:
            lines.append(f"  metering unavailable for {line.unmetered_request_count} requests")
    lines.append(f"Total known cost: ${summary.total_cost_usd}")
    return "\n".join(lines)


@dataclass(slots=True)
class _MutableUsageLine:
    """Internal mutable accumulator never exposed across the application boundary."""

    request_count: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Decimal = Decimal()
    estimated_cost_usd: Decimal = Decimal()
    provider_reported_cost_usd: Decimal = Decimal()
    estimated_request_count: int = 0
    provider_reported_request_count: int = 0
    unknown_cost_request_count: int = 0
    unmetered_request_count: int = 0


def _token_count(value: object) -> int | None:
    """Accept only provider-returned non-boolean non-negative integer counts."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _total_token_count(value: object, *, input_tokens: int, output_tokens: int) -> int | None:
    """Use a provider total when coherent, otherwise derive it only when the provider omitted it."""
    if value is None:
        return input_tokens + output_tokens
    total_tokens = _token_count(value)
    if total_tokens is None or total_tokens < input_tokens + output_tokens:
        return None
    return total_tokens


def _cost(value: object) -> Decimal | None:
    """Parse one finite non-negative provider-reported USD cost without accepting floats or booleans."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() and result >= 0 else None


def _estimate_cost(
    provider: str,
    model: str,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> Decimal | None:
    """Calculate a transparent rate-card estimate or return no cost for an unknown model/rate shape."""
    rate = REVIEWED_MODEL_RATES.get((provider, model))
    if rate is None or (rate.input_includes_cached_tokens and cached_input_tokens > input_tokens):
        return None
    uncached_input_tokens = (
        input_tokens - cached_input_tokens if rate.input_includes_cached_tokens else input_tokens
    )
    return (
        Decimal(uncached_input_tokens) * rate.input_usd_per_million
        + Decimal(cached_input_tokens) * rate.cached_input_usd_per_million
        + Decimal(output_tokens) * rate.output_usd_per_million
    ) / _TOKENS_PER_MILLION


def _usage_from_audit_payload(payload: Mapping[str, object]) -> LLMUsage | None:
    """Reconstruct normalized usage only from the exact scalar audit vocabulary written by adapters."""
    input_tokens = _token_count(payload.get("input_tokens"))
    cached_input_tokens = _token_count(payload.get("cached_input_tokens"))
    output_tokens = _token_count(payload.get("output_tokens"))
    total_tokens = _token_count(payload.get("total_tokens"))
    cost_source_value = payload.get("cost_source")
    if (
        input_tokens is None
        or cached_input_tokens is None
        or output_tokens is None
        or total_tokens is None
        or not isinstance(cost_source_value, str)
    ):
        return None
    try:
        cost_source = LLMCostSource(cost_source_value)
    except ValueError:
        return None
    cost_usd = _cost(payload.get("cost_usd"))
    try:
        return LLMUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            cost_source=cost_source,
        )
    except ValueError:
        return None


def _usage_line(provider: str, model: str, aggregate: _MutableUsageLine) -> LLMUsageLine:
    """Freeze one grouped aggregate before it crosses the reporting boundary."""
    return LLMUsageLine(
        provider=provider,
        model=model,
        request_count=aggregate.request_count,
        input_tokens=aggregate.input_tokens,
        cached_input_tokens=aggregate.cached_input_tokens,
        output_tokens=aggregate.output_tokens,
        total_tokens=aggregate.total_tokens,
        cost_usd=aggregate.cost_usd,
        estimated_cost_usd=aggregate.estimated_cost_usd,
        provider_reported_cost_usd=aggregate.provider_reported_cost_usd,
        estimated_request_count=aggregate.estimated_request_count,
        provider_reported_request_count=aggregate.provider_reported_request_count,
        unknown_cost_request_count=aggregate.unknown_cost_request_count,
        unmetered_request_count=aggregate.unmetered_request_count,
    )
