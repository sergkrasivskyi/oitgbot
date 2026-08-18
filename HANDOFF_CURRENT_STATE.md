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

## Product architecture after Task 20

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

## Planned roadmap (not yet implemented)

- Task 21 — Long-term research telemetry for OI + Price, suitable for
  1h/2h/6h/12h/24h/48h/72h analysis and multi-day retention.
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
