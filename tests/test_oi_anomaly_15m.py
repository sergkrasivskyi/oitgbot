import csv
import math
import sqlite3
import statistics
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from oitgbot.services.oi_anomaly_15m import (
    BAR,
    EPSILON,
    INTERVAL,
    MAD_SCALE,
    SourceBar,
    aggregate_bars,
    aggregate_interval,
    align_15m,
    research_sort_key,
    score_observation,
)
from oitgbot.services.oi_anomaly_15m_reader import (
    SOURCE_COLUMNS,
    analyze_database,
    connect_read_only,
)
from tools.oi_anomaly_15m import CSV_COLUMNS, main

NOW = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)


def bars(start=NOW, change=4.0, symbol="AAA"):
    return tuple(
        SourceBar(
            symbol,
            start + i * BAR,
            100.0,
            100.0 + change,
            200.0,
            210.0,
            9 + i,
            299 + i,
            True,
        )
        for i in range(3)
    )


def observation(start=NOW, change=4.0, symbol="AAA"):
    return aggregate_interval(symbol, start, bars(start, change, symbol))


def history(values, symbol="AAA"):
    return [
        observation(NOW - (len(values) - i) * INTERVAL, value, symbol)
        for i, value in enumerate(values)
    ]


@pytest.mark.parametrize(
    "minutes,expected",
    [(0, 0), (14, 0), (15, 15), (29, 15), (30, 30), (44, 30), (45, 45), (59, 45)],
)
def test_alignment(minutes, expected):
    assert align_15m(NOW.replace(minute=minutes, second=59)) == NOW.replace(
        minute=expected
    )


