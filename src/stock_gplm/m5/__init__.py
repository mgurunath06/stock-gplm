"""M5 manifest-verified historical NSE data adapter and real-data strategy backtesting.

Research only: no orders, broker routing, credentials, collection or network use.
"""

from .adapter import HistoricalDataAdapter
from .authority import ApprovalTrustPolicy, EvidenceRegistration, load_evidence_registration, load_trust_policy
from .evidence import (
    InstrumentClassification,
    ReferenceEvidence,
    RegisteredCostSchedule,
    load_reference_evidence,
)
from .models import (
    DataEligibilityDecision,
    DataEligibilityRequest,
    DecisionStatus,
    EligibilityEvidence,
    EligibilityFailure,
    EligibilityMode,
    StaleAssessmentError,
    UniverseAssessment,
    UniverseRequest,
    VerifiedHistoricalDataset,
)
from .run_store import RunIntegrityError, RunNotFound, VerifiedRun, compare_runs, load_run
from .runner import BacktestSettings, run_historical_backtest

__all__ = [
    "ApprovalTrustPolicy", "BacktestSettings", "DataEligibilityDecision", "DataEligibilityRequest", "DecisionStatus",
    "EvidenceRegistration",
    "EligibilityEvidence", "EligibilityFailure", "EligibilityMode", "HistoricalDataAdapter",
    "InstrumentClassification", "ReferenceEvidence", "RegisteredCostSchedule", "RunIntegrityError", "RunNotFound",
    "StaleAssessmentError", "UniverseAssessment", "UniverseRequest",
    "VerifiedHistoricalDataset", "VerifiedRun", "compare_runs", "load_evidence_registration",
    "load_reference_evidence", "load_run", "load_trust_policy",
    "run_historical_backtest",
]
