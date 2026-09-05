# OI TG Bot (Binance Futures в†’ Telegram)

Р‘РѕС‚ СЃРєР°РЅСѓС” **Binance USDв“€-M Perpetual (USDT)** С„вЂ™СЋС‡РµСЂСЃРё, СЂР°С…СѓС” Р·РјС–РЅСѓ **Open Interest (OI)** С‚Р° **С†С–РЅРё**, С– РїСѓР±Р»С–РєСѓС” Р·РІС–С‚Рё РІ Telegram-РєР°РЅР°Р»Рё Р·Р° СЂРѕР·РєР»Р°РґРѕРј.

## РњРѕР¶Р»РёРІРѕСЃС‚С–

- **OI Binance HH** вЂ” вЂњС–РјРїСѓР»СЊСЃРёвЂќ: OI Р·Р° 5 С…РІРёР»РёРЅ >= `IMPULSE_THRESHOLD`
- **OI Binance All** вЂ” С‚РѕРї РїРѕ СЂРѕСЃС‚Сѓ OI Р·Р° 20 С…РІРёР»РёРЅ >= `TOP_THRESHOLD`
- Р¤РѕСЂРјР°С‚ Р·РІС–С‚Сѓ: `OI% | PX% | Ticker` (С‚С–РєРµСЂРё РєР»С–РєР°Р±РµР»СЊРЅС– в†’ Coinglass)
- Р¤С–Р»СЊС‚СЂР°С†С–СЏ С–РЅСЃС‚СЂСѓРјРµРЅС‚С–РІ:
  - `PERPETUAL`
  - Р»РёС€Рµ `...USDT`
  - Р»РёС€Рµ ASCII (Р±РµР· С–С”СЂРѕРіР»С–С„С–РІ)
- РџР°СЂР°Р»РµР»СЊРЅРµ СЃРєР°РЅСѓРІР°РЅРЅСЏ (ThreadPool) РґР»СЏ С€РІРёРґРєРѕСЃС‚С–
- РљРµС€ СЃРёРјРІРѕР»С–РІ (TTL 1 РіРѕРґРёРЅР°)
- РЎС‚С–Р№РєС–СЃС‚СЊ РґРѕ РЅРµСЃС‚Р°Р±С–Р»СЊРЅРѕРіРѕ С–РЅС‚РµСЂРЅРµС‚Сѓ:
  - Binance timeout/retry
  - Telegram timeout + 1 retry
  - РїРѕРјРёР»РєРё РІС–РґРїСЂР°РІРєРё РЅРµ РІР°Р»СЏС‚СЊ scheduler

---

## РЎС‚СЂСѓРєС‚СѓСЂР° РїСЂРѕС”РєС‚Сѓ

```

.
в”њв”Ђ oitgbot/
в”‚  в”њв”Ђ app.py
в”‚  в”њв”Ђ config.py
в”‚  в”њв”Ђ logger_setup.py
в”‚  в”њв”Ђ models.py
в”‚  в”њв”Ђ scheduler_jobs.py
в”‚  в”њв”Ђ clients/
в”‚  в”‚  в”њв”Ђ binance_api.py
в”‚  в”‚  в””в”Ђ telegram_sender.py
в”‚  в””в”Ђ services/
в”‚     в”њв”Ђ oi_scanner.py
в”‚     в””в”Ђ report_formatter.py
в”њв”Ђ run.py
в”њв”Ђ requirements.txt
в”њв”Ђ .env
в”њв”Ђ Dockerfile
в”њв”Ђ docker-compose.yml
в””в”Ђ .dockerignore

````

---

## РќР°Р»Р°С€С‚СѓРІР°РЅРЅСЏ `.env`

РЎС‚РІРѕСЂРё/РѕРЅРѕРІРё С„Р°Р№Р» `.env` РІ РєРѕСЂРµРЅС– РїСЂРѕС”РєС‚Сѓ:

```env
BOT_TOKEN=your_telegram_bot_token
ALL_CHANNEL_ID=<your_all_channel_id>
PROP_CHANNEL_ID=<your_prop_channel_id>
TELEGRAM_PUBLISH_ENABLED=1

