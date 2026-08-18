from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from oitgbot.app import build_shadow_runtime
from oitgbot.clients.telegram_sender import TelegramSender
from oitgbot.config import Settings
from oitgbot.models import RollingOISample, RollingOIWindowResult
from oitgbot.scheduler_jobs import SchedulerJobs
from oitgbot.services.report_formatter import ReportFormatter
from oitgbot.services.rolling_impulse_publisher import RollingImpulsePublisher
from oitgbot.services.rolling_oi_shadow_runtime import RollingOIShadowRuntime
from oitgbot.services.rolling_oi_signal_state import RollingOISignalStateMachine

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


class NoNetworkBot:
    def __init__(self) -> None:
        self.calls = 0

    async def send_message(self, **_kwargs: object) -> None:
        self.calls += 1
        raise AssertionError("Telegram network call was attempted")


class RecordingBot:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_message(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class TelegramPublishConfigurationTests(TestCase):
    def test_default_keeps_publishing_enabled_and_requires_credentials(self) -> None:
        settings = Settings(bot_token="", all_channel_id="", prop_channel_id="")
        self.assertTrue(settings.telegram_publish_enabled)
        with self.assertRaisesRegex(
            RuntimeError, "BOT_TOKEN, ALL_CHANNEL_ID, PROP_CHANNEL_ID"
        ):
            settings.validate()

    def test_disabled_mode_validates_without_telegram_credentials(self) -> None:
        Settings(telegram_publish_enabled=False).validate()

    def test_disabled_runtime_wires_research_telemetry(self) -> None:
        configured = Settings(telegram_publish_enabled=False)
        jobs = SimpleNamespace(get_symbols_cached=lambda: ["BTCUSDT"])
        with (
            patch("oitgbot.app.settings", configured),
            patch("oitgbot.app.RollingOIShadowRuntime") as runtime_type,
        ):
            build_shadow_runtime(object(), jobs)  # type: ignore[arg-type]

        kwargs = runtime_type.call_args.kwargs
        self.assertTrue(kwargs["research_telemetry_enabled"])
        self.assertEqual("state/oi_research.sqlite3", kwargs["research_db_path"])


class TelegramPublishSuppressionTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = NoNetworkBot()
        self.app = SimpleNamespace(bot=self.bot)
        self.sender = TelegramSender(self.app, publish_enabled=False)

    async def test_disabled_sender_makes_no_network_call_and_logs_suppression(
        self,
    ) -> None:
        with self.assertLogs("oi_publisher", level="INFO") as captured:
            sent = await self.sender.send_if_not_empty(
                "all", "calculated report", report_type="top", target_name="all"
            )
        self.assertFalse(sent)
        self.assertEqual(0, self.bot.calls)
        self.assertIn(
            "TELEGRAM_PUBLISH_SUPPRESSED report=top", "\n".join(captured.output)
        )

    async def test_enabled_sender_preserves_normal_delivery(self) -> None:
        bot = RecordingBot()
        sender = TelegramSender(SimpleNamespace(bot=bot))
        self.assertTrue(
            await sender.send_if_not_empty(
                "all", "calculated report", report_type="top", target_name="all"
            )
        )
        self.assertEqual(1, len(bot.calls))
        self.assertEqual("all", bot.calls[0]["chat_id"])

    async def test_5m_trigger_is_calculated_but_not_delivered(self) -> None:
        result = RollingOIWindowResult(
            symbol="BTCUSDT",
            window_seconds=300,
            available=True,
            unavailable_reason=None,
            latest_timestamp=NOW,
            baseline_timestamp=NOW - timedelta(minutes=5),
            target_timestamp=NOW - timedelta(minutes=5),
            actual_window_seconds=300,
            baseline_offset_seconds=0,
            latest_oi_quantity=105,
            baseline_oi_quantity=100,
            oi_quantity_change_pct=5,
            latest_mark_price=101,
            baseline_mark_price=100,
            price_change_pct=1,
            latest_oi_value_usd=None,
            baseline_oi_value_usd=None,
            oi_value_change_pct=None,
        )
        event = RollingOISignalStateMachine().evaluate(result)
        self.assertIsNotNone(event)
        publisher = RollingImpulsePublisher(
            self.sender,
            ReportFormatter(),
            all_channel_id="all",
            prop_channel_id="prop",
            prop_symbols={"BTCUSDT"},
        )
        await publisher.publish(event)  # type: ignore[arg-type]
        self.assertEqual(0, self.bot.calls)

    async def test_20m_top_is_calculated_but_not_delivered(self) -> None:
        runtime = RollingOIShadowRuntime(
            object(), lambda: (), clock=lambda: NOW, stream_factory=lambda _: object()
        )
        for symbol, change in (("BTCUSDT", 2.0), ("ETHUSDT", 0.5)):
            runtime.rolling_store.add(
                RollingOISample(
                    symbol,
                    100,
                    NOW - timedelta(minutes=20),
                    NOW - timedelta(minutes=20),
                    mark_price=100,
                    price_exchange_time=NOW - timedelta(minutes=20),
                )
            )
            runtime.rolling_store.add(
                RollingOISample(
                    symbol,
                    100 + change,
                    NOW,
                    NOW,
                    mark_price=101,
                    price_exchange_time=NOW,
                )
            )
        runtime._refresh_completed_top_snapshot(
            SimpleNamespace(
                cycle_finished_at_utc=NOW,
                successful_samples=2,
                symbols_requested=2,
                failed_symbols=0,
                cycle_timed_out=False,
                timed_out_symbols=0,
                cycle_skipped=False,
                skip_reason=None,
            ),
            ("BTCUSDT", "ETHUSDT"),
        )
        jobs = SchedulerJobs(object(), self.sender, ReportFormatter(), runtime)  # type: ignore[arg-type]
        soak_settings = SimpleNamespace(
            top_threshold=1.0,
            send_empty_reports=False,
            prop_symbols={"BTCUSDT"},
            all_channel_id="all",
            prop_channel_id="prop",
        )
        with patch("oitgbot.scheduler_jobs.settings", soak_settings):
            await jobs.job_top()
        self.assertEqual(0, self.bot.calls)
