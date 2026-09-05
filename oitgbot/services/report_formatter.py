from __future__ import annotations

import html

from ..models import OIRow, RollingOIWindowResult
from .rolling_oi_signal_state import RollingOISignalEvent


class ReportFormatter:
    @staticmethod
    def coinglass_link(symbol: str) -> str:
        return f"https://www.coinglass.com/tv/Binance_{symbol}"

    @staticmethod
    def _fmt_signed(value: float) -> str:
        return f"{value:+.2f}"

    def format_message(
        self,
        rows: list[OIRow],
        impulse_prefix: bool,
        empty_note: str | None = None,
    ) -> str:
        header = "<b>OI% | PX% | Ticker</b>"

        if not rows and empty_note:
            return f"{header}\n\n{html.escape(empty_note)}"

        lines: list[str] = [header, ""]

        for row in rows:
            link = self.coinglass_link(row.symbol)
            ticker = f'<a href="{link}">{html.escape(row.symbol)}</a>'

            oi_str = self._fmt_signed(row.oi_pct)
            px_str = self._fmt_signed(row.price_pct)
            prefix = "⚡ " if impulse_prefix else ""

            lines.append(f"{prefix}{oi_str} | {px_str} | {ticker}")

        return "\n".join(lines).strip()

    @staticmethod
    def _fmt_anomaly_value(value: float | None, *, percent: bool = False) -> str:
        if value is None:
            return "N/A"
        rendered = f"{value:+.2f}" if percent else f"{value:.2f}"
        return rendered + ("%" if percent else "")

    def _format_oi_anomaly_row(self, candidate) -> str:
        link = self.coinglass_link(candidate.symbol)
        ticker = f'<a href="{link}">{html.escape(candidate.symbol)}</a>'
        marker = (
            "🆕 "
            if candidate.is_new is True
            else "⏳ "
            if candidate.is_new is None
            else ""
        )
        return (
            f"{marker}{self._fmt_anomaly_value(candidate.z_score)} | "
            f"{self._fmt_anomaly_value(candidate.oi_change_pct, percent=True)} | "
            f"{self._fmt_anomaly_value(candidate.price_change_pct, percent=True)} | {ticker}"
        )

    def format_oi_anomaly_15m(self, candidates) -> str:
        """Format eligible candidates in their existing deterministic rank order."""
        candidates = [candidate for candidate in candidates if candidate.is_eligible]
        lines = ["<b>📊 OI ANOMALY · 15m</b>", "<b>Z | OI% | PX% | Ticker</b>", ""]
        lines.extend(self._format_oi_anomaly_row(candidate) for candidate in candidates)
        if any(candidate.is_new is not False for candidate in candidates):
            lines.extend(["", "🆕 NEW · ⏳ NEW history incomplete"])
        return "\n".join(lines).strip()

    def format_oi_anomaly_15m_chunks(self, candidates, *, max_length: int = 4096):
        """Split at row boundaries without reordering or dropping candidates."""
        if max_length < 1:
            raise ValueError("max_length must be positive")
        eligible = [candidate for candidate in candidates if candidate.is_eligible]
        if not eligible:
            return (self.format_oi_anomaly_15m(()),)
        chunks = []
        pending = []
        for candidate in eligible:
            proposal = self.format_oi_anomaly_15m((*pending, candidate))
            if pending and len(proposal) > max_length:
                chunks.append(self.format_oi_anomaly_15m(pending))
                pending = [candidate]
            else:
                pending.append(candidate)
        if pending:
            chunks.append(self.format_oi_anomaly_15m(pending))
        if any(len(chunk) > max_length for chunk in chunks):
            raise ValueError("max_length is too small for one anomaly row")
        return tuple(chunks)

    def format_rolling_impulse(self, event: RollingOISignalEvent) -> str:
        link = self.coinglass_link(event.symbol)
        ticker = f'<a href="{link}">{html.escape(event.symbol)}</a>'
        price = (
            self._fmt_signed(event.price_change_pct)
            if event.price_change_pct is not None
            else "NA"
        )
        return (
            "<b>OI% | PX% | Ticker</b>\n\n"
            f"\u26a1 {self._fmt_signed(event.oi_quantity_change_pct)} | {price} | {ticker}"
        )

    def format_rolling_top(
        self,
        results: list[RollingOIWindowResult],
        empty_note: str | None = None,
    ) -> str:
        header = "<b>OI% | PX% | Ticker</b>"
        if not results and empty_note:
            return f"{header}\n\n{html.escape(empty_note)}"
        lines = [header, ""]
        for result in results:
            link = self.coinglass_link(result.symbol)
            ticker = f'<a href="{link}">{html.escape(result.symbol)}</a>'
            price = (
                self._fmt_signed(result.price_change_pct)
                if result.price_change_pct is not None
                else "NA"
            )
            lines.append(
                f"{self._fmt_signed(result.oi_quantity_change_pct or 0.0)} | "
                f"{price} | {ticker}"
            )
        return "\n".join(lines).strip()
