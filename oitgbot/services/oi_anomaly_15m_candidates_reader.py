"""Read-only offline composition of Task 23 statistics and Task 24 decisions."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .oi_anomaly_15m_candidates import (
    DEFAULT_ELIGIBILITY_THRESHOLD_PCT,
    DEFAULT_NEW_LOOKBACK_HOURS,
    EligibilityHistory,
    OIAnomaly15mCandidate,
    build_candidates,
)
from .oi_anomaly_15m_reader import analyze_database_with_observations


def analyze_candidates_database(
    path: str | Path,
    *,
    as_of: datetime | None = None,
    symbols: tuple[str, ...] = (),
    baseline_days: int = 14,
    min_history: int = 96,
    eligibility_threshold_pct: float = DEFAULT_ELIGIBILITY_THRESHOLD_PCT,
    new_lookback_hours: float = DEFAULT_NEW_LOOKBACK_HOURS,
    now_utc: datetime | None = None,
) -> tuple[datetime | None, tuple[OIAnomaly15mCandidate, ...]]:
    """Use the Task 23 bounded data read; no runtime or network integration."""
    start, anomalies, observations = analyze_database_with_observations(
        path,
        as_of=as_of,
        symbols=symbols,
        baseline_days=baseline_days,
        min_history=min_history,
        now_utc=now_utc,
    )
    if start is None:
        return None, ()
    history = EligibilityHistory.reconstruct(
        observations,
        eligibility_threshold_pct=eligibility_threshold_pct,
        lookback_hours=new_lookback_hours,
    )
    return start, build_candidates(
        anomalies,
        history,
        eligibility_threshold_pct=eligibility_threshold_pct,
    )