# РЎРїРёСЃРѕРє "РѕР±СЂР°РЅРёС…" СЃРёРјРІРѕР»С–РІ РґР»СЏ РґСЂСѓРіРѕРіРѕ РєР°РЅР°Р»Сѓ (РѕРїС†С–Р№РЅРѕ)
PROP_SYMBOLS=BTCUSDT,ETHUSDT,SOLUSDT

IMPULSE_THRESHOLD=5.0
TOP_THRESHOLD=1.0

# РџРѕСЂРѕР¶РЅС– Р·РІС–С‚Рё (1 = РЅР°РґСЃРёР»Р°С‚Рё, 0 = РЅРµ РЅР°РґСЃРёР»Р°С‚Рё)
SEND_EMPTY_REPORTS=0

# РЇРєС‰Рѕ С–РјРїСѓР»СЊСЃС–РІ РЅРµРјР°, РјРѕР¶РЅР° СЃР»Р°С‚Рё fallback TOP-N Р·Р° OI_5m
SHOW_TOP_WHEN_EMPTY=0
TOP_WHEN_EMPTY_N=10

DEBUG_OI=0

LOG_FILE=bot.log
ROLLING_OI_LOG_FILE=rolling_oi.log
LOG_MAX_BYTES=5000000
LOG_BACKUP_COUNT=5

BINANCE_BASE_URL=https://fapi.binance.com
HTTP_TIMEOUT=5
HTTP_RETRIES=1

# Rolling OI runtime (production 5m, live 15m, and legacy 20m rollback)
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
ROLLING_OI_20M_OBSERVATION_PCT=1
ROLLING_OI_60M_OBSERVATION_PCT=3
ROLLING_OI_120M_OBSERVATION_PCT=4
ROLLING_OI_20M_TOP_ENABLED=0

# Durable LONG research layer (enabled by default)
RESEARCH_TELEMETRY_ENABLED=1
RESEARCH_TELEMETRY_DB_PATH=state/oi_research.sqlite3
RESEARCH_TELEMETRY_RETENTION_DAYS=14
````

`ROLLING_OI_OBSERVATION_MAX_AGE_SECONDS` controls freshness of the bot's local
observation clock. The former `ROLLING_OI_MAX_OI_AGE_SECONDS` name is accepted
as a compatibility fallback but no longer rejects present OI because Binance's
transaction timestamp is old. `ROLLING_OI_TRANSACTION_AGE_WARNING_SECONDS`
controls diagnostics only.

The production 5m IMPULSE signal uses rolling current OI quantity only. It is
evaluated after each rolling collection cycle (30 seconds by default), triggers
at `ROLLING_OI_5M_TRIGGER_PCT` (5%), and re-arms at
`ROLLING_OI_5M_REARM_PCT` (3%). A persistent positive or negative extreme
produces one Telegram alert; REARM is diagnostic only and permits a later new
crossing. Price and derived OI USD are optional display context, never trigger
conditions.

Recent per-symbol triggered state is written atomically to
`ROLLING_OI_SIGNAL_STATE_FILE`. On restart it suppresses a duplicate alert for
the same continuous extreme. Restored state expires after
`ROLLING_OI_SIGNAL_STATE_TTL_MINUTES` (15 minutes by default) unless a valid
rolling observation confirms it. Missing, corrupt, incompatible, or stale state
starts safely. The rolling data window itself is never seeded from historical OI
and still warms naturally.

### FAST production vs LONG research data

The production and research paths are deliberately independent:

```text
FAST: 30s RollingOIStore -> 5m IMPULSE / live 15m OI ANOMALY / 60m+120m shadow
LONG: UTC 5m research bars -> SQLite -> offline 1h/2h/6h/12h/24h/48h/72h research
```

The LONG layer is enabled by default and writes to
`state/oi_research.sqlite3`, with WAL mode, a single background writer, and
14-day retention. Disable it with `RESEARCH_TELEMETRY_ENABLED=0`. Telemetry
startup/write/observer failures are isolated and never stop or suppress the
collector, price stream, production signals, TOP snapshots, or Telegram.

