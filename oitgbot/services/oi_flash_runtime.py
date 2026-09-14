from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from oitgbot.models import MarkPriceUpdate, RollingOISample
from oitgbot.services.oi_flash import OIFlashCandidate, OIFlashDetector
from oitgbot.services.oi_flash_research import (
    OIFlashResearchStore,
    OIFlashResearchTelemetry,
)
from oitgbot.services.rolling_oi_store import RollingOIStore

logger = logging.getLogger("oitgbot.rolling.oi_flash.runtime")


class OIFlashRuntime:
    """Isolated FLASH decisions, durable cooldown, telemetry, and publication."""

    def __init__(
        self,
        *,
        db_path: str,
        window_seconds: float = 60.0,
        threshold_pct: float = 3.0,
        baseline_max_lag_seconds: float = 45.0,
        cooldown_seconds: float = 900.0,
        retention_days: float = 7.0,
        price_max_age_seconds: float = 5.0,
        publisher: Any | None = None,
        store: OIFlashResearchStore | None = None,
        telemetry: OIFlashResearchTelemetry | None = None,
    ) -> None:
        if not math.isfinite(cooldown_seconds) or cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be finite and positive")
        self.detector = OIFlashDetector(
            window_seconds=window_seconds,
            threshold_pct=threshold_pct,
            baseline_max_lag_seconds=baseline_max_lag_seconds,
            price_max_age_seconds=price_max_age_seconds,
        )
        self.cooldown = timedelta(seconds=float(cooldown_seconds))
        self.store = store or OIFlashResearchStore(db_path)
        self.telemetry = telemetry or OIFlashResearchTelemetry(
            db_path, retention_days=retention_days, store=self.store
        )
        self.publisher = publisher
        self._last_accepted: dict[tuple[str, object], datetime] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._decision_lock = asyncio.Lock()

    async def start(self) -> None:
        try:
            await asyncio.to_thread(self.telemetry.start)
            self._last_accepted = await asyncio.to_thread(self.store.load_last_accepted)
            logger.info(
                "OI_FLASH_STATUS enabled=true telegram_enabled=%s window_s=%.0f "
                "baseline_max_lag_s=%.0f threshold_pct=%.2f cooldown_s=%.0f "
                "db_path=%s retention_days=%.1f restored_cooldowns=%d",
                self.publisher is not None,
                self.detector.window_seconds,
                self.detector.baseline_max_lag_seconds,
                self.detector.threshold_pct,
                self.cooldown.total_seconds(),
                self.store.path,
                self.telemetry.retention_days,
                len(self._last_accepted),
            )
        except Exception:
            logger.exception("OI_FLASH_STATUS status=degraded reason=start_failed")

    def set_eligible_symbols(self, symbols: Sequence[str]) -> None:
        self.telemetry.set_eligible_symbols(symbols)

    def observe_oi(self, sample: RollingOISample) -> None:
        self.telemetry.observe_oi(sample)

    def observe_price(self, update: MarkPriceUpdate) -> None:
        self.telemetry.observe_price(update)

    def submit_cycle(
        self,
        rolling_store: RollingOIStore,
        symbols: Sequence[str],
        detected_at_utc: datetime,
    ) -> None:
        try:
            candidates = self.detector.evaluate(
                rolling_store, tuple(symbols), detected_at_utc
            )
        except Exception:
            logger.exception("OI_FLASH_STATUS status=degraded reason=evaluation_failed")
            return
        if not candidates:
            return
        task = asyncio.create_task(
            self._process_candidates(candidates), name="oi-flash-decisions"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._task_done)

    @staticmethod
    def _task_done(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        if task.exception() is not None:
            logger.error(
                "OI_FLASH_STATUS status=degraded reason=decision_task_failed error=%s",
                type(task.exception()).__name__,
            )

    async def _process_candidates(self, candidates: Sequence[OIFlashCandidate]) -> None:
        async with self._decision_lock:
            decisions = tuple(
                (candidate, self._is_suppressed(candidate)) for candidate in candidates
            )
            try:
                persisted = await asyncio.to_thread(
                    self.store.record_crossings, decisions
                )
            except Exception:
                logger.exception(
                    "OI_FLASH_RESEARCH status=event_write_failed events=%d "
                    "telegram_blocked=true",
                    len(decisions),
                )
                return

            accepted = tuple(
                event for event in persisted if not event.suppressed_by_cooldown
            )
            for event in persisted:
                candidate = event.candidate
                if event.suppressed_by_cooldown:
                    logger.info(
                        "OI_FLASH_SUPPRESSED symbol=%s direction=%s oi_pct=%+.2f "
                        "reason=cooldown",
                        candidate.symbol,
                        candidate.direction.value,
                        candidate.oi_pct,
                    )
                else:
                    self._last_accepted[(candidate.symbol, candidate.direction)] = (
                        candidate.detected_at_utc
                    )
                    logger.info(
                        "OI_FLASH_EVENT symbol=%s direction=%s oi_pct=%+.2f px_pct=%s "
                        "actual_window_s=%.1f",
                        candidate.symbol,
                        candidate.direction.value,
                        candidate.oi_pct,
                        "NA"
                        if candidate.px_pct is None
                        else f"{candidate.px_pct:+.2f}",
                        candidate.actual_window_seconds,
                    )

            if not accepted or self.publisher is None:
                return
            event_ids = tuple(event.event_id for event in accepted)
            try:
                await asyncio.to_thread(self.store.mark_telegram_attempted, event_ids)
            except Exception:
                logger.exception(
                    "OI_FLASH_PUBLISH status=blocked reason=attempt_state_write_failed"
                )
                return
            outcomes = await self.publisher.publish(
                tuple(event.candidate for event in accepted)
            )
            results = tuple(
                (event.event_id, outcomes[index] if index < len(outcomes) else False)
                for index, event in enumerate(accepted)
            )
            try:
                await asyncio.to_thread(self.store.mark_telegram_results, results)
            except Exception:
                logger.exception(
                    "OI_FLASH_RESEARCH status=telegram_result_write_failed"
                )

    def _is_suppressed(self, candidate: OIFlashCandidate) -> bool:
        previous = self._last_accepted.get((candidate.symbol, candidate.direction))
        if previous is None:
            return False
        elapsed = candidate.detected_at_utc - previous
        return elapsed < self.cooldown

    async def stop(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        try:
            await self.telemetry.stop()
        except Exception:
            logger.exception("OI_FLASH_RESEARCH status=stop_failed")
        logger.info("OI_FLASH_STATUS status=stopped")
