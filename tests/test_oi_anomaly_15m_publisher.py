from __future__ import annotations

import asyncio
from types import SimpleNamespace

from oitgbot.services.oi_anomaly_15m_publisher import OIAnomaly15mPublisher
from oitgbot.services.report_formatter import ReportFormatter


def candidate(symbol: str, *, is_new=False, rank=1):
    return SimpleNamespace(
        symbol=symbol,
        is_eligible=True,
        is_new=is_new,
        rank=rank,
        z_score=None,
        oi_change_pct=1.5,
        price_change_pct=-0.25,
    )


class Sender:
    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    async def send_if_not_empty(self, channel_id, message, **kwargs):
        self.calls.append((channel_id, message, kwargs))
        if self.fail:
            raise RuntimeError("transport failed")
        return True


def test_all_and_prop_routing_preserves_new_marker_state():
    sender = Sender()
    publisher = OIAnomaly15mPublisher(
        sender,
        ReportFormatter(),
        all_channel_id="all",
        prop_channel_id="prop",
        prop_symbols={"PROPUSDT"},
    )
    candidates = (candidate("ALLUSDT", is_new=True), candidate("PROPUSDT", rank=2))
    assert asyncio.run(publisher.publish(candidates)) == (1, 1)
    assert len(sender.calls) == 2
    assert "ALLUSDT" in sender.calls[0][1] and "PROPUSDT" in sender.calls[0][1]
    assert "ALLUSDT" not in sender.calls[1][1] and "PROPUSDT" in sender.calls[1][1]
    assert "⏳" not in sender.calls[1][1]
    assert "🆕" not in sender.calls[1][1]
    assert sender.calls[0][2]["report_type"] == "oi_anomaly_15m"


def test_legend_appears_only_when_a_new_marker_exists():
    formatter = ReportFormatter()
    confirmed = formatter.format_oi_anomaly_15m((candidate("NEWUSDT", is_new=True),))
    prior = formatter.format_oi_anomaly_15m((candidate("OLDUSDT"),))
    assert "🆕 NEW" in confirmed
    assert "🆕 NEW" not in prior
    assert "⏳" not in confirmed and "⏳" not in prior
