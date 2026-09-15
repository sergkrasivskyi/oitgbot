# Current project handoff

## Status

Task 28 spot-backed universe and ticker hints is **CODE + PRODUCTION LIVE PASS**.
Current production is 5m rolling OI IMPULSE plus 15m OI ANOMALY with ALL/PROP
publication over the canonical spot-backed futures universe. The legacy 20m TOP
scheduler remains disabled with ROLLING_OI_20M_TOP_ENABLED=0 and exists only for
rollback.

Task 29 implements opt-in 1m OI FLASH v0 and dedicated research telemetry. Its
status is **CODE PASS / LIVE VALIDATION PENDING** after local verification. The
base implementation commit is `e53f37bf7150e3ebdef23e969526a069af71d7b7`
with message "feat: add 1m oi flash research pipeline". The cooldown-recovery
and OI-only live-output addenda are also CODE PASS. Do not mark LIVE PASS until
the laptop/tablet checklist succeeds.

## Task 29 live semantics

After each completed existing current-OI cycle, for each canonical Task 28 symbol:

1. Set target = current_observed_at - 60 seconds.
2. Select the latest valid OI observation at or before target. Never interpolate
   or select a future-nearest observation.
3. Require an actual window in [60s, 105s], using the 45-second maximum baseline
   lag.
4. Calculate (current_oi / baseline_oi - 1) * 100.
5. Trigger inclusively at >= +3.00% or <= -3.00%.

There is no Z score, percentile, persistence gate, PX gate, trading direction,
or squeeze/liquidation interpretation. Live FLASH Telegram is OI-only. Missing,
stale, or unmatched price never suppresses detection, acceptance, or publication.
Price close/change/validity telemetry remains in closed UTC-minute research bars,
separate from the live signal; nullable historical event PX stays compatible.

Cooldown is 900 seconds per (futures symbol, direction). The opposite direction
bypasses and does not reset the other direction. Elapsed time exactly 900 seconds
is allowed. Cooldown begins with every durably accepted event regardless of
Telegram success and is reconstructed from non-suppressed flash_events rows
after restart. Startup distinguishes a successfully restored empty cooldown map
from an unavailable cooldown state. After a failed startup restore, qualifying
FLASH decisions retry lazily and remain fail-closed—with no event acceptance or
Telegram send—until SQLite restoration succeeds.

## Telegram and failure boundary

FLASH uses the existing bot/sender but one dedicated configured chat. A cycle is
ordered by absolute OI% descending then symbol and batched/chunked.

~~~text
⚡ OI FLASH · 1m

OI +3.74% | THETAUSDT
OI +3.21% | 1000PEPEUSDT · PEPE (S)
~~~

Tickers keep CoinGlass futures links. Task 28 Spot hints remain plain text.

Every crossing reaching the decision stage is stored, including cooldown
suppression. An accepted event is committed before Telegram is attempted. If
that write fails, Telegram is not attempted. FLASH DB, aggregation, formatting,
or publication failure is isolated from the collector, mark-price stream,
5m/15m products, 60m/120m shadow, Task 21 telemetry, and startup. There is no
in-memory-only persistence fallback.

## Configuration defaults

~~~env
OI_FLASH_ENABLED=0
OI_FLASH_TELEGRAM_ENABLED=0
OI_FLASH_TELEGRAM_CHAT_ID=
OI_FLASH_WINDOW_SECONDS=60
OI_FLASH_THRESHOLD_PCT=3.0
OI_FLASH_BASELINE_MAX_LAG_SECONDS=45
OI_FLASH_COOLDOWN_SECONDS=900
OI_FLASH_RESEARCH_DB_PATH=state/oi_flash_research.sqlite3
OI_FLASH_RESEARCH_RETENTION_DAYS=7
~~~

Both enable flags default off. FLASH Telegram also requires global Telegram
publication and a non-empty external chat ID. Task 29 commits no secret or
production environment change.

## Dedicated research database

state/oi_flash_research.sqlite3 is separate from Task 21 state/oi_research.sqlite3,
uses WAL, and has two primary tables:

- flash_bars_1m: closed UTC calendar-minute OI/price closes, actual epoch-ms
  timestamps, consecutive-minute OI/PX changes, counts, and independent validity
  flags. Bars are pruned in batches after seven days.
- flash_events: exact rolling crossings, actual window/timestamps, threshold,
  cooldown decision, and Telegram attempt/success state. Events have no Task 29
  retention.

Minute aggregation uses background queued writes. Status/export tools are
read-only and render epoch milliseconds as UTC ISO timestamps. Export creates
separate -bars.csv.gz and -events.csv.gz files.
## Unchanged production contracts and non-goals

Task 29 adds no current-OI request, Spot price request, polling loop, or second
mark-price WebSocket. It consumes the existing RollingOIStore and existing
OI/price observers after Task 28 filtering. It does not change 5m IMPULSE math,
state, routing, or formatting; 15m anomaly math, Z, NEW, ranking, routing, or
formatting; Task 21 schema/retention; Task 28 mapping; collector cadence; or the
legacy 20m flag.

Task 29 deliberately does not implement Z1m, robust Z, percentiles, automated
FLASH-to-5m/15m correlation, profitability, success labels, BUY/SELL or
LONG/SHORT interpretation, PX eligibility, or complex re-arm logic.

## Validation and next steps

Local CODE PASS verification: focused FLASH tests passed 26 tests plus 10
subtests; the full repository passed 323 tests plus 61 subtests. Ruff on the
changed Python files, compile/import sanity, and git diff --check passed before
commit. Repository-wide Ruff still reports 51 pre-existing legacy violations.

Laptop forward-validation with no Telegram sends:

~~~powershell
$env:TELEGRAM_PUBLISH_ENABLED='0'
$env:OI_FLASH_ENABLED='1'
$env:OI_FLASH_TELEGRAM_ENABLED='0'
$env:OI_FLASH_RESEARCH_DB_PATH='state/oi_flash_research_test.sqlite3'
python run.py
python -m tools.flash_research_status --db state/oi_flash_research_test.sqlite3
python -m tools.flash_research_export --db state/oi_flash_research_test.sqlite3 --hours 4 --output flash-laptop-4h.csv.gz
~~~

Tablet deployment/validation, only after the commit is available to the tablet:

~~~bash
cd <project>
git pull --ff-only
nano ~/.config/oitgbot/env
# Manually add OI_FLASH_ENABLED=1, OI_FLASH_TELEGRAM_ENABLED=1,
# OI_FLASH_TELEGRAM_CHAT_ID=<dedicated channel>, and optional explicit defaults.
bash deploy/tablet/check-ubuntu.sh
ps -ef | grep -E '[p]ython.*run.py'
bash deploy/tablet/run-ubuntu.sh
python -m tools.flash_research_status --db state/oi_flash_research.sqlite3
grep -E 'OI_FLASH_(STATUS|EVENT|SUPPRESSED|PUBLISH|RESEARCH|RETENTION)' rolling_oi.log
~~~

Stop the prior process before starting the updated runtime and never run two
production instances intentionally. Validate dedicated-channel formatting,
cooldown behavior, DB growth, 5m/15m continuity, request-rate continuity, and
logs before marking Task 29 LIVE PASS.