Each logical `symbol + bucket_start_utc` row represents a fixed, UTC-aligned
five-minute bucket. OI open/high/low/close, count, and first/last observation
timestamps come only from valid current-OI samples accepted by
`RollingOIStore`. Price open/high/low/close, count, and first/last event
timestamps come from every validated event on the existing all-market ~1s
mark-price WebSocket, preserving intrabucket price highs/lows without another
connection. Rows also carry `is_closed`; normal reads and exports include only
closed buckets. A graceful shutdown may store the current bucket as an explicit
partial row, which is safely merged if collection resumes in that bucket.

The versioned SQLite table is `research_bars_5m`. Its persisted research fields
are `symbol`, `bucket_start_utc`, `oi_open`, `oi_high`, `oi_low`, `oi_close`,
`oi_sample_count`, `first_oi_observed_at_utc`, `last_oi_observed_at_utc`,
`price_open`, `price_high`, `price_low`, `price_close`, `price_sample_count`,
`first_price_event_at_utc`, `last_price_event_at_utc`, and `is_closed` (plus an
internal update timestamp). No historical Binance backfill is performed.

Export a recent closed-bar range without copying the database:

```bash
python -m tools.research_telemetry_export --hours 96 --output research-96h.csv.gz
```

Use repeated `--symbol BTCUSDT` options for optional filtering. The command is
read-only and reports row count, first/last bucket, and output path.

### Local live soak (development laptop)

For a real-data development soak, use a separate research database; never mix
it with the production database.
The normal bot still uses public Binance REST and the existing all-market
mark-price WebSocket, with unchanged production logic.

Set these existing environment values in the laptop test environment:

```env
TELEGRAM_PUBLISH_ENABLED=0
RESEARCH_TELEMETRY_ENABLED=1
RESEARCH_TELEMETRY_DB_PATH=state/oi_research_test.sqlite3
RESEARCH_TELEMETRY_RETENTION_DAYS=14
```

Production thresholds remain unchanged. Signals and reports are still calculated
and logged, but nothing is sent to Telegram; research SQLite continues
accumulating real Binance data.

Then run the normal bot, wait at least 15-30 minutes, inspect it, and leave it
running for several hours if practical:

```powershell
python run.py

python -m tools.research_telemetry_status --db state/oi_research_test.sqlite3

python -m tools.research_telemetry_export --db state/oi_research_test.sqlite3 --hours 4 --output research-test.csv.gz
```

The status command is read-only: it makes no Binance or Telegram request. It
reports database/schema details; closed and partial bar counts; OI/price sample
quality; OI-only and price-only rows; integrity checks; and latest-closed-bucket
coverage/sample counts. Re-run it during the soak to compare the newest closed
bucket rather than relying only on historical totals.

```text
Binance Current OI REST (~30s)
              |
              v
        RollingOIStore
             / \\
            /   \\
          5m             15m
           |               |
       IMPULSE       OI ANOMALY
        5%/3%     OI15 >= +1%, Z15
           |               |
      immediate    closed UTC intervals
           |               |
           +----- Telegram ALL/PROP -----+
```

The legacy 20m TOP implementation is an in-memory rolling quantity ranking; its scheduler is disabled in current production and retained for rollback. After each
fully successful collector cycle, the rolling runtime atomically publishes an
immutable completed-cycle TOP snapshot. The scheduled TOP job reads only that
cached snapshot, so a collector cycle still in progress cannot expose a partial
universe. Partial, failed, skipped, or timed-out cycles retain the previous good
snapshot. Missing and stale snapshots are skipped safely using the existing
rolling observation freshness limit.

When enabled only for rollback, TOP includes symbols at `TOP_THRESHOLD` (+1% by default), sorts them descending,
and retains ALL/PROP delivery. Its PX% value comes from existing rolling price
context and renders as `NA` when unavailable; price never gates an OI candidate.
A cold restart requires a natural approximately 20-minute warm-up. During
warm-up the scheduled report is skipped without a historical fallback or fake
empty report.

