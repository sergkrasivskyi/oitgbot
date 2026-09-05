from __future__ import annotations

import csv
import math
import socket
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from oitgbot.services.oi_anomaly_15m import (
    BAR,
    SourceBar,
    aggregate_interval,
    score_observation,
)
from oitgbot.services.oi_anomaly_15m_candidates import (
    EligibilityHistory,
    build_candidates,
    is_eligible_observation,
)
from oitgbot.services.oi_anomaly_15m_candidates_reader import (
    analyze_candidates_database,
)
from tools.oi_anomaly_15m_candidates import CANDIDATE_COLUMNS, main

NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def observation(
    change: float,
    *,
    start: datetime = NOW,
    symbol: str = "AAAUSDT",
    price_change: float = 5,
):
    start_value = 100.0
    price_start = 200.0
    rows = tuple(
        SourceBar(
            symbol,
            start + i * BAR,
            start_value,
            start_value + change,
            price_start,
            price_start + price_change,
            9 + i,
            90 + i,
            True,
        )
        for i in range(3)
    )
    return aggregate_interval(symbol, start, rows)


def anomaly(
    change: float,
    *,
    z: float | None = None,
    start: datetime = NOW,
    symbol: str = "AAAUSDT",
    price_change: float = 5,
):
    result = score_observation(
        observation(change, start=start, symbol=symbol, price_change=price_change), ()
    )
    return replace(result, z_score=z)


def candidate_results(anomalies, historical=(), *, threshold=1.0, lookback=14):
    index = EligibilityHistory.reconstruct(
        historical,
        eligibility_threshold_pct=threshold,
        lookback_hours=lookback,
    )
    return build_candidates(anomalies, index, eligibility_threshold_pct=threshold)


@pytest.mark.parametrize(
    "change,expected", [(1.0, True), (0.9999, False), (-1.0, False)]
)
def test_exact_eligibility_threshold(change, expected):
    eligible, _ = is_eligible_observation(observation(change))
    assert eligible is expected


def test_price_never_gates_and_na_z_stays_eligible():
    negative_price = anomaly(1.5, z=None, price_change=-99)
    candidate = candidate_results((negative_price,))[0]
    assert candidate.is_eligible
    assert candidate.z_score is None
    assert candidate.eligibility_reason == "oi_change_meets_threshold"


def test_invalid_current_is_ineligible_and_new_non_applicable():
    invalid = replace(
        observation(2), is_valid=False, invalid_reason="missing_or_invalid_constituents"
    )
    candidate = candidate_results((replace(anomaly(2), observation=invalid),))[0]
    assert not candidate.is_eligible
    assert candidate.is_new is False
    assert candidate.new_status_reason == "current_not_eligible"
    assert candidate.rank is None


def test_ranking_finite_z_then_oi_then_symbol_and_na_last():
    values = (
        anomaly(3, z=None, symbol="NAHIGHUSDT"),
        anomaly(2, z=5, symbol="ZUSDT"),
        anomaly(4, z=5, symbol="BUSDT"),
        anomaly(4, z=5, symbol="AUSDT"),
        anomaly(9, z=None, symbol="NALOWUSDT"),
    )
    candidates = candidate_results(
        values, historical=(observation(0, start=NOW - timedelta(minutes=15)),)
    )
    assert [candidate.symbol for candidate in candidates] == [
        "AUSDT",
        "BUSDT",
        "ZUSDT",
        "NALOWUSDT",
        "NAHIGHUSDT",
    ]
    assert [candidate.rank for candidate in candidates] == [1, 2, 3, 4, 5]


def test_ineligible_candidate_follows_ranked_eligible_candidates():
    candidates = candidate_results(
        (
            anomaly(2, z=None, symbol="ELIGIBLEUSDT"),
            anomaly(0, z=100, symbol="INELIGIBLEUSDT"),
        )
    )
    assert [candidate.symbol for candidate in candidates] == [
        "ELIGIBLEUSDT",
        "INELIGIBLEUSDT",
    ]
    assert [candidate.rank for candidate in candidates] == [1, None]


def test_new_boundaries_and_current_interval_exclusion():
    current = anomaly(2)
    old = observation(2, start=NOW - timedelta(hours=14, minutes=15))
    exactly = observation(2, start=NOW - timedelta(hours=14))
    recent = observation(2, start=NOW - timedelta(minutes=15))
    current_observation = current.observation
    assert candidate_results((current,), (old,))[0].is_new is True
    assert candidate_results((current,), (exactly,))[0].is_new is False
    assert candidate_results((current,), (recent,))[0].is_new is False
    assert candidate_results((current,), (current_observation,))[0].is_new is None


