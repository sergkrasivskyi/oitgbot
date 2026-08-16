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

## Product architecture after Task 17

- 5m IMPULSE → rolling current OI quantity → production, event-driven at 5%
  trigger / 3% rearm.
- 20m TOP → rolling current OI quantity → production, scheduled at minutes
  0/20/40, second 10, with a +1% threshold.
- 60m → rolling shadow/observational analytics only.
- 120m → rolling shadow/observational analytics only.
- Historical OI → no longer part of normal production 5m/20m signaling.

OI quantity (Coins) is the primary metric. OI USD and price are optional
context. The production TOP job reads the in-memory RollingOIStore and adds no
historical OI, current-OI, or kline request. After a cold restart, TOP warms
naturally for approximately 20 minutes and has no historical fallback.

## Next phase

Run several days of tablet live collection, then analyze real 5m quantity
impulses, rolling 20m accumulation, OI + price relationships, threshold
quality, future Price Action + OI regimes, and possible 60m/120m
productization.
