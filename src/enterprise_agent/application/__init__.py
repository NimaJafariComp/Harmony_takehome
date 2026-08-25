"""Application services that compose domain policy with typed ports."""

from .approvals import (
    PendingPlanApproval,
    PlanNotApprovableError,
    ScenarioAApprovalService,
    recompute_plan_hash,
)
from .candidates import (
    SupplierCandidate,
    SupplierCandidateFilter,
    SupplierCandidateResult,
    SupplierExclusion,
    SupplierExclusionReason,
)
from .context import (
    AuthorizedContextBundle,
    MissingScenarioAContextEvidenceError,
    ScenarioAContextAssembler,
    StaleAttentionEvidenceError,
)
from .gate import GateDecision, GateDenialReason, GateStatus, ScenarioAGate
from .planning import (
    EnterWorkflowRecommendation,
    FakeLLMPort,
    InvalidScenarioARecommendationError,
    ManualReviewRecommendation,
    NoActionRecommendation,
    ScenarioARecommendation,
    validate_scenario_a_recommendation,
)
from .stockout import StockoutDetection, StockoutDetector, StockoutRisk

__all__ = [
    "AuthorizedContextBundle",
    "EnterWorkflowRecommendation",
    "FakeLLMPort",
    "GateDecision",
    "GateDenialReason",
    "GateStatus",
    "InvalidScenarioARecommendationError",
    "ManualReviewRecommendation",
    "MissingScenarioAContextEvidenceError",
    "NoActionRecommendation",
    "PendingPlanApproval",
    "PlanNotApprovableError",
    "ScenarioAApprovalService",
    "ScenarioAContextAssembler",
    "ScenarioAGate",
    "ScenarioARecommendation",
    "StaleAttentionEvidenceError",
    "StockoutDetection",
    "StockoutDetector",
    "StockoutRisk",
    "SupplierCandidate",
    "SupplierCandidateFilter",
    "SupplierCandidateResult",
    "SupplierExclusion",
    "SupplierExclusionReason",
    "recompute_plan_hash",
    "validate_scenario_a_recommendation",
]