def test_historical_ineligible_price_z_and_rank_do_not_affect_new():
    history = (
        observation(0.5, start=NOW - timedelta(minutes=15), price_change=999),
        observation(-5, start=NOW - timedelta(minutes=30), price_change=-99),
    )
    candidate = candidate_results((anomaly(2, z=None),), history)[0]
    assert candidate.is_new is True
    assert candidate.previous_eligible_count == 0
    assert candidate.history_valid_interval_count == 2


def test_missing_history_coverage_unknown_and_partial_history_true():
    current = anomaly(2)
    unknown = candidate_results((current,))[0]
    assert unknown.is_new is None
    assert unknown.new_status_reason == "history_unavailable"
    assert unknown.history_coverage_ratio == 0
    partial = candidate_results(
        (current,), (observation(0, start=NOW - timedelta(minutes=30)),)
    )[0]
    assert partial.is_new is True
    assert partial.history_valid_interval_count == 1
    assert partial.history_expected_interval_count == 56
    assert partial.history_coverage_ratio == pytest.approx(1 / 56)


def test_symbols_are_isolated_and_reconstruction_equals_incremental_history():
    historical = (
        observation(2, start=NOW - timedelta(minutes=15), symbol="OTHERUSDT"),
        observation(0, start=NOW - timedelta(minutes=30), symbol="AAAUSDT"),
    )
    rebuilt = candidate_results((anomaly(2),), historical)[0]
    live = EligibilityHistory()
    for item in historical:
        live.register(item)
    uninterrupted = build_candidates((anomaly(2),), live)[0]
    assert rebuilt.is_new is True
    assert rebuilt == uninterrupted


def test_prune_removes_only_older_than_inclusive_boundary():
    index = EligibilityHistory()
    boundary = NOW - timedelta(hours=14)
    index.register(observation(2, start=boundary))
    index.register(observation(2, start=boundary - timedelta(minutes=15)))
    index.prune(NOW)
    status = index.status("AAAUSDT", NOW)
    assert status.previous_eligible_count == 1
    assert status.last_eligible_interval_start_utc == boundary


def test_configurable_threshold_and_lookback_validation():
    assert candidate_results((anomaly(2),), threshold=2.0)[0].is_eligible
    with pytest.raises(ValueError):
        EligibilityHistory(lookback_hours=0)
    with pytest.raises(ValueError):
        candidate_results((anomaly(2),), threshold=math.inf)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "research.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE research_bars_5m (symbol TEXT, bucket_start_utc TEXT, "
        "oi_open REAL, oi_close REAL, price_open REAL, price_close REAL, "
        "oi_sample_count INTEGER, price_sample_count INTEGER, is_closed INTEGER)"
    )
    yield path, connection
    connection.close()


def insert(connection, observations):
    rows = []
    for item in observations:
        for index in range(3):
            rows.append(
                (
                    item.symbol,
                    (item.interval_start_utc + index * BAR).isoformat(),
                    item.oi_start,
                    item.oi_end,
                    item.price_start,
                    item.price_end,
                    10,
                    10,
                    1,
                )
            )
    connection.executemany(
        "INSERT INTO research_bars_5m VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    connection.commit()


def test_cli_filters_csv_read_only_and_no_network(
    database, tmp_path, capsys, monkeypatch
):
    path, writer = database
    insert(writer, (observation(0, start=NOW - timedelta(minutes=15)), observation(2)))
    before = writer.execute("SELECT count(*) FROM research_bars_5m").fetchone()[0]
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")),
    )
    output = tmp_path / "candidates.csv"
    assert (
        main(
            [
                "--db",
                str(path),
                "--as-of",
                (NOW + timedelta(minutes=15)).isoformat(),
                "--eligible-only",
                "--new-only",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "RANK NEW SYMBOL Z15 OI15%" in text
    with output.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert set(rows[0]) == set(CANDIDATE_COLUMNS)
    assert rows[0]["is_eligible"] == "True"
    assert rows[0]["is_new"] == "True"
    assert (
        writer.execute("SELECT count(*) FROM research_bars_5m").fetchone()[0] == before
    )


def test_candidate_reader_and_imports_remain_offline(database):
    path, writer = database
    insert(writer, (observation(0, start=NOW - timedelta(minutes=15)), observation(2)))
    start, candidates = analyze_candidates_database(
        path, as_of=NOW + timedelta(minutes=15)
    )
    assert start == NOW
    assert candidates[0].is_eligible
    code = (
        "import sys; import tools.oi_anomaly_15m_candidates; "
        "assert not any(n.startswith(('telegram', 'requests', 'websockets', 'oitgbot.app', 'oitgbot.clients')) for n in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
