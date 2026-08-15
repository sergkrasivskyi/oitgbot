from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence


# Running ``python tools/live_oi_probe.py`` sets sys.path to tools/, not the
# project root. Add the root so the existing project Binance client is reused.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from oitgbot.clients.binance_api import BinanceAPI
from oitgbot.services.oi_diagnostics import parse_binance_timestamp, utc_iso, utc_now


UTC = timezone.utc
FIVE_MINUTES_SECONDS = 300
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    symbol: str
    probe_utc: datetime
    previous_utc: datetime
    latest_utc: datetime
    gap_seconds: float
    latest_age_seconds: float
    expected_boundary_utc: datetime
    boundary_lag_seconds: float
    boundary_lag_buckets: float
    previous_oi_usd: float
    latest_oi_usd: float
    legacy_change_pct: float


def floor_five_minute_boundary(probe_utc: datetime) -> datetime:
    if probe_utc.tzinfo is None:
        raise ValueError("probe_utc must be timezone-aware")
    utc_time = probe_utc.astimezone(UTC)
    return utc_time.replace(
        minute=utc_time.minute - (utc_time.minute % 5),
        second=0,
        microsecond=0,
    )


def legacy_change_pct(previous_oi: float, latest_oi: float) -> float:
    """Keep the existing scanner's percentage and zero-denominator behavior."""
    if previous_oi == 0:
        return 0.0
    return (latest_oi - previous_oi) / previous_oi * 100.0


def boundary_lag(expected_boundary_utc: datetime, latest_utc: datetime) -> tuple[float, float]:
    lag_seconds = (expected_boundary_utc - latest_utc).total_seconds()
    return lag_seconds, lag_seconds / FIVE_MINUTES_SECONDS


def _legacy_oi_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def probe_symbol(api: BinanceAPI, symbol: str, probe_utc: datetime | None = None) -> ProbeResult:
    symbol = symbol.upper()
    history = api.get_open_interest_history(symbol, period="5m", limit=2)
    if not history or len(history) < 2:
        raise ValueError("Binance returned fewer than two historical OI records")

    if probe_utc is None:
        probe_utc = utc_now()
    elif probe_utc.tzinfo is None:
        raise ValueError("probe_utc must be timezone-aware")
    else:
        probe_utc = probe_utc.astimezone(UTC)

    previous = history[-2]
    latest = history[-1]
    previous_utc, previous_issue = parse_binance_timestamp(previous.get("timestamp"))
    latest_utc, latest_issue = parse_binance_timestamp(latest.get("timestamp"))
    if previous_utc is None:
        raise ValueError(f"previous timestamp is {previous_issue}")
    if latest_utc is None:
        raise ValueError(f"latest timestamp is {latest_issue}")

    previous_oi = _legacy_oi_value(previous.get("sumOpenInterestValue"))
    latest_oi = _legacy_oi_value(latest.get("sumOpenInterestValue"))
    expected_boundary_utc = floor_five_minute_boundary(probe_utc)
    lag_seconds, lag_buckets = boundary_lag(expected_boundary_utc, latest_utc)
    return ProbeResult(
        symbol=symbol,
        probe_utc=probe_utc,
        previous_utc=previous_utc,
        latest_utc=latest_utc,
        gap_seconds=(latest_utc - previous_utc).total_seconds(),
        latest_age_seconds=(probe_utc - latest_utc).total_seconds(),
        expected_boundary_utc=expected_boundary_utc,
        boundary_lag_seconds=lag_seconds,
        boundary_lag_buckets=lag_buckets,
        previous_oi_usd=previous_oi,
        latest_oi_usd=latest_oi,
        legacy_change_pct=legacy_change_pct(previous_oi, latest_oi),
    )


def _format_number(value: float, decimals: int = 3) -> str:
    return f"{value:.{decimals}f}"


def _format_buckets(value: float) -> str:
    return str(int(value)) if value.is_integer() else _format_number(value)


