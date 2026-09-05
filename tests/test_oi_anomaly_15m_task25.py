from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from oitgbot.services.oi_anomaly_15m import (
    BAR,
    SourceBar,
    aggregate_interval,
    score_observation,
)
from oitgbot.services.oi_anomaly_15m_candidates import (
    DEFAULT_NEW_LOOKBACK_HOURS,
    EligibilityHistory,
    build_candidates,
)
from oitgbot.services.oi_anomaly_15m_reader import IntervalSnapshot
from oitgbot.services.oi_anomaly_15m_runtime import OIAnomaly15mRuntime
from oitgbot.services.report_formatter import ReportFormatter

NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def observation(change, *, start=NOW, symbol="AAAUSDT", price_change=1.0):
    bars = tuple(
        SourceBar(
            symbol,
            start + index * BAR,
            100.0,
            100.0 + change,
            200.0,
            200.0 + price_change,
            10,
            10,
            True,
        )
        for index in range(3)
    )
    return aggregate_interval(symbol, start, bars)


def anomaly(change, *, symbol="AAAUSDT", z=None):
    return replace(score_observation(observation(change, symbol=symbol), ()), z_score=z)


def full_quiet_history(symbol="AAAUSDT"):
    return tuple(
        observation(0.0, start=NOW - timedelta(minutes=15 * index), symbol=symbol)
        for index in range(1, 49)
    )


def test_locked_new_12h_full_and_incomplete_semantics():
    assert DEFAULT_NEW_LOOKBACK_HOURS == 12
    full = EligibilityHistory.reconstruct(full_quiet_history())
    confirmed = build_candidates((anomaly(2.0),), full)[0]
    assert confirmed.is_new is True
    assert confirmed.history_valid_interval_count == 48
    assert confirmed.history_expected_interval_count == 48
    partial = EligibilityHistory.reconstruct(full_quiet_history()[:-1])
    unknown = build_candidates((anomaly(2.0),), partial)[0]
    assert unknown.is_new is None
    assert unknown.new_status_reason == "incomplete_new_history"
    assert unknown.history_valid_interval_count == 47


def test_known_prior_eligible_conclusively_not_new_despite_gaps():
    history = EligibilityHistory.reconstruct(
        (observation(2.0, start=NOW - timedelta(hours=12)),)
    )
    candidate = build_candidates((anomaly(2.0),), history)[0]
    assert candidate.is_new is False
    assert candidate.previous_eligible_count == 1


def test_formatter_markers_legend_semantics_and_order():
    confirmed = build_candidates(
        (anomaly(2.0, symbol="NEWUSDT", z=5.26),),
        EligibilityHistory.reconstruct(full_quiet_history("NEWUSDT")),
    )[0]
    incomplete = build_candidates(
        (anomaly(1.5, symbol="WAITUSDT", z=None),), EligibilityHistory()
    )[0]
    prior = EligibilityHistory.reconstruct(
        (observation(3.0, start=NOW - BAR * 3, symbol="OLDUSDT"),)
    )
    old = build_candidates((anomaly(1.2, symbol="OLDUSDT", z=4.0),), prior)[0]
    text = ReportFormatter().format_oi_anomaly_15m((confirmed, old, incomplete))
    assert "📊 OI ANOMALY · 15m" in text
    assert "Z | OI% | PX% | Ticker" in text
    assert "🆕 5.26 | +2.00% | +0.50%" in text
    assert "⏳ N/A | +1.50% | +0.50%" in text
    old_line = next(line for line in text.splitlines() if "OLDUSDT" in line)
    assert "🆕" not in old_line and "⏳" not in old_line
    assert "🆕 NEW · ⏳ NEW history incomplete" in text
    assert "UNKNOWN" not in text and "MAYBE NEW" not in text


def test_marker_state_does_not_change_rank_or_eligibility_and_split_keeps_rows():
    current = (anomaly(3.0, symbol="AUSDT", z=5), anomaly(2.0, symbol="BUSDT", z=4))
    history = EligibilityHistory.reconstruct(full_quiet_history("AUSDT"))
    candidates = build_candidates(current, history)
    assert [(c.symbol, c.rank, c.is_eligible) for c in candidates] == [
        ("AUSDT", 1, True),
        ("BUSDT", 2, True),
    ]
    chunks = ReportFormatter().format_oi_anomaly_15m_chunks(candidates, max_length=230)
    joined = "\n".join(chunks)
    assert sum("AUSDT" in chunk for chunk in chunks) == 1
    assert sum("BUSDT" in chunk for chunk in chunks) == 1
    assert joined.index("AUSDT") < joined.index("BUSDT")


class Publisher:
    def __init__(self):
        self.calls = 0

    async def publish(self, candidates):
        self.calls += 1


def test_runtime_wait_retry_exactly_once_and_startup_no_replay():
    publisher = Publisher()
    complete = {"value": False}
    history = full_quiet_history()

    def bootstrap(*args, **kwargs):
        return NOW - timedelta(minutes=15), (), history

    def interval(*args, **kwargs):
        obs = (observation(2.0),)
        return IntervalSnapshot(NOW, (), obs, complete["value"])

    runtime = OIAnomaly15mRuntime(
        "unused.sqlite3",
        publisher=publisher,
        now=lambda: NOW + timedelta(minutes=15),
        bootstrap_reader=bootstrap,
        interval_reader=interval,
    )

    async def exercise():
        await runtime._bootstrap()
        assert publisher.calls == 0
        assert not await runtime.wake_once()
        complete["value"] = True
        assert await runtime.wake_once()
        assert not await runtime.wake_once()

    asyncio.run(exercise())
    assert publisher.calls == 1
    assert runtime.last_processed_interval_start_utc == NOW


def test_eligible_12h15m_old_does_not_block_new_with_complete_window():
    history = EligibilityHistory.reconstruct(
        full_quiet_history()
        + (observation(2.0, start=NOW - timedelta(hours=12, minutes=15)),)
    )
    candidate = build_candidates((anomaly(2.0),), history)[0]
    assert candidate.is_new is True
    assert candidate.history_valid_interval_count == 48
    assert candidate.previous_eligible_count == 0


def test_runtime_clean_shutdown():
    def bootstrap(*args, **kwargs):
        return None, (), ()

    runtime = OIAnomaly15mRuntime(
        "unused.sqlite3",
        cadence_seconds=3600,
        bootstrap_reader=bootstrap,
    )

    async def exercise():
        await runtime.start()
        await asyncio.sleep(0)
        await runtime.stop()
        assert runtime._task is None

    asyncio.run(exercise())
