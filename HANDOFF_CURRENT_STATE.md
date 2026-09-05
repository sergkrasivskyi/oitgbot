# Current project handoff

## Current state

- Task 15: CODE + LIVE PASS; production 5m IMPULSE rolling cutover complete.
- Task 16: main stabilization checks passed; restart-state validation can
  continue during the tablet field test.
- Task 17: CODE + LIVE PASS; production 20m TOP cut over to rolling current OI
  quantity.
- Task 18: tablet field-test release prepared with the same Telegram
  destinations, Ubuntu setup/check/run scripts, and ZIP export to Android
  shared Downloads.
- Task 20: completed collector snapshot implemented for production 20m TOP.
- Task 21: durable long-term OI + Price research telemetry implemented.
- Task 23: analytics core implemented with a read-only offline CLI.
- Task 24: pure 15m eligibility, Z-first ranking and restart-reconstructible NEW(12h)
  semantics implemented. Production remains 5m IMPULSE + 20m TOP; 15m has no
  runtime wiring, Telegram report or permanent NEW state file.

## Product architecture after Task 21

- 5m IMPULSE → rolling current OI quantity → production, event-driven at 5%
  trigger / 3% rearm.
- 20m TOP → rolling current OI quantity → production, scheduled at minutes
  0/20/40, second 10, with a +1% threshold.
- 60m → rolling shadow/observational analytics only.
- 120m → rolling shadow/observational analytics only.
- Historical OI → no longer part of normal production 5m/20m signaling.

OI quantity (Coins) is the primary metric. OI USD and price are optional
context. A fully successful collector cycle now builds and atomically publishes
an immutable 20m TOP snapshot after the full eligible universe is complete.
The scheduled TOP job reads only the latest fresh completed snapshot. While a
new cycle updates the mutable RollingOIStore, TOP continues using the preceding
completed snapshot. Partial, failed, skipped, and timed-out cycles retain that
previous good snapshot.

The TOP job adds no historical OI, current-OI, kline, or other Binance request.
If no completed snapshot exists, the snapshot is stale, or 20m data is still
warming, TOP skips safely. After a cold restart, TOP warms naturally for
approximately 20 minutes and has no historical fallback.

Two data layers are now intentionally separate:

- FAST production: the unchanged 30-second, 150-minute RollingOIStore feeds 5m
  IMPULSE, completed-cycle 20m TOP, and observational 60m/120m analytics.
- LONG research: accepted current-OI observations plus every validated ~1s
  mark-price WebSocket event build fixed UTC five-minute bars. A single
  background writer persists them to `state/oi_research.sqlite3` in WAL mode.

The research schema stores OI and price OHLC, sample counts, first/last source
timestamps, bucket identity, and explicit closed/partial status. Closed bars are
the default query/export scope. Retention defaults to 14 days and is configured
with `RESEARCH_TELEMETRY_RETENTION_DAYS`; disable the layer with
`RESEARCH_TELEMETRY_ENABLED=0`. Telemetry failures are isolated from production.
There is no backfill, new Binance request, or new WebSocket.

Recent data can be exported read-only as compressed CSV:

`python -m tools.research_telemetry_export --hours 96 --output research-96h.csv.gz`

For a development-laptop real-data soak, set `TELEGRAM_PUBLISH_ENABLED=0` and
`RESEARCH_TELEMETRY_DB_PATH=state/oi_research_test.sqlite3` (with
`RESEARCH_TELEMETRY_ENABLED=1` and the normal 14-day
`RESEARCH_TELEMETRY_RETENTION_DAYS`). Run the ordinary bot, wait 15-30 minutes,
then inspect the isolated DB without any Binance or Telegram request:

`python -m tools.research_telemetry_status --db state/oi_research_test.sqlite3`

The status output covers closed/partial counts, OI and price sample quality,
integrity, and the latest closed bucket. Export the same isolated DB with the
existing `--db` override; never mix the test and production research databases.

Normal tablet diagnostic ZIPs include all existing rotations of both logs but
exclude the research database and its WAL/SHM files.

## Approved next target and roadmap (Tasks 22-27)

Current production is still 5m IMPULSE plus scheduled 20m TOP. Task 22 is
documentation-only: it approves, but does not implement or deploy, the future
replacement of 20m TOP by UTC-aligned 15m OI ANOMALY TOP. See
`docs/15m_oi_anomaly_top_spec.md` for the implementation-ready product contract.

- Task 22 — product/specification documentation rebaseline (this task).
- Task 23 — analytics core implemented: aligned aggregation, classic/robust Z,
  percentile, coverage, read-only SQLite CLI and CSV; no runtime/Telegram changes.
- Task 24 — implemented: eligibility, deterministic Z-first ranking, NEW(12h)
  history reconstruction, read-only candidate CLI and CSV; no runtime/Telegram changes.
- Task 25 — implemented opt-in live 15m runtime and staging Telegram publication;
  safe defaults leave existing 20m TOP production behavior unchanged.
- Task 26 — production cutover: replace 20m TOP with 15m anomaly TOP while
  preserving 5m IMPULSE and ALL/PROP routing.
- Task 27 — live stabilization and final documentation cleanup; only then may
  README describe 15m as current production.

## Deferred backlog (not cancelled; no new task numbers)

- Grid Candidate research.
- Impulse -> Pullback -> Continuation research.
- 60m/120m accumulation product decision.
- Production threshold tuning, including negative OI impulses.
- Operational hardening (env/CRLF, restart field validation, diagnostics,
  tablet Git workflow, Termux:Boot/autostart, and one-instance health).
Production thresholds remain unchanged until research telemetry provides
sufficient evidence.

## Task 25 status

Implemented the live 15m anomaly runtime and staging Telegram publication behind
safe-off feature flags. Runtime startup is bounded/read-only, establishes a
non-published reference, and steady state is incremental, retryable and
exactly-once per successful interval. NEW is now locked to 12h: `True` requires
48/48 valid aligned prior intervals and no eligible prior; incomplete quiet
history is `None` and displays `⏳`; any known eligible prior is `False`.
The default legacy 20m schedule remains enabled and can be independently omitted
with `ROLLING_OI_20M_TOP_ENABLED=0`. No secrets or machine identities are
hard-coded.
Telegram-disabled laptop soak on 2026-09-05 bootstrapped 8,908 aligned
observations across 524 historical symbols in about 0.72 seconds. It retried the
pre-start incomplete interval, then processed `10:30-10:45 UTC` once from 1,566
source rows: 522 valid observations, 6 eligible candidates, all retained with
`Z=N/A` and `is_new=None` because fresh 12h coverage was incomplete. Repeated
wakeups did not duplicate the interval. The legacy 20m scheduler was disabled
for the soak while the implementation remained available. No real Telegram
message was sent; manual staging publication is intentionally left to the user.
The process was stopped after the successful interval; deterministic runtime
shutdown is covered by the automated suite.