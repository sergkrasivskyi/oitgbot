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

## Planned roadmap (Tasks 22-27 are not yet implemented)

- Task 22 — Grid Candidate research: smooth OI build-up -> price weakness ->
  price stabilization. This hypothesis is not simply `OI up + Price down`; the
  key is smooth OI accumulation followed by price stabilization while OI
  remains elevated.
- Task 23 — Impulse -> Pullback -> Continuation research: positive OI+price
  impulse, pullback depth, OI behavior during pullback, high reclaim, and later
  outcomes. Capture the absolute price path/high/low data so pullback depth and
  subsequent high reclaim can be measured statistically.
- Task 24 — Telegram report UX system with explicit type + horizon headings.
- Task 25 — 60m/120m accumulation product decision based on collected data.
- Task 26 — Statistical tuning of production 5m/20m thresholds and a decision
  on negative OI impulses.
- Task 27 — Remaining operational hardening: env/CRLF resilience,
  restart-state field validation, diagnostics, tablet Git workflow,
  Termux:Boot/autostart, and one-instance health.

Production thresholds remain unchanged until research telemetry provides
sufficient evidence.
