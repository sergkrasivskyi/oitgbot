from __future__ import annotations

import asyncio
import logging
import math
import statistics
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from oitgbot.models import CurrentOpenInterest, RollingOISample
from oitgbot.services.price_state import PriceStateStore
from oitgbot.services.rate_limit_budget import (
    BudgetDecision,
    BudgetState,
    RateLimitBudget,
)
from oitgbot.services.rolling_oi_store import RollingOIStore

logger = logging.getLogger("oitgbot.rolling.collector")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_utc(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timezone.utc.utcoffset(value)
    ):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CurrentOICycleResult:
    cycle_started_at_utc: datetime
    cycle_finished_at_utc: datetime
    elapsed_seconds: float
    symbols_requested: int
    oi_requests_attempted: int
    successful_samples: int
    failed_symbols: int
    future_oi_rejected: int
    old_transaction_time_count: int
    transaction_time_unchanged: int
    price_fresh: int
    price_missing: int
    price_receipt_stale: int
    price_alignment_rejected: int
    samples_inserted: int
    samples_ignored_duplicate_or_out_of_order: int
    timed_out_symbols: int
    http_429_errors: int
    http_418_errors: int
    cycle_timed_out: bool
    cycle_skipped: bool
    skip_reason: str | None
    rate_budget_state: str
    failure_counts: tuple[tuple[str, int], ...]
    transaction_age_min_s: float | None
    transaction_age_median_s: float | None
    transaction_age_p95_s: float | None
    transaction_age_max_s: float | None
    price_receipt_age_max_s: float | None
    price_event_age_abs_max_s: float | None
    price_oi_transaction_skew_abs_max_s: float | None


@dataclass(frozen=True, slots=True)
class _FetchedOI:
    reading: CurrentOpenInterest
    observed_at_utc: datetime


@dataclass(slots=True)
class _CycleStats:
    attempted: int = 0
    completed: int = 0
    successful_samples: int = 0
    future_oi_rejected: int = 0
    old_transaction_time_count: int = 0
    transaction_time_unchanged: int = 0
    price_fresh: int = 0
    price_missing: int = 0
    price_receipt_stale: int = 0
    price_alignment_rejected: int = 0
    samples_inserted: int = 0
    samples_ignored: int = 0
    timed_out_symbols: int = 0
    http_429_errors: int = 0
    http_418_errors: int = 0
    failures: Counter[str] = field(default_factory=Counter)
    transaction_ages: list[float] = field(default_factory=list)
    price_receipt_ages: list[float] = field(default_factory=list)
    price_event_ages_abs: list[float] = field(default_factory=list)
    price_oi_transaction_skews: list[float] = field(default_factory=list)


