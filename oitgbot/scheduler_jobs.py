from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .clients.binance_api import BinanceAPI
from .clients.telegram_sender import TelegramSender
from .config import settings
from .services.oi_diagnostics import utc_iso, utc_now
from .services.report_formatter import ReportFormatter

log = logging.getLogger("oi_publisher")
rolling_top_log = logging.getLogger("oitgbot.rolling.top")


class SchedulerJobs:
    def __init__(
        self,
        binance_api: BinanceAPI,
        telegram_sender: TelegramSender,
        report_formatter: ReportFormatter,
        shadow_runtime: object | None = None,
    ) -> None:
        self.binance_api = binance_api
        self.telegram_sender = telegram_sender
        self.report_formatter = report_formatter
        self.shadow_runtime = shadow_runtime
        self._symbols_cache: list[str] = []
        self._symbols_cache_ts = 0.0
        self._symbols_cache_ttl = 3600

    def _get_symbols_cached(self) -> list[str]:
        now = time.time()
        if self._symbols_cache and now - self._symbols_cache_ts < self._symbols_cache_ttl:
            return self._symbols_cache
        try:
            symbols = self.binance_api.get_perpetual_futures_symbols()
            self._symbols_cache = symbols
            self._symbols_cache_ts = now
            log.info("Symbols cache refreshed: %d symbols", len(symbols))
            return symbols
        except Exception as exc:
            if self._symbols_cache:
                log.warning(
                    "Failed to refresh symbols cache, using stale cache (%d symbols): %s",
                    len(self._symbols_cache),
                    exc,
                )
                return self._symbols_cache
            log.error("Failed to load symbols and cache is empty: %s", exc)
            return []

    def get_symbols_cached(self) -> list[str]:
        """Return the collector's cached eligible perpetual universe."""
        return self._get_symbols_cached()

    async def job_top(self) -> None:
        job_started_utc = utc_now()
        calculation_started = time.perf_counter()
        try:
            runtime = self.shadow_runtime
            snapshot_reader = getattr(runtime, "completed_top_snapshot", None)
            if snapshot_reader is None:
                rolling_top_log.warning(
                    "ROLLING_TOP_SKIP reason=runtime_unavailable"
                )
                return

            access: Any = await asyncio.to_thread(snapshot_reader)
            calculation_elapsed = time.perf_counter() - calculation_started
            if access.status == "unavailable":
                rolling_top_log.info(
                    "ROLLING_TOP_SKIP reason=snapshot_unavailable"
                )
                return
            if access.status == "stale":
                rolling_top_log.warning(
                    "ROLLING_TOP_SKIP reason=snapshot_stale snapshot_age_s=%.3f "
                    "source_cycle_utc=%s",
                    access.age_seconds,
                    access.source_cycle_utc.isoformat(),
                )
                return
            snapshot = access.snapshot
            rolling_top_log.info(
                "ROLLING_TOP_SNAPSHOT status=using snapshot_age_s=%.3f "
                "source_cycle_utc=%s",
                access.age_seconds,
                snapshot.source_cycle_utc.isoformat(),
            )
            if snapshot.ready_20m == 0:
                rolling_top_log.info(
                    "ROLLING_TOP_SKIP reason=warmup report_utc=%s ready_20m=0 "
                    "symbols=%d quantity_coverage=%.3f calculation_elapsed_s=%.3f",
                    snapshot.report_utc.isoformat(),
                    snapshot.symbol_count,
                    snapshot.quantity_sample_coverage,
                    calculation_elapsed,
                )
                return

            rows_all = [
                result
                for result in snapshot.results
                if result.oi_quantity_change_pct is not None
                and result.oi_quantity_change_pct >= settings.top_threshold
            ]
            rows_all.sort(
                key=lambda item: item.oi_quantity_change_pct or 0.0,
                reverse=True,
            )
            rows_prop = [
                result
                for result in rows_all
                if result.symbol in settings.prop_symbols
            ]
            price_coverage = snapshot.price_ready_20m / snapshot.ready_20m
            rolling_top_log.info(
                "ROLLING_TOP_SUMMARY report_utc=%s symbols=%d ready_20m=%d "
                "candidates=%d prop_candidates=%d threshold=%.2f "
                "quantity_coverage=%.3f price_coverage=%.3f calculation_elapsed_s=%.3f",
                snapshot.report_utc.isoformat(),
                snapshot.symbol_count,
                snapshot.ready_20m,
                len(rows_all),
                len(rows_prop),
                settings.top_threshold,
                snapshot.quantity_sample_coverage,
                price_coverage,
                calculation_elapsed,
            )

            if not rows_all and not settings.send_empty_reports:
                rolling_top_log.info(
                    "ROLLING_TOP_SKIP reason=no_candidates threshold=%.2f",
                    settings.top_threshold,
                )
                return

            empty_note = (
                f"(no growth) rolling OI_20m < {settings.top_threshold:.2f}%"
            )
            msg_all = self.report_formatter.format_rolling_top(
                rows_all,
                empty_note=empty_note if settings.send_empty_reports else None,
            )
            msg_prop = (
                self.report_formatter.format_rolling_top(
                    rows_prop,
                    empty_note=empty_note if settings.send_empty_reports else None,
                )
                if settings.send_empty_reports or rows_prop
                else ""
            )

            publish_started = time.perf_counter()
            sent_all = await self.telegram_sender.send_if_not_empty(
                settings.all_channel_id,
                msg_all,
                report_type="top",
                target_name="all",
            )
            sent_prop = await self.telegram_sender.send_if_not_empty(
                settings.prop_channel_id,
                msg_prop,
                report_type="top",
                target_name="prop",
            )
            prop_expected = bool(rows_prop) or settings.send_empty_reports
            rolling_top_log.info(
                "ROLLING_TOP_PUBLISH status=%s all_sent=%s prop_sent=%s "
                "candidates=%d prop_candidates=%d publish_elapsed_s=%.3f",
                "complete"
                if sent_all and (not prop_expected or sent_prop)
                else "failed",
                sent_all,
                sent_prop,
                len(rows_all),
                len(rows_prop),
                time.perf_counter() - publish_started,
            )
        except Exception:
            rolling_top_log.exception("ROLLING_TOP_PUBLISH status=failed")
        finally:
            job_finished_utc = utc_now()
            rolling_top_log.info(
                "ROLLING_TOP_TIMING start_utc=%s finish_utc=%s elapsed_s=%.3f",
                utc_iso(job_started_utc),
                utc_iso(job_finished_utc),
                (job_finished_utc - job_started_utc).total_seconds(),
            )
