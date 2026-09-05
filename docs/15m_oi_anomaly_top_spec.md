# 15m OI Anomaly TOP — approved product specification

Status: Task 23 pure analytics and offline CLI implemented. Production 15m
remains an approved target; no runtime integration or Telegram 15m report yet.

## Scope and current production

Current production remains unchanged:

- **5m IMPULSE:** rolling current OI quantity, evaluated on collector cycles;
  ±5% trigger, 3% re-arm, persistent per-symbol signal state, and existing
  ALL/PROP Telegram routing.
- **20m TOP:** positive rolling current OI quantity changes of at least +1%,
  scheduled at minutes 0/20/40, ranked by OI%, with PX% context and existing
  ALL/PROP routing.
- 60m and 120m are observational only.

This specification defines the approved next target, not current behavior. It
was documentation-only in Task 22. Task 23 adds offline analytics and tests.
The 5m IMPULSE path is explicitly out of scope and must remain
unchanged.

## Target architecture and interval identity

At final cutover, production will be:

```text
5m IMPULSE + 15m OI ANOMALY TOP
```

The 20m TOP is **replaced**, not supplemented. It remains the only TOP report
until shadow validation is complete; there must be no permanent simultaneous
production 20m and 15m reports.

The new report uses closed, standard UTC-aligned 15-minute intervals:

```text
00–15  15–30  30–45  45–00
10:00:00Z -> 10:15:00Z
10:15:00Z -> 10:30:00Z
```

Each report belongs to the interval that has closed. It may publish a few
seconds after its boundary once complete-universe data is available. Correct
interval identity and complete-universe data take priority over a fragile
fixed-second publication target.

## Metrics, eligibility, and ranking

For each symbol and the same aligned closed interval, calculate:

```text
OI15%  current aligned 15m OI quantity percent change
PX15%  current aligned 15m price percent change
Z15    classic same-symbol Z-score of OI15%
```

OI quantity is the primary OI representation. PX15% is display context only;
it never gates OI eligibility.

Production-v1 eligibility is inherited from TOP: positive OI only, with
`OI15% >= +1.0%`. Every eligible symbol participates in eligibility/NEW history
even if a later presentation limit hides a row.

Order candidates deterministically:

1. finite Z15, descending;
2. OI15%, descending;
3. symbol, deterministic lexical order.

Candidates with `Z=N/A` follow all finite-Z candidates. Raw OI% must not become
the primary ranking metric.

## Classic Z15 baseline

For current `r_current = OI15%`:

```text
Z15 = (r_current - mean(history)) / population_stddev(history)
```

`history` contains only valid, closed, aligned 15m OI% observations for the
same symbol in the trailing 14 days. It excludes the current interval, never
uses look-ahead, and must use the same 15m aggregation and percent-change
semantics as `r_current`.

Production v1 uses **population** standard deviation (`sqrt(sum((x-mean)^2)/n)`),
not sample standard deviation. The baseline is the complete retained rolling
population available to the product, rather than an estimate of an unobserved
statistical sample; this also keeps the value deterministic at the minimum
history threshold.

At least 96 prior valid observations (about 24 hours) are required. With fewer,
Z15 is `N/A`; the symbol remains eligible and visible according to normal
ranking. If history has zero or near-zero standard deviation, or any required
current/baseline value or computed result is NaN/non-finite, Z15 is `N/A` and a
diagnostic is recorded. Gaps are never imputed: only valid closed source bars
form valid 15m observations, and insufficient valid history remains `N/A`.

For research only, calculate or preserve a plan to calculate a robust Z-score
(median/MAD) and the historical percentile of current OI15%. Neither is a v1
Telegram ranking or display field.

## NEW marker

Use `🆕` when a symbol is eligible in the current aligned 15m interval and had
no eligible 15m interval in the preceding 14 hours. Eligibility history means
all intervals with `OI15% >= +1.0%`, not only visible TOP-N rows; the current
interval cannot count as its own prior appearance.

Restart behavior must reconstruct the prior 14-hour eligibility history from
durable research telemetry when required, optionally keeping an in-memory cache
after startup. A restart must not create a market-wide false NEW storm. Do not
require a new permanent JSON state file unless later implementation work proves
one necessary.

## Data, failure isolation, and target Telegram UX