def test_utc_required():
    with pytest.raises(ValueError):
        align_15m(NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        aggregate_interval("AAA", NOW + BAR, ())


def test_exact_aggregation_formulas_quality_and_order():
    rows = list(bars())
    rows[1] = replace(rows[1], oi_open=999, oi_close=888)
    result = aggregate_interval("AAA", NOW, reversed(rows))
    assert result.is_valid
    assert (result.oi_start, result.oi_end, result.oi_change_pct) == (100, 104, 4)
    assert (result.price_start, result.price_end, result.price_change_pct) == (
        200,
        210,
        5,
    )
    assert result.interval_end_utc == NOW + INTERVAL
    assert (
        result.constituent_5m_bar_count,
        result.total_oi_sample_count,
        result.min_oi_sample_count,
    ) == (3, 30, 9)
    assert (result.total_price_sample_count, result.min_price_sample_count) == (
        900,
        299,
    )
    assert result.source_coverage == 1
    assert aggregate_interval(
        "AAA", NOW, [replace(r, oi_sample_count=1) for r in rows]
    ).is_valid


@pytest.mark.parametrize(
    "field,index",
    [("oi_open", 0), ("oi_close", 2), ("price_open", 0), ("price_close", 2)],
)
@pytest.mark.parametrize("value", [None, 0.0, -1.0, math.nan, math.inf, -math.inf])
def test_invalid_endpoints(field, index, value):
    rows = list(bars())
    rows[index] = replace(rows[index], **{field: value})
    result = aggregate_interval("AAA", NOW, rows)
    assert not result.is_valid
    assert result.oi_change_pct is None
    assert result.invalid_reason == "invalid_endpoint"


def test_gap_open_duplicate_wrong_identity():
    rows = bars()
    for invalid in [
        rows[::2],
        rows + (rows[0],),
        (rows[0], replace(rows[1], is_closed=False), rows[2]),
        (
            rows[0],
            replace(rows[1], bucket_start_utc=NOW + BAR + timedelta(seconds=1)),
            rows[2],
        ),
    ]:
        assert not aggregate_interval("AAA", NOW, invalid).is_valid
    grouped = aggregate_bars(rows[::2] + bars(NOW + INTERVAL))
    assert [r.is_valid for r in grouped] == [False, True]


def test_population_robust_and_midrank_known_fixture():
    prior = history([1, 2, 3, 4] * 24)
    result = score_observation(observation(), prior)
    assert result.baseline_count == 96
    assert result.baseline_mean_oi_pct == 2.5
    assert result.baseline_population_std_oi_pct == pytest.approx(math.sqrt(1.25))
    assert result.z_score == pytest.approx(1.5 / math.sqrt(1.25))
    assert result.z_score != pytest.approx(1.5 / statistics.stdev([1, 2, 3, 4] * 24))
    assert result.baseline_median_oi_pct == 2.5
    assert result.baseline_mad_oi_pct == 1
    assert result.robust_z_score == pytest.approx(MAD_SCALE * 1.5)
    assert result.percentile == 87.5
    assert result.baseline_coverage == 96 / 1344
    assert result.oldest_baseline_interval_start_utc == NOW - timedelta(days=1)
    assert result.newest_baseline_interval_start_utc == NOW - INTERVAL


def test_same_semantics_exclusion_window_and_symbol_isolation():
    prior = history([1, 2, 3, 4] * 24)
    expected = score_observation(observation(), prior)
    extras = [
        observation(),
        observation(NOW + INTERVAL, 50),
        observation(NOW - timedelta(days=14) - INTERVAL, 50),
        *history([50] * 100, "BBB"),
    ]
    assert score_observation(observation(), prior + extras) == expected
    boundary = observation(NOW - timedelta(days=14), 2)
    result = score_observation(observation(), prior + [boundary])
    assert result.baseline_count == 97
    assert result.oldest_baseline_interval_start_utc == boundary.interval_start_utc


def test_extreme_finite_baseline_statistics_fail_safely():
    prior = [replace(item, oi_change_pct=1e308) for item in history([1] * 96)]
    result = score_observation(observation(), prior)
    assert result.z_score is None
    assert result.robust_z_score is None
    assert result.z_unavailable_reason == "non_finite_baseline_statistics"
    assert result.baseline_median_oi_pct is None


def test_percentile_uses_exact_float_ties():
    current = replace(observation(), oi_change_pct=1.0)
    prior = history([1] * 96)
    prior[0] = replace(prior[0], oi_change_pct=math.nextafter(1.0, 0.0))
    result = score_observation(current, prior)
    assert result.percentile == 100 * (1 + 0.5 * 95) / 96


def test_insufficient_and_empty_history():
    for n in (0, 95):
        result = score_observation(observation(), history([1] * n))
        assert result.z_score is None
        assert result.robust_z_score is None
        assert result.z_unavailable_reason == "insufficient_history"
    assert score_observation(observation(), []).percentile is None


@pytest.mark.parametrize("spread", [0, EPSILON / 10])
def test_zero_near_zero_variance(spread):
    result = score_observation(observation(), history([1, 1 + spread] * 48))
    assert result.z_score is None
    assert result.z_unavailable_reason == "zero_or_near_zero_std"
    assert result.robust_z_score is None


def test_zero_mad_with_nonzero_sigma():
    result = score_observation(observation(), history([1] * 95 + [4]))
    assert result.z_score is not None
    assert result.robust_z_score is None
    assert result.robust_z_unavailable_reason == "zero_or_near_zero_mad"


def test_invalid_history_reduces_count_and_invalid_current_explained():
    prior = history([1, 2, 3, 4] * 24)
    prior[0] = replace(prior[0], oi_change_pct=math.nan)
    prior[1] = replace(prior[1], is_valid=False)
    result = score_observation(observation(), prior)
    assert result.baseline_count == 94
    result = score_observation(aggregate_interval("AAA", NOW, ()), prior)
    assert result.z_unavailable_reason == "missing_or_invalid_constituents"


def test_deterministic_order_and_no_eligibility_filter():
    prior = history([1, 2, 3, 4] * 24)
    a = score_observation(observation(change=-2), prior)
    b = replace(a, observation=replace(a.observation, symbol="BBB"))
    na = score_observation(observation(change=100), [])
    assert sorted([na, b, a], key=research_sort_key) == [a, b, na]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "research #1.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(
        "CREATE TABLE research_bars_5m (symbol TEXT, bucket_start_utc TEXT, "
        "oi_open REAL, oi_close REAL, price_open REAL, price_close REAL, "
        "oi_sample_count INTEGER, price_sample_count INTEGER, is_closed INTEGER)"
    )
    connection.execute("CREATE INDEX bucket_idx ON research_bars_5m(bucket_start_utc)")
    connection.commit()
    yield path, connection
    connection.close()


def insert(connection, rows):
    connection.executemany(
        "INSERT INTO research_bars_5m VALUES (?,?,?,?,?,?,?,?,?)",
        [
            tuple(
                getattr(row, col).isoformat()
                if col == "bucket_start_utc"
                else getattr(row, col)
                for col in SOURCE_COLUMNS
            )
            for row in rows
        ],
    )
    connection.commit()


