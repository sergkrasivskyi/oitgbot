from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from oitgbot.models import OIRow, RollingOIWindowResult
from oitgbot.services.current_oi_collector import (
    CurrentOICollector,
    CurrentOICycleResult,
)
from oitgbot.services.mark_price_stream import MarkPriceStream
from oitgbot.services.price_state import PriceStateStore
from oitgbot.services.rate_limit_budget import BudgetState, RateLimitBudget
from oitgbot.services.rolling_oi_calculator import (
    AccumulationAnalyzer,
    RollingOICalculator,
)
from oitgbot.services.rolling_oi_store import RollingOIStore
from oitgbot.services.rolling_oi_signal_state import (
    RollingOISignalDirection,
    RollingOISignalEvent,
    RollingOISignalEventType,
    RollingOISignalStateMachine,
)
from oitgbot.services.rolling_oi_signal_persistence import (
    RollingOISignalStatePersistence,
)

logger = logging.getLogger("oitgbot.rolling.runtime")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _pct(value: float | None) -> str:
    return "NA" if value is None else f"{value:+.2f}"


def _metric(value: float | None) -> str:
    return "NA" if value is None else f"{value:.2f}"


@dataclass(frozen=True, slots=True)
class RollingShadowEvaluation:
    cycle_utc: datetime
    symbol_count: int
    ready_5m: int
    ready_20m: int
    ready_60m: int
    ready_120m: int
    candidates_5m: int
    candidates_20m: int
    candidates_60m: int
    candidates_120m: int
    price_coverage: float
    quantity_sample_coverage: float
    new_positive_triggers: int
    new_negative_triggers: int
    rearmed_positive: int
    rearmed_negative: int
    active_positive_states: int
    active_negative_states: int


@dataclass(frozen=True, slots=True)
class RollingShadowHealth:
    enabled: bool
    price_stream_connected: bool
    price_stream_stale: bool
    collector_last_cycle_utc: datetime | None
    collector_last_cycle_state: str
    rolling_symbol_count: int
    ready_5m: int
    ready_20m: int
    ready_60m: int
    ready_120m: int
    rate_budget_state: str


