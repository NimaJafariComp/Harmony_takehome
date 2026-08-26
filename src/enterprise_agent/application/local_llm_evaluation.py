"""Explicit no-write live-LLM evaluation service for the optional local Demo page."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from enterprise_agent.application.llm_evaluation import (
    EvaluationCaseSelectionError,
    LLMEvaluationReport,
    evaluate_cases,
    evaluation_cases,
    select_evaluation_cases,
)
from enterprise_agent.config import ProviderConfiguration
from enterprise_agent.ports import LLMPort
from enterprise_agent.smoke import create_no_write_adapter


class LocalLLMEvaluationSelectionError(ValueError):
    """Raised before a provider call for an unknown local profile or non-fixed evaluation case."""


class LocalLLMEvaluationUnavailableError(RuntimeError):
    """Raised when the configured no-write adapter cannot be constructed for evaluation."""


@dataclass(frozen=True, slots=True)
class LLMEvaluationProfile:
    """One complete locally configured profile rendered without its credential."""

    profile: str
    model: str


@dataclass(frozen=True, slots=True)
class LLMEvaluationCaseOption:
    """One fixed synthetic case that bounds a manually authorized live request."""

    case_id: str
    scenario: str
    title: str


@dataclass(frozen=True, slots=True)
class LLMEvaluationAvailability:
    """Safe local presentation state for the no-write evaluation lane."""

    can_evaluate: bool
    profiles: tuple[LLMEvaluationProfile, ...]
    cases: tuple[LLMEvaluationCaseOption, ...]


@dataclass(frozen=True, slots=True)
class LLMEvaluationReceipt:
    """A selected profile/model and scalar-only report; no prompt, credential, or output payload."""

    profile: str
    model: str
    case_id: str
    case_title: str
    report: LLMEvaluationReport


class LocalLLMEvaluationPort:
    """Browser-facing evaluation contract that cannot name a key, arbitrary model, or prompt."""

    def availability(self) -> LLMEvaluationAvailability:
        """List complete local profiles and fixed cases without contacting a provider."""
        raise NotImplementedError

    def evaluate(self, *, profile_id: str, case_id: str) -> LLMEvaluationReceipt:
        """Call exactly one configured provider with one fixed synthetic no-write case."""
        raise NotImplementedError


@dataclass(slots=True)
class LocalLLMEvaluationService(LocalLLMEvaluationPort):
    """Create a single in-memory-audit adapter only after explicit profile/case selection."""

    configurations: tuple[ProviderConfiguration, ...]
    adapter_factory: Callable[[ProviderConfiguration], LLMPort] = create_no_write_adapter

    def __post_init__(self) -> None:
        """Reject duplicate profile configuration so profile selection is always unambiguous."""
        profile_ids = tuple(configuration.profile for configuration in self.configurations)
        if not profile_ids or len(set(profile_ids)) != len(profile_ids):
            raise ValueError("local LLM evaluation requires one configuration per profile")

    def availability(self) -> LLMEvaluationAvailability:
        """Expose only profile/model labels and the reviewed synthetic case catalogue."""
        return LLMEvaluationAvailability(
            can_evaluate=True,
            profiles=tuple(
                LLMEvaluationProfile(profile=item.profile, model=item.model)
                for item in self.configurations
            ),
            cases=tuple(
                LLMEvaluationCaseOption(
                    case_id=item.case_id,
                    scenario=item.scenario,
                    title=item.title,
                )
                for item in evaluation_cases()
            ),
        )

    def evaluate(self, *, profile_id: str, case_id: str) -> LLMEvaluationReceipt:
        """Run one fixed synthetic case with exactly one named local profile and no persistence."""
        configuration = next(
            (item for item in self.configurations if item.profile == profile_id.strip().lower()),
            None,
        )
        if configuration is None:
            raise LocalLLMEvaluationSelectionError("unknown configured profile")
        try:
            selected_case = select_evaluation_cases((case_id,), include_all=False)[0]
        except EvaluationCaseSelectionError as error:
            raise LocalLLMEvaluationSelectionError(str(error)) from error
        try:
            report = evaluate_cases((selected_case,), self.adapter_factory(configuration))
        except ValueError as error:
            raise LocalLLMEvaluationUnavailableError(
                "no-write LLM evaluation is unavailable"
            ) from error
        return LLMEvaluationReceipt(
            profile=configuration.profile,
            model=configuration.model,
            case_id=selected_case.case_id,
            case_title=selected_case.title,
            report=report,
        )


@dataclass(frozen=True, slots=True)
class UnconfiguredLocalLLMEvaluationService(LocalLLMEvaluationPort):
    """Fail closed unless at least one complete local provider profile is configured."""

    def availability(self) -> LLMEvaluationAvailability:
        """Keep the evaluation explanation visible while withholding form choices and provider access."""
        return LLMEvaluationAvailability(can_evaluate=False, profiles=(), cases=())

    def evaluate(self, *, profile_id: str, case_id: str) -> LLMEvaluationReceipt:
        """Refuse an evaluation that lacks a complete local provider configuration."""
        del profile_id, case_id
        raise LocalLLMEvaluationUnavailableError("no configured local profile")
