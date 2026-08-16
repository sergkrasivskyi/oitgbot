# Current project handoff

## Current state

- Task 15: CODE PASS; production 5m IMPULSE rolling cutover complete.
- Task 16: ongoing non-blocking live stabilization; field validation continues
  during development.
- Task 17: production 20m TOP cut over to rolling current OI quantity.

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

## Near-term roadmap

- Task 17: rolling 20m TOP / accumulation production cutover.
- Task 18: tablet test release.

Task 18 will prepare a long-running field-test version containing production
rolling 5m IMPULSE, production rolling 20m TOP, `bot.log`, `rolling_oi.log`,
`rolling_oi_signal_state.json`, tablet deployment/runtime checks, and a simple
log export utility. The planned `deploy/tablet/collect-logs.sh` should create a
timestamped ZIP containing `bot.log`, `rolling_oi.log`, the signal-state JSON,
and `runtime_info.txt`. Runtime metadata should include the git commit, branch,
Python version, timezone/current time, and useful process/runtime information.

The tablet release and export script are not part of Task 17. Decisions about
Price Action + OI classification, 60m/120m accumulation, threshold tuning, and
possible cadence optimization will follow several days of observed tablet data.
