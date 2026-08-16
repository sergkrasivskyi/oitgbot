from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip() == "1"


def _get_int(name: str, default: str) -> int:
    return int(os.environ.get(name, default))


def _get_float(name: str, default: str) -> float:
    return float(os.environ.get(name, default))


def _get_set(name: str) -> set[str]:
    raw = os.environ.get(name, "")
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(default_factory=lambda: os.environ.get("BOT_TOKEN", ""))
    all_channel_id: str = field(default_factory=lambda: os.environ.get("ALL_CHANNEL_ID", ""))
    prop_channel_id: str = field(default_factory=lambda: os.environ.get("PROP_CHANNEL_ID", ""))
    prop_symbols: set[str] = field(default_factory=lambda: _get_set("PROP_SYMBOLS"))

    impulse_threshold: float = field(default_factory=lambda: _get_float("IMPULSE_THRESHOLD", "5.0"))
    top_threshold: float = field(default_factory=lambda: _get_float("TOP_THRESHOLD", "1.0"))

    send_empty_reports: bool = field(default_factory=lambda: _get_bool("SEND_EMPTY_REPORTS", "0"))
    show_top_when_empty: bool = field(default_factory=lambda: _get_bool("SHOW_TOP_WHEN_EMPTY", "0"))
    top_when_empty_n: int = field(default_factory=lambda: _get_int("TOP_WHEN_EMPTY_N", "10"))

    debug_oi: bool = field(default_factory=lambda: _get_bool("DEBUG_OI", "0"))

    log_file: str = field(default_factory=lambda: os.environ.get("LOG_FILE", "bot.log"))
    rolling_oi_log_file: str = field(
        default_factory=lambda: os.environ.get("ROLLING_OI_LOG_FILE", "rolling_oi.log")
    )
    log_max_bytes: int = field(default_factory=lambda: _get_int("LOG_MAX_BYTES", "5000000"))
    log_backup_count: int = field(default_factory=lambda: _get_int("LOG_BACKUP_COUNT", "5"))

    binance_base_url: str = field(default_factory=lambda: os.environ.get("BINANCE_BASE_URL", "https://fapi.binance.com"))
    http_timeout: int = field(default_factory=lambda: _get_int("HTTP_TIMEOUT", "15"))
    http_retries: int = field(default_factory=lambda: _get_int("HTTP_RETRIES", "2"))

    rolling_oi_shadow_enabled: bool = field(
        default_factory=lambda: _get_bool("ROLLING_OI_SHADOW_ENABLED", "1")
    )
    rolling_oi_cadence_seconds: float = field(
        default_factory=lambda: _get_float("ROLLING_OI_CADENCE_SECONDS", "30")
    )
    rolling_oi_workers: int = field(
        default_factory=lambda: _get_int("ROLLING_OI_WORKERS", "20")
    )
    rolling_oi_retention_minutes: float = field(
        default_factory=lambda: _get_float("ROLLING_OI_RETENTION_MINUTES", "150")
    )
    rolling_oi_price_max_age_seconds: float = field(
        default_factory=lambda: _get_float("ROLLING_OI_PRICE_MAX_AGE_SECONDS", "5")
    )
    rolling_oi_observation_max_age_seconds: float = field(
        default_factory=lambda: _get_float(
            "ROLLING_OI_OBSERVATION_MAX_AGE_SECONDS",
            os.environ.get("ROLLING_OI_MAX_OI_AGE_SECONDS", "60"),
        )
    )
    rolling_oi_transaction_age_warning_seconds: float = field(
        default_factory=lambda: _get_float(
            "ROLLING_OI_TRANSACTION_AGE_WARNING_SECONDS", "60"
        )
    )
    rolling_oi_5m_observation_pct: float = field(
        default_factory=lambda: _get_float("ROLLING_OI_5M_OBSERVATION_PCT", "2")
    )
    rolling_oi_5m_trigger_pct: float = field(
        default_factory=lambda: _get_float("ROLLING_OI_5M_TRIGGER_PCT", "5")
    )
    rolling_oi_5m_rearm_pct: float = field(
        default_factory=lambda: _get_float("ROLLING_OI_5M_REARM_PCT", "3")
    )
    rolling_oi_signal_state_file: str = field(
        default_factory=lambda: os.environ.get(
            "ROLLING_OI_SIGNAL_STATE_FILE", "rolling_oi_signal_state.json"
        )
    )
    rolling_oi_signal_state_ttl_minutes: float = field(
        default_factory=lambda: _get_float(
            "ROLLING_OI_SIGNAL_STATE_TTL_MINUTES", "15"
        )
    )
    rolling_oi_20m_observation_pct: float = field(
        default_factory=lambda: _get_float("ROLLING_OI_20M_OBSERVATION_PCT", "1")
    )
    rolling_oi_60m_observation_pct: float = field(
        default_factory=lambda: _get_float("ROLLING_OI_60M_OBSERVATION_PCT", "3")
    )
    rolling_oi_120m_observation_pct: float = field(
        default_factory=lambda: _get_float("ROLLING_OI_120M_OBSERVATION_PCT", "4")
    )

    max_tg_len: int = 4096

    def validate(self) -> None:
        missing = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.all_channel_id:
            missing.append("ALL_CHANNEL_ID")
        if not self.prop_channel_id:
            missing.append("PROP_CHANNEL_ID")

        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

        if self.rolling_oi_shadow_enabled:
            invalid = []
            if self.rolling_oi_cadence_seconds <= 0:
                invalid.append("ROLLING_OI_CADENCE_SECONDS must be > 0")
            if self.rolling_oi_workers <= 0:
                invalid.append("ROLLING_OI_WORKERS must be > 0")
            if self.rolling_oi_retention_minutes < 120:
                invalid.append("ROLLING_OI_RETENTION_MINUTES must be >= 120")
            if self.rolling_oi_price_max_age_seconds <= 0:
                invalid.append("ROLLING_OI_PRICE_MAX_AGE_SECONDS must be > 0")
            if self.rolling_oi_observation_max_age_seconds <= 0:
                invalid.append(
                    "ROLLING_OI_OBSERVATION_MAX_AGE_SECONDS must be > 0"
                )
            if self.rolling_oi_transaction_age_warning_seconds <= 0:
                invalid.append(
                    "ROLLING_OI_TRANSACTION_AGE_WARNING_SECONDS must be > 0"
                )
            if (
                not math.isfinite(self.rolling_oi_5m_trigger_pct)
                or self.rolling_oi_5m_trigger_pct <= 0
            ):
                invalid.append("ROLLING_OI_5M_TRIGGER_PCT must be finite and > 0")
            if (
                not math.isfinite(self.rolling_oi_5m_rearm_pct)
                or self.rolling_oi_5m_rearm_pct < 0
            ):
                invalid.append("ROLLING_OI_5M_REARM_PCT must be finite and >= 0")
            if (
                math.isfinite(self.rolling_oi_5m_trigger_pct)
                and math.isfinite(self.rolling_oi_5m_rearm_pct)
                and self.rolling_oi_5m_rearm_pct
                >= self.rolling_oi_5m_trigger_pct
            ):
                invalid.append(
                    "ROLLING_OI_5M_REARM_PCT must be less than "
                    "ROLLING_OI_5M_TRIGGER_PCT"
                )
            if (
                not math.isfinite(self.rolling_oi_signal_state_ttl_minutes)
                or self.rolling_oi_signal_state_ttl_minutes <= 0
            ):
                invalid.append(
                    "ROLLING_OI_SIGNAL_STATE_TTL_MINUTES must be finite and > 0"
                )
            if not self.rolling_oi_signal_state_file.strip():
                invalid.append("ROLLING_OI_SIGNAL_STATE_FILE must not be empty")
            if invalid:
                raise RuntimeError("Invalid rolling OI shadow config: " + "; ".join(invalid))


settings = Settings()