Neither the production 5m IMPULSE nor the 15m anomaly report adds a historical
`openInterestHist` request. The rollback-only TOP job also makes no current-OI
or kline request; it consumes the latest fresh completed collector snapshot
produced at the unchanged 30-second cadence.
Rolling 60m and 120m analytics remain observational only.
Docker Compose persists the signal-state JSON under the host `state` directory.

### Log files

`bot.log` contains general application, scheduler, and Telegram diagnostics.
`rolling_oi.log`
contains the rolling OI engine, collector, mark-price stream, rolling analytics,
production 5m signal/publish diagnostics, live 15m anomaly diagnostics,
rollback-only rolling 20m TOP diagnostics, and
remaining shadow analytics. Console output continues to show both streams.

```powershell
Get-Content .\rolling_oi.log -Tail 0 -Wait

Get-Content .\rolling_oi.log -Tail 0 -Wait |
    Select-String -Pattern 'ROLLING_SIGNAL|ROLLING_SIGNAL_PUBLISH'

Get-Content .\rolling_oi.log -Tail 0 -Wait |
    Select-String -Pattern 'ROLLING_TOP_SNAPSHOT|ROLLING_TOP_SUMMARY|ROLLING_TOP_PUBLISH|ROLLING_TOP_SKIP'
```

### Current production and roadmap

Current production is **5m IMPULSE + 15m OI ANOMALY**. The 5m rolling OI
IMPULSE remains unchanged. The 15m report uses closed UTC-aligned intervals,
positive `OI15% >= +1%` eligibility, the same-symbol trailing 14-day classic
Z baseline (minimum 96 valid prior observations), and existing ALL/PROP routing.
Finite Z ranks first descending, then OI%, then symbol; PX% is context only.

On 2026-09-05, commit `d2aa19735f7597cc7d518cf45526f9de4216fa9e` was manually
cut over and validated in production: numeric-Z reports reached ALL and PROP
for all 522 current symbols. Production enables 15m runtime and publication and
disables the legacy 20m schedule with `ROLLING_OI_20M_TOP_ENABLED=0`. The 20m
implementation remains in code as rollback compatibility.

Task history: Task 25 implemented the opt-in runtime and staging publication.
Task 26 corrects NEW semantics and rebaselines post-cutover documentation.
Task 27 is production stabilization and observation, not a preplanned product
change. Deferred directions remain unnumbered.

### Planned research direction: OI Z-SCORE FLASH

FLASH is not implemented or assigned a task number. It is a possible near-online
1m research direction: positive OI anomalies with Z > +3 and negative OI
anomalies with Z < -3. OI% and PX% would be context only, not gates; it carries
no BUY/SELL, squeeze, or liquidation interpretation. Any future publication
requires 1m telemetry, shadow validation, and anti-spam trigger/rearm design.
## Tablet test release (Termux + proot Ubuntu)

Runtime stack: Android -> Termux -> `proot-distro` Ubuntu -> this project ->
`.venv` -> `run.py`. The tablet must use the same Telegram bot token and the
same ALL and PROP destinations as the legacy tablet bot; do not create a test
bot, test token, or separate channel.

The tablet environment file lives outside Git, by default at
`~/.config/oitgbot/env`. Create it with restrictive permissions where the
environment supports them:

```bash
mkdir -p ~/.config/oitgbot
chmod 700 ~/.config/oitgbot
nano ~/.config/oitgbot/env
chmod 600 ~/.config/oitgbot/env
```

Use the existing production values for `BOT_TOKEN`, `ALL_CHANNEL_ID`, and
`PROP_CHANNEL_ID`. Set `TZ=Europe/Kyiv` there to retain the intended scheduler
local-time behavior; rolling `observed_at_utc` semantics remain UTC. Do not put
this file in the repository or upload it with diagnostics.

Initial setup from Ubuntu (after cloning or updating this branch):

```bash
cd <project>
bash deploy/tablet/setup-ubuntu.sh
bash deploy/tablet/check-ubuntu.sh
bash deploy/tablet/run-ubuntu.sh
```

