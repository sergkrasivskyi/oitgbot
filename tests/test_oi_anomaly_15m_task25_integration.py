from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import oitgbot.app as app_module
from oitgbot.config import Settings
from oitgbot.services.oi_anomaly_15m_publisher import OIAnomaly15mPublisher
from oitgbot.services.report_formatter import ReportFormatter


class Sender:
    def __init__(self):
        self.calls = []

    async def send_if_not_empty(self, channel_id, message, **kwargs):
        self.calls.append((channel_id, message, kwargs))
        return True


def test_empty_reports_do_not_create_staging_spam():
    sender = Sender()
    publisher = OIAnomaly15mPublisher(
        sender,
        ReportFormatter(),
        all_channel_id="all",
        prop_channel_id="prop",
        prop_symbols=set(),
        send_empty_reports=False,
    )
    assert asyncio.run(publisher.publish(())) == (0, 0)
    assert sender.calls == []


def test_20m_scheduler_feature_flag(monkeypatch):
    class Scheduler:
        def __init__(self):
            self.calls = []

        def add_job(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    scheduler = Scheduler()
    monkeypatch.setattr(
        app_module, "settings", SimpleNamespace(rolling_oi_20m_top_enabled=False)
    )
    app_module.configure_scheduler(scheduler, SimpleNamespace(job_top=object()))
    assert scheduler.calls == []
    app_module.settings.rolling_oi_20m_top_enabled = True
    app_module.configure_scheduler(scheduler, SimpleNamespace(job_top=object()))
    assert len(scheduler.calls) == 1
    assert scheduler.calls[0][1]["id"] == "top_20m"


def test_anomaly_config_is_safe_off_and_rejects_incompatible_flags():
    configured = Settings(telegram_publish_enabled=False)
    assert not configured.oi_anomaly_15m_enabled
    assert not configured.oi_anomaly_15m_telegram_enabled
    assert configured.rolling_oi_20m_top_enabled
    assert configured.oi_anomaly_15m_new_lookback_hours == 12
    with pytest.raises(RuntimeError, match="requires OI_ANOMALY_15M_ENABLED"):
        Settings(
            telegram_publish_enabled=False,
            oi_anomaly_15m_enabled=False,
            oi_anomaly_15m_telegram_enabled=True,
        ).validate()


def anomaly_settings(*, enabled=True, telegram=False):
    return SimpleNamespace(
        oi_anomaly_15m_enabled=enabled,
        oi_anomaly_15m_telegram_enabled=telegram,
        all_channel_id="all",
        prop_channel_id="prop",
        prop_symbols={"BTCUSDT"},
        send_empty_reports=False,
        max_tg_len=4096,
        research_telemetry_db_path="unused.sqlite3",
        oi_anomaly_15m_baseline_days=14,
        oi_anomaly_15m_min_history=96,
        oi_anomaly_15m_eligibility_pct=1.0,
        oi_anomaly_15m_new_lookback_hours=12,
        oi_anomaly_15m_log_top_n=20,
        rolling_oi_cadence_seconds=30,
    )


def test_runtime_factory_feature_and_telegram_gates(monkeypatch):
    monkeypatch.setattr(app_module, "settings", anomaly_settings(enabled=False))
    assert app_module.build_oi_anomaly_runtime(Sender(), ReportFormatter()) is None

    monkeypatch.setattr(app_module, "settings", anomaly_settings(telegram=False))
    shadow = app_module.build_oi_anomaly_runtime(Sender(), ReportFormatter())
    assert shadow is not None and shadow.publisher is None

    monkeypatch.setattr(app_module, "settings", anomaly_settings(telegram=True))
    publish = app_module.build_oi_anomaly_runtime(Sender(), ReportFormatter())
    assert publish is not None
    assert isinstance(publish.publisher, OIAnomaly15mPublisher)
