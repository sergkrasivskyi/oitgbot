# OI TG Bot

OI TG Bot collects Binance current open interest and mark-price context for a canonical USD-M USDT perpetual universe, then publishes production alerts and reports to Telegram.

## Current production

Production runs:

- 5m rolling OI IMPULSE with the unchanged 5% trigger and 3% re-arm behavior.
- 15m OI ANOMALY on closed UTC-aligned intervals, with ALL and PROP publication.
- Durable closed 5m research telemetry in SQLite.
- 60m and 120m observational/shadow analytics.

The legacy rolling 20m TOP scheduler is disabled with `ROLLING_OI_20M_TOP_ENABLED=0`. Its implementation remains only for rollback compatibility.

## Analytics

The 5m signal uses rolling current OI quantity only. Collection runs every 30 seconds by default. Price and derived OI USD are display context, never trigger conditions. Persistent trigger state suppresses duplicate alerts for one continuous extreme across a restart; the rolling window itself warms naturally.

The 15m analyzer uses:

- positive eligibility: `OI15 >= +1%`;
- classic same-symbol Z15 from trailing 14-day history;
- at least 96 prior valid observations;
- ranking by finite Z descending, OI% descending, then symbol ascending;
- PX% as context only;
- production ALL and PROP routing.

For an eligible candidate at interval `T`, NEW examines `[T - 12h, T)`. Any known prior eligible occurrence makes `is_new=False`; no prior eligible occurrence makes `is_new=True`. The current interval is excluded. Missing intervals, restarts, downtime, or coverage below 48/48 do not make NEW unknown. Telegram uses `🆕` on NEW rows and includes `🆕 NEW` only when a NEW row appears. Non-NEW rows have no marker.

## OI FLASH v0

Task 29 adds FLASH as an opt-in parallel path. It consumes the same canonical
current-OI observations and mark-price stream as the existing products; it adds
no Binance polling and no second WebSocket.

For each symbol after a completed collector cycle, FLASH targets 60 seconds
before the latest OI observation and selects the latest observation at or before
that target. The baseline must produce an actual window from 60 through 105
seconds. There is no interpolation or future-nearest selection. OI change is
(current / baseline - 1) * 100; changes at or beyond +3.00% and -3.00% qualify.
Live FLASH Telegram rows contain only OI% and the futures ticker. Price remains
in closed-minute research telemetry; missing or stale price never suppresses FLASH.

Publication uses one dedicated configured Telegram destination and batches a
cycle by absolute OI% descending, then symbol. Cooldown is 15 minutes per
(symbol, direction): the opposite direction bypasses and does not reset it.
Accepted events establish cooldown even if Telegram fails. Cooldown restores
from the dedicated database after restart.

The separate state/oi_flash_research.sqlite3 database uses WAL and contains:

- flash_bars_1m: closed UTC calendar-minute OI/price closes, actual observation
  timestamps, consecutive-minute changes, counts, and validity flags; retained
  for seven days.
- flash_events: all threshold crossings reaching the decision stage, including
  cooldown-suppressed crossings and Telegram outcomes; retained indefinitely.

Minute aggregation is queued to a background writer. An event must be durably
stored before Telegram is attempted. A FLASH database failure blocks FLASH
publication but is isolated from collection, 5m/15m products, shadow analytics,
and Task 21 telemetry. See docs/oi_flash_v0_spec.md.

Read-only research tools:

~~~powershell
python -m tools.flash_research_status --db state/oi_flash_research.sqlite3
python -m tools.flash_research_export --db state/oi_flash_research.sqlite3 --hours 168 --output flash-research-7d.csv.gz
~~~

Export creates flash-research-7d-bars.csv.gz and
flash-research-7d-events.csv.gz, rendering stored epoch milliseconds as UTC ISO
timestamps.
## Spot-backed futures universe

The hourly refresh resolves active Binance USD-M USDT perpetual futures against active Binance Spot `TRADING` USDT markets:

1. Exact futures/Spot match.
2. Explicit `DODOX -> DODO` and `LUNA2 -> LUNA` aliases.
3. Verified `1000` or `1000000` multiplier transformation when the transformed Spot market exists.
4. Otherwise unresolved and excluded.

The futures symbol remains the internal and PROP identity. Exclusion happens before current-OI requests, rolling calculations, new telemetry, alerts, reports, and shadow analytics. Existing SQLite history expires normally.

