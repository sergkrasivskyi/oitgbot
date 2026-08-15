from __future__ import annotations

import logging
import math
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from ..config import settings
from ..models import OIRow
from ..clients.binance_api import BinanceAPI
from .oi_diagnostics import OIWindowDiagnostic, build_oi_window_diagnostic, utc_iso, utc_now

log = logging.getLogger("oi_publisher")


@dataclass(slots=True)
class OIScanResult:
    window: str
    rows: list[OIRow]
    errors: int
    diagnostics: list[OIWindowDiagnostic]
    scan_started_utc: datetime
    scan_finished_utc: datetime
    requested_symbols: int

    def diagnostic_for(self, symbol: str) -> OIWindowDiagnostic | None:
        symbol = symbol.upper()
        return next((item for item in self.diagnostics if item.symbol == symbol), None)


class OIScanner:
    def __init__(self, binance_api: BinanceAPI) -> None:
        self.binance_api = binance_api
        self.max_workers = 10  # можна винести в .env

    @staticmethod
    def _pct(prev: float, cur: float) -> float:
        if prev == 0:
            return 0.0
        return (cur - prev) / prev * 100.0

    @staticmethod
    def _safe_float(value: object) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    @staticmethod
    def filter_prop(rows: list[OIRow], prop_set: set[str]) -> list[OIRow]:
        return [row for row in rows if row.symbol in prop_set]

    def log_top(self, rows: list[OIRow], title: str, n: int = 10) -> None:
        if not settings.debug_oi:
            return

        if not rows:
            log.info("%s: <empty>", title)
            return

        sample = rows[:n]
        line = ", ".join([f"{r.symbol}={r.oi_pct:.2f}%" for r in sample])
        log.info("%s top-%d: %s", title, min(n, len(rows)), line)

    def _fetch_oi_5m_for_symbol(
        self, symbol: str
    ) -> tuple[OIRow | None, OIWindowDiagnostic | None]:
        symbol = symbol.upper()
        hist = self.binance_api.get_open_interest_history(symbol, period="5m", limit=2)
        if not hist or len(hist) < 2:
            if settings.debug_oi:
                log.warning("OI_5m insufficient data for %s: %s", symbol, hist)
            return None, None

        prev_oi = self._safe_float(hist[-2].get("sumOpenInterestValue"))
        cur_oi = self._safe_float(hist[-1].get("sumOpenInterestValue"))
        oi_pct = self._pct(prev_oi, cur_oi)

        diagnostic = build_oi_window_diagnostic(
            symbol=symbol,
            window="5m",
            start_timestamp=hist[-2].get("timestamp"),
            end_timestamp=hist[-1].get("timestamp"),
            start_oi=prev_oi,
            end_oi=cur_oi,
            change_pct=oi_pct,
            expected_gap_seconds=300,
            scan_utc=utc_now(),
        )
        return OIRow(symbol=symbol, oi_pct=oi_pct), diagnostic

    def _fetch_oi_20m_for_symbol(
        self, symbol: str
    ) -> tuple[OIRow | None, OIWindowDiagnostic | None]:
        symbol = symbol.upper()
        hist = self.binance_api.get_open_interest_history(symbol, period="5m", limit=5)
        if not hist or len(hist) < 5:
            if settings.debug_oi:
                log.warning("OI_20m insufficient data for %s: %s", symbol, hist)
            return None, None

        first_oi = self._safe_float(hist[0].get("sumOpenInterestValue"))
        last_oi = self._safe_float(hist[-1].get("sumOpenInterestValue"))
        oi_pct = self._pct(first_oi, last_oi)

        diagnostic = build_oi_window_diagnostic(
            symbol=symbol,
            window="20m",
            start_timestamp=hist[0].get("timestamp"),
            end_timestamp=hist[-1].get("timestamp"),
            start_oi=first_oi,
            end_oi=last_oi,
            change_pct=oi_pct,
            expected_gap_seconds=1200,
            scan_utc=utc_now(),
        )
        return OIRow(symbol=symbol, oi_pct=oi_pct), diagnostic

    def scan_oi_5m_all(self, symbols_all: Iterable[str]) -> OIScanResult:
        rows: list[OIRow] = []
        diagnostics: list[OIWindowDiagnostic] = []
        errors = 0
        symbols = [s.upper() for s in symbols_all]
        scan_started_utc = utc_now()

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self._fetch_oi_5m_for_symbol, symbol): symbol for symbol in symbols}

            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    row, diagnostic = future.result()
                    if row is not None:
                        rows.append(row)
                        if diagnostic is not None:
                            diagnostics.append(diagnostic)
                    else:
                        errors += 1
                except Exception as exc:
                    errors += 1
                    if settings.debug_oi:
                        log.warning("OI_5m error for %s: %s", symbol, exc)

        rows.sort(key=lambda x: x.oi_pct, reverse=True)
        return OIScanResult(
            window="5m",
            rows=rows,
            errors=errors,
            diagnostics=diagnostics,
            scan_started_utc=scan_started_utc,
            scan_finished_utc=utc_now(),
            requested_symbols=len(symbols),
        )

    def scan_oi_20m_all(self, symbols_all: Iterable[str]) -> OIScanResult:
        rows: list[OIRow] = []
        diagnostics: list[OIWindowDiagnostic] = []
        errors = 0
        symbols = [s.upper() for s in symbols_all]
        scan_started_utc = utc_now()

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self._fetch_oi_20m_for_symbol, symbol): symbol for symbol in symbols}

            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    row, diagnostic = future.result()
                    if row is not None:
                        rows.append(row)
                        if diagnostic is not None:
                            diagnostics.append(diagnostic)
                    else:
                        errors += 1
                except Exception as exc:
                    errors += 1
                    if settings.debug_oi:
                        log.warning("OI_20m error for %s: %s", symbol, exc)

        rows.sort(key=lambda x: x.oi_pct, reverse=True)
        return OIScanResult(
            window="20m",
            rows=rows,
            errors=errors,
            diagnostics=diagnostics,
            scan_started_utc=scan_started_utc,
            scan_finished_utc=utc_now(),
            requested_symbols=len(symbols),
        )

    @staticmethod
    def _format_metric(value: float | None) -> str:
        if value is None or not math.isfinite(value):
            return "invalid"
        return f"{value:.3f}"

    def log_scan_diagnostics(self, result: OIScanResult) -> None:
        for diagnostic in result.diagnostics:
            for issue in diagnostic.anomaly_codes():
                log.warning(
                    "OI_TIMESTAMP_WARNING window=%s symbol=%s issue=%s scan_utc=%s "
                    "start_utc=%s end_utc=%s gap_s=%s age_s=%s",
                    diagnostic.window,
                    diagnostic.symbol,
                    issue,
                    utc_iso(diagnostic.scan_utc),
                    utc_iso(diagnostic.start_utc),
                    utc_iso(diagnostic.end_utc),
                    self._format_metric(diagnostic.actual_gap_seconds),
                    self._format_metric(diagnostic.latest_age_seconds),
                )

        ages = sorted(
            diagnostic.latest_age_seconds
            for diagnostic in result.diagnostics
            if diagnostic.latest_age_seconds is not None and math.isfinite(diagnostic.latest_age_seconds)
        )
        age_min = ages[0] if ages else None
        age_median = statistics.median(ages) if ages else None
        age_p95 = ages[math.ceil(len(ages) * 0.95) - 1] if ages else None
        age_max = ages[-1] if ages else None
        invalid_timestamps = sum(item.has_invalid_timestamp for item in result.diagnostics)
        gap_mismatches = sum(item.has_gap_mismatch for item in result.diagnostics)
        elapsed_seconds = (result.scan_finished_utc - result.scan_started_utc).total_seconds()
        log.info(
            "OI_DIAG_SUMMARY window=%s scan_start_utc=%s scan_finish_utc=%s "
            "elapsed_s=%.3f symbols=%d valid=%d errors=%d invalid_ts=%d gap_mismatch=%d "
            "age_min_s=%s age_median_s=%s age_p95_s=%s age_max_s=%s",
            result.window,
            utc_iso(result.scan_started_utc),
            utc_iso(result.scan_finished_utc),
            elapsed_seconds,
            result.requested_symbols,
            len(result.diagnostics),
            result.errors,
            invalid_timestamps,
            gap_mismatches,
            self._format_metric(age_min),
            self._format_metric(age_median),
            self._format_metric(age_p95),
            self._format_metric(age_max),
        )

    def log_qualifying_diagnostics(self, rows: list[OIRow], result: OIScanResult) -> None:
        for row in rows:
            diagnostic = result.diagnostic_for(row.symbol)
            if diagnostic is None:
                continue
            log.info(
                "OI_DIAG window=%s symbol=%s scan_utc=%s start_utc=%s end_utc=%s "
                "gap_s=%s expected_gap_s=%d age_s=%s start_oi=%.8f end_oi=%.8f change_pct=%.8f",
                diagnostic.window,
                diagnostic.symbol,
                utc_iso(diagnostic.scan_utc),
                utc_iso(diagnostic.start_utc),
                utc_iso(diagnostic.end_utc),
                self._format_metric(diagnostic.actual_gap_seconds),
                diagnostic.expected_gap_seconds,
                self._format_metric(diagnostic.latest_age_seconds),
                diagnostic.start_oi,
                diagnostic.end_oi,
                diagnostic.change_pct,
            )
