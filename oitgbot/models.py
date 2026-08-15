from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class OIRow:
    symbol: str
    oi_pct: float
    price_pct: float = 0.0