def print_result(result: ProbeResult) -> None:
    print(f"=== {result.symbol} ===")
    print(f"probe_utc:             {utc_iso(result.probe_utc)}")
    print(f"previous_utc:          {utc_iso(result.previous_utc)}")
    print(f"latest_utc:            {utc_iso(result.latest_utc)}")
    print(f"gap_s:                 {_format_number(result.gap_seconds)}")
    print(f"latest_age_s:          {_format_number(result.latest_age_seconds)}")
    print(f"expected_boundary_utc: {utc_iso(result.expected_boundary_utc)}")
    print(f"boundary_lag_s:        {_format_number(result.boundary_lag_seconds)}")
    print(f"boundary_lag_buckets:  {_format_buckets(result.boundary_lag_buckets)}")
    print(f"previous_oi_usd:       {_format_number(result.previous_oi_usd, 2)}")
    print(f"latest_oi_usd:         {_format_number(result.latest_oi_usd, 2)}")
    print(f"legacy_change_pct:     {result.legacy_change_pct:+.2f}%")


def print_summary(
    *,
    probe_started_utc: datetime,
    probe_finished_utc: datetime,
    requested_symbols: Sequence[str],
    results: Sequence[ProbeResult],
    failures: int,
) -> None:
    ages = sorted(result.latest_age_seconds for result in results)
    lag_buckets = [result.boundary_lag_buckets for result in results]
    lag_zero = sum(bucket == 0 for bucket in lag_buckets)
    lag_one = sum(bucket == 1 for bucket in lag_buckets)
    lag_two_or_more = sum(bucket >= 2 for bucket in lag_buckets)

    print("\nPROBE SUMMARY")
    print(f"probe_start_utc:             {utc_iso(probe_started_utc)}")
    print(f"probe_finish_utc:            {utc_iso(probe_finished_utc)}")
    print(f"elapsed_s:                   {_format_number((probe_finished_utc - probe_started_utc).total_seconds())}")
    print(f"symbols_requested:           {len(requested_symbols)}")
    print(f"symbols_successful:          {len(results)}")
    print(f"symbols_failed:              {failures}")
    print(f"latest_age_min_s:            {_format_number(min(ages)) if ages else 'n/a'}")
    print(f"latest_age_median_s:         {_format_number(statistics.median(ages)) if ages else 'n/a'}")
    print(f"latest_age_max_s:            {_format_number(max(ages)) if ages else 'n/a'}")
    print(f"max_boundary_lag_buckets:    {_format_buckets(max(lag_buckets)) if lag_buckets else 'n/a'}")
    print(f"boundary_lag_buckets_0:      {lag_zero}")
    print(f"boundary_lag_buckets_1:      {lag_one}")
    print(f"boundary_lag_buckets_ge_2:   {lag_two_or_more}")

    if results:
        print("\nSYMBOL            AGE_S    LAG_S  LAG_BUCKETS   CHANGE")
        for result in results:
            print(
                f"{result.symbol:<12} {_format_number(result.latest_age_seconds):>8} "
                f"{_format_number(result.boundary_lag_seconds):>8} "
                f"{_format_buckets(result.boundary_lag_buckets):>12} "
                f"{result.legacy_change_pct:+.2f}%"
            )


def run_probe(api: BinanceAPI, symbols: Sequence[str]) -> None:
    probe_started_utc = utc_now()
    results: list[ProbeResult] = []
    failures = 0
    for symbol in symbols:
        try:
            result = probe_symbol(api, symbol)
        except Exception as exc:
            failures += 1
            print(f"=== {symbol.upper()} ===\nERROR: {exc}")
            continue
        print_result(result)
        results.append(result)
    print_summary(
        probe_started_utc=probe_started_utc,
        probe_finished_utc=utc_now(),
        requested_symbols=symbols,
        results=results,
        failures=failures,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manually inspect Binance 5m historical OI freshness for a small symbol set."
    )
    parser.add_argument("symbols", nargs="*", help="Symbols to probe (default: a small liquid-symbol set).")
    parser.add_argument("--repeat", type=int, default=1, help="Number of finite probe runs (default: 1).")
    parser.add_argument("--interval", type=float, default=15.0, help="Seconds between repeated runs (default: 15).")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.interval < 0:
        parser.error("--interval must be zero or greater")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    symbols = tuple(symbol.upper() for symbol in args.symbols) or DEFAULT_SYMBOLS
    api = BinanceAPI()
    try:
        for run_number in range(1, args.repeat + 1):
            if args.repeat > 1:
                print(f"\n##### PROBE RUN {run_number}/{args.repeat} #####")
            run_probe(api, symbols)
            if run_number < args.repeat:
                time.sleep(args.interval)
    finally:
        api.close()


if __name__ == "__main__":
    main()