`setup-ubuntu.sh` creates/reuses `.venv`, installs the project requirements,
keeps existing state intact, and performs an import smoke check.
`check-ubuntu.sh` is local and non-destructive: it verifies the venv, imports,
required environment-variable presence (never their values), writable log/state
parents, timezone, Android shared-storage availability, and Git identity. It
does not contact Binance, send Telegram, or alter rolling signal state.
`run-ubuntu.sh` is the sole tablet runtime command and starts the existing
`run.py` entrypoint with console output visible and normal file logging active.
It only supplies absent defaults: 30-second cadence, 20 workers, 5% 5m trigger,
and 3% rearm.

### Tablet cutover and warm-up

1. Stop the old legacy bot manually; do not intentionally run both production
   bots in parallel.
2. Confirm it is gone, for example: `ps -ef | grep -E '[p]ython.*run.py'`.
3. Update/deploy this rolling version, run the preflight check, then start it.
4. Inspect `bot.log` and `rolling_oi.log` for startup and collector health.
5. Allow natural warm-up before evaluating output: about 5 minutes for 5m
   IMPULSE and the next closed 15m interval for OI ANOMALY. The collector starts
   immediately.
6. Verify the normal Telegram output in the existing ALL/PROP destinations.

The rolling market-history store is deliberately not persisted or seeded from
historical OI. The separate `rolling_oi_signal_state.json` is persistent and
may suppress duplicate active 5m extremes across a restart for its configured
15-minute TTL; normal setup and run never reset it.

### Tablet logs and export

Application logs remain in the project/runtime location, never continuously in
Android shared storage. `bot.log` holds startup, scheduler, general application
and Telegram events. `rolling_oi.log` holds the price stream, rate-limit budget,
collector summaries, rolling analytics/signals, 15m anomaly diagnostics,
rollback-only 20m TOP diagnostics, and signal
state diagnostics. Each uses the existing rotating-file configuration: 5 MB per
file with five retained backups by default.

While the bot is still running, create an uploadable snapshot with:

```bash
cd <project>
bash deploy/tablet/collect-logs.sh
```

It snapshots only active `bot.log`/`rolling_oi.log` files, every existing
rotation of both logs, and `rolling_oi_signal_state.json` into staging before creating
`oi-bot-logs-YYYYMMDD-HHMMSS.zip`. The archive also has a no-secret
`runtime_info.txt` with timestamp/timezone, Git identity/status, Python/runtime
details, file sizes, state presence, process count, and disk space.
The research SQLite database and its WAL/SHM files are deliberately excluded;
use the research telemetry export command when research data is needed.

The primary destination is `/sdcard/Download/OI-bot-logs`; the fallback is
`/storage/emulated/0/Download/OI-bot-logs`. The script requires an existing
writable Downloads parent and fails with a clear Termux shared-storage message
if neither is available. It never moves, deletes, pauses, or resets live logs
or state. Select the resulting ZIP from Android Downloads and upload it to
ChatGPT for analysis. A Telegram `/logs` command is only a possible future
convenience, not part of this release.

### РџРѕСЏСЃРЅРµРЅРЅСЏ РєР»СЋС‡РѕРІРёС… РїР°СЂР°РјРµС‚СЂС–РІ

* `ROLLING_OI_5M_TRIGGER_PCT` / `ROLLING_OI_5M_REARM_PCT` вЂ” production 5m hysteresis
* `TOP_THRESHOLD` вЂ” РїРѕСЂС–Рі OI% Р·Р° 20 С…РІ (Р·РІС–С‚ All)
* `SEND_EMPTY_REPORTS=1` вЂ” РЅР°РґСЃРёР»Р°С‚Рё РїРѕРІС–РґРѕРјР»РµРЅРЅСЏ РЅР°РІС–С‚СЊ СЏРєС‰Рѕ СЃРёРіРЅР°Р»С–РІ РЅРµРјР° (Р· РїСЂРёРјС–С‚РєРѕСЋ)
* `IMPULSE_THRESHOLD` / `SHOW_TOP_WHEN_EMPTY` вЂ” retained legacy configuration; not production 5m inputs
* `HTTP_TIMEOUT/HTTP_RETRIES` вЂ” РІР°Р¶Р»РёРІРѕ РґР»СЏ РЅРµСЃС‚Р°Р±С–Р»СЊРЅРѕРіРѕ С–РЅС‚РµСЂРЅРµС‚Сѓ

