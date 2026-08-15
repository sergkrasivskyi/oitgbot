from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram.ext import Application

from .clients.binance_api import BinanceAPI
from .clients.telegram_sender import TelegramSender
from .config import settings
from .logger_setup import setup_logging
from .scheduler_jobs import SchedulerJobs
from .services.oi_scanner import OIScanner
from .services.report_formatter import ReportFormatter
from .services.rolling_oi_shadow_runtime import RollingOIShadowRuntime


def build_shadow_runtime(
    binance_api: BinanceAPI,
    jobs: SchedulerJobs,
) -> RollingOIShadowRuntime | None:
    if not settings.rolling_oi_shadow_enabled:
        return None
    return RollingOIShadowRuntime(
        binance_api,
        jobs.get_symbols_cached,
        cadence_seconds=settings.rolling_oi_cadence_seconds,
        workers=settings.rolling_oi_workers,
        retention_minutes=settings.rolling_oi_retention_minutes,
        price_max_age_seconds=settings.rolling_oi_price_max_age_seconds,
        observation_max_age_seconds=(
            settings.rolling_oi_observation_max_age_seconds
        ),
        transaction_age_warning_seconds=(
            settings.rolling_oi_transaction_age_warning_seconds
        ),
        observation_5m_pct=settings.rolling_oi_5m_observation_pct,
        signal_5m_trigger_pct=settings.rolling_oi_5m_trigger_pct,
        signal_5m_rearm_pct=settings.rolling_oi_5m_rearm_pct,
        observation_20m_pct=settings.rolling_oi_20m_observation_pct,
        observation_60m_pct=settings.rolling_oi_60m_observation_pct,
        observation_120m_pct=settings.rolling_oi_120m_observation_pct,
    )


async def main_async() -> None:
    log = setup_logging()
    settings.validate()

    app: Application | None = None
    scheduler: AsyncIOScheduler | None = None
    binance_api: BinanceAPI | None = None
    shadow_runtime: RollingOIShadowRuntime | None = None
    jobs: SchedulerJobs | None = None

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(reason: str) -> None:
        if not stop_event.is_set():
            log.info("Shutdown requested: %s", reason)
            stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown, sig_name)

    try:
        log.info("Initializing Telegram application...")
        app = (
            Application.builder()
            .token(settings.bot_token)
            .connect_timeout(10)
            .read_timeout(20)
            .write_timeout(20)
            .pool_timeout(10)
            .build()
        )
        await app.initialize()

        log.info("Initializing services...")
        binance_api = BinanceAPI()
        telegram_sender = TelegramSender(app)
        oi_scanner = OIScanner(binance_api)
        report_formatter = ReportFormatter()

        jobs = SchedulerJobs(
            binance_api=binance_api,
            telegram_sender=telegram_sender,
            oi_scanner=oi_scanner,
            report_formatter=report_formatter,
        )

        if settings.rolling_oi_shadow_enabled:
            shadow_runtime = build_shadow_runtime(binance_api, jobs)
            assert shadow_runtime is not None
            jobs.shadow_runtime = shadow_runtime
            await shadow_runtime.start()
        else:
            log.info("ROLLING_SHADOW_STATUS enabled=false")

        scheduler = AsyncIOScheduler(event_loop=loop)

        scheduler.add_job(
            jobs.job_impulses,
            CronTrigger(minute="*/5", second=0),
            id="impulses_5m",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=60,
            max_instances=1,
        )

        scheduler.add_job(
            jobs.job_top,
            CronTrigger(minute="0,20,40", second=10),
            id="top_20m",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=60,
            max_instances=1,
        )

        scheduler.start()

        log.info("Publisher started. impulses=*/5@sec0, top=0,20,40@sec10")
        log.info(
            "Thresholds: impulse=%.2f%%, top=%.2f%%, send_empty=%s, fallback=%s, fallback_n=%d, debug=%s",
            settings.impulse_threshold,
            settings.top_threshold,
            settings.send_empty_reports,
            settings.show_top_when_empty,
            settings.top_when_empty_n,
            settings.debug_oi,
        )
        log.info("PROP symbols loaded: %d", len(settings.prop_symbols))
        log.info("Logging to %s", settings.log_file)
        log.info("HTTP config: timeout=%s, retries=%s", settings.http_timeout, settings.http_retries)
        log.info("Telegram timeouts: connect=10, read=20, write=20, pool=10")

        await stop_event.wait()

    except asyncio.CancelledError:
        log.info("Main task cancelled")
        raise
    except Exception:
        log.exception("Fatal error in main_async")
        raise
    finally:
        log.info("Shutting down...")

        if shadow_runtime is not None:
            with contextlib.suppress(Exception):
                await shadow_runtime.stop()

        if scheduler is not None:
            with contextlib.suppress(Exception):
                if scheduler.running:
                    scheduler.shutdown(wait=False)
                    log.info("Scheduler stopped")

        if jobs is not None:
            with contextlib.suppress(Exception):
                await jobs.close()
                log.info("Legacy job executor stopped")

        if binance_api is not None:
            with contextlib.suppress(Exception):
                binance_api.close()
                log.info("Binance API client closed")

        if app is not None:
            with contextlib.suppress(Exception):
                await app.shutdown()
                log.info("Telegram application shut down")

        log.info("Shutdown complete")


def main() -> None:
    log = logging.getLogger("oi_publisher")

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Stopped by user")
