# 15m OI Anomaly TOP — approved product specification

Status: approved target for Tasks 23–27; **not implemented or deployed** by
Task 22.

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
does not authorize a calculation, scheduler, Telegram, configuration, test, or
runtime change. The 5m IMPULSE path is explicitly out of scope and must remain
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
- Exact small-gap policy that maintains no-look-ahead and consistent semantics.
- Operational diagnostics and coverage thresholds for shadow validation.

## Rebaselined roadmap

| Task | Scope |
|---|---|
| 22 | Product/specification documentation rebaseline (this task). |
| 23 | Pure 15m anomaly analytics core and offline CLI: aligned aggregation, OI%, PX%, classic/robust Z, percentile, coverage, deterministic tests; no runtime or Telegram change. |
| 24 | Pure NEW(14h), ranking, and eligibility layer, including restart reconstruction from historical data. |
| 25 | Runtime shadow integration with timing/coverage/baseline/skip diagnostics and no Telegram; existing 20m TOP remains production. |
| 26 | Production cutover: replace 20m TOP, preserve 5m IMPULSE and ALL/PROP routing, and introduce final format/NEW marker. |
| 27 | Live stabilization and final documentation cleanup; validate timing, duplicates, restart/NEW behavior, Z ordering, routing, and CoinGlass-friendly alignment; only then describe 15m as current in README. |

## Deferred backlog

The following remains deferred, not cancelled, and has no new task number:

- Grid Candidate research.
- Impulse → Pullback → Continuation research.
- 60m/120m product decision.
- Production threshold tuning and negative-impulse decision.
- Operational-hardening work.