class CurrentOICollector:
    """Run one bounded, non-overlapping current-OI collection cycle."""

    def __init__(
        self,
        binance_api: Any,
        price_state: PriceStateStore,
        rolling_store: RollingOIStore,
        rate_budget: RateLimitBudget,
        *,
        max_workers: int = 20,
        default_cadence_seconds: float = 30.0,
        cycle_timeout_seconds: float | None = None,
        price_max_age_seconds: float = 5.0,
        max_price_observation_skew_seconds: float = 5.0,
        transaction_age_warning_seconds: float = 60.0,
        future_oi_tolerance_seconds: float = 5.0,
        observation_sink: Callable[[RollingOISample], None] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        if (
            isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or max_workers <= 0
        ):
            raise ValueError("max_workers must be a positive integer")
        self._require_positive_finite(
            default_cadence_seconds, "default_cadence_seconds"
        )
        if cycle_timeout_seconds is not None:
            self._require_positive_finite(
                cycle_timeout_seconds, "cycle_timeout_seconds"
            )
        self._require_non_negative_finite(
            price_max_age_seconds, "price_max_age_seconds"
        )
        self._require_non_negative_finite(
            max_price_observation_skew_seconds,
            "max_price_observation_skew_seconds",
        )
        self._require_non_negative_finite(
            transaction_age_warning_seconds,
            "transaction_age_warning_seconds",
        )
        self._require_non_negative_finite(
            future_oi_tolerance_seconds, "future_oi_tolerance_seconds"
        )

        self.binance_api = binance_api
        self.price_state = price_state
        self.rolling_store = rolling_store
        self.rate_budget = rate_budget
        self.max_workers = max_workers
        self.default_cadence_seconds = float(default_cadence_seconds)
        self.cycle_timeout_seconds = (
            float(cycle_timeout_seconds)
            if cycle_timeout_seconds is not None
            else None
        )
        self.price_max_age_seconds = float(price_max_age_seconds)
        self.max_price_observation_skew_seconds = float(
            max_price_observation_skew_seconds
        )
        self.transaction_age_warning_seconds = float(
            transaction_age_warning_seconds
        )
        self.future_oi_tolerance_seconds = float(
            future_oi_tolerance_seconds
        )
        self._observation_sink = observation_sink
        self._clock = clock
        self._monotonic = monotonic
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="current-oi",
        )
        self._state_lock = threading.RLock()
        self._cycle_active = False
        self._cycle_returned = True
        self._active_request_futures: set[Future[Any]] = set()
        self._closed = False
        self._idle_event = threading.Event()
        self._idle_event.set()

    async def collect_cycle(
        self,
        symbols: Iterable[str] | None = None,
        *,
        cadence_seconds: float | None = None,
        timeout_seconds: float | None = None,
    ) -> CurrentOICycleResult:
        cadence = (
            self.default_cadence_seconds
            if cadence_seconds is None
            else float(cadence_seconds)
        )
        self._require_positive_finite(cadence, "cadence_seconds")
        timeout = self._effective_timeout(cadence, timeout_seconds)
        explicit_symbols = (
            self._normalize_symbols(symbols) if symbols is not None else None
        )
        started_at = _require_utc(self._clock(), "clock")
        started_monotonic = self._monotonic()

        with self._state_lock:
            if self._closed:
                return self._skipped_result(
                    started_at,
                    started_monotonic,
                    len(explicit_symbols or ()),
                    "collector_closed",
                    "NOT_EVALUATED",
                )
            if self._cycle_active:
                return self._skipped_result(
                    started_at,
                    started_monotonic,
                    len(explicit_symbols or ()),
                    "cycle_already_running",
                    "NOT_EVALUATED",
                )
            self._cycle_active = True
            self._cycle_returned = False
            self._idle_event.clear()

        stats = _CycleStats()
        decision: BudgetDecision | None = None
        requested_symbols: tuple[str, ...] = explicit_symbols or ()
        cycle_timed_out = False
        try:
            if explicit_symbols is None:
                try:
                    requested_symbols = await self._discover_symbols(timeout)
                except TimeoutError:
                    stats.failures["symbol_discovery_timeout"] = 1
                    result = self._build_result(
                        started_at,
                        started_monotonic,
                        0,
                        stats,
                        "NOT_EVALUATED",
                        cycle_skipped=True,
                        skip_reason="symbol_discovery_timeout",
                        cycle_timed_out=True,
                    )
                    self._log_summary(result)
                    return result
                except Exception:
                    stats.failures["symbol_discovery_error"] = 1
                    result = self._build_result(
                        started_at,
                        started_monotonic,
                        0,
                        stats,
                        "NOT_EVALUATED",
                        cycle_skipped=True,
                        skip_reason="symbol_discovery_failed",
                        cycle_timed_out=False,
                    )
                    self._log_summary(result)
                    return result
                elapsed = self._monotonic() - started_monotonic
                timeout = max(0.0, timeout - elapsed)
                if timeout <= 0:
                    cycle_timed_out = True

            decision = self.rate_budget.evaluate_cycle(
                len(requested_symbols), cadence
            )
            if decision.state is BudgetState.UNSAFE:
                result = self._build_result(
                    started_at,
                    started_monotonic,
                    len(requested_symbols),
                    stats,
                    decision.state.value,
                    cycle_skipped=True,
                    skip_reason="rate_budget_unsafe",
                    cycle_timed_out=cycle_timed_out,
                )
                self._log_summary(result)
                return result

            if cycle_timed_out:
                stats.timed_out_symbols = len(requested_symbols)
                stats.failures["cycle_timeout"] = len(requested_symbols)
            else:
                cycle_timed_out = await self._collect_symbols(
                    requested_symbols,
                    timeout,
                    stats,
                )

            result = self._build_result(
                started_at,
                started_monotonic,
                len(requested_symbols),
                stats,
                decision.state.value,
                cycle_skipped=False,
                skip_reason=None,
                cycle_timed_out=cycle_timed_out,
            )
            self._log_summary(result)
            return result
        finally:
            with self._state_lock:
                self._cycle_returned = True
                if not self._active_request_futures:
                    self._cycle_active = False
                    self._idle_event.set()

    async def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        await asyncio.to_thread(self._idle_event.wait)
        await asyncio.to_thread(
            self._executor.shutdown,
            wait=True,
            cancel_futures=True,
        )
        with self._state_lock:
            self._active_request_futures.clear()
            self._cycle_active = False
            self._idle_event.set()

    async def __aenter__(self) -> "CurrentOICollector":
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def _discover_symbols(self, timeout: float) -> tuple[str, ...]:
        future = self._submit(self.binance_api.get_perpetual_futures_symbols)
        try:
            symbols = await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=timeout
            )
        except TimeoutError as exc:
            future.cancel()
            raise TimeoutError("symbol discovery timed out") from exc
        return self._normalize_symbols(symbols)

    async def _collect_symbols(
        self,
        symbols: tuple[str, ...],
        timeout: float,
        stats: _CycleStats,
    ) -> bool:
        if not symbols:
            return False
        queue: asyncio.Queue[str] = asyncio.Queue()
        for symbol in symbols:
            queue.put_nowait(symbol)

        workers = [
            asyncio.create_task(self._worker(queue, stats))
            for _ in range(min(self.max_workers, len(symbols)))
        ]
        try:
            done, pending = await asyncio.wait(workers, timeout=timeout)
        except asyncio.CancelledError:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
            stats.timed_out_symbols = len(symbols) - stats.completed
            stats.failures["cycle_timeout"] += stats.timed_out_symbols
            return True

        for task in done:
            task.result()
        return False

    async def _worker(
        self,
        queue: asyncio.Queue[str],
        stats: _CycleStats,
    ) -> None:
        while True:
            try:
                symbol = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            stats.attempted += 1
            future = self._submit(self._fetch_current_oi, symbol)
            try:
                fetched = await asyncio.wrap_future(future)
            except asyncio.CancelledError:
                future.cancel()
                raise
            except Exception as exc:
                stats.completed += 1
                failure_class = self._classify_error(exc)
                stats.failures[failure_class] += 1
                if failure_class == "http_429":
                    stats.http_429_errors += 1
                elif failure_class == "http_418":
                    stats.http_418_errors += 1
                continue

            stats.completed += 1
            self._accept_fetched(symbol, fetched, stats)

    def _fetch_current_oi(self, symbol: str) -> _FetchedOI:
        reading = self.binance_api.get_current_open_interest(symbol)
        observed_at = _require_utc(self._clock(), "clock")
        if not isinstance(reading, CurrentOpenInterest):
            raise ValueError("current OI client returned an unexpected model")
        if reading.symbol != symbol:
            raise ValueError("current OI response symbol does not match request")
        if (
            isinstance(reading.oi_quantity, bool)
            or not isinstance(reading.oi_quantity, (int, float))
            or reading.oi_quantity < 0
            or not math.isfinite(reading.oi_quantity)
        ):
            raise ValueError("current OI model contains an invalid quantity")
        _require_utc(reading.exchange_time, "OI exchange time")
        return _FetchedOI(reading, observed_at)

    def _accept_fetched(
        self,
        symbol: str,
        fetched: _FetchedOI,
        stats: _CycleStats,
    ) -> None:
        try:
            oi_exchange_time = _require_utc(
                fetched.reading.exchange_time, "OI exchange time"
            )
            transaction_age = (
                fetched.observed_at_utc - oi_exchange_time
            ).total_seconds()
            stats.transaction_ages.append(transaction_age)
            if transaction_age < -self.future_oi_tolerance_seconds:
                stats.future_oi_rejected += 1
                stats.failures["future_timestamp"] += 1
                return
            if transaction_age > self.transaction_age_warning_seconds:
                stats.old_transaction_time_count += 1

            previous = self.rolling_store.latest(symbol)
            if (
                previous is not None
                and previous.oi_exchange_time == oi_exchange_time
            ):
                stats.transaction_time_unchanged += 1

            stored_price = self.price_state.get(symbol)
            if stored_price is None:
                stats.price_missing += 1
                mark_price = None
                price_exchange_time = None
            else:
                receipt_age = (
                    fetched.observed_at_utc - stored_price.received_at_utc
                ).total_seconds()
                stats.price_receipt_ages.append(receipt_age)
                if receipt_age < 0 or receipt_age > self.price_max_age_seconds:
                    stats.price_receipt_stale += 1
                    mark_price = None
                    price_exchange_time = None
                else:
                    event_age = (
                        fetched.observed_at_utc - stored_price.exchange_time
                    ).total_seconds()
                    stats.price_event_ages_abs.append(abs(event_age))
                    stats.price_oi_transaction_skews.append(
                        abs(
                            (
                                stored_price.exchange_time - oi_exchange_time
                            ).total_seconds()
                        )
                    )
                    if (
                        abs(event_age)
                        > self.max_price_observation_skew_seconds
                    ):
                        stats.price_alignment_rejected += 1
                        mark_price = None
                        price_exchange_time = None
                    else:
                        stats.price_fresh += 1
                        mark_price = stored_price.mark_price
                        price_exchange_time = stored_price.exchange_time

            sample = RollingOISample(
                symbol=fetched.reading.symbol,
                oi_quantity=fetched.reading.oi_quantity,
                observed_at_utc=fetched.observed_at_utc,
                oi_exchange_time=oi_exchange_time,
                mark_price=mark_price,
                price_exchange_time=price_exchange_time,
            )
            stats.successful_samples += 1
            if self.rolling_store.add(sample):
                stats.samples_inserted += 1
                sink = self._observation_sink
                if sink is not None:
                    try:
                        sink(sample)
                    except Exception:
                        self._observation_sink = None
                        logger.exception(
                            "OI_COLLECTOR_OBSERVER status=disabled reason=observer_failed"
                        )
            else:
                stats.samples_ignored += 1
        except (TypeError, ValueError, OverflowError):
            stats.failures["parse_error"] += 1

    def _submit(self, function: Callable[..., Any], *args: object) -> Future[Any]:
        future = self._executor.submit(function, *args)
        with self._state_lock:
            self._active_request_futures.add(future)
        future.add_done_callback(self._request_finished)
        return future

    def _request_finished(self, future: Future[Any]) -> None:
        with self._state_lock:
            self._active_request_futures.discard(future)
            if self._cycle_returned and not self._active_request_futures:
                self._cycle_active = False
                self._idle_event.set()

    def _effective_timeout(
        self,
        cadence_seconds: float,
        timeout_seconds: float | None,
    ) -> float:
        if timeout_seconds is not None:
            self._require_positive_finite(timeout_seconds, "timeout_seconds")
            return float(timeout_seconds)
        if self.cycle_timeout_seconds is not None:
            return self.cycle_timeout_seconds
        return min(20.0, 0.8 * cadence_seconds)

    @staticmethod
    def _normalize_symbols(symbols: Iterable[str]) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("symbols must contain non-empty strings")
            value = symbol.upper()
            if value not in seen:
                normalized.append(value)
                seen.add(value)
        return tuple(normalized)

    @staticmethod
    def _classify_error(error: Exception) -> str:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
        if status_code == 429:
            return "http_429"
        if status_code == 418:
            return "http_418"
        if isinstance(error, ValueError):
            return "parse_error"
        return "request_error"

    def _build_result(
        self,
        started_at: datetime,
        started_monotonic: float,
        symbols_requested: int,
        stats: _CycleStats,
        budget_state: str,
        *,
        cycle_skipped: bool,
        skip_reason: str | None,
        cycle_timed_out: bool,
    ) -> CurrentOICycleResult:
        finished_at = _require_utc(self._clock(), "clock")
        return CurrentOICycleResult(
            cycle_started_at_utc=started_at,
            cycle_finished_at_utc=finished_at,
            elapsed_seconds=max(0.0, self._monotonic() - started_monotonic),
            symbols_requested=symbols_requested,
            oi_requests_attempted=stats.attempted,
            successful_samples=stats.successful_samples,
            failed_symbols=sum(stats.failures.values()),
            future_oi_rejected=stats.future_oi_rejected,
            old_transaction_time_count=stats.old_transaction_time_count,
            transaction_time_unchanged=stats.transaction_time_unchanged,
            price_fresh=stats.price_fresh,
            price_missing=stats.price_missing,
            price_receipt_stale=stats.price_receipt_stale,
            price_alignment_rejected=stats.price_alignment_rejected,
            samples_inserted=stats.samples_inserted,
            samples_ignored_duplicate_or_out_of_order=stats.samples_ignored,
            timed_out_symbols=stats.timed_out_symbols,
            http_429_errors=stats.http_429_errors,
            http_418_errors=stats.http_418_errors,
            cycle_timed_out=cycle_timed_out,
            cycle_skipped=cycle_skipped,
            skip_reason=skip_reason,
            rate_budget_state=budget_state,
            failure_counts=tuple(sorted(stats.failures.items())),
            transaction_age_min_s=min(stats.transaction_ages, default=None),
            transaction_age_median_s=(
                statistics.median(stats.transaction_ages)
                if stats.transaction_ages
                else None
            ),
            transaction_age_p95_s=self._percentile_nearest_rank(
                stats.transaction_ages, 0.95
            ),
            transaction_age_max_s=max(stats.transaction_ages, default=None),
            price_receipt_age_max_s=max(
                stats.price_receipt_ages, default=None
            ),
            price_event_age_abs_max_s=max(
                stats.price_event_ages_abs, default=None
            ),
            price_oi_transaction_skew_abs_max_s=max(
                stats.price_oi_transaction_skews, default=None
            ),
        )

    def _skipped_result(
        self,
        started_at: datetime,
        started_monotonic: float,
        symbols_requested: int,
        reason: str,
        budget_state: str,
    ) -> CurrentOICycleResult:
        result = self._build_result(
            started_at,
            started_monotonic,
            symbols_requested,
            _CycleStats(),
            budget_state,
            cycle_skipped=True,
            skip_reason=reason,
            cycle_timed_out=False,
        )
        self._log_summary(result)
        return result

    def _log_summary(self, result: CurrentOICycleResult) -> None:
        logger.info(
            "OI_COLLECTOR_SUMMARY start=%s end=%s elapsed_s=%.3f symbols=%d "
            "attempted=%d success=%d failures=%d future=%d "
            "old_transaction_time_count=%d transaction_time_unchanged=%d "
            "transaction_age_min_s=%s transaction_age_median_s=%s "
            "transaction_age_p95_s=%s transaction_age_max_s=%s "
            "price_fresh=%d price_missing=%d price_receipt_stale=%d "
            "price_alignment_rejected=%d inserted=%d "
            "ignored=%d timed_out=%d budget=%s skipped=%s reason=%s",
            result.cycle_started_at_utc.isoformat(),
            result.cycle_finished_at_utc.isoformat(),
            result.elapsed_seconds,
            result.symbols_requested,
            result.oi_requests_attempted,
            result.successful_samples,
            result.failed_symbols,
            result.future_oi_rejected,
            result.old_transaction_time_count,
            result.transaction_time_unchanged,
            self._format_optional(result.transaction_age_min_s),
            self._format_optional(result.transaction_age_median_s),
            self._format_optional(result.transaction_age_p95_s),
            self._format_optional(result.transaction_age_max_s),
            result.price_fresh,
            result.price_missing,
            result.price_receipt_stale,
            result.price_alignment_rejected,
            result.samples_inserted,
            result.samples_ignored_duplicate_or_out_of_order,
            result.timed_out_symbols,
            result.rate_budget_state,
            result.cycle_skipped,
            result.skip_reason,
        )

    @staticmethod
    def _percentile_nearest_rank(
        values: list[float], percentile: float
    ) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        rank = max(1, math.ceil(percentile * len(ordered)))
        return ordered[rank - 1]

    @staticmethod
    def _format_optional(value: float | None) -> str:
        return "NA" if value is None else f"{value:.3f}"

    @staticmethod
    def _require_positive_finite(value: float, field_name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
            or not math.isfinite(value)
        ):
            raise ValueError(f"{field_name} must be finite and positive")

    @staticmethod
    def _require_non_negative_finite(value: float, field_name: str) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or not math.isfinite(value)
        ):
            raise ValueError(f"{field_name} must be finite and non-negative")
