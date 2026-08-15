from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from oitgbot.models import BinanceRateLimit

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = {
    "SECOND": 1,
    "MINUTE": 60,
    "HOUR": 3_600,
    "DAY": 86_400,
}


class BudgetState(str, Enum):
    SAFE = "SAFE"
    PRESSURE = "PRESSURE"
    UNSAFE = "UNSAFE"


@dataclass(frozen=True, slots=True)
class CadenceEstimate:
    symbols: int
    cadence_seconds: float
    cycles_per_minute: float
    oi_calls_per_minute: float
    base_request_weight_per_minute: float


@dataclass(frozen=True, slots=True)
class RateWindowProjection:
    interval: str
    interval_num: int
    interval_seconds: int
    runtime_limit: int
    usable_limit: float
    projected_weight: float
    projected_fraction: float
    state: BudgetState


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    state: BudgetState
    reason: str | None
    symbols: int
    cadence_seconds: float
    base_weight_per_cycle: int
    retry_reserve_per_cycle: int
    other_rest_reserve_per_minute: float
    projected_collector_weight_per_minute: float
    projections: tuple[RateWindowProjection, ...]


def estimate_cadence(symbols: int, cadence_seconds: float) -> CadenceEstimate:
    if isinstance(symbols, bool) or not isinstance(symbols, int) or symbols < 0:
        raise ValueError("symbols must be a non-negative integer")
    if (
        isinstance(cadence_seconds, bool)
        or not isinstance(cadence_seconds, (int, float))
        or cadence_seconds <= 0
        or not math.isfinite(cadence_seconds)
    ):
        raise ValueError("cadence_seconds must be finite and positive")

    cycles_per_minute = 60.0 / cadence_seconds
    oi_calls_per_minute = symbols * cycles_per_minute
    return CadenceEstimate(
        symbols=symbols,
        cadence_seconds=float(cadence_seconds),
        cycles_per_minute=cycles_per_minute,
        oi_calls_per_minute=oi_calls_per_minute,
        base_request_weight_per_minute=oi_calls_per_minute,
    )


