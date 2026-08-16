from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .rolling_oi_signal_state import (
    RollingOISignalState,
    RollingOISignalStateMachine,
)

logger = logging.getLogger("oitgbot.rolling.signal_persistence")


class RollingOISignalStatePersistence:
    VERSION = 1

    def __init__(self, path: str, ttl_minutes: float = 15.0) -> None:
        if ttl_minutes <= 0:
            raise ValueError("ttl_minutes must be positive")
        self.path = Path(path)
        self.ttl = timedelta(minutes=float(ttl_minutes))

    def load(
        self,
        machine: RollingOISignalStateMachine,
        reference_utc: datetime,
    ) -> int:
        if not self.path.exists():
            logger.info("ROLLING_SIGNAL_STATE status=missing path=%s", self.path)
            return 0
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != self.VERSION or not isinstance(
                payload.get("states"), dict
            ):
                raise ValueError("unsupported state schema")
            valid_states: list[tuple[str, RollingOISignalState, datetime]] = []
            expired = 0
            for symbol, raw in payload["states"].items():
                if not isinstance(symbol, str) or not isinstance(raw, dict):
                    raise ValueError("invalid state entry")
                state = RollingOISignalState(raw["state"])
                transitioned = datetime.fromisoformat(raw["transitioned_at_utc"])
                if transitioned.tzinfo is None:
                    raise ValueError("state timestamp is not timezone-aware")
                transitioned = transitioned.astimezone(timezone.utc)
                if (
                    reference_utc < transitioned
                    or reference_utc - transitioned > self.ttl
                ):
                    expired += 1
                    continue
                valid_states.append((symbol, state, transitioned))
            for symbol, state, transitioned in valid_states:
                machine.restore(symbol, state, transitioned, reference_utc)
            restored = len(valid_states)
            logger.info(
                "ROLLING_SIGNAL_STATE status=loaded path=%s restored=%d expired=%d",
                self.path,
                restored,
                expired,
            )
            return restored
        except Exception:
            logger.exception(
                "ROLLING_SIGNAL_STATE status=load_failed path=%s", self.path
            )
            return 0

    def save(
        self,
        machine: RollingOISignalStateMachine,
        saved_at_utc: datetime,
    ) -> None:
        states = {}
        for symbol, state in machine.snapshot().items():
            if state.transitioned_at_utc is None:
                continue
            states[symbol] = {
                "state": state.state.value,
                "transitioned_at_utc": state.transitioned_at_utc.isoformat(),
            }
        payload = {
            "version": self.VERSION,
            "saved_at_utc": saved_at_utc.isoformat(),
            "states": states,
        }
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            logger.info(
                "ROLLING_SIGNAL_STATE status=saved path=%s states=%d",
                self.path,
                len(states),
            )
        except Exception:
            if temporary_path is not None:
                try:
                    Path(temporary_path).unlink(missing_ok=True)
                except OSError:
                    pass
            raise
