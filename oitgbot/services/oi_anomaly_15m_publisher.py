from __future__ import annotations

import logging

from .oi_anomaly_15m_candidates import OIAnomaly15mCandidate
from .report_formatter import ReportFormatter

logger = logging.getLogger("oitgbot.rolling.oi_anomaly_15m.publisher")


class OIAnomaly15mPublisher:
    """Route one ranked candidate set to ALL and the configured PROP subset."""

    def __init__(
        self,
        telegram_sender,
        formatter: ReportFormatter,
        *,
        all_channel_id: str,
        prop_channel_id: str,
        prop_symbols: set[str],
        send_empty_reports: bool = False,
        max_message_length: int = 4096,
    ) -> None:
        self._sender = telegram_sender
        self._formatter = formatter
        self._all_channel_id = all_channel_id
        self._prop_channel_id = prop_channel_id
        self._prop_symbols = {symbol.upper() for symbol in prop_symbols}
        self._send_empty = send_empty_reports
        self._max_length = max_message_length

    async def publish(
        self, candidates: tuple[OIAnomaly15mCandidate, ...]
    ) -> tuple[int, int]:
        eligible = tuple(candidate for candidate in candidates if candidate.is_eligible)
        prop = tuple(c for c in eligible if c.symbol.upper() in self._prop_symbols)
        all_sent = await self._send_target("all", self._all_channel_id, eligible)
        prop_sent = await self._send_target("prop", self._prop_channel_id, prop)
        return all_sent, prop_sent

    async def _send_target(self, name: str, channel_id: str, candidates: tuple) -> int:
        if not candidates and not self._send_empty:
            logger.info("OI_ANOMALY_15M_PUBLISH_SKIP target=%s reason=empty", name)
            return 0
        chunks = self._formatter.format_oi_anomaly_15m_chunks(
            candidates, max_length=self._max_length
        )
        sent = 0
        for index, message in enumerate(chunks, 1):
            ok = await self._sender.send_if_not_empty(
                channel_id,
                message,
                report_type="oi_anomaly_15m",
                target_name=name,
            )
            sent += int(ok)
            logger.info(
                "OI_ANOMALY_15M_PUBLISH target=%s chunk=%d/%d sent=%s rows=%d",
                name,
                index,
                len(chunks),
                ok,
                len(candidates),
            )
        return sent
