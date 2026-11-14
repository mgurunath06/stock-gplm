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
    VerifiedHistoricalDataset,
)
from .runner import BacktestSettings, compare_runs, run_historical_backtest

__all__ = [
    "BacktestSettings", "DataEligibilityDecision", "DataEligibilityRequest", "DecisionStatus",
    "EligibilityEvidence", "EligibilityFailure", "EligibilityMode", "HistoricalDataAdapter",
    "ReferenceEvidence", "VerifiedHistoricalDataset", "compare_runs", "load_reference_evidence",
    "run_historical_backtest",
]
