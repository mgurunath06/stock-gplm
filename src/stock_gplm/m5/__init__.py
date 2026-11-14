"""M5 manifest-verified historical NSE data adapter and real-data strategy backtesting.

Research only: no orders, broker routing, credentials, collection or network use.
"""

from .adapter import HistoricalDataAdapter
from .evidence import ReferenceEvidence, load_reference_evidence
from .models import (
    DataEligibilityDecision,
    DataEligibilityRequest,
    DecisionStatus,
    EligibilityEvidence,
    EligibilityFailure,
    EligibilityMode,
    UniverseAssessment,
    UniverseRequest,
    VerifiedHistoricalDataset,
)
from .run_store import RunIntegrityError, RunNotFound, VerifiedRun, compare_runs, load_run
from .runner import BacktestSettings, run_historical_backtest

__all__ = [
    "BacktestSettings", "DataEligibilityDecision", "DataEligibilityRequest", "DecisionStatus",
    "EligibilityEvidence", "EligibilityFailure", "EligibilityMode", "HistoricalDataAdapter",
    "ReferenceEvidence", "RunIntegrityError", "RunNotFound", "UniverseAssessment", "UniverseRequest",
    "VerifiedHistoricalDataset", "VerifiedRun", "compare_runs", "load_reference_evidence", "load_run",
    "run_historical_backtest",
]
