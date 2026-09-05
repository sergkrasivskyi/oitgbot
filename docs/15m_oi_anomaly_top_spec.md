# 15m OI Anomaly — production product specification

Status: production cutover was manually completed and validated on 2026-09-05
using commit `d2aa19735f7597cc7d518cf45526f9de4216fa9e`.

## Current production

Production is **5m IMPULSE + 15m OI ANOMALY**. 5m IMPULSE is unchanged. 15m
uses closed UTC-aligned intervals, positive `OI15% >= +1%` eligibility, classic
same-symbol trailing-14-day Z15 with at least 96 valid prior observations, and
finite-Z / OI% / symbol ranking. PX15% is context only. ALL and PROP routing
are production-validated. The legacy rolling 20m TOP scheduler is disabled in
production with `ROLLING_OI_20M_TOP_ENABLED=0`; its implementation remains as
rollback compatibility.

## NEW marker

For an eligible current interval T, NEW checks only known eligible occurrences
for the same symbol in `[T - 12h, T)`. Any known prior eligible occurrence means
`is_new=False`; otherwise `is_new=True`. The current interval is excluded.
Missing aligned intervals, restarts, downtime, and coverage below 48/48 never
turn NEW into an unknown state. Coverage remains diagnostic data only.

Telegram prefixes NEW rows with `🆕`; non-NEW rows have no marker. If any NEW
row exists, append `🆕 NEW`; otherwise append no legend. There is no ⏳ marker,
no `UNKNOWN`, and no coverage-derived suppression of an eligible candidate.

## Runtime and data contract

The live runtime uses the existing durable closed 5m OI and price telemetry,
bootstraps one bounded SQLite history, establishes a non-published startup
reference, and processes only newly completed intervals. It scores before
appending current observations, retries incomplete source data, and does not
replay historic Telegram reports after restart. No new Binance REST request,
WebSocket, telemetry schema, collector cadence, or market-data subscription is
introduced.

## Rebaselined history and roadmap

| Item | Status |
|---|---|
| Task 23 | Pure aligned aggregation/statistics and offline CLI. |
| Task 24 | Eligibility, ranking, and 12h occurrence history. |
| Task 25 | Opt-in live runtime and staging publication. |
| 2026-09-05 cutover | `d2aa197` manually promoted and validated in production. |
| Task 26 | Correct NEW semantics and rebaseline post-cutover documentation. |
| Task 27 | Production stabilization and observation. |

## Planned research direction: OI Z-SCORE FLASH

FLASH has no task number and is not implemented here. It is a possible 1m
near-online research direction: positive OI anomaly Z > +3 and negative OI
anomaly Z < -3; OI% and PX% are context only. It has no BUY/SELL, squeeze, or
liquidation interpretation. Any future Telegram work requires 1m telemetry,
shadow validation, and anti-spam trigger/rearm design first.
## Task 23 implementation details (offline research only)

`oitgbot/services/oi_anomaly_15m.py` contains immutable read/observation/result
models and pure aggregation/statistics. `oi_anomaly_15m_reader.py` is the SQLite
adapter; `tools/oi_anomaly_15m.py` handles terminal presentation and CSV export.
These modules are not imported by production runtime. No new dependencies,
market-data requests, persistent cache, eligibility layer or NEW state were added.

For `[T,T+15m)`, exactly three closed rows at T, T+5m and T+10m are required.
OI15% is `100 * (third.oi_close - first.oi_open) / first.oi_open`.
PX15% is `100 * (third.price_close - first.price_open) / first.price_open`.
These are sampled first-open/last-close returns within the aligned interval,
not interpolated exact-boundary prices or chained 5m returns. Both current and
historical intervals use the same aggregator. Required endpoints must be finite
and positive. Missing, duplicate, misaligned or open constituents invalidate that
interval. Invalid current observations expose a reason and unavailable metrics;
invalid historical observations reduce the baseline count only. No interpolation,
gap bridging, forward fill or backfill is performed.

Task 23 requires both OI and price endpoints for a complete research observation,
as specified by its input contract. This is data validity, not price-direction
eligibility: no price change sign or magnitude is filtered. Counts such as OI
9/10/11 or low restart counts remain valid. Count totals/minima and constituent
closed-bucket coverage (0 to 1) accompany the independent validity/reason fields.

For current start T, include same-symbol valid observations with starts in
`[T - baseline_days, T)`, default 14 calendar days (1344 possible intervals).
Current/future intervals are excluded; non-finite historical OI returns are
excluded. Duplicate baseline identities raise an explicit error. Coverage is
`baseline_count / (96 * baseline_days)`, with oldest/newest starts and raw mean,
population standard deviation, median and MAD also returned.