---

## Р›РѕРєР°Р»СЊРЅРёР№ Р·Р°РїСѓСЃРє (Р±РµР· Docker)

### 1) Р’СЃС‚Р°РЅРѕРІРёС‚Рё Р·Р°Р»РµР¶РЅРѕСЃС‚С–

**PowerShell (Windows):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2) Р—Р°РїСѓСЃС‚РёС‚Рё

```powershell
python run.py
```

---

## Docker (СЂРµРєРѕРјРµРЅРґРѕРІР°РЅРѕ)

### РџРµСЂРµРґСѓРјРѕРІРё

* Р’СЃС‚Р°РЅРѕРІР»РµРЅРёР№ **Docker Desktop**
* РЈРІС–РјРєРЅРµРЅРёР№ Р°РІС‚РѕР·Р°РїСѓСЃРє Docker Desktop:
  **Settings в†’ General в†’ Start Docker Desktop when you log in**

### Р—Р°РїСѓСЃРє Сѓ С„РѕРЅС–

```powershell
docker compose up -d --build
```

### Р›РѕРіРё

```powershell
docker compose logs -f
```

Р’РёР№С‚Рё Р· РїРµСЂРµРіР»СЏРґСѓ Р»РѕРіС–РІ: `Ctrl + C` (РєРѕРЅС‚РµР№РЅРµСЂ РїСЂРѕРґРѕРІР¶СѓС” РїСЂР°С†СЋРІР°С‚Рё)

### РџРµСЂРµРІС–СЂРёС‚Рё СЃС‚Р°С‚СѓСЃ

```powershell
docker ps
```

### РџРµСЂРµР·Р°РїСѓСЃРє

```powershell
docker restart oitgbot
```

Р°Р±Рѕ:

```powershell
docker compose restart
```

### Р—СѓРїРёРЅРёС‚Рё С– РїСЂРёР±СЂР°С‚Рё РєРѕРЅС‚РµР№РЅРµСЂ

```powershell
docker compose down
```

### РџС–СЃР»СЏ Р·РјС–РЅ Сѓ РєРѕРґС– (РїРµСЂРµР±СѓРґСѓРІР°С‚Рё РѕР±СЂР°Р·)

```powershell
docker compose up -d --build
```

---

## РђРІС‚РѕРІС–РґРЅРѕРІР»РµРЅРЅСЏ РїС–СЃР»СЏ СЂРµР±СѓС‚Сѓ / РїР°РґС–РЅРЅСЏ

РЈ `docker-compose.yml` РІРёРєРѕСЂРёСЃС‚РѕРІСѓС”С‚СЊСЃСЏ:

* `restart: unless-stopped`
* `stop_signal: SIGTERM`
* `stop_grace_period: 20s`

Р¦Рµ РѕР·РЅР°С‡Р°С”:

* РїС–СЃР»СЏ РїРµСЂРµР·Р°РІР°РЅС‚Р°Р¶РµРЅРЅСЏ Windows С– СЃС‚Р°СЂС‚Сѓ Docker Desktop РєРѕРЅС‚РµР№РЅРµСЂ РїС–РґРЅС–РјРµС‚СЊСЃСЏ СЃР°Рј
* РїСЂРё `docker stop` Р±РѕС‚ Р·Р°РІРµСЂС€СѓС”С‚СЊСЃСЏ РєРѕСЂРµРєС‚РЅРѕ (graceful shutdown)

---

## Р РѕР·РєР»Р°Рґ (cron)

* **5m IMPULSE**: rolling collector cycle, every 30 seconds by default (no cron)
* **15m OI ANOMALY**: UTC-aligned closed intervals, with ALL/PROP publication
* **20m TOP**: legacy rollback scheduler, disabled in production with
  `ROLLING_OI_20M_TOP_ENABLED=0`

---

## РўРёРїРѕРІС– РїСЂРѕР±Р»РµРјРё

### `Chat not found`

