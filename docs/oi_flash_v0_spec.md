# OI FLASH v0 specification

Status: Task 29 code implementation. Production live validation is pending.

## Inputs and universe

FLASH consumes only the existing current-OI collector observations,
`RollingOIStore`, and single all-market mark-price WebSocket. Its universe is
the Task 28 canonical spot-backed futures set. It introduces no Binance request,
polling loop, second WebSocket, or Spot-price lookup.

## Live calculation

For the latest valid symbol observation at `current_observed_at`:

```text
target_time = current_observed_at - 60 seconds
baseline = latest valid OI observation at or before target_time
oi_pct = (current_oi / baseline_oi - 1) * 100
```

The baseline must be no more than 45 seconds before the target, so the actual
window is inclusively 60 through 105 seconds. There is no interpolation and no
future-nearest selection. `oi_pct >= +3.00` is positive; `oi_pct <= -3.00` is
negative. OI% is the only eligibility input.

The live FLASH Telegram signal is OI-only: `OI +3.74% | Ticker`, with no PX.
Missing, stale, future, or unmatched rolling price context never suppresses
FLASH detection, acceptance, or publication. Price remains research telemetry
in closed UTC-minute bars, separate from the live signal. Optional event PX
values remain compatible with the existing nullable SQLite column.

## Cooldown and publication

Cooldown is 900 seconds per `(symbol, direction)`. Same-direction crossings
inside the interval are stored but suppressed. A crossing at exactly 900 seconds
is accepted. Opposite direction is independent and does not reset the prior
direction. Every accepted non-suppressed event establishes cooldown even if
Telegram fails. Startup restores the latest accepted time from SQLite and tracks
whether that restore succeeded separately from the restored map, for which empty is
a valid loaded state. After a startup restore failure, an otherwise qualifying
decision retries the restore lazily. FLASH decisions, event insertion, and Telegram
remain fail-closed until a restore succeeds; recovered state is used immediately.

Accepted events in one cycle are sorted by absolute OI% descending and symbol
ascending, then batched/chunked to the dedicated FLASH chat through the existing
sender. Futures CoinGlass links and Task 28 plain-text Spot hints are reused.

## Persistence

The default database is `state/oi_flash_research.sqlite3`, separate from Task 21.
All stored timestamps are UTC epoch milliseconds.

`flash_bars_1m` contains closed UTC calendar-minute records keyed by symbol and
minute. It stores the latest OI and price observations belonging to the minute,
their actual timestamps, consecutive-minute OI/PX changes, observation counts,
and independent validity flags. Missing current or immediately preceding valid
minute data yields a null change. Bars are pruned in batches after seven days.

`flash_events` stores every threshold crossing reaching cooldown evaluation:
detection/sampling timestamps, symbol, direction, OI/PX, actual window,
threshold, suppression, and Telegram attempt/success flags. Events are not
automatically pruned in Task 29.

Minute writes use a background queue. Crossing insertion is durably committed
before Telegram is marked attempted or called. Failure to insert blocks FLASH
publication. All FLASH exceptions are isolated from existing production and
Task 21 telemetry; there is no in-memory-only fallback.

## Safe defaults

```env
OI_FLASH_ENABLED=0
OI_FLASH_TELEGRAM_ENABLED=0
OI_FLASH_TELEGRAM_CHAT_ID=
OI_FLASH_WINDOW_SECONDS=60
OI_FLASH_THRESHOLD_PCT=3.0
OI_FLASH_BASELINE_MAX_LAG_SECONDS=45
OI_FLASH_COOLDOWN_SECONDS=900
OI_FLASH_RESEARCH_DB_PATH=state/oi_flash_research.sqlite3
OI_FLASH_RESEARCH_RETENTION_DAYS=7
```

## Research tooling and non-goals

`tools.flash_research_status` reports schema/integrity, size, bar range, recent
coverage, and crossing/publication counts. `tools.flash_research_export` writes
separate gzip CSV bars/events files and renders timestamps as UTC ISO strings.

Task 29 does not implement Z1m, robust Z, percentile ranking, automated lead-time
or profitability studies, success labels, trading advice, PX gating, or complex
re-arm/persistence logic.
