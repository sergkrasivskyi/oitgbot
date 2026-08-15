from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram.error import TelegramError, TimedOut
from telegram.ext import Application

from ..services.oi_diagnostics import utc_iso, utc_now

log = logging.getLogger("oi_publisher")


class TelegramSender:
    def __init__(self, app: Application) -> None:
        self.app = app

    @staticmethod
    def _log_timing(
        report_type: str,
        target_name: str,
        started_utc: datetime,
        success: bool,
        retry_outcome: str,
    ) -> None:
        finished_utc = utc_now()
        log.info(
            "TG_TIMING report=%s target=%s start_utc=%s finish_utc=%s elapsed_s=%.3f "
            "success=%s retry=%s",
            report_type,
            target_name,
            utc_iso(started_utc),
            utc_iso(finished_utc),
            (finished_utc - started_utc).total_seconds(),
            success,
            retry_outcome,
        )

    async def send_if_not_empty(
        self,
        chat_id: str,
        text: str,
        *,
        report_type: str = "unknown",
        target_name: str = "unknown",
    ) -> bool:
        started_utc = utc_now()
        if not text.strip():
            self._log_timing(report_type, target_name, started_utc, False, "not_attempted_empty")
            return False

        for attempt in range(2):  # 1 основна спроба + 1 повтор
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                self._log_timing(
                    report_type,
                    target_name,
                    started_utc,
                    True,
                    "retry_succeeded" if attempt else "not_retried",
                )
                return True

            except TimedOut as exc:
                if attempt == 0:
                    log.warning(
                        "Telegram send timeout, retrying once: chat_id=%s error=%s",
                        chat_id,
                        exc,
                    )
                    await asyncio.sleep(1.0)
                    continue

                log.error(
                    "Telegram send failed after retry: chat_id=%s error=%s",
                    chat_id,
                    exc,
                )
                self._log_timing(report_type, target_name, started_utc, False, "retry_failed")
                return False

            except TelegramError as exc:
                log.error("Telegram send failed: chat_id=%s error=%s", chat_id, exc)
                self._log_timing(report_type, target_name, started_utc, False, "not_retried")
                return False

            except Exception as exc:
                log.error("Unexpected Telegram send error: chat_id=%s error=%s", chat_id, exc)
                self._log_timing(report_type, target_name, started_utc, False, "not_retried")
                return False

        self._log_timing(report_type, target_name, started_utc, False, "exhausted")
        return False