* РЅРµРїСЂР°РІРёР»СЊРЅРёР№ `ALL_CHANNEL_ID` / `PROP_CHANNEL_ID`
* Р±РѕС‚ РЅРµ РґРѕРґР°РЅРёР№ Сѓ РєР°РЅР°Р» Р°Р±Рѕ РЅРµ РјР°С” РїСЂР°РІ РїРёСЃР°С‚Рё
* ID РјР°С” Р±СѓС‚Рё Сѓ С„РѕСЂРјР°С‚С– `-100...`

### `Telegram send timeout`

Р†РЅРѕРґС– Telegram РїСЂРёР№РјР°С” РїРѕРІС–РґРѕРјР»РµРЅРЅСЏ, Р°Р»Рµ РІС–РґРїРѕРІС–РґСЊ РїСЂРёС…РѕРґРёС‚СЊ РїС–Р·РЅРѕ в†’ РєР»С–С”РЅС‚ Р±Р°С‡РёС‚СЊ `TimedOut`.
РЈ РЅР°СЃ С”:

* Р·Р±С–Р»СЊС€РµРЅС– С‚Р°Р№РјР°СѓС‚Рё РІ `Application.builder()`
* 1 РїРѕРІС‚РѕСЂРЅР° СЃРїСЂРѕР±Р° РІ `TelegramSender`

### РќРµРјР° С–РЅС‚РµСЂРЅРµС‚Сѓ

РљРѕРЅС‚РµР№РЅРµСЂ РЅРµ РІРїР°РґРµ. РњРѕР¶СѓС‚СЊ Р±СѓС‚Рё РїРѕРјРёР»РєРё Сѓ Р»РѕРіР°С…. РљРѕР»Рё С–РЅС‚РµСЂРЅРµС‚ РїРѕРІРµСЂРЅРµС‚СЊСЃСЏ вЂ” Р±РѕС‚ РїСЂРѕРґРѕРІР¶РёС‚СЊ СЂРѕР±РѕС‚Сѓ.

---

## РљРѕСЂРёСЃРЅС– РєРѕРјР°РЅРґРё (С€РїР°СЂРіР°Р»РєР°)

```powershell
# СЃС‚Р°СЂС‚
docker compose up -d

# СЃС‚Р°СЂС‚ Р· РїРµСЂРµР±СѓРґРѕРІРѕСЋ
docker compose up -d --build

# Р»РѕРіРё
docker compose logs -f

# СЃС‚Р°С‚СѓСЃ
docker ps

# РїРµСЂРµР·Р°РїСѓСЃРє
docker restart oitgbot

# СЃС‚РѕРї
docker compose down
```

---

## Р‘РµР·РїРµРєР°

* `.env` РЅРµ РґРѕРґР°РІР°Р№ Сѓ git
* `BOT_TOKEN` С‚СЂРёРјР°Р№ РїСЂРёРІР°С‚РЅРёРј

```
```

## Production 15m OI anomaly

The 15m analyzer is live in production after the validated 2026-09-05 cutover.
Its runtime bootstraps a bounded 14-day SQLite snapshot, establishes a
non-published startup reference, and then reads only newly closed UTC-aligned
15m intervals. It does not replay historical reports or add Binance requests.
The active production deployment enables 15m runtime and Telegram publication;
the legacy 20m scheduler is disabled there with
`ROLLING_OI_20M_TOP_ENABLED=0`, while its code remains available for rollback.

`NEW` uses exactly `[T - 12h, T)`: an eligible current candidate is NEW when no
prior eligible same-symbol candidate is known in that window. Missing intervals,
restart gaps, downtime, and coverage below 48/48 do not change this result.
The current interval is excluded. Telegram prefixes NEW rows with `рџ†•` and adds
`рџ†• NEW` only when at least one NEW row exists; there is no вЏі status marker.

For manual configuration, use the existing production credentials and explicitly
set `OI_ANOMALY_15M_ENABLED=1`, `OI_ANOMALY_15M_TELEGRAM_ENABLED=1`, and
`ROLLING_OI_20M_TOP_ENABLED=0`. Do not place credentials in source control.