Classic Z is `(x - mean) / sigma`, with `sigma = statistics.pstdev(history)`
(population divisor N). Robust Z is `0.6744897501960817 * (x - median) / MAD`,
where MAD is the median absolute deviation. Both scores require 96 prior valid
observations by default; research CLI overrides do not change production defaults.
Shared absolute epsilon is `1e-12` percentage points: sigma or MAD <= epsilon
makes the respective score unavailable. Non-finite computed scores are unavailable
with a reason. No numeric sentinel or infinity stands in for an unavailable score.

Percentile is `100 * (below + 0.5 * equal) / N`. Comparisons use exact Python
float `<` and `==`, without rounding or approximate tie tolerance. It is available
for any nonempty valid baseline, even below 96; an empty baseline yields N/A.
Robust Z and percentile remain research-only metrics.

The reader uses SQLite `mode=ro`, `query_only=ON` and one read transaction for a
consistent snapshot including committed WAL records; it never uses `immutable=1`.
With `--as-of`, the selected interval starts at `floor15(as_of) - 15m` and is
reported unavailable if incomplete, without silently falling back to an older
interval. With no timestamp, it streams newest-first interval groups, bounded
above by the current closed UTC boundary, and stops at the first mathematically
complete observation among the requested symbols. This does not certify full
market coverage for a production report. Empty data yields an explicit message.

After discovery, one bounded SQL read covers only the requested current interval
and its baseline. Processing streams by symbol, retaining one symbol's history
at a time; there is no per-symbol full-database query. Latest-interval discovery
can scan older groups if recent data is invalid. The existing bucket index serves
time-range reads; the schema and writer are untouched.

```bash
python -m tools.oi_anomaly_15m --db state/oi_research.sqlite3 --top 30
python -m tools.oi_anomaly_15m --db state/oi_research.sqlite3 --as-of 2026-09-05T10:15:00Z --symbol BTCUSDT
python -m tools.oi_anomaly_15m --db state/oi_research.sqlite3 --baseline-days 14 --min-history 96 --output anomaly.csv
```

Terminal research sorting is finite classic Z descending, then OI descending,
then lexical symbol; N/A follows finite Z. No +1% production eligibility filter
is implemented. `--top` limits terminal rows only. CSV exports all results and
all observation, quality, score and baseline fields, with empty cells for None.
CSV exclusively creates a new file and refuses to overwrite existing files,
including the database or WAL. No generated research artifacts belong in Git.

## Task 24 implementation details (offline research only)

The candidate layer is separate from Task 23 statistics. Future-v1 eligibility
is exactly valid `OI15% >= +1.0%`; the pure API accepts a finite positive
research threshold, while no production setting changed. PX15%, classic-Z
availability, robust Z and percentile never gate eligibility. An invalid current
observation is ineligible with its explicit Task 23 reason. A Z=N/A result at
or above +1% remains eligible.

Eligible candidates receive ranks beginning at 1. Finite classic Z values sort
first by Z descending, then OI15% descending, then lexical symbol ascending.
Z=N/A candidates follow, ordered by OI15% descending then symbol ascending.
No price, robust-Z or percentile value participates. Ineligible observations have
no rank. The candidate result retains the complete Task 23 anomaly result and
adds eligibility, rank, NEW and history-quality diagnostics.

NEW uses known same-symbol eligible observations and the 12-hour window `[T - 12h, T)`. A known prior eligible occurrence makes NEW false; no known occurrence makes NEW true. The current interval is excluded. Valid-interval coverage remains a diagnostic and never gates NEW or report visibility.

`tools/oi_anomaly_15m_candidates.py` reuses Task 23's single bounded aligned
analysis read and SQLite read-only/WAL snapshot. It adds `--eligible-only`,
`--new-only` (which shows only `NEW=True`, excluding false and unknown),
`--eligibility-threshold`, `--new-lookback-hours` and full CSV decisions.

```bash
python -m tools.oi_anomaly_15m_candidates --db state/oi_research.sqlite3 --top 30
python -m tools.oi_anomaly_15m_candidates --db state/oi_research.sqlite3 --symbol BTCUSDT --eligible-only
python -m tools.oi_anomaly_15m_candidates --db state/oi_research.sqlite3 --new-only --output candidates.csv
```
## Deferred backlog (unchanged)

The following remains deferred, not cancelled, and has no new task number:

- Grid Candidate research.
- Impulse → Pullback → Continuation research.
- 60m/120m product decision.
- Production threshold tuning and negative-impulse decision.
- Operational-hardening work.

## Current production note

The Task 25 runtime is now operating as production 15m OI ANOMALY after the 2026-09-05 cutover. The legacy 20m implementation is retained only for rollback.