class RollingOIShadowRuntime:
    """Own the rolling OI shadow services without any Telegram dependency."""

    def __init__(
        self,
        binance_api: Any,
        symbol_provider: Callable[[], Iterable[str]],
        *,
        cadence_seconds: float = 30.0,
        workers: int = 20,
        retention_minutes: float = 150.0,
        price_max_age_seconds: float = 5.0,
        observation_max_age_seconds: float = 60.0,
        transaction_age_warning_seconds: float = 60.0,
        observation_5m_pct: float = 2.0,
        signal_5m_trigger_pct: float = 5.0,
        signal_5m_rearm_pct: float = 3.0,
        observation_20m_pct: float = 1.0,
        observation_60m_pct: float = 3.0,
        observation_120m_pct: float = 4.0,
        max_candidate_logs: int = 20,
        clock: Callable[[], datetime] = _utc_now,
        stream_factory: Callable[[PriceStateStore], Any] = MarkPriceStream,
        budget_factory: Callable[[Iterable[Any]], Any] = RateLimitBudget,
        collector_factory: Callable[..., Any] = CurrentOICollector,
        signal_publisher: Any | None = None,
        signal_state_persistence: RollingOISignalStatePersistence | None = None,
    ) -> None:
        if cadence_seconds <= 0 or not math.isfinite(cadence_seconds):
            raise ValueError("cadence_seconds must be finite and positive")
        if isinstance(workers, bool) or not isinstance(workers, int) or workers <= 0:
            raise ValueError("workers must be a positive integer")
        if retention_minutes < 120 or not math.isfinite(retention_minutes):
            raise ValueError("retention_minutes must be finite and at least 120")
        if (
            price_max_age_seconds <= 0
            or observation_max_age_seconds <= 0
            or transaction_age_warning_seconds <= 0
        ):
            raise ValueError("freshness limits must be positive")
        thresholds = (
            observation_5m_pct,
            observation_20m_pct,
            observation_60m_pct,
            observation_120m_pct,
        )
        if any(value < 0 or not math.isfinite(value) for value in thresholds):
            raise ValueError("observation thresholds must be finite and non-negative")

        self.binance_api = binance_api
        self.symbol_provider = symbol_provider
        self.cadence_seconds = float(cadence_seconds)
        self.workers = workers
        self.price_max_age_seconds = float(price_max_age_seconds)
        self.observation_max_age_seconds = float(
            observation_max_age_seconds
        )
        self.transaction_age_warning_seconds = float(
            transaction_age_warning_seconds
        )
        self.observation_thresholds = {
            300: float(observation_5m_pct),
            1200: float(observation_20m_pct),
            3600: float(observation_60m_pct),
            7200: float(observation_120m_pct),
        }
        self.max_candidate_logs = max_candidate_logs
        self._clock = clock
        self._budget_factory = budget_factory
        self._collector_factory = collector_factory
        self.signal_publisher = signal_publisher
        self.signal_state_persistence = signal_state_persistence

        self.price_state = PriceStateStore(())
        self.rolling_store = RollingOIStore(
            retention_minutes=retention_minutes,
            cadence_seconds=cadence_seconds,
        )
        self.calculator = RollingOICalculator(cadence_seconds=cadence_seconds)
        self.analyzer = AccumulationAnalyzer(self.calculator)
        self.signal_state_machine = RollingOISignalStateMachine(
            trigger_threshold_pct=signal_5m_trigger_pct,
            rearm_threshold_pct=signal_5m_rearm_pct,
        )
        self.price_stream = stream_factory(self.price_state)
        self.rate_budget: Any | None = None
        self.collector: Any | None = None

        self._stream_task: asyncio.Task[None] | None = None
        self._initialization_task: asyncio.Task[None] | None = None
        self._periodic_task: asyncio.Task[None] | None = None
        self._publish_tasks: set[asyncio.Task[Any]] = set()
        self._stop_event = asyncio.Event()
        self._started = False
        self._stopped = False
        self._protected_418 = False
        self._backoff_until = 0.0
        self._last_cycle_utc: datetime | None = None
        self._last_cycle_state = "not_started"
        self._rate_budget_state = "NOT_INITIALIZED"
        self._last_evaluation: RollingShadowEvaluation | None = None
        self._last_signal_events: tuple[RollingOISignalEvent, ...] = ()
        self._signal_state_changed = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        logger.info(
            "ROLLING_SHADOW_STATUS enabled=true cadence_s=%.0f retention_min=%.0f workers=%d",
            self.cadence_seconds,
            self.rolling_store.retention_minutes,
            self.workers,
        )
        self._stream_task = asyncio.create_task(
            self.price_stream.run(), name="rolling-oi-price-stream"
        )
        self._stream_task.add_done_callback(
            lambda task: self._background_done("price_stream", task)
        )
        self._initialization_task = asyncio.create_task(
            self._initialize(), name="rolling-oi-initialize"
        )

    @staticmethod
    def _background_done(name: str, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "ROLLING_SHADOW_STATUS component=%s status=failed error=%s",
                name,
                type(error).__name__,
            )

    async def wait_initialized(self) -> None:
        if self._initialization_task is not None:
            await self._initialization_task

    async def _initialize(self) -> None:
        try:
            symbols = await asyncio.to_thread(self._load_symbols)
            self.price_state.set_eligible_symbols(symbols)
            if self.signal_state_persistence is not None:
                await asyncio.to_thread(
                    self.signal_state_persistence.load,
                    self.signal_state_machine,
                    self._clock(),
                )
            rate_limits = await asyncio.to_thread(
                self.binance_api.get_request_weight_limits
            )
            self.rate_budget = self._budget_factory(rate_limits)
            if not getattr(self.rate_budget, "request_weight_limits", rate_limits):
                self._rate_budget_state = BudgetState.UNSAFE.value
                self._last_cycle_state = "rate_budget_unavailable"
                logger.error(
                    "ROLLING_SHADOW_STATUS status=degraded reason=missing_request_weight_limits"
                )
                return
            self.collector = self._collector_factory(
                self.binance_api,
                self.price_state,
                self.rolling_store,
                self.rate_budget,
                max_workers=self.workers,
                default_cadence_seconds=self.cadence_seconds,
                price_max_age_seconds=self.price_max_age_seconds,
                transaction_age_warning_seconds=(
                    self.transaction_age_warning_seconds
                ),
            )
            self._rate_budget_state = BudgetState.SAFE.value
            self._last_cycle_state = "ready"
            self._periodic_task = asyncio.create_task(
                self._periodic_loop(), name="rolling-oi-collector"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._rate_budget_state = BudgetState.UNSAFE.value
            self._last_cycle_state = "initialization_failed"
            logger.exception(
                "ROLLING_SHADOW_STATUS status=degraded reason=initialization_failed"
            )

    def _load_symbols(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(symbol.upper() for symbol in self.symbol_provider()))

    async def _periodic_loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while not self._stop_event.is_set() and not self._protected_418:
            delay = max(0.0, next_tick - loop.time())
            if delay and await self._wait_for_stop(delay):
                break
            now = loop.time()
            if now >= self._backoff_until:
                await self.run_cycle()
            else:
                self._last_cycle_state = "rate_pressure_backoff"
                logger.warning(
                    "ROLLING_SHADOW_STATUS status=backoff remaining_s=%.1f",
                    self._backoff_until - now,
                )
            next_tick += self.cadence_seconds
            if next_tick <= loop.time():
                missed = math.floor((loop.time() - next_tick) / self.cadence_seconds) + 1
                next_tick += missed * self.cadence_seconds

    async def _wait_for_stop(self, delay: float) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            return True
        except TimeoutError:
            return False

    async def run_cycle(self) -> CurrentOICycleResult | None:
        if self.collector is None or self._protected_418:
            return None
        try:
            symbols = await asyncio.to_thread(self._load_symbols)
            self.price_state.set_eligible_symbols(symbols)
            result = await self.collector.collect_cycle(
                symbols, cadence_seconds=self.cadence_seconds
            )
            self._last_cycle_utc = result.cycle_finished_at_utc
            self._rate_budget_state = result.rate_budget_state
            self._last_cycle_state = self._classify_cycle(result)
            if result.http_418_errors:
                self._protected_418 = True
                self._last_cycle_state = "protected_http_418"
                logger.critical(
                    "ROLLING_SHADOW_STATUS status=protected reason=http_418 polling=stopped"
                )
            elif result.http_429_errors:
                self._backoff_until = asyncio.get_running_loop().time() + max(
                    60.0, 2 * self.cadence_seconds
                )
                self._last_cycle_state = "rate_pressure_http_429"
                logger.warning(
                    "ROLLING_SHADOW_STATUS status=backoff reason=http_429 delay_s=%.0f",
                    max(60.0, 2 * self.cadence_seconds),
                )
            self.evaluate_and_log(
                result,
                log_candidates=result.samples_inserted > 0,
                eligible_symbols=symbols,
            )
            if self._signal_state_changed:
                await self._persist_signal_state(result.cycle_finished_at_utc)
            self._schedule_trigger_publications(self._last_signal_events)
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            self._last_cycle_state = "cycle_failed"
            logger.exception("ROLLING_SHADOW_STATUS status=degraded reason=cycle_failed")
            return None

    @staticmethod
    def _classify_cycle(result: CurrentOICycleResult) -> str:
        if result.cycle_skipped:
            return result.skip_reason or "skipped"
        if result.cycle_timed_out:
            return "partial_timeout"
        if result.failed_symbols:
            return "partial_failure"
        return "ok"

    def evaluate_and_log(
        self,
        cycle: CurrentOICycleResult,
        *,
        log_candidates: bool = True,
        eligible_symbols: Iterable[str] | None = None,
    ) -> RollingShadowEvaluation:
        symbols = (
            tuple(dict.fromkeys(eligible_symbols))
            if eligible_symbols is not None
            else self.rolling_store.symbols()
        )
        self.signal_state_machine.prune(symbols)
        expired_states = 0
        if self.signal_state_persistence is not None:
            expired_states = self.signal_state_machine.expire_active(
                cycle.cycle_finished_at_utc,
                self.signal_state_persistence.ttl,
            )
            if expired_states:
                logger.info(
                    "ROLLING_SIGNAL_STATE status=expired active_states=%d",
                    expired_states,
                )
        windows: dict[int, list[RollingOIWindowResult]] = {
            seconds: [] for seconds in self.observation_thresholds
        }
        long_metrics: dict[int, list[tuple[RollingOIWindowResult, Any]]] = {
            3600: [],
            7200: [],
        }
        for symbol in symbols:
            latest = self.rolling_store.latest(symbol)
            if latest is None:
                continue
            latest_age = (
                cycle.cycle_finished_at_utc - latest.observed_at_utc
            ).total_seconds()
            if (
                latest_age < 0
                or latest_age > self.observation_max_age_seconds
            ):
                continue
            for seconds in windows:
                try:
                    result = self.calculator.calculate(
                        self.rolling_store, symbol, seconds
                    )
                    if result.available:
                        windows[seconds].append(result)
                        if seconds in long_metrics:
                            long_metrics[seconds].append(
                                (
                                    result,
                                    self.analyzer.analyze(
                                        self.rolling_store, symbol, seconds
                                    ),
                                )
                            )
                except Exception:
                    logger.exception(
                        "ROLLING_SHADOW_SYMBOL_ERROR symbol=%s window_s=%d",
                        symbol,
                        seconds,
                    )

        short_candidates = {
            seconds: [
                result
                for result in results
                if result.oi_quantity_change_pct is not None
                and abs(result.oi_quantity_change_pct)
                >= self.observation_thresholds[seconds]
            ]
            for seconds, results in windows.items()
            if seconds in (300, 1200)
        }
        accumulation_candidates = {
            seconds: [
                pair
                for pair in pairs
                if pair[0].oi_quantity_change_pct is not None
                and pair[0].oi_quantity_change_pct
                >= self.observation_thresholds[seconds]
            ]
            for seconds, pairs in long_metrics.items()
        }
        signal_events = self.signal_state_machine.evaluate_batch(windows[300])
        self._last_signal_events = signal_events
        self._signal_state_changed = bool(signal_events) or bool(expired_states)
        new_positive_triggers = sum(
            event.event_type is RollingOISignalEventType.TRIGGER
            and event.direction is RollingOISignalDirection.POSITIVE
            for event in signal_events
        )
        new_negative_triggers = sum(
            event.event_type is RollingOISignalEventType.TRIGGER
            and event.direction is RollingOISignalDirection.NEGATIVE
            for event in signal_events
        )
        rearmed_positive = sum(
            event.event_type is RollingOISignalEventType.REARM
            and event.direction is RollingOISignalDirection.POSITIVE
            for event in signal_events
        )
        rearmed_negative = sum(
            event.event_type is RollingOISignalEventType.REARM
            and event.direction is RollingOISignalDirection.NEGATIVE
            for event in signal_events
        )
        active_positive_states, active_negative_states = (
            self.signal_state_machine.active_counts()
        )

        if log_candidates:
            self._log_short_candidates(300, short_candidates[300])
            self._log_short_candidates(1200, short_candidates[1200])
            self._log_accumulation_candidates(3600, accumulation_candidates[3600])
            self._log_accumulation_candidates(7200, accumulation_candidates[7200])
        self._log_signal_events(signal_events)

        quantity_coverage = (
            cycle.successful_samples / cycle.symbols_requested
            if cycle.symbols_requested
            else 0.0
        )
        price_coverage = (
            cycle.price_fresh / cycle.successful_samples
            if cycle.successful_samples
            else 0.0
        )
        evaluation = RollingShadowEvaluation(
            cycle_utc=cycle.cycle_finished_at_utc,
            symbol_count=len(symbols),
            ready_5m=len(windows[300]),
            ready_20m=len(windows[1200]),
            ready_60m=len(windows[3600]),
            ready_120m=len(windows[7200]),
            candidates_5m=len(short_candidates[300]),
            candidates_20m=len(short_candidates[1200]),
            candidates_60m=len(accumulation_candidates[3600]),
            candidates_120m=len(accumulation_candidates[7200]),
            price_coverage=price_coverage,
            quantity_sample_coverage=quantity_coverage,
            new_positive_triggers=new_positive_triggers,
            new_negative_triggers=new_negative_triggers,
            rearmed_positive=rearmed_positive,
            rearmed_negative=rearmed_negative,
            active_positive_states=active_positive_states,
            active_negative_states=active_negative_states,
        )
        self._last_evaluation = evaluation
        logger.info(
            "ROLLING_SHADOW_SUMMARY cycle_utc=%s symbols=%d ready_5m=%d ready_20m=%d "
            "ready_60m=%d ready_120m=%d candidates_5m=%d candidates_20m=%d "
            "candidates_60m=%d candidates_120m=%d price_coverage=%.3f quantity_coverage=%.3f "
            "new_positive_triggers=%d new_negative_triggers=%d rearmed_positive=%d "
            "rearmed_negative=%d active_positive_states=%d active_negative_states=%d",
            evaluation.cycle_utc.isoformat(),
            evaluation.symbol_count,
            evaluation.ready_5m,
            evaluation.ready_20m,
            evaluation.ready_60m,
            evaluation.ready_120m,
            evaluation.candidates_5m,
            evaluation.candidates_20m,
            evaluation.candidates_60m,
            evaluation.candidates_120m,
            evaluation.price_coverage,
            evaluation.quantity_sample_coverage,
            evaluation.new_positive_triggers,
            evaluation.new_negative_triggers,
            evaluation.rearmed_positive,
            evaluation.rearmed_negative,
            evaluation.active_positive_states,
            evaluation.active_negative_states,
        )
        if evaluation.ready_120m < evaluation.symbol_count:
            logger.info(
                "ROLLING_OI_WARMUP symbols=%d ready_5m=%d ready_20m=%d ready_60m=%d ready_120m=%d",
                evaluation.symbol_count,
                evaluation.ready_5m,
                evaluation.ready_20m,
                evaluation.ready_60m,
                evaluation.ready_120m,
            )
        return evaluation

    @staticmethod
    def _log_signal_events(events: Sequence[RollingOISignalEvent]) -> None:
        for event in events:
            logger.info(
                "ROLLING_SIGNAL event=%s direction=%s symbol=%s oi_5m=%s "
                "threshold=%.2f rearm=%.2f previous=%s new=%s price_5m=%s "
                "oi_usd_5m=%s latest_utc=%s baseline_utc=%s actual_window_s=%s",
                event.event_type.value,
                event.direction.value,
                event.symbol,
                _pct(event.oi_quantity_change_pct),
                event.trigger_threshold_pct,
                event.rearm_threshold_pct,
                event.previous_state.value,
                event.new_state.value,
                _pct(event.price_change_pct),
                _pct(event.oi_value_change_pct),
                (
                    event.latest_observed_at_utc.isoformat()
                    if event.latest_observed_at_utc
                    else "NA"
                ),
                (
                    event.baseline_observed_at_utc.isoformat()
                    if event.baseline_observed_at_utc
                    else "NA"
                ),
                _metric(event.actual_window_seconds),
            )

    async def _persist_signal_state(self, saved_at_utc: datetime) -> None:
        persistence = self.signal_state_persistence
        if persistence is None:
            return
        try:
            await asyncio.to_thread(
                persistence.save,
                self.signal_state_machine,
                saved_at_utc,
            )
        except Exception:
            logger.exception(
                "ROLLING_SIGNAL_STATE status=save_failed path=%s",
                persistence.path,
            )

    def _schedule_trigger_publications(
        self, events: Sequence[RollingOISignalEvent]
    ) -> None:
        if self.signal_publisher is None:
            return
        for event in events:
            if event.event_type is not RollingOISignalEventType.TRIGGER:
                continue
            task = asyncio.create_task(
                self.signal_publisher.publish(event),
                name=f"rolling-oi-publish-{event.symbol}",
            )
            self._publish_tasks.add(task)
            task.add_done_callback(
                lambda completed, symbol=event.symbol: self._publish_done(
                    symbol, completed
                )
            )
            task.add_done_callback(self._publish_tasks.discard)

    @staticmethod
    def _publish_done(symbol: str, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            logger.error(
                "ROLLING_SIGNAL_PUBLISH status=cancelled symbol=%s", symbol
            )
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "ROLLING_SIGNAL_PUBLISH status=failed symbol=%s error=%s",
                symbol,
                type(error).__name__,
            )

    def _log_short_candidates(
        self, seconds: int, candidates: Sequence[RollingOIWindowResult]
    ) -> None:
        for result in sorted(
            candidates,
            key=lambda item: abs(item.oi_quantity_change_pct or 0.0),
            reverse=True,
        )[: self.max_candidate_logs]:
            logger.info(
                "ROLLING_OI_SHADOW window=%dm symbol=%s oi_qty_pct=%s price_pct=%s "
                "oi_usd_pct=%s latest_utc=%s baseline_utc=%s actual_window_s=%.0f",
                seconds // 60,
                result.symbol,
                _pct(result.oi_quantity_change_pct),
                _pct(result.price_change_pct),
                _pct(result.oi_value_change_pct),
                result.latest_timestamp.isoformat() if result.latest_timestamp else "NA",
                result.baseline_timestamp.isoformat() if result.baseline_timestamp else "NA",
                result.actual_window_seconds or 0.0,
            )

    def _log_accumulation_candidates(
        self, seconds: int, candidates: Sequence[tuple[RollingOIWindowResult, Any]]
    ) -> None:
        prefix = (
            "SLOW_ACCUMULATION_SHADOW"
            if seconds == 3600
            else "LONG_ACCUMULATION_SHADOW"
        )
        for result, metrics in sorted(
            candidates,
            key=lambda pair: pair[0].oi_quantity_change_pct or 0.0,
            reverse=True,
        )[: self.max_candidate_logs]:
            logger.info(
                "%s symbol=%s oi_%dm=%s price_%dm=%s oi_usd_pct=%s persistence=%s "
                "positive_blocks=%d valid_blocks=%d expected_blocks=%d efficiency=%s "
                "drawdown=%s concentration=%s coverage=%s max_5m=%s",
                prefix,
                result.symbol,
                seconds // 60,
                _pct(result.oi_quantity_change_pct),
                seconds // 60,
                _pct(result.price_change_pct),
                _pct(result.oi_value_change_pct),
                _metric(metrics.persistence),
                metrics.positive_blocks,
                metrics.valid_blocks,
                metrics.expected_blocks,
                _metric(metrics.trend_efficiency),
                _metric(metrics.max_drawdown_pct),
                _metric(metrics.impulse_concentration),
                _metric(metrics.coverage_ratio),
                _pct(metrics.max_5m_change_pct),
            )

    def compare_legacy(self, window_seconds: int, rows: Sequence[OIRow]) -> None:
        """Log a bounded comparison when an existing legacy job already has data."""
        if window_seconds not in (300, 1200):
            return
        threshold = self.observation_thresholds[window_seconds]
        comparisons: list[tuple[float, OIRow, RollingOIWindowResult]] = []
        for row in rows:
            latest = self.rolling_store.latest(row.symbol)
            if latest is None:
                continue
            latest_age = (self._clock() - latest.observed_at_utc).total_seconds()
            if (
                latest_age < 0
                or latest_age > self.observation_max_age_seconds
            ):
                continue
            try:
                rolling = self.calculator.calculate(
                    self.rolling_store, row.symbol, window_seconds
                )
            except Exception:
                logger.exception(
                    "ROLLING_SHADOW_SYMBOL_ERROR symbol=%s window_s=%d comparison=true",
                    row.symbol,
                    window_seconds,
                )
                continue
            if not rolling.available or rolling.oi_quantity_change_pct is None:
                continue
            difference = rolling.oi_quantity_change_pct - row.oi_pct
            if (
                abs(row.oi_pct) >= threshold
                or abs(rolling.oi_quantity_change_pct) >= threshold
                or abs(difference) >= threshold
            ):
                comparisons.append((abs(difference), row, rolling))
        for _magnitude, row, rolling in sorted(
            comparisons, key=lambda item: item[0], reverse=True
        )[:20]:
            difference = (rolling.oi_quantity_change_pct or 0.0) - row.oi_pct
            logger.info(
                "OI_SHADOW_COMPARE symbol=%s legacy_window=%dm legacy_oi_pct=%+.2f "
                "rolling_quantity_pct=%s rolling_usd_pct=%s rolling_price_pct=%s "
                "legacy_latest_utc=NA rolling_latest_utc=%s difference_pp=%+.2f",
                row.symbol,
                window_seconds // 60,
                row.oi_pct,
                _pct(rolling.oi_quantity_change_pct),
                _pct(rolling.oi_value_change_pct),
                _pct(rolling.price_change_pct),
                rolling.latest_timestamp.isoformat() if rolling.latest_timestamp else "NA",
                difference,
            )

    def health(self) -> RollingShadowHealth:
        now = self._clock()
        stream_health = self.price_stream.health(
            now, max_age_seconds=self.price_max_age_seconds
        )
        evaluation = self._last_evaluation
        return RollingShadowHealth(
            enabled=True,
            price_stream_connected=stream_health.connected,
            price_stream_stale=stream_health.stale,
            collector_last_cycle_utc=self._last_cycle_utc,
            collector_last_cycle_state=self._last_cycle_state,
            rolling_symbol_count=len(self.rolling_store.symbols()),
            ready_5m=evaluation.ready_5m if evaluation else 0,
            ready_20m=evaluation.ready_20m if evaluation else 0,
            ready_60m=evaluation.ready_60m if evaluation else 0,
            ready_120m=evaluation.ready_120m if evaluation else 0,
            rate_budget_state=self._rate_budget_state,
        )

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        for task in (self._periodic_task, self._initialization_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (self._periodic_task, self._initialization_task)
                if task is not None
            ),
            return_exceptions=True,
        )
        if self.collector is not None:
            with contextlib.suppress(Exception):
                await self.collector.close()
        with contextlib.suppress(Exception):
            await self.price_stream.stop()
        if self._stream_task is not None:
            with contextlib.suppress(Exception):
                await self._stream_task
        if self._publish_tasks:
            await asyncio.gather(*self._publish_tasks, return_exceptions=True)
        logger.info("ROLLING_SHADOW_STATUS status=stopped")
