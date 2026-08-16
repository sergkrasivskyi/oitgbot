# Current project handoff

## Completed state

- Tasks 7–14 are complete.
- Task 13 rolling 5m signal state machine: CODE PASS and LIVE PASS.
- Task 14 split runtime logging: CODE PASS and LIVE PASS.
- Task 15 is the rolling-current-OI production cutover for 5m IMPULSE alerts.

## Product decisions

- OI quantity (Coins) is the primary metric. OI USD is optional context.
- Price is optional context and never gates a quantity-based signal.
- The time-sensitive 5m IMPULSE product is migrated before 20m TOP.
- Legacy historical 5m is not a production fallback or a long-term publisher.
- Legacy code remains only where still required, especially for 20m TOP.
- Telegram UX stays close to the existing report during cutover.
- A faster cadence such as 20 seconds is deferred until production is stable.

## Roadmap

- Task 15: 5m IMPULSE → rolling production.
- Task 16: post-cutover live validation, stabilization, and focused cleanup.
- Task 17: 20m TOP → rolling 20m.
- Task 18: 60m Slow Accumulation product/report design and implementation.
- Task 19: 120m Long Accumulation, after validating the value of the 60m layer.

Task 16 is a production stabilization gate, not another feature migration. Live
validation must cover actual rolling Telegram timing, persistent-extreme
deduplication, REARM, restart duplicate suppression, Telegram failure
observability, collector timing, quantity and price-context coverage, no 20m TOP
regression, and no rolling/legacy double publishing.
