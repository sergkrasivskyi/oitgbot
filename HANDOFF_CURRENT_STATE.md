# Current project handoff

## Current production

Production is **5m rolling OI IMPULSE + 15m OI ANOMALY**.

- 5m IMPULSE remains enabled and unchanged.
- 15m OI ANOMALY and its ALL/PROP Telegram publication are enabled.
- The legacy 20m TOP scheduler is disabled in production through
  `ROLLING_OI_20M_TOP_ENABLED=0`.
- The legacy 20m implementation remains in code for rollback compatibility.
- Production bootstrap supplied sufficient numeric-Z history for all 522 current
  symbols, and real numeric-Z 15m reports were validated in ALL and PROP.

On 2026-09-05, commit `d2aa19735f7597cc7d518cf45526f9de4216fa9e` was manually
cut over and successfully validated in production. Task 25 implemented the
opt-in runtime and staging publication; Task 26 corrects NEW semantics and
rebaselines documentation after that cutover.

## 15m product contract

15m reports use closed UTC-aligned intervals. Eligibility is positive
`OI15% >= +1%`; classic same-symbol Z15 uses trailing 14-day history with at
least 96 valid prior observations. Finite Z ranks descending, then OI%, then
symbol. PX% is context only.

NEW checks prior eligible same-symbol candidates in `[T - 12h, T)`. Any known
prior occurrence yields `is_new=False`; no known occurrence yields
`is_new=True`. Gaps, restarts, downtime, and coverage below 48/48 do not create
an unknown state or suppress a candidate. The current interval never counts as
its own prior occurrence. Telegram uses `🆕` for NEW rows, no marker otherwise,
and shows `🆕 NEW` only when at least one NEW row exists.

## Runtime and rollback

The live runtime uses existing durable 5m telemetry, one bounded SQLite
bootstrap, and incremental processing of new closed intervals. It does not add
Binance requests, WebSockets, schema changes, or collector-cadence changes.
Its restart reference prevents historical replay. The 20m code remains available
for rollback; the 5m IMPULSE path is unaffected.

## Roadmap

Task 27 is production stabilization and observation, not a preplanned product
change. Deferred work remains unnumbered. A possible future research direction,
OI Z-SCORE FLASH, would investigate near-online 1m positive Z > +3 and negative
Z < -3 OI anomalies, with OI% and PX% as context only and no directional trading
interpretation. It requires 1m telemetry, shadow validation, and anti-spam
trigger/rearm design before any Telegram publication.