Each refresh makes one bulk Futures and one bulk Spot exchange-info request, with no per-symbol Spot polling. Success uses the normal one-hour TTL. An initial failure with no last-known-good universe returns an empty universe and retries on the next normal collector cycle, about 30 seconds later. A later failure retains the non-empty last-known-good universe under the normal TTL. Failure never falls back to unfiltered futures.

Telegram retains the linked futures ticker. A differing Spot base adds a plain-text hint such as `1000PEPEUSDT · PEPE (S)`, with no Spot link. Exact matches have no suffix.

Read-only live audit:

```powershell
python -m tools.spot_universe_audit
```

It performs only the two exchange-info requests; it does not request current OI, mutate SQLite, or send Telegram.

## Data paths

```text
exchange info -> canonical universe -> current OI REST (~30s)
                                      + mark-price WebSocket context
                                      -> RollingOIStore
                                         |- 5m IMPULSE -> Telegram
                                         |- 15m ANOMALY -> Telegram
                                         |- ~60s FLASH -> dedicated Telegram
                                         |- UTC 1m FLASH research DB
                                         |- 60m/120m shadow
                                         `- UTC 5m Task 21 research DB
```

The SQLite LONG path uses `research_bars_5m` in `state/oi_research.sqlite3` by default, WAL mode, one background writer, and 14-day retention. It stores fixed UTC 5m OI/price OHLC bars, sample metadata, and closed/partial state. There is no historical Binance backfill. Telemetry failures are isolated from live collection and publication.

## Configuration

Keep secrets outside source control.

```env
BOT_TOKEN=your_telegram_bot_token
ALL_CHANNEL_ID=-100...
PROP_CHANNEL_ID=-100...
PROP_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT
TELEGRAM_PUBLISH_ENABLED=1

BINANCE_BASE_URL=https://fapi.binance.com
BINANCE_SPOT_BASE_URL=https://api.binance.com
HTTP_TIMEOUT=5
HTTP_RETRIES=1

ROLLING_OI_SHADOW_ENABLED=1
ROLLING_OI_CADENCE_SECONDS=30
ROLLING_OI_WORKERS=20
ROLLING_OI_RETENTION_MINUTES=150
ROLLING_OI_PRICE_MAX_AGE_SECONDS=5
ROLLING_OI_OBSERVATION_MAX_AGE_SECONDS=60
ROLLING_OI_TRANSACTION_AGE_WARNING_SECONDS=60
ROLLING_OI_5M_OBSERVATION_PCT=2
ROLLING_OI_5M_TRIGGER_PCT=5
ROLLING_OI_5M_REARM_PCT=3
ROLLING_OI_SIGNAL_STATE_FILE=rolling_oi_signal_state.json
ROLLING_OI_SIGNAL_STATE_TTL_MINUTES=15

OI_ANOMALY_15M_ENABLED=1
OI_ANOMALY_15M_TELEGRAM_ENABLED=1
OI_ANOMALY_15M_BASELINE_DAYS=14
OI_ANOMALY_15M_MIN_HISTORY=96
OI_ANOMALY_15M_ELIGIBILITY_PCT=1.0
OI_ANOMALY_15M_NEW_LOOKBACK_HOURS=12
OI_ANOMALY_15M_LOG_TOP_N=20

# Safe defaults are disabled; set the chat ID manually before enabling publication.
OI_FLASH_ENABLED=0
OI_FLASH_TELEGRAM_ENABLED=0
OI_FLASH_TELEGRAM_CHAT_ID=
OI_FLASH_WINDOW_SECONDS=60
OI_FLASH_THRESHOLD_PCT=3.0
OI_FLASH_BASELINE_MAX_LAG_SECONDS=45
OI_FLASH_COOLDOWN_SECONDS=900
OI_FLASH_RESEARCH_DB_PATH=state/oi_flash_research.sqlite3
OI_FLASH_RESEARCH_RETENTION_DAYS=7

ROLLING_OI_20M_TOP_ENABLED=0
ROLLING_OI_20M_OBSERVATION_PCT=1
ROLLING_OI_60M_OBSERVATION_PCT=3
ROLLING_OI_120M_OBSERVATION_PCT=4

RESEARCH_TELEMETRY_ENABLED=1
RESEARCH_TELEMETRY_DB_PATH=state/oi_research.sqlite3
RESEARCH_TELEMETRY_RETENTION_DAYS=14

