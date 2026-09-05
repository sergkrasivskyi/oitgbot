from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from .oi_anomaly_15m import (
    INTERVAL,
    Observation,
    align_15m,
    score_observation,
)
from .oi_anomaly_15m_candidates import EligibilityHistory, build_candidates
from .oi_anomaly_15m_reader import (
    analyze_database_with_observations,
    read_interval_snapshot,
)

logger = logging.getLogger("oitgbot.rolling.oi_anomaly_15m.runtime")


class OIAnomaly15mRuntime:
    """Single-worker, incremental analyzer for newly closed UTC 15m intervals."""

    def __init__(
        self,
        db_path: str,
        *,
        baseline_days: int = 14,
        min_history: int = 96,
        eligibility_threshold_pct: float = 1.0,
        new_lookback_hours: float = 12,
        log_top_n: int = 20,
        cadence_seconds: float = 30,
        publisher=None,
        now: Callable[[], datetime] | None = None,
        bootstrap_reader=analyze_database_with_observations,
        interval_reader=read_interval_snapshot,
    ) -> None:
        self.db_path = db_path
        self.baseline_days = baseline_days
        self.min_history = min_history
        self.eligibility_threshold_pct = eligibility_threshold_pct
        self.new_lookback_hours = new_lookback_hours
        self.log_top_n = log_top_n
        self.cadence_seconds = cadence_seconds
        self.publisher = publisher
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._bootstrap_reader = bootstrap_reader
        self._interval_reader = interval_reader
        self._history: dict[str, deque[Observation]] = defaultdict(deque)
        self._eligibility = EligibilityHistory(
            eligibility_threshold_pct=eligibility_threshold_pct,
            lookback_hours=new_lookback_hours,
        )
        self._symbols: tuple[str, ...] = ()
        self._last_processed: datetime | None = None
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._ready = False

    @property
    def last_processed_interval_start_utc(self) -> datetime | None:
        return self._last_processed

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="oi-anomaly-15m")
        logger.info("OI_ANOMALY_15M_RUNTIME status=started")

    async def _bootstrap(self) -> None:
        started = time.perf_counter()
        reference, results, observations = await asyncio.to_thread(
            self._bootstrap_reader,
            self.db_path,
            baseline_days=self.baseline_days,
            min_history=self.min_history,
            now_utc=self._now(),
        )
        for observation in observations:
            self._history[observation.symbol].append(observation)
            self._eligibility.register(observation)
        self._symbols = tuple(sorted(self._history))
        self._last_processed = reference
        self._ready = True
        sufficient = sum(result.z_score is not None for result in results)
        logger.info(
            "OI_ANOMALY_15M_BOOTSTRAP status=ready baseline_days=%d min_history=%d "
            "eligibility_pct=%.4f new_lookback_hours=%.1f expected_new_intervals=48 "
            "source_rows=%d aligned_observations=%d symbols=%d latest_complete_interval=%s "
            "sufficient_z=%d telegram_enabled=%s elapsed_ms=%.1f",
            self.baseline_days,
            self.min_history,
            self.eligibility_threshold_pct,
            self.new_lookback_hours,
            sum(item.constituent_5m_bar_count for item in observations),
            len(observations),
            len(self._symbols),
            reference.isoformat() if reference else "none",
            sufficient,
            self.publisher is not None,
            (time.perf_counter() - started) * 1000,
        )

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                if self._ready:
                    await self.wake_once()
                else:
                    await self._bootstrap()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "OI_ANOMALY_15M_ERROR stage=%s retry=true",
                    "wake" if self._ready else "bootstrap",
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.cadence_seconds
                )
            except TimeoutError:
                pass

    async def wake_once(self) -> bool:
        if not self._ready:
            return False
        target = align_15m(self._now()) - INTERVAL
        if self._last_processed is not None and target <= self._last_processed:
            logger.debug(
                "OI_ANOMALY_15M_SKIP interval_start_utc=%s reason=already_processed",
                target.isoformat(),
            )
            return False
        snapshot = await asyncio.to_thread(
            self._interval_reader,
            self.db_path,
            target,
            symbols=(),
        )
        if not snapshot.is_complete:
            logger.info(
                "OI_ANOMALY_15M_SKIP interval_start_utc=%s reason=incomplete retry=true source_rows=%d",
                target.isoformat(),
                len(snapshot.source_rows),
            )
            return False
        observations = self._complete_universe(snapshot.observations)
        started = time.perf_counter()
        candidates = await asyncio.to_thread(self._analyze, observations)
        eligible = tuple(candidate for candidate in candidates if candidate.is_eligible)
        for candidate in eligible[: self.log_top_n]:
            logger.info(
                "OI_ANOMALY_15M_CANDIDATE rank=%s symbol=%s z=%s oi15_pct=%s "
                "px15_pct=%s new=%s baseline_count=%d new_history=%d/%d",
                candidate.rank,
                candidate.symbol,
                candidate.z_score,
                candidate.oi_change_pct,
                candidate.price_change_pct,
                candidate.is_new,
                candidate.baseline_count,
                candidate.history_valid_interval_count,
                candidate.history_expected_interval_count,
            )
        if self.publisher is not None:
            try:
                await self.publisher.publish(eligible)
            except Exception:
                logger.exception(
                    "OI_ANOMALY_15M_PUBLISH status=failed interval_start_utc=%s",
                    target.isoformat(),
                )
        self._last_processed = target
        self._append_and_prune(observations, target)
        valid = sum(observation.is_valid for observation in observations)
        logger.info(
            "OI_ANOMALY_15M_INTERVAL status=processed interval_start_utc=%s "
            "interval_end_utc=%s source_rows=%d universe=%d valid_current=%d "
            "invalid_or_missing=%d eligible=%d new_true=%d new_false=%d "
            "new_incomplete=%d z_available=%d z_na=%d elapsed_ms=%.1f",
            target.isoformat(),
            (target + INTERVAL).isoformat(),
            len(snapshot.source_rows),
            len(observations),
            valid,
            len(observations) - valid,
            len(eligible),
            sum(candidate.is_new is True for candidate in eligible),
            sum(candidate.is_new is False for candidate in eligible),
            sum(candidate.is_new is None for candidate in eligible),
            sum(candidate.z_score is not None for candidate in eligible),
            sum(candidate.z_score is None for candidate in eligible),
            (time.perf_counter() - started) * 1000,
        )
        return True

    def _complete_universe(
        self, observations: tuple[Observation, ...]
    ) -> tuple[Observation, ...]:
        by_symbol = {observation.symbol: observation for observation in observations}
        self._symbols = tuple(sorted(by_symbol))
        return tuple(by_symbol[symbol] for symbol in self._symbols)

    def _analyze(self, observations: tuple[Observation, ...]):
        if observations:
            self._eligibility.prune(observations[0].interval_start_utc)
        anomalies = tuple(
            score_observation(
                current,
                self._history.get(current.symbol, ()),
                baseline_days=self.baseline_days,
                min_history=self.min_history,
            )
            for current in observations
        )
        return build_candidates(
            anomalies,
            self._eligibility,
            eligibility_threshold_pct=self.eligibility_threshold_pct,
        )

    def _append_and_prune(
        self, observations: tuple[Observation, ...], current_start: datetime
    ) -> None:
        cutoff = current_start - timedelta(days=self.baseline_days)
        for observation in observations:
            self._history[observation.symbol].append(observation)
            self._eligibility.register(observation)
        for symbol, values in tuple(self._history.items()):
            while values and values[0].interval_start_utc < cutoff:
                values.popleft()
            if not values:
                del self._history[symbol]

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("OI_ANOMALY_15M_RUNTIME status=stopped")