def test_latest_complete_wal_readonly_and_incomplete_skip(db):
    path, writer = db
    insert(writer, bars() + bars(NOW + INTERVAL)[:2])
    assert PathLikeWal(path).exists()
    start, results = analyze_database(path, now_utc=NOW + INTERVAL * 3)
    assert start == NOW
    assert results[0].observation.is_valid
    with connect_read_only(path) as reader, pytest.raises(sqlite3.OperationalError):
        reader.execute("DELETE FROM research_bars_5m")
    assert writer.execute("SELECT count(*) FROM research_bars_5m").fetchone()[0] == 5


def PathLikeWal(path):
    return path.with_name(path.name + "-wal")


def test_asof_closed_boundary_and_no_future_or_open_data(db):
    path, writer = db
    insert(writer, bars() + bars(NOW + INTERVAL))
    start, results = analyze_database(path, as_of=NOW + INTERVAL + timedelta(seconds=1))
    assert start == NOW
    assert results[0].baseline_count == 0
    start, results = analyze_database(
        path, as_of=NOW + INTERVAL - timedelta(microseconds=1), symbols=("AAA",)
    )
    assert start == NOW - INTERVAL
    assert not results[0].observation.is_valid
    writer.execute(
        "UPDATE research_bars_5m SET is_closed=0 WHERE bucket_start_utc=?",
        ((NOW + INTERVAL + BAR).isoformat(),),
    )
    writer.commit()
    assert analyze_database(path, now_utc=NOW + INTERVAL * 3)[0] == NOW


def test_cli_csv_raw_diagnostics_all_rows_and_no_network(
    db, tmp_path, capsys, monkeypatch
):
    path, writer = db
    insert(writer, bars() + bars(symbol="BBB"))

    def forbidden(*args, **kwargs):
        raise AssertionError("network is forbidden")

    monkeypatch.setattr("socket.socket", forbidden)
    output = tmp_path / "export.csv"
    assert main(["--db", str(path), "--output", str(output), "--top", "1"]) == 0
    text = capsys.readouterr().out
    assert "SYMBOL Z15 OI15% PX15% ROBUST_Z PERCENTILE HISTORY_N" in text
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert set(rows[0]) == set(CSV_COLUMNS)
    assert rows[0]["oi_start"] == "100.0"
    assert rows[0]["z_score"] == ""
    assert rows[0]["baseline_count"] == "0"
    with pytest.raises(SystemExit):
        main(["--db", str(path), "--output", str(path)])
    assert writer.execute("SELECT count(*) FROM research_bars_5m").fetchone()[0] == 6


def test_imports_have_no_application_network_dependencies():
    code = (
        "import sys; import tools.oi_anomaly_15m; "
        "assert not any(n.startswith(('telegram', 'requests', 'websockets', "
        "'oitgbot.app', 'oitgbot.clients')) for n in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_empty_missing_database_options_and_symbol_filter(db, tmp_path):
    path, writer = db
    assert analyze_database(path) == (None, ())
    with pytest.raises(sqlite3.OperationalError):
        analyze_database(tmp_path / "missing.sqlite3")
    assert not (tmp_path / "missing.sqlite3").exists()
    with pytest.raises(ValueError):
        analyze_database(path, baseline_days=0)
    insert(writer, bars(symbol="AAA") + bars(NOW + INTERVAL, symbol="BBB"))
    assert analyze_database(path, symbols=("aaa",))[0] == NOW


def test_database_history_aggregation_and_gap_isolation(db):
    path, writer = db
    rows = tuple(
        row for i in range(96) for row in bars(NOW - (96 - i) * INTERVAL, 1 + i % 4)
    )
    insert(writer, rows + bars() + bars(symbol="BBB"))
    _, results = analyze_database(path, as_of=NOW + INTERVAL)
    assert results[0].baseline_count == 96
    assert results[0].z_score == pytest.approx(1.5 / math.sqrt(1.25))
    assert results[1].baseline_count == 0
    writer.execute(
        "DELETE FROM research_bars_5m WHERE bucket_start_utc=?",
        ((NOW - INTERVAL + BAR).isoformat(),),
    )
    writer.commit()
    _, results = analyze_database(path, as_of=NOW + INTERVAL)
    assert (
        next(r for r in results if r.observation.symbol == "AAA").baseline_count == 95
    )