class RateLimitBudget:
    """Project collector load against every runtime REQUEST_WEIGHT window."""

    def __init__(
        self,
        rate_limits: Iterable[BinanceRateLimit],
        *,
        usable_fraction: float = 0.70,
        pressure_fraction_of_usable: float = 0.85,
        retry_allowance_ratio: float = 0.05,
        other_rest_reserve_per_minute: float = 0.0,
    ) -> None:
        self._validate_fraction(usable_fraction, "usable_fraction", allow_zero=False)
        self._validate_fraction(
            pressure_fraction_of_usable,
            "pressure_fraction_of_usable",
            allow_zero=False,
        )
        self._validate_fraction(
            retry_allowance_ratio,
            "retry_allowance_ratio",
            allow_zero=True,
        )
        if (
            isinstance(other_rest_reserve_per_minute, bool)
            or not isinstance(other_rest_reserve_per_minute, (int, float))
            or other_rest_reserve_per_minute < 0
            or not math.isfinite(other_rest_reserve_per_minute)
        ):
            raise ValueError(
                "other_rest_reserve_per_minute must be finite and non-negative"
            )

        all_limits = tuple(rate_limits)
        if not all(isinstance(limit, BinanceRateLimit) for limit in all_limits):
            raise TypeError("rate_limits must contain BinanceRateLimit values")

        self.request_weight_limits = tuple(
            limit
            for limit in all_limits
            if limit.rate_limit_type == "REQUEST_WEIGHT"
        )
        self.usable_fraction = float(usable_fraction)
        self.pressure_fraction_of_usable = float(pressure_fraction_of_usable)
        self.retry_allowance_ratio = float(retry_allowance_ratio)
        self.other_rest_reserve_per_minute = float(
            other_rest_reserve_per_minute
        )

    def evaluate_cycle(
        self,
        symbols: int,
        cadence_seconds: float,
    ) -> BudgetDecision:
        cadence = estimate_cadence(symbols, cadence_seconds)
        retry_reserve = math.ceil(symbols * self.retry_allowance_ratio)
        projected_per_cycle = symbols + retry_reserve
        projected_per_minute = (
            projected_per_cycle * cadence.cycles_per_minute
        )

        if not self.request_weight_limits:
            decision = BudgetDecision(
                state=BudgetState.UNSAFE,
                reason="missing_request_weight_limits",
                symbols=symbols,
                cadence_seconds=float(cadence_seconds),
                base_weight_per_cycle=symbols,
                retry_reserve_per_cycle=retry_reserve,
                other_rest_reserve_per_minute=self.other_rest_reserve_per_minute,
                projected_collector_weight_per_minute=projected_per_minute,
                projections=(),
            )
            self._log_decision(decision)
            return decision

        projections = tuple(
            self._project_window(
                limit,
                cadence_seconds=float(cadence_seconds),
                projected_per_cycle=projected_per_cycle,
            )
            for limit in self.request_weight_limits
        )
        state = max(
            (projection.state for projection in projections),
            key=self._state_rank,
        )
        decision = BudgetDecision(
            state=state,
            reason="projected_usage_exceeds_usable_budget"
            if state is BudgetState.UNSAFE
            else None,
            symbols=symbols,
            cadence_seconds=float(cadence_seconds),
            base_weight_per_cycle=symbols,
            retry_reserve_per_cycle=retry_reserve,
            other_rest_reserve_per_minute=self.other_rest_reserve_per_minute,
            projected_collector_weight_per_minute=projected_per_minute,
            projections=projections,
        )
        self._log_decision(decision)
        return decision

    def cadence_estimate(
        self, symbols: int, cadence_seconds: float
    ) -> CadenceEstimate:
        return estimate_cadence(symbols, cadence_seconds)

    def _project_window(
        self,
        limit: BinanceRateLimit,
        *,
        cadence_seconds: float,
        projected_per_cycle: int,
    ) -> RateWindowProjection:
        interval_unit = _INTERVAL_SECONDS.get(limit.interval)
        if interval_unit is None:
            raise ValueError(f"unsupported rate-limit interval: {limit.interval}")
        interval_seconds = interval_unit * limit.interval_num
        cycles_in_window = max(1.0, interval_seconds / cadence_seconds)
        collector_weight = projected_per_cycle * cycles_in_window
        other_weight = (
            self.other_rest_reserve_per_minute * interval_seconds / 60.0
        )
        projected_weight = collector_weight + other_weight
        usable_limit = limit.limit * self.usable_fraction
        pressure_boundary = usable_limit * self.pressure_fraction_of_usable

        if projected_weight > usable_limit:
            state = BudgetState.UNSAFE
        elif projected_weight > pressure_boundary:
            state = BudgetState.PRESSURE
        else:
            state = BudgetState.SAFE

        return RateWindowProjection(
            interval=limit.interval,
            interval_num=limit.interval_num,
            interval_seconds=interval_seconds,
            runtime_limit=limit.limit,
            usable_limit=usable_limit,
            projected_weight=projected_weight,
            projected_fraction=projected_weight / limit.limit,
            state=state,
        )

    def _log_decision(self, decision: BudgetDecision) -> None:
        limits = ",".join(
            f"{item.interval_num}{item.interval}:{item.runtime_limit}"
            for item in decision.projections
        ) or "missing"
        projected = ",".join(
            f"{item.interval_num}{item.interval}:{item.projected_weight:.2f}"
            for item in decision.projections
        ) or "unavailable"
        logger.info(
            "RATE_LIMIT_BUDGET limits=%s projected=%s usable_fraction=%.2f "
            "state=%s",
            limits,
            projected,
            self.usable_fraction,
            decision.state.value,
        )

    @staticmethod
    def _state_rank(state: BudgetState) -> int:
        return {
            BudgetState.SAFE: 0,
            BudgetState.PRESSURE: 1,
            BudgetState.UNSAFE: 2,
        }[state]

    @staticmethod
    def _validate_fraction(
        value: float,
        field_name: str,
        *,
        allow_zero: bool,
    ) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or (value < 0 if allow_zero else value <= 0)
            or value > 1
        ):
            qualifier = (
                "between zero and one"
                if allow_zero
                else "above zero and at most one"
            )
            raise ValueError(f"{field_name} must be {qualifier}")
