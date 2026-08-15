from __future__ import annotations

import logging
import time

from .config import settings
from .models import OIRow
from .clients.binance_api import BinanceAPI
from .clients.telegram_sender import TelegramSender
from .services.oi_scanner import OIScanner
from .services.oi_diagnostics import utc_iso, utc_now
from .services.report_formatter import ReportFormatter

log = logging.getLogger("oi_publisher")


class SchedulerJobs:
    def __init__(
        self,
        binance_api: BinanceAPI,
        telegram_sender: TelegramSender,
        oi_scanner: OIScanner,
        report_formatter: ReportFormatter,
        shadow_runtime: object | None = None,
    ) -> None:
        self.binance_api = binance_api
        self.telegram_sender = telegram_sender
        self.oi_scanner = oi_scanner
        self.report_formatter = report_formatter
        self.shadow_runtime = shadow_runtime

        self._symbols_cache: list[str] = []
        self._symbols_cache_ts = 0.0
        self._symbols_cache_ttl = 3600  # 1 година

    def _get_symbols_cached(self) -> list[str]:
        now = time.time()

        if self._symbols_cache and (now - self._symbols_cache_ts) < self._symbols_cache_ttl:
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
        """Return the legacy bot's cached eligible perpetual universe."""
        return self._get_symbols_cached()

    def _compare_shadow(self, window_seconds: int, rows: list[OIRow]) -> None:
        if self.shadow_runtime is None:
            return
        compare = getattr(self.shadow_runtime, "compare_legacy", None)
        if compare is None:
            return
        try:
            compare(window_seconds, rows)
        except Exception:
            log.exception("Shadow comparison failed window_s=%d", window_seconds)

    def _fill_price_5m(self, rows: list[OIRow]) -> int:
        errors = 0

        for row in rows:
            try:
                row.price_pct = self.binance_api.price_change_pct(
                    row.symbol,
                    interval="5m",
                    limit=2,
                )
            except Exception as exc:
                errors += 1
                row.price_pct = 0.0
                if settings.debug_oi:
                    log.warning("Price 5m error for %s: %s", row.symbol, exc)

        return errors

    def _fill_price_20m(self, rows: list[OIRow]) -> int:
        errors = 0

        for row in rows:
            try:
                row.price_pct = self.binance_api.price_change_20m_pct_via_5m(row.symbol)
            except Exception as exc:
                errors += 1
                row.price_pct = 0.0
                if settings.debug_oi:
                    log.warning("Price 20m error for %s: %s", row.symbol, exc)

        return errors

    async def job_impulses(self) -> None:
        job_started_utc = utc_now()
        try:
            symbols_all = self._get_symbols_cached()
            if not symbols_all:
                log.warning("Impulse scan skipped: no symbols available")
                return

            log.info(
                "Impulse scan: symbols=%d, threshold=%.2f%%",
                len(symbols_all),
                settings.impulse_threshold,
            )

            scan_result = self.oi_scanner.scan_oi_5m_all(symbols_all)
            all_rows = scan_result.rows
            errors = scan_result.errors
            log.info("OI_5m computed: rows=%d, errors=%d", len(all_rows), errors)
            self.oi_scanner.log_scan_diagnostics(scan_result)
            self.oi_scanner.log_top(all_rows, "OI_5m ALL (sorted)", n=10)
            self._compare_shadow(300, all_rows)

            impulses = [row for row in all_rows if row.oi_pct >= settings.impulse_threshold]
            self.oi_scanner.log_qualifying_diagnostics(impulses, scan_result)
            self.oi_scanner.log_top(
                impulses,
                f"IMPULSES >= {settings.impulse_threshold:.2f}%",
                n=10,
            )

            use_fallback = False
            rows_all: list[OIRow] = impulses

            if not rows_all and settings.show_top_when_empty:
                use_fallback = True
                rows_all = all_rows[:max(1, int(settings.top_when_empty_n))]
                log.info("No impulses. Using fallback TOP-%d by OI_5m%%", len(rows_all))
                self.oi_scanner.log_top(
                    rows_all,
                    f"FALLBACK TOP-{len(rows_all)} (OI_5m)",
                    n=10,
                )

            if not rows_all and not settings.send_empty_reports:
                log.info("No impulses and fallback disabled/empty. Not sending.")
                return

            price_errors = self._fill_price_5m(rows_all)
            if price_errors:
                log.warning("Impulse price fill completed with errors=%d", price_errors)

            rows_prop = self.oi_scanner.filter_prop(rows_all, settings.prop_symbols)

            empty_note = f"(no impulses) OI_5m < {settings.impulse_threshold:.2f}%"

            msg_all = self.report_formatter.format_message(
                rows_all,
                impulse_prefix=not use_fallback,
                empty_note=empty_note if settings.send_empty_reports else None,
            )

            msg_prop = (
                self.report_formatter.format_message(
                    rows_prop,
                    impulse_prefix=not use_fallback,
                    empty_note=empty_note if settings.send_empty_reports else None,
                )
                if settings.send_empty_reports
                else (
                    self.report_formatter.format_message(
                        rows_prop,
                        impulse_prefix=not use_fallback,
                    )
                    if rows_prop
                    else ""
                )
            )

            log.info(
                "Sending impulses: ALL=%d, PROP=%d, send_empty=%s, fallback=%s",
                len(rows_all),
                len(rows_prop),
                settings.send_empty_reports,
                use_fallback,
            )

            sent_all = await self.telegram_sender.send_if_not_empty(
                settings.all_channel_id,
                msg_all,
                report_type="impulses",
                target_name="all",
            )
            sent_prop = await self.telegram_sender.send_if_not_empty(
                settings.prop_channel_id,
                msg_prop,
                report_type="impulses",
                target_name="prop",
            )

            log.info(
                "Impulse send results: all_sent=%s, prop_sent=%s",
                sent_all,
                sent_prop,
            )

        except Exception:
            log.exception("Unhandled error in job_impulses")
        finally:
            job_finished_utc = utc_now()
            log.info(
                "JOB_TIMING job=impulses start_utc=%s finish_utc=%s elapsed_s=%.3f",
                utc_iso(job_started_utc),
                utc_iso(job_finished_utc),
                (job_finished_utc - job_started_utc).total_seconds(),
            )

    async def job_top(self) -> None:
        job_started_utc = utc_now()
        try:
            symbols_all = self._get_symbols_cached()
            if not symbols_all:
                log.warning("TOP scan skipped: no symbols available")
                return

            log.info(
                "TOP scan: symbols=%d, threshold=%.2f%%",
                len(symbols_all),
                settings.top_threshold,
            )

            scan_result = self.oi_scanner.scan_oi_20m_all(symbols_all)
            all_rows = scan_result.rows
            errors = scan_result.errors
            log.info("OI_20m computed: rows=%d, errors=%d", len(all_rows), errors)
            self.oi_scanner.log_scan_diagnostics(scan_result)
            self.oi_scanner.log_top(all_rows, "OI_20m ALL (sorted)", n=10)
            self._compare_shadow(1200, all_rows)

            rows_all = [row for row in all_rows if row.oi_pct >= settings.top_threshold]
            self.oi_scanner.log_qualifying_diagnostics(rows_all, scan_result)
            self.oi_scanner.log_top(
                rows_all,
                f"TOP >= {settings.top_threshold:.2f}% (20m)",
                n=10,
            )

            if not rows_all and not settings.send_empty_reports:
                log.info("No TOP OI growth >= %.2f%%. Not sending.", settings.top_threshold)
                return

            price_errors = self._fill_price_20m(rows_all)
            if price_errors:
                log.warning("TOP price fill completed with errors=%d", price_errors)

            rows_prop = self.oi_scanner.filter_prop(rows_all, settings.prop_symbols)

            empty_note = f"(no growth) OI_20m < {settings.top_threshold:.2f}%"

            msg_all = self.report_formatter.format_message(
                rows_all,
                impulse_prefix=False,
                empty_note=empty_note if settings.send_empty_reports else None,
            )

            msg_prop = (
                self.report_formatter.format_message(
                    rows_prop,
                    impulse_prefix=False,
                    empty_note=empty_note if settings.send_empty_reports else None,
                )
                if settings.send_empty_reports
                else (
                    self.report_formatter.format_message(
                        rows_prop,
                        impulse_prefix=False,
                    )
                    if rows_prop
                    else ""
                )
            )

            log.info(
                "Sending TOP: ALL=%d, PROP=%d, send_empty=%s",
                len(rows_all),
                len(rows_prop),
                settings.send_empty_reports,
            )

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

            log.info(
                "TOP send results: all_sent=%s, prop_sent=%s",
                sent_all,
                sent_prop,
            )

        except Exception:
            log.exception("Unhandled error in job_top")
        finally:
            job_finished_utc = utc_now()
            log.info(
                "JOB_TIMING job=top start_utc=%s finish_utc=%s elapsed_s=%.3f",
                utc_iso(job_started_utc),
                utc_iso(job_finished_utc),
                (job_finished_utc - job_started_utc).total_seconds(),
            )