Use existing Task 21 durable closed 5m OI + Price telemetry (14-day retention),
aggregating it into aligned 15m observations as needed. No new Binance REST
endpoint, WebSocket, or `openInterestHist` fallback is permitted. Current and
historical calculations must share identical interval semantics.

If data/history is unavailable, the anomaly feature fails safely (for example,
skip an affected report/row with diagnostics); it must not affect OI collection,
the mark-price WebSocket, 5m IMPULSE, unrelated Telegram reports, or telemetry
persistence.

At cutover, the report concept is:

```text
📊 OI ANOMALY · 15m

Z     OI       PX       SYMBOL
5.26  +2.14%   +3.82%   🆕 BUSDT
4.73  +1.31%   +0.94%      XYZUSDT
1.41  +6.27%   +2.55%      DEFUSDT
```

Exact monospace spacing is an implementation detail. Semantic display order is
Z, raw OI change, price change, then symbol. Preserve clickable tickers and
ALL/PROP routing at final cutover.

## Product motivation

The 5m ±5% IMPULSE remains the fast alert for already-large OI moves. The old
20m TOP is mainly useful for earlier-stage activity. The 15m anomaly report is
intended to improve early-stage discovery by comparing a current OI move with
that symbol's own historical behavior: an OI +3%, Z +5 event may be more unusual
than OI +6%, Z +1.4. Z-score is not a claim of future price direction or
profitability.

## Locked decisions

- 20m production TOP is ultimately replaced by 15m; 5m IMPULSE is unchanged.
- Intervals are standard UTC-aligned closed 15m periods.
- Initial eligibility is positive `OI15% >= +1.0%`; PX is context only.
- Classic Z is v1's primary rank, with a 14-day same-symbol history and a
  minimum of 96 prior observations; current interval is excluded.
- Population standard deviation is the chosen classic-Z convention.
- NEW uses a 14-hour lookback across all eligible intervals.
- Existing closed 5m telemetry is the source: no new market-data requests.
- Shadow validation precedes cutover; robust Z and percentile are research-only.

## Open implementation questions

- Exact cache/service boundary and publication trigger after an interval closes.
- Efficient 14-day bootstrap and incremental-update strategy.
- Future operational quality thresholds; strict missing-bucket exclusion in
  Task 23 is resolved below.
- Operational diagnostics and coverage thresholds for shadow validation.

## Rebaselined roadmap

| Task | Scope |
|---|---|
| 22 | Product/specification documentation rebaseline (this task). |
| 23 | Pure 15m anomaly analytics core and offline CLI: aligned aggregation, OI%, PX%, classic/robust Z, percentile, coverage, deterministic tests; no runtime or Telegram change. |
| 24 | Implemented: pure eligibility, deterministic ranking, NEW(14h), restart-reconstructible history, read-only candidate CLI and tests; no runtime or Telegram change. |
| 25 | Runtime shadow integration with timing/coverage/baseline/skip diagnostics and no Telegram; existing 20m TOP remains production. |
| 26 | Production cutover: replace 20m TOP, preserve 5m IMPULSE and ALL/PROP routing, and introduce final format/NEW marker. |
| 27 | Live stabilization and final documentation cleanup; validate timing, duplicates, restart/NEW behavior, Z ordering, routing, and CoinGlass-friendly alignment; only then describe 15m as current in README. |

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

NEW uses the same aligned valid OI15% observations and threshold. For a current
eligible interval T, prior eligibility is tested in `[T - 14h, T)`: the lower
boundary is included and T is excluded. Thus an eligible appearance precisely
14 hours earlier makes NEW false; one at 14h15m does not. NEW is independent of
historical Z, rank, price, robust Z, percentile and visible TOP-N rows.

`EligibilityHistory` is an in-memory, thread-independent index of valid and
eligible interval starts. It reconstructs from Task 23 aligned observations,
registers each later closed observation, and prunes starts older than the
inclusive lookback boundary. Task 25 can reconstruct it from the bounded SQLite
analysis history after a restart, decide the current interval before registering
that interval, then retain it in memory. No permanent NEW-state file is used.

The index reports valid interval count, expected count (56 at 14 hours),
coverage ratio, prior eligible count and last eligible start. Sparse history does
not suppress a NEW result, but its coverage is visible. If there is no prior
valid telemetry at all for the symbol, or only the current interval exists,
`is_new` is `None` with `history_unavailable`; False is reserved for a prior
eligible appearance or an ineligible current result. Historical invalid/missing
intervals never qualify and are never fabricated.

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
