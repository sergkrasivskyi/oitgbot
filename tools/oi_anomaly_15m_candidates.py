"""Read-only research CLI for Task 24 candidate semantics."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from oitgbot.services.oi_anomaly_15m import AnomalyResult, Observation, utc
from oitgbot.services.oi_anomaly_15m_candidates import (
    DEFAULT_NEW_LOOKBACK_HOURS,
    OIAnomaly15mCandidate,
)
from oitgbot.services.oi_anomaly_15m_candidates_reader import (
    analyze_candidates_database,
)

CANDIDATE_COLUMNS = (
    tuple(
        field.name for field in fields(OIAnomaly15mCandidate) if field.name != "anomaly"
    )
    + tuple(field.name for field in fields(Observation))
    + tuple(
        field.name for field in fields(AnomalyResult) if field.name != "observation"
    )
)


def _flat(candidate: OIAnomaly15mCandidate) -> dict[str, object]:
    """Flatten every Task 23 and Task 24 field for offline CSV research."""
    return {
        **{
            field.name: getattr(candidate, field.name)
            for field in fields(OIAnomaly15mCandidate)
            if field.name != "anomaly"
        },
        **{
            field.name: getattr(candidate.observation, field.name)
            for field in fields(Observation)
        },
        **{
            field.name: getattr(candidate.anomaly, field.name)
            for field in fields(AnomalyResult)
            if field.name != "observation"
        },
    }


def export_csv(path: Path, candidates: tuple[OIAnomaly15mCandidate, ...]) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CANDIDATE_COLUMNS)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(
                {
                    key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in _flat(candidate).items()
                }
            )


def _format(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.4f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only 15m OI anomaly candidate research"
    )
    parser.add_argument("--db", type=Path, default=Path("state/oi_research.sqlite3"))
    parser.add_argument("--as-of")
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--eligible-only", action="store_true")
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Show only candidates with NEW=True; unknown is excluded.",
    )
    parser.add_argument("--eligibility-threshold", type=float, default=1.0)
    parser.add_argument(
        "--new-lookback-hours", type=float, default=DEFAULT_NEW_LOOKBACK_HOURS
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.top < 1:
            raise ValueError("--top must be positive")
        as_of = (
            utc(datetime.fromisoformat(args.as_of.replace("Z", "+00:00")))
            if args.as_of
            else None
        )
        start, candidates = analyze_candidates_database(
            args.db,
            as_of=as_of,
            symbols=tuple(args.symbol),
            eligibility_threshold_pct=args.eligibility_threshold,
            new_lookback_hours=args.new_lookback_hours,
        )
        if args.eligible_only:
            candidates = tuple(item for item in candidates if item.is_eligible)
        if args.new_only:
            candidates = tuple(item for item in candidates if item.is_new is True)
        if args.output:
            export_csv(args.output, candidates)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(
        f"Research interval start: {start.isoformat() if start else 'N/A (no complete interval)'}"
    )
    print("RANK NEW SYMBOL Z15 OI15% PX15% HISTORY_N PREV_ELIGIBLE HISTORY_COVERAGE")
    for candidate in candidates[: args.top]:
        print(
            candidate.rank if candidate.rank is not None else "N/A",
            "YES"
            if candidate.is_new
            else "NO"
            if candidate.is_new is False
            else "UNKNOWN",
            candidate.symbol,
            _format(candidate.z_score),
            _format(candidate.oi_change_pct),
            _format(candidate.price_change_pct),
            candidate.baseline_count,
            candidate.previous_eligible_count,
            _format(candidate.history_coverage_ratio),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
