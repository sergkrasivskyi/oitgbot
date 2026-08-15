from __future__ import annotations

import html

from ..models import OIRow


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