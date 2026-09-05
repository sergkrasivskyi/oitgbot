"""Pure future-report eligibility, ranking, and NEW semantics for 15m OI."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .oi_anomaly_15m import AnomalyResult, Observation, utc

DEFAULT_ELIGIBILITY_THRESHOLD_PCT = 1.0
DEFAULT_NEW_LOOKBACK_HOURS = 12
EXPECTED_INTERVALS_PER_HOUR = 4


def _validate_threshold(threshold_pct: float) -> None:
    if not math.isfinite(threshold_pct) or threshold_pct <= 0:
        raise ValueError("eligibility threshold must be finite and > 0")


def _expected_interval_count(lookback_hours: float) -> int:
    """Count aligned prior interval starts inside a possibly fractional lookback."""
    return math.floor(lookback_hours * EXPECTED_INTERVALS_PER_HOUR + 1e-12)


def _validate_lookback(lookback_hours: float) -> None:
    if not math.isfinite(lookback_hours) or lookback_hours <= 0:
        raise ValueError("new lookback hours must be finite and > 0")


def is_eligible_observation(
    observation: Observation,
    *,
    threshold_pct: float = DEFAULT_ELIGIBILITY_THRESHOLD_PCT,
) -> tuple[bool, str]:
    """Return future-v1 OI eligibility without considering price or Z."""
    _validate_threshold(threshold_pct)
    value = observation.oi_change_pct
    if not observation.is_valid:
        return False, observation.invalid_reason or "invalid_current_observation"
    if value is None or not math.isfinite(value):
        return False, "invalid_oi_change_pct"
    if value < threshold_pct:
        return False, "oi_change_below_threshold"
    return True, "oi_change_meets_threshold"


@dataclass(frozen=True, slots=True)
class NewHistoryStatus:
    is_new: bool | None
    new_status_reason: str
    new_lookback_hours: float
    previous_eligible_count: int
    last_eligible_interval_start_utc: datetime | None
    history_valid_interval_count: int
    history_expected_interval_count: int
    history_coverage_ratio: float


class EligibilityHistory:
    """Mutable in-memory index reconstructible from aligned observations.

    Valid observations are retained independently of their eligibility so coverage
    diagnostics never mistake an incomplete history for a complete quiet period.
    """

    def __init__(
        self,
        *,
        eligibility_threshold_pct: float = DEFAULT_ELIGIBILITY_THRESHOLD_PCT,
        lookback_hours: float = DEFAULT_NEW_LOOKBACK_HOURS,
    ) -> None:
        _validate_threshold(eligibility_threshold_pct)
        _validate_lookback(lookback_hours)
        self.eligibility_threshold_pct = eligibility_threshold_pct
        self.lookback_hours = lookback_hours
        self._valid_starts: dict[str, set[datetime]] = defaultdict(set)
        self._eligible_starts: dict[str, set[datetime]] = defaultdict(set)

    @classmethod
    def reconstruct(
        cls,
        observations: Iterable[Observation],
        *,
        eligibility_threshold_pct: float = DEFAULT_ELIGIBILITY_THRESHOLD_PCT,
        lookback_hours: float = DEFAULT_NEW_LOOKBACK_HOURS,
    ) -> EligibilityHistory:
        history = cls(
            eligibility_threshold_pct=eligibility_threshold_pct,
            lookback_hours=lookback_hours,
        )
        for observation in observations:
            history.register(observation)
        return history

    def register(self, observation: Observation) -> None:
        """Register one completed aligned observation; invalid observations add no data."""
        if not observation.is_valid:
            return
        start = utc(observation.interval_start_utc)
        self._valid_starts[observation.symbol].add(start)
        if is_eligible_observation(
            observation, threshold_pct=self.eligibility_threshold_pct
        )[0]:
            self._eligible_starts[observation.symbol].add(start)

    def prune(self, reference_interval_start_utc: datetime) -> None:
        cutoff = utc(reference_interval_start_utc) - timedelta(
            hours=self.lookback_hours
        )
        for values in (self._valid_starts, self._eligible_starts):
            for symbol in tuple(values):
                values[symbol].difference_update(
                    [start for start in values[symbol] if start < cutoff]
                )
                if not values[symbol]:
                    del values[symbol]

    def status(
        self, symbol: str, current_interval_start_utc: datetime
    ) -> NewHistoryStatus:
        current = utc(current_interval_start_utc)
        cutoff = current - timedelta(hours=self.lookback_hours)
        valid = sorted(
            start
            for start in self._valid_starts.get(symbol, ())
            if cutoff <= start < current
        )
        eligible = sorted(
            start
            for start in self._eligible_starts.get(symbol, ())
            if cutoff <= start < current
        )
        expected = _expected_interval_count(self.lookback_hours)
        coverage = len(valid) / expected

        if eligible:
            return NewHistoryStatus(
                False,
                "previous_eligible_interval",
                self.lookback_hours,
                len(eligible),
                eligible[-1],
                len(valid),
                expected,
                coverage,
            )
        complete = len(valid) == expected
        return NewHistoryStatus(
            True if complete else None,
            "no_previous_eligible_interval" if complete else "incomplete_new_history",
            self.lookback_hours,
            0,
            None,
            len(valid),
            expected,
            coverage,
        )


@dataclass(frozen=True, slots=True)
class OIAnomaly15mCandidate:
    """Future-v1 decision result; `anomaly` retains all Task 23 diagnostics."""

    anomaly: AnomalyResult
    is_eligible: bool
    eligibility_threshold_pct: float
    eligibility_reason: str
    rank: int | None
    is_new: bool | None
    new_status_reason: str
    new_lookback_hours: float
    previous_eligible_count: int
    last_eligible_interval_start_utc: datetime | None
    history_valid_interval_count: int
    history_expected_interval_count: int
    history_coverage_ratio: float

    @property
    def observation(self) -> Observation:
        return self.anomaly.observation

    @property
    def symbol(self) -> str:
        return self.observation.symbol

    @property
    def interval_start_utc(self) -> datetime:
        return self.observation.interval_start_utc

    @property
    def interval_end_utc(self) -> datetime:
        return self.observation.interval_end_utc

    @property
    def oi_change_pct(self) -> float | None:
        return self.observation.oi_change_pct

    @property
    def price_change_pct(self) -> float | None:
        return self.observation.price_change_pct

    @property
    def z_score(self) -> float | None:
        return self.anomaly.z_score

    @property
    def robust_z_score(self) -> float | None:
        return self.anomaly.robust_z_score

    @property
    def percentile(self) -> float | None:
        return self.anomaly.percentile

    @property
    def baseline_count(self) -> int:
        return self.anomaly.baseline_count


def _ranking_key(candidate: OIAnomaly15mCandidate) -> tuple[bool, float, float, str]:
    z = candidate.z_score
    finite_z = z is not None and math.isfinite(z)
    oi = candidate.oi_change_pct
    return (
        not finite_z,
        -z if finite_z else 0.0,
        -(oi if oi is not None and math.isfinite(oi) else -math.inf),
        candidate.symbol,
    )


def build_candidates(
    anomalies: Iterable[AnomalyResult],
    history: EligibilityHistory,
    *,
    eligibility_threshold_pct: float = DEFAULT_ELIGIBILITY_THRESHOLD_PCT,
) -> tuple[OIAnomaly15mCandidate, ...]:
    """Build deterministic candidates without registering current observations.

    The caller registers newly closed observations after this decision, which
    prevents the current interval from being considered its own prior history.
    """
    _validate_threshold(eligibility_threshold_pct)
    provisional: list[OIAnomaly15mCandidate] = []
    for anomaly in anomalies:
        eligible, reason = is_eligible_observation(
            anomaly.observation, threshold_pct=eligibility_threshold_pct
        )
        if eligible:
            new = history.status(
                anomaly.observation.symbol, anomaly.observation.interval_start_utc
            )
        else:
            new = NewHistoryStatus(
                False,
                "current_not_eligible",
                history.lookback_hours,
                0,
                None,
                0,
                _expected_interval_count(history.lookback_hours),
                0.0,
            )
        provisional.append(
            OIAnomaly15mCandidate(
                anomaly,
                eligible,
                eligibility_threshold_pct,
                reason,
                None,
                new.is_new,
                new.new_status_reason,
                new.new_lookback_hours,
                new.previous_eligible_count,
                new.last_eligible_interval_start_utc,
                new.history_valid_interval_count,
                new.history_expected_interval_count,
                new.history_coverage_ratio,
            )
        )
    eligible = sorted(
        (item for item in provisional if item.is_eligible), key=_ranking_key
    )
    ranks = {id(candidate): rank for rank, candidate in enumerate(eligible, start=1)}
    return tuple(
        OIAnomaly15mCandidate(
            candidate.anomaly,
            candidate.is_eligible,
            candidate.eligibility_threshold_pct,
            candidate.eligibility_reason,
            ranks.get(id(candidate)),
            candidate.is_new,
            candidate.new_status_reason,
            candidate.new_lookback_hours,
            candidate.previous_eligible_count,
            candidate.last_eligible_interval_start_utc,
            candidate.history_valid_interval_count,
            candidate.history_expected_interval_count,
            candidate.history_coverage_ratio,
        )
        for candidate in sorted(
            provisional, key=lambda item: (not item.is_eligible, _ranking_key(item))
        )
    )