LOG_FILE=bot.log
ROLLING_OI_LOG_FILE=rolling_oi.log
LOG_MAX_BYTES=5000000
LOG_BACKUP_COUNT=5
```

`ROLLING_OI_OBSERVATION_MAX_AGE_SECONDS` governs local freshness; the old `ROLLING_OI_MAX_OI_AGE_SECONDS` name remains a compatibility fallback. Transaction age is diagnostic only.

## Development and operations

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
python -m pytest
python -m ruff check .
```

Use `TELEGRAM_PUBLISH_ENABLED=0` and a separate `RESEARCH_TELEMETRY_DB_PATH` for a development soak. Read-only inspection and export:

```powershell
python -m tools.research_telemetry_status --db state/oi_research_test.sqlite3
python -m tools.research_telemetry_export --db state/oi_research_test.sqlite3 --hours 4 --output research-test.csv.gz
```

The export supports repeated `--symbol BTCUSDT` filters.

Docker:

```powershell
docker compose up -d --build
docker compose logs -f
docker ps
docker compose restart
docker compose down
```

Compose persists runtime state under the host `state` directory and shuts down gracefully.

## Tablet runtime

The supported stack is Android, Termux, `proot-distro` Ubuntu, this repository, `.venv`, and `run.py`. Keep the environment outside Git at `~/.config/oitgbot/env`, or set `OITGBOT_ENV_FILE`.

```bash
mkdir -p ~/.config/oitgbot
chmod 700 ~/.config/oitgbot
nano ~/.config/oitgbot/env
chmod 600 ~/.config/oitgbot/env

cd <project>
bash deploy/tablet/setup-ubuntu.sh
bash deploy/tablet/check-ubuntu.sh
bash deploy/tablet/run-ubuntu.sh
```

Setup preserves state and performs an import check. Preflight is non-destructive and contacts neither Binance nor Telegram. At manual cutover, stop the prior process first. Allow about five minutes for IMPULSE warm-up and the next closed 15m boundary for ANOMALY.

Export logs without stopping the process:

```bash
bash deploy/tablet/collect-logs.sh
```

The archive contains active and rotated logs, signal state, and a no-secret runtime summary. Research SQLite/WAL/SHM files are excluded. The preferred destination is `/sdcard/Download/OI-bot-logs`, with `/storage/emulated/0/Download/OI-bot-logs` as fallback.

## Logs

- `bot.log`: startup, scheduler, application, and Telegram.
- `rolling_oi.log`: universe, collector, price, signal, anomaly, telemetry, shadow, and rollback-only 20m diagnostics.

```powershell
Get-Content .\rolling_oi.log -Tail 0 -Wait
Get-Content .\rolling_oi.log -Tail 0 -Wait |
    Select-String -Pattern 'ROLLING_SIGNAL|OI_ANOMALY_15M|SPOT_BACKED_UNIVERSE'
```

For `Chat not found`, verify the `-100...` channel IDs and bot posting permission.

## Key paths

- `oitgbot/app.py`: lifecycle and runtime wiring.
- `oitgbot/scheduler_jobs.py`: jobs and universe cache policy.
- `oitgbot/clients/binance_api.py`: Binance REST clients.
- `oitgbot/services/spot_backed_universe.py`: deterministic resolver.
- `oitgbot/services/current_oi_collector.py`: current-OI collection.
- `oitgbot/services/report_formatter.py`: Telegram formatting.
- `tools/spot_universe_audit.py`: read-only universe audit.
- `deploy/tablet/`: tablet operations.

## Task history and roadmap

- Task 25 implemented opt-in live 15m runtime and staging publication.
- On 2026-09-05, `d2aa19735f7597cc7d518cf45526f9de4216fa9e` was manually cut over and validated.
- Task 26 corrected NEW semantics and rebaselined documentation.
- Task 27 production stabilization and observation is **PASS**.
- Task 28 spot-backed universe and hints is **CODE + PRODUCTION LIVE PASS**.
- Task 29 FLASH v0 is **CODE PASS / LIVE VALIDATION PENDING** after local
  verification; do not call it production-live until the separate deployment
  checklist succeeds.

Task 29 changes no 5m/15m analytics, routing, collector cadence, Binance request
load, mark-price subscription, Spot mapping rule, or production 20m flag. It
does not implement Z1m, percentiles, profitability or lead-time correlation,
trading labels, PX gating, or a complex re-arm state machine.

## Security

Never commit `.env`, Telegram tokens, channel credentials, private exports, or production SQLite files.
