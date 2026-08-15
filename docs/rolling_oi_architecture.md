# Hybrid Rolling Open Interest Architecture

Status: implementation specification for Tasks 7+

This design replaces historical five-minute Open Interest buckets as the primary real-time signal source. It preserves the historical endpoint for diagnostics and shadow validation while building production signals from timestamped current OI samples and WebSocket mark prices.

## Goals and constraints

The engine must:

- react faster than the legacy five-minute historical-bucket scanner;
- define deterministic 5m, 20m, 60m, and 120m windows from observation timestamps;
- keep request usage within runtime Binance limits with explicit headroom;
- avoid blocking the asyncio event loop;
- reuse the existing formatter, Telegram sender, symbol eligibility rules, and configuration where their semantics still fit;
- tolerate per-symbol failures without inserting fabricated zero values;
- support shadow operation and rollback before Telegram cutover;
- remain fully testable without Binance or Telegram network access.

Historical `openInterestHist` remains diagnostic-only after cutover. It must not be mixed silently with current `openInterest` samples because their fields and semantics differ.

## Target architecture

```text
                                BINANCE
                                   |
               +-------------------+-------------------+
               |                                       |
               v                                       v
   !markPrice@arr@1s WebSocket               /fapi/v1/openInterest REST
               |                              one request per symbol
               v                                       |
        MarkPriceStream                                v
               |                              CurrentOICollector
               v                                       |
       PriceStateStore <-------------------------------+
               |                         combines OI with fresh price
               +-------------------+-------------------+
                                   v
                         RollingOIStore
                                   |
                  +----------------+----------------+
                  |                                 |
                  v                                 v
     RollingWindowCalculator 5m/20m     AccumulationAnalyzer 60m/120m
                  |                                 |
                  v                                 v
        Impulse/TOP consumers              quality diagnostics
                  |                                 |
                  +----------------+----------------+
                                   v
                         Existing formatter
                                   |
                                   v
                      Existing Telegram sender

 Legacy openInterestHist scanner --------> ShadowComparisonService
 New rolling calculations ---------------> diagnostics and cutover evidence
```

## Components and contracts

| Component | Responsibility | Input | Output | Lifecycle / execution | Failure boundary |
|---|---|---|---|---|---|
| `MarkPriceStream` | Maintain the all-market `!markPrice@arr@1s` connection, parse messages, reconnect, and publish health | Binance WebSocket frames | Validated mark-price updates | One long-lived async task owned by the application | Disconnect or malformed frame affects price only; OI quantity collection continues |
| `PriceStateStore` | Keep the latest validated price per eligible symbol | Parsed mark-price update | O(1) latest-price lookup and health snapshot | In-memory, application-owned; event-loop writes and lock-free/short-lock reads | Missing/stale price returns unavailable, never zero |
| `CurrentOIClient` | Call `GET /fapi/v1/openInterest` for one symbol and parse quantity/time | Symbol | `CurrentOIReading` or typed error | Initially synchronous existing-client method invoked through a dedicated bounded executor | One request failure is isolated to one symbol |
| `RateLimitBudget` | Parse runtime limits, reserve capacity, authorize primary/retry/optional work, and enter protection states | `exchangeInfo`, response headers/statuses, planned request costs | Permit/delay/deny decision plus metrics | Application-owned state, updated every cycle and after responses | Missing budget information chooses conservative cadence and disables optional work |
| `CurrentOICollector` | Run non-overlapping full-market current-OI cycles with bounded concurrency and combine readings with price state | Eligible symbols, current OI client, price store, budget | `CollectionCycleResult` and valid `RollingOISample` objects | One async service task; blocking HTTP only in dedicated executor | Partial failures stay local; cycle timeout cancels pending work without deleting history |
| `RollingOIStore` | Store bounded, ordered per-symbol samples without performing analytics | Valid `RollingOISample` | Latest sample and immutable history snapshot | In-memory application-owned store | Rejects invalid/out-of-order samples; old valid history remains intact |
| `RollingWindowCalculator` | Calculate quantity, derived USD, and aligned price changes | Latest sample, selected baseline, window definition | `RollingWindowResult` or explicit unavailable reason | Pure synchronous calculation, called on the event loop after a cycle | Invalid/zero baselines produce unavailable, never a false zero percent |
| `AccumulationAnalyzer` | Describe 60m/120m accumulation shape without applying production thresholds | Rolling OI history and exact long-window result | Persistence, efficiency, drawdown, impulse concentration, and coverage | Pure synchronous calculation | Missing anchors reduce reported coverage; they are never treated as negative or zero samples |
| `ImpulseStateMachine` | Detect threshold crossings and suppress repeats using hysteresis | Valid 5m rolling result per symbol | Durable-in-memory signal event and state transition | Event-loop owned; evaluated after healthy/degraded collection cycles | Signal state advances when an event is accepted, not according to Telegram success |
| `TopReportService` | Read the rolling store on the existing 20-minute schedule and build TOP rows | Store snapshot and 20m calculations | Existing report-row shape | Short async scheduled job; no Binance collection | Skips report when market coverage is below the report threshold |
| `ShadowComparisonService` | Compare meaningful legacy and rolling cases without driving Telegram | Legacy diagnostics and rolling results | `OI_SHADOW_COMPARE` logs/metrics | Enabled during migration; optional and budget-subordinate | Legacy failure does not affect the rolling collector |
| Existing `ReportFormatter` | Preserve Telegram presentation | Adapted rolling rows | Message text | Reused | Formatting error affects one report |
| Existing `TelegramSender` | Deliver prepared messages with existing retry behavior | Target and message | Send result/timing | Reused async boundary | Send failure does not restart collection or re-trigger a signal repeatedly |
| Application runtime | Start/stop services and coordinate graceful shutdown | Configuration | Running service graph | One asyncio application | Cancels tasks, closes WebSocket/executor/client, and stops accepting new cycles before shutdown |

## Data models

### Current OI reading

```python
CurrentOIReading(
    symbol: str,
    oi_quantity: Decimal,
    oi_exchange_time: datetime,
)
```

Parsing rules:

- `openInterest` must parse to a finite, non-negative `Decimal`;
- Binance `time` must parse to a timezone-aware UTC datetime;
- missing/malformed fields return a typed parse failure, not zero;
- the collector captures `observed_at_utc` immediately after the validated response arrives;
- responses more than 5 seconds in the future relative to local receipt are rejected and logged;
- older transaction timestamps remain valid present-OI observations and feed
  cycle-level transaction-age diagnostics.

### Price state

```python
MarkPriceState(
    symbol: str,
    mark_price: Decimal,
    price_exchange_time: datetime,
    received_at_utc: datetime,
)
```

The store also exposes stream-level `connected`, `last_message_received_at`, reconnect count, and fresh-symbol count.

### Rolling sample

```python
RollingOISample(
    symbol: str,
    oi_quantity: Decimal,
    observed_at_utc: datetime,
    oi_exchange_time: datetime,
    mark_price: Decimal | None,
    price_exchange_time: datetime | None,
    oi_value_usd: Decimal | None,
)
```

Every field has a direct purpose:

- `observed_at_utc` is the canonical ordering and rolling-window timestamp;
- `oi_exchange_time` preserves Binance's current-OI transaction time for diagnostics;
- `price_exchange_time` exposes mark-price event timing;
- `mark_price` is the freshest acceptable WebSocket price captured when the OI reading is accepted;
- `oi_value_usd` is derived only when price is usable: `oi_quantity * mark_price`.

No `collector_cycle_time` is stored per sample. Cycle ID/start/end belong to `CollectionCycleResult` and logs, avoiding redundant data in every sample.

Price and OI are not atomic. Their skew is:

```text
price_observation_age_s = observed_at_utc - price_exchange_time
price_transaction_skew_s = price_exchange_time - oi_exchange_time  # diagnostic only
```

A price is attachable when both conditions hold:

```text
observed_at_utc - price_received_at_utc <= 5 seconds
abs(observed_at_utc - price_exchange_time) <= 5 seconds
```

If either condition fails, the sample retains valid OI quantity but stores `mark_price`, `price_exchange_time`, and `oi_value_usd` as unavailable for rolling USD/price calculations. The raw price state remains available for diagnostics.

## OI metric semantics

### Option A: quantity only

`oi_quantity_change_pct` measures changes in current contract/base quantity. It is the cleanest structural indicator because it is less directly influenced by price. Its drawback is that it may differ visibly from CoinGlass or legacy USD-denominated OI displays.

### Option B: derived USD only

`oi_value_usd = oi_quantity * mark_price` is conceptually closer to USD OI displays. It is not guaranteed to equal historical `sumOpenInterestValue`. It also changes when price changes even if position quantity does not, so using it alone can misclassify price appreciation as structural OI growth.

### Option C: both — selected

Each priced sample stores quantity, mark price, and derived USD value. The engine calculates independently:

```text
oi_quantity_change_pct
oi_value_change_pct
price_change_pct
```

Production impulse triggering and TOP ranking use `oi_quantity_change_pct`. Derived USD change is a secondary diagnostic for CoinGlass/legacy comparison. Price change remains an independently displayed contextual metric. This separates structural position-size change from mark-price effects without losing comparability.

## Mark-price WebSocket design

`MarkPriceStream` consumes the all-market `!markPrice@arr@1s` stream. It parses only finite positive prices and valid exchange event times, then updates `PriceStateStore` by symbol. It does not perform REST price calls.

Proposed interface:

```python
class MarkPriceStream:
    async def run(self, stop_event: asyncio.Event) -> None: ...
    async def close(self) -> None: ...

class PriceStateStore:
    def update(self, update: MarkPriceState) -> None: ...
    def latest(self, symbol: str, reference_utc: datetime) -> PriceLookup: ...
    def health(self, reference_utc: datetime) -> PriceStreamHealth: ...
```

Reconnect policy:

1. Connect and consume frames continuously without blocking the event loop.
2. If connection establishment, parsing infrastructure, or receive loop fails, publish `PRICE_STREAM_STATUS status=disconnected`.
3. Reconnect with full-jitter exponential backoff: base 1 second, then 2, 4, 8, 16, capped at 30 seconds.
4. Reset backoff only after 60 seconds of stable messages, preventing tight reconnect loops.
5. If the all-market stream has no valid frame for 5 seconds, mark it stale, close it, and reconnect.
6. Reject individual malformed updates without dropping the connection unless malformed-frame volume indicates protocol corruption.

The price store treats symbol data older than 5 seconds as stale. Missing/stale price never invalidates OI quantity collection. It makes the sample's USD and price metrics unavailable and emits rate-limited `PRICE_STALE_WARNING` diagnostics.

## Current OI collector design

### Symbol source

The collector uses the existing eligible-symbol rules from `exchangeInfo`: trading, USDⓈ-M perpetual, USDT suffix, ASCII symbol. Refresh hourly and on a listing-related response. New symbols enter cold warm-up; removed symbols are immediately excluded from new collection and reporting while retained samples age out.

### Phase 1 execution model

Reuse the existing synchronous Binance client by adding a focused current-OI method, then invoke it in a dedicated `ThreadPoolExecutor(max_workers=20)` from the async collector. A dedicated executor is preferred over the unconstrained default `asyncio.to_thread` pool because concurrency, shutdown, and resource ownership are explicit. This is a migration step; a native async HTTP client is not required initially.

Collector defaults:

| Setting | Phase 1 value |
|---|---:|
| Base cadence | 30 seconds, configurable |
| Concurrency | 20 workers, configurable downward by protection state |
| Connect timeout | 3.5 seconds |
| Read timeout | 5 seconds |
| HTTP retry behavior | Existing `BinanceAPI` retry behavior remains unchanged in Task 10 |
| Cycle timeout | `min(20 seconds, 0.8 * cadence)` |
| Overlap | Forbidden; skip the next tick rather than queue another cycle |
| Maximum accepted observation age at evaluation | 60 seconds, configurable |
| Old transaction-time warning | 60 seconds, diagnostic only |
| Future OI clock tolerance | 5 seconds, configurable |
| Price receipt age and price-event/observation skew | At most 5 seconds each, configurable |
| Projected retry reserve | 5% of symbol requests per cycle by default, configurable; this budgets existing client retries but does not change their behavior |

Cycle flow:

```text
refresh/obtain eligible symbols
  -> RateLimitBudget reserves projected primary cost
  -> start one bounded task per symbol
  -> each task calls current OI in the dedicated executor
  -> parse and validate quantity/time
  -> obtain latest acceptable WebSocket price
  -> build sample (price fields may be unavailable)
  -> gather cycle results until cycle timeout
  -> classify cycle health
  -> insert valid samples and calculate windows
  -> publish one collector summary
```

One failed symbol does not invalidate successes. Failed, malformed, or materially future OI is never inserted as zero. An old valid transaction timestamp does not invalidate a freshly obtained present-OI observation. A failed cycle does not erase existing store history.

Cycle health:

- `healthy`: at least 90% of requested symbols returned valid OI;
- `degraded`: at least 50% but less than 90%; valid samples are stored and per-symbol results may be evaluated, but scheduled TOP requires at least 90% current eligible-symbol coverage;
- `major_failure`: less than 50%; valid samples may be retained for future baselines, but the cycle emits no new impulse events and TOP is suppressed;
- `network_failure`: no meaningful success; retain old history and enter request protection/recovery behavior.

These thresholds are operational safeguards, not substitutions for per-symbol freshness checks.

### Evaluation timing

Phase 1 evaluates rolling changes after the bounded full-market cycle completes. This costs the cycle duration but gives simple health classification, consistent alert batching, deterministic tests, and a coherent TOP coverage snapshot. With a 30-second cadence and a 20-second hard timeout, the worst planned reaction is bounded by cadence plus cycle duration rather than five-minute bucket publication.

Per-symbol immediate evaluation is a future optimization. It can reduce latency for early responses but risks emitting signals before a major partial-market failure is known. The store API should support immediate insertion later without redesign, but it is not part of initial migration.

## Request-budget analysis

Current OI costs one request weight per symbol under the supplied design assumptions.

| Cadence | 500 symbols | 550 symbols | Operational assessment |
|---:|---:|---:|---|
| 15 seconds | 2,000 calls/min | 2,200 calls/min | Aggressive; retry reserve and other REST work may be unsafe |
| 20 seconds | 1,500 calls/min | 1,650 calls/min | Desirable latency, but requires runtime-limit and cycle-duration validation |
| 30 seconds | 1,000 calls/min | 1,100 calls/min | Selected safe default pending measured limits |
| 60 seconds | 500 calls/min | 550 calls/min | Strong headroom but slower reaction and coarser windows |

The total REST budget must also include:

- hourly/on-demand `exchangeInfo` refreshes;
- allowed current-OI retries;
- startup health calls;
- legacy historical shadow scans during migration;
- Task 2 diagnostics and manual probe calls when run;
- future optional fast-watch calls.

WebSocket mark-price traffic replaces per-row REST kline lookups and does not consume this polling request budget in the same way.

Thirty seconds is the Phase 1 default because the repository has not yet measured the runtime request-weight limit, response headers, and current-OI p95 cycle duration under 500–550-symbol load. Twenty seconds may be enabled only after Task 10 validates all of the following:

```text
projected normal usage <= 70% of the active relevant request-weight limit
projected usage including planned retry allowance <= 80%
p95 full-market cycle duration <= 15 seconds
no 429 or 418 during sustained shadow testing
```

This is **VALIDATION REQUIRED**. The architecture remains cadence-configurable.

## Dynamic request-budget protection

`RateLimitBudget` reads every relevant rate-limit definition from `exchangeInfo` instead of relying on one permanently hard-coded requests-per-minute value. Task 10 implements conservative projection against every runtime window. Reconciliation with Binance used-weight response headers remains a later runtime-integration enhancement because the current client does not expose those headers.

Policy:

```text
0–~60% projected limit    SAFE; collector and budgeted optional work fit comfortably
~60–70%                   PRESSURE; collector may run, but optional work/retries reduce first
>70% projected            UNSAFE; do not start a cycle under the normal 30% reserve policy
HTTP 429                  honor Retry-After, stop retries, exponential backoff with jitter
HTTP 418                  enter protection state, stop polling, honor ban/retry time,
                          require successful low-rate health recovery before resuming
```

At least 30% of the advertised relevant capacity is reserved during normal operation. Primary current-OI collection has highest priority; symbol refresh is required but infrequent; retries are conditional; historical shadow/diagnostic work is lowest priority and skipped first. Lowering concurrency smooths bursts but does not reduce total weight, so budget pressure also delays cycles or increases cadence.

When runtime limits cannot be parsed, the service stays at 30 seconds, disables optional historical full-market diagnostics, permits no speculative fast-watch polling, and logs `RATE_LIMIT_BUDGET state=unknown_conservative`.

## Optional adaptive fast polling

Fast-watch mode is explicitly excluded from the initial release. The interfaces allow a later scheduler to poll selected symbols every 5–10 seconds while full-market base polling continues every 20–30 seconds.

Potential entry signals include approaching the quantity threshold, unusual mark-price movement, or future trade/volume features. Entry must require a bounded score and expiry; exit occurs after a fixed quiet period or return below a lower watch threshold. The watch set must have a configured maximum size and separately reserved budget.

Benefits are lower latency for likely impulses. Risks are concentrated request bursts, feedback loops, starvation of the base market scan, and bias toward already-moving symbols. Full-market base polling remains mandatory to discover new candidates and maintain consistent TOP coverage.

## Rolling sample store

Basic structure:

```python
dict[str, deque[RollingOISample]]
```

Retention is 150 minutes based on `observed_at_utc`. This covers the 120-minute product window plus tolerance and operational headroom. Pruning occurs after each successful insertion and during explicit maintenance.

Approximate retained counts:

| Cadence | Samples/symbol over 150m (inclusive allowance) | Samples for 550 symbols |
|---:|---:|---:|
| 15 seconds | ~601 | ~330,550 |
| 20 seconds | ~451 | ~248,050 |
| 30 seconds | ~301 | ~165,550 |
| 60 seconds | ~151 | ~83,050 |

The hard per-symbol cap is `ceil(retention / configured_cadence) + 2`. At the 30-second default this is 302 samples per symbol. Any cadence change must recalculate the cap or supply an explicit bound. This prevents unbounded growth even if pruning or clocks misbehave.

Insertion rules:

- order by `observed_at_utc`;
- repeated Binance transaction timestamps remain separate valid observations;
- same observation timestamp and identical or less-complete data: ignore as an idempotent duplicate;
- same observation timestamp and consistent quantity: replace only when the new sample adds richer price context;
- same observation timestamp with conflicting quantity: ignore;
- older/out-of-order samples: ignore so accepted history never moves backward;
- older than retention: reject;
- materially future transaction timestamps reject; old transaction timestamps warn but remain valid;
- failed cycles insert nothing for failed symbols;
- added symbols start an empty deque and warm up naturally;
- removed symbols become inactive immediately, are excluded from reports, and are purged after retention.

## Exact rolling-window semantics

For window `W` (300, 1,200, 3,600, or 7,200 seconds):

1. Select the latest valid, fresh sample for the symbol.
2. Define `target_time = latest.observed_at_utc - W`.
3. Select the baseline as the latest valid sample whose `observed_at_utc <= target_time`.
4. Accept it only when:

```text
target_time - baseline.observed_at_utc <= tolerance
```

5. Otherwise return `window unavailable: no baseline within tolerance`.

The same rule is used for 5m, 20m, 60m, and 120m. No sample after the target is selected, so there is no future-data leakage relative to the target. No interpolation is performed.

Tolerance is:

```text
tolerance = max(60 seconds, 2 * configured base cadence)
```

Therefore:

- 15, 20, or 30-second cadence: 60-second tolerance;
- 60-second cadence: 120-second tolerance.

At the selected 30-second cadence:

- a 5m window has an actual duration from 300 through 360 seconds;
- a 20m window has an actual duration from 1,200 through 1,260 seconds.
- a 60m window has an actual duration from 3,600 through 3,660 seconds;
- a 120m window has an actual duration from 7,200 through 7,260 seconds.

Example:

```text
latest OI time = 14:10:27
5m target      = 14:05:27
candidate      = 14:05:04
offset         = 23 seconds <= 60 seconds
actual window  = 323 seconds -> available
```

Rule A (latest sample at or before target) is selected. Rule B (nearest either side) can leak data after the target and produce a shorter-than-requested interval. Rule C (interpolation) fabricates an OI value and makes failure behavior harder to reason about. Both are rejected for Phase 1.

The latest sample itself must also be fresh at evaluation time:

```text
evaluation_utc - latest.observed_at_utc <= max(60 seconds, 2 * cadence)
```

## Rolling calculations and price alignment

For each metric with valid positive baseline and current values:

```text
change_pct = (current - baseline) / baseline * 100
```

Outputs:

- `oi_quantity_change_pct`: available when both OI quantities are finite and baseline is greater than zero;
- `oi_value_change_pct`: available only when both samples have valid derived USD values and baseline USD is greater than zero;
- `price_change_pct`: available only when both samples have attached fresh prices and baseline price is greater than zero.

Missing, malformed, non-finite, or zero baselines return an explicit unavailable reason. They never return a synthetic `0.0%`.

Price change uses the prices captured on the same latest and baseline OI observations. A price is attached when its receipt and event timestamps are fresh relative to `observed_at_utc`. The possibly older OI transaction timestamp does not reject a current price. `price_exchange_time - oi_exchange_time` remains diagnostic metadata only.

## Slow and long accumulation quality

The 60m Slow Accumulation and 120m Long Accumulation views reuse the exact rolling-window rule and add descriptive shape metrics. Ten-minute anchors calculate positive, negative, and flat directional blocks, persistence, trend efficiency, positive-magnitude peak-to-trough drawdown, and anchor coverage. Five-minute anchors calculate the concentration of positive OI growth in the largest positive block and the maximum calculable 5m percentage change.

Missing anchors reduce coverage and do not count as negative blocks. A flat path has trend efficiency `0.0`; no positive 5m movement makes impulse concentration unavailable. These metrics are diagnostics only. Hard Slow/Long Accumulation thresholds for persistence, efficiency, drawdown, concentration, or coverage are **VALIDATION REQUIRED** and are not production eligibility gates yet.

## Clock semantics

### Live-validated canonical timestamp decision

Task 11 live shadow operation showed that `/fapi/v1/openInterest` returns the
present OI quantity with transaction timestamps that may be older or repeated
across polling cycles. Those responses are still distinct observations that the
bot obtained at different times. Consequently, Binance transaction time is not
a reliable regular sampling grid and cannot be the rolling clock.

The canonical rolling timestamp is `observed_at_utc`, captured separately for
each symbol immediately after its successful validated REST response. Binance
`oi_exchange_time` remains preserved for transaction-age distributions,
unchanged-transaction counts, upstream diagnostics, and materially impossible
future-time protection.

Four clocks remain distinct:

- `observed_at_utc`: canonical sample ordering, target, and baseline clock, captured per symbol immediately after a validated REST response;
- `oi_exchange_time`: Binance transaction-time diagnostic metadata;
- `price_exchange_time`: price association and skew diagnostics;
- collector cycle start/finish: operational scheduling and duration only.

Behavior:

- OI more than 5 seconds in the future relative to receipt is rejected;
- OI up to 5 seconds in the future is retained with a clock-skew warning and must normalize on later cycles;
- old OI transaction time is counted and summarized but does not reject present OI;
- stale price receipt or excessive price-event/observation skew removes only price/USD fields;
- local wall clock must be UTC-aware; a materially future Binance timestamp remains an explicit corruption/clock-skew rejection.

## Warm-up and restart

Phase 1 uses cold in-memory warm-up.

- A 5m signal is eligible only when the store can select an acceptable 5m baseline using the exact rule above.
- A 20m TOP row is eligible only when an acceptable 20m baseline exists.
- A 60m or 120m quality result requires its exact baseline; partial internal anchors remain visible through coverage rather than fabricated data.
- Eligibility depends on actual samples, not process uptime.
- Historical `sumOpenInterestValue` is never used to seed current `openInterest` quantity history.

Expected normal availability at 30-second cadence is shortly after 5, 20, 60, and 120 minutes for their respective windows, subject to baseline tolerance and successful cycles. Restarts explicitly log `ROLLING_OI_WARMUP` coverage until each window is available.

Durable store persistence may be evaluated after Phase 1. It requires versioned schema, atomic writes, corruption handling, expiry, and exchange-time validation. It is not justified before the in-memory design is proven.

## Alert state machine

State is maintained per symbol and direction:

```text
NORMAL
POSITIVE_TRIGGERED
NEGATIVE_TRIGGERED
```

The production threshold `T` comes from the existing impulse threshold (currently 5%). The re-arm level is `R = 0.60 * T`, currently 3%. The ratio is configurable with validation `0 <= R < T`.

Transitions:

```text
NORMAL + change >= +T
  -> emit one positive signal -> POSITIVE_TRIGGERED

NORMAL + change <= -T (only if negative alerts are enabled)
  -> emit one negative signal -> NEGATIVE_TRIGGERED

POSITIVE_TRIGGERED + change remains > +R
  -> no repeat

POSITIVE_TRIGGERED + -T < change <= +R
  -> NORMAL (re-armed)

POSITIVE_TRIGGERED + change <= -T
  -> emit one reversal signal -> NEGATIVE_TRIGGERED

NEGATIVE_TRIGGERED is symmetric:
  no repeat while change < -R;
  re-arm at change >= -R;
  direct reversal at change >= +T.
```

Phase 1 preserves existing positive-alert behavior; negative detection/state is implemented and tested but Telegram delivery remains configuration-gated until product behavior is approved.

No fixed time cooldown is used initially because hysteresis reflects the metric returning toward neutral. A second legitimate impulse requires re-arm or a direct reversal. The 60% choice balances chatter and sensitivity: a very high re-arm value chatters around 5%, while a very low value can suppress a distinct later move. Shadow data must validate this ratio before cutover.

State changes when a signal event is accepted into the publisher/outbox path, not when Telegram confirms delivery. A failed send therefore does not generate a new event every collector cycle. Delivery retry must use an event ID and bounded retry policy.

## 20-minute TOP behavior

TOP remains schedule-driven at the existing `minute=0,20,40`, `second=10` cadence. The job reads the latest store snapshot and calculates rolling 20m results; it never initiates a Binance OI collection.

Initial cutover behavior:

- rank/filter on primary `oi_quantity_change_pct` using the existing TOP threshold;
- map quantity change and aligned sample price change into the existing formatter row;
- retain derived USD change in diagnostic logs until a future report-format decision;
- exclude symbols without valid 20m windows;
- suppress the report if current eligible-symbol coverage is below 90%, rather than publishing a misleading partial-market TOP.

Impulse and TOP consume the same collected samples, eliminating duplicate historical full-market passes.

## Failure handling

| Failure | Required behavior |
|---|---|
| One OI request failure | Record typed error, keep old samples, process other symbols |
| Several failed symbols | Classify coverage; retain/process successes according to cycle health |
| Less than 50% success | Major failure: store valid samples but emit no new signals; suppress TOP |
| Complete network failure | Preserve store, back off, mark collector unavailable; never insert zeros |
| Request timeout | Isolate symbol; allow one retry only if transient and budget permits |
| HTTP 429 | Stop retries, honor server delay, enter budget backoff |
| HTTP 418 | Stop polling and enter protection state until ban/recovery criteria expire |
| Old/future OI transaction time | Accept and summarize old transaction age; reject only materially impossible future time |
| Malformed OI | Typed parse failure; never coerce to zero |
| Missing/stale price | Store OI quantity; leave price and derived USD unavailable |
| WebSocket disconnect | Continue OI quantity collection; reconnect price stream with backoff |
| Newly listed symbol | Add to base polling, cold warm-up, no window until baseline exists |
| Delisted symbol | Stop polling/reporting immediately; prune retained data normally |
| Collector overrun | Cycle timeout cancels pending tasks; next tick is skipped if lock remains held |
| Local clock anomaly | Degrade/suppress signals when repeated exchange-time validation fails |

## Diagnostics

Normal operation emits summaries, state changes, and candidate details—not one INFO record per normal symbol.

### `OI_COLLECTOR_SUMMARY`

Cycle start/finish, elapsed, requested/success/failed counts, timeouts, budget
state, old/unchanged transaction counts, min/median/p95/max transaction age,
and distinct fresh/missing/receipt-stale/alignment-rejected price counts.

### `OI_SAMPLE_WARNING`

Only malformed, materially future, duplicate-conflict, or out-of-order cases.
Old transaction time is summarized rather than emitted once per symbol.

### `ROLLING_OI_DIAG`

For threshold candidates, shadow-selected symbols, and large divergence cases: symbol, window, latest OI time, target, baseline, baseline offset, actual duration, quantity/USD/price changes, and price/OI skews.

### `ROLLING_OI_SIGNAL`

Symbol, previous/new state, threshold, re-arm, metric value, event ID, and reason.

### `ROLLING_OI_WARMUP`

One summary per cycle/window: eligible symbols, available windows, missing baseline count, stale latest count.

### `PRICE_STREAM_STATUS` / `PRICE_STALE_WARNING`

Connection transitions, reconnect count, last-message age, frames parsed/rejected, and symbols with fresh prices. Per-symbol stale warnings are rate-limited.

### `RATE_LIMIT_BUDGET`

Parsed limit windows, planned/observed use, reserve, state, optional work skipped, next allowed cycle, 429/418 counts.

Task 2 `OI_DIAG`/`OI_DIAG_SUMMARY` and Task 4 live probe remain available for legacy diagnosis.

## Shadow mode and cutover

Migration runs both engines:

```text
legacy openInterestHist calculation -> Telegram remains legacy-driven
new current-OI rolling calculation  -> no production Telegram initially
                                  \-> OI_SHADOW_COMPARE
```

Comparison is INFO-level only for:

- either metric approaching/crossing threshold (for example absolute change at least 60% of threshold);
- the largest bounded set of divergences per cycle;
- manually selected diagnostic symbols;
- legacy/rolling endpoint-time gaps that explain different results.

Other comparisons may be DEBUG or aggregate summary only. Historical shadow requests are optional budget work and must be skipped before jeopardizing the current-OI collector.

Example fields:

```text
OI_SHADOW_COMPARE symbol=ABCUSDT
legacy_5m=+2.8 rolling_quantity_5m=+5.4 rolling_usd_5m=+5.9
legacy_end_utc=... rolling_end_utc=... rolling_baseline_utc=...
```

Cutover to rolling-driven impulse Telegram requires all of the following, measured over at least 7 consecutive days and at least 30 threshold-candidate events, whichever takes longer:

1. at least 99% median cycle coverage and no unexplained sustained coverage below 90%;
2. at least 99.9% valid current-OI timestamp/value parsing;
3. p95 accepted OI sample age within the configured freshness limit;
4. fresh attached prices for at least 99% of accepted OI samples used in compared signals;
5. no HTTP 418 and no unresolved HTTP 429 behavior;
6. every rolling candidate has reproducible baseline selection and no future-data leakage;
7. manual review confirms rolling detects verified events earlier or more accurately than legacy and exposes explained USD/quantity differences;
8. restart, warm-up, WebSocket reconnect, partial failure, and Telegram retry drills pass;
9. threshold/re-arm behavior produces no repeated-alert spam in shadow replay.

This is **VALIDATION REQUIRED** in Task 13. Failing any criterion keeps Telegram on legacy logic. Cutover is configuration-gated and independently reversible for 5m and 20m.

## Historical endpoint after migration

`GET /futures/data/openInterestHist` remains for:

- Task 2 timestamp/freshness diagnostics;
- Task 4 live probing;
- bounded shadow comparison;
- manual debugging and validation;
- an explicitly labelled fallback report if product requirements later demand it.

It does not seed quantity history and does not define the primary real-time impulse or TOP after their respective cutovers.

## Migration plan

### Task 7 — Current OI client and core models

Add the current-OI client method, strict response/timestamp parsing, `Decimal`-based reading/sample models, and `exchangeInfo` rate-limit parsing. Unit-test all parsing and errors. No runtime integration.

### Task 8 — Mark-price WebSocket service

Implement `MarkPriceStream` and `PriceStateStore`, reconnect/staleness health, pure frame parsing, and deterministic fake-stream tests. No collector integration.

### Task 9 — Rolling store and calculator

Implement 150-minute bounded retention, deterministic duplicate/out-of-order rules, generic exact 5m/20m/60m/120m baseline selection, quantity/USD/aligned-price calculations, and descriptive long-accumulation quality metrics. No Slow/Long production thresholds are applied.

### Task 10 — Rate budget and current OI collector

Implement `RateLimitBudget`, dedicated bounded executor, non-overlapping cycle orchestration, timeouts/retries, partial-success classification, sample creation, and fake-client integration tests. Measure whether 20 seconds satisfies the documented validation gate; retain 30 seconds otherwise.

### Task 11 — Application lifecycle and shadow integration

Start/stop WebSocket, collector, store, and comparison service safely alongside legacy jobs. Telegram remains legacy-driven. Add graceful-shutdown and service-failure integration tests.

Implemented as one application-owned `RollingOIShadowRuntime`. When enabled,
it starts one mark-price stream, obtains runtime `REQUEST_WEIGHT` limits, reuses
the legacy scheduler's cached eligible symbol universe, and begins natural-startup
30-second collection without five-minute boundary alignment. It evaluates
5m/20m/60m/120m windows after successful insertions and emits bounded shadow
candidates plus one cycle summary. Legacy 5m and 20m jobs retain all Telegram
ownership and provide only a bounded comparison hook. HTTP 429 enters a minimum
60-second polling backoff; HTTP 418 stops shadow polling. Shutdown stops the
periodic task, drains/closes the collector executor, then stops the WebSocket.

### Task 12 — Observation timing and legacy event-loop stabilization

Rolling storage, retention, exact-window baselines, and long-window anchors use
`observed_at_utc`. Price receipt/event freshness is evaluated against that same
observation context; OI transaction/price skew is diagnostic only. Legacy symbol
discovery, full-market scans, bounded internal scanner pools, price fill, and
shadow comparison execute through one application-owned single-worker legacy
executor. The outer worker only waits for each blocking phase and serializes
colliding legacy jobs; the scanner's existing ten-worker pool is unchanged, so
this does not create another per-symbol executor or alter legacy
formulas, schedules, formatting, routing, or Telegram ownership.

### Follow-up — Rolling impulse state machine

Implement crossing, hysteresis re-arm, reversal, event IDs, send-independent state, configuration gating, and replay tests. Still shadow-only.

### Task 13 — Live shadow validation

Run sustained comparisons, collect budget/coverage/freshness/reconnect evidence, replay candidate signals, validate the 30s default versus conditional 20s cadence, and produce a cutover report against every criterion.

### Task 14 — Switch 5m impulse to rolling quantity

After Task 13 approval, feature-flag Telegram to the rolling quantity state machine. Preserve immediate rollback to legacy and keep comparisons running.

### Task 15 — Switch 20m TOP to rolling store

Use scheduled rolling 20m results, preserve the existing Telegram schedule/formatter mapping, enforce coverage, and remove the duplicate historical full-market TOP scan from the production path.

### Task 16 — Post-cutover cleanup and persistence decision

After a stabilization period, remove obsolete primary legacy scheduling while retaining diagnostic/probe capabilities. Evaluate whether restart persistence or adaptive fast polling is justified by measured operations.

Every task is independently tested and committed. Feature flags preserve rollback across integration and cutover tasks.

## Test strategy

All automated tests use fake clients, fake clocks, controlled executors, recorded WebSocket frames, and fake Telegram senders. They require no live Binance or Telegram access.

Required tests:

1. current OI response parsing;
2. current OI timestamp parsing and UTC awareness;
3. mark-price WebSocket message parsing;
4. stale/missing price detection;
5. rolling sample insertion and ordering;
6. time- and count-based pruning;
7. idempotent and conflicting duplicate samples;
8. retained and rejected out-of-order samples;
9. exact 5m baseline selection;
10. exact 20m baseline selection;
11. tolerance inclusive edge and one-microsecond/second outside edge;
12. insufficient history and cold warm-up;
13. missed collector cycles within/outside tolerance;
14. quantity-change calculation and zero baseline failure;
15. USD-change calculation and missing-price unavailability;
16. aligned sample-price change and skew reporting;
17. old transaction-time acceptance and materially future-time rejection;
18. valid quantity insertion with missing price;
19. positive and optional negative threshold crossing;
20. no repeat while above threshold;
21. re-arm at the exact hysteresis boundary;
22. positive-to-negative and negative-to-positive reversal;
23. one-symbol, degraded, and major collector failures;
24. request-budget arithmetic at 15/20/30/60 seconds and optional-work priority;
25. prevention of overlapping cycles and cycle timeout;
26. WebSocket reconnect/backoff/stable-reset state;
27. shadow comparison selection and aggregation;
28. TOP coverage suppression and no collection side effect;
29. graceful shutdown with in-flight executor work;
30. signal state independence from Telegram send success.

## DECISIONS

1. **Primary OI trigger metric:** rolling `oi_quantity_change_pct` from current `openInterest`.
2. **Secondary OI diagnostic metric:** derived `oi_value_change_pct` from `oi_quantity * mark_price`; explicitly not assumed equal to historical `sumOpenInterestValue`.
3. **Price source:** all-market `!markPrice@arr@1s` WebSocket via one persistent reconnecting service.
4. **OI source:** per-symbol `GET /fapi/v1/openInterest`; historical OI is not a primary signal source.
5. **Base collector cadence:** configurable, default 30 seconds. A 20-second cadence is **VALIDATION REQUIRED** under the documented budget and cycle-duration gates.
6. **Adaptive fast polling in initial release:** no; interfaces permit a bounded future watch set.
7. **Rolling retention:** 150 minutes plus a cadence-derived hard sample cap (302 samples per symbol at the 30-second default).
8. **Rolling baseline rule:** for 5m/20m/60m/120m, use the latest valid sample at or before `latest.observed_at_utc - window`, within tolerance; no future-side selection or interpolation.
9. **Long accumulation quality:** 60m/120m results expose persistence, trend efficiency and direction, positive-magnitude maximum drawdown, 5m impulse concentration, and coverage. Hard quality thresholds are **VALIDATION REQUIRED**.
10. **Baseline tolerance:** `max(60 seconds, 2 * configured base cadence)` for all supported windows.
11. **Canonical sample timestamp:** per-request `observed_at_utc`; Binance `oi_exchange_time` is transaction-time diagnostic metadata and may repeat without collapsing observations.
12. **Warm-up/restart strategy:** cold in-memory warm-up based on actual acceptable baseline availability; no historical/current semantic mixing.
13. **5m alert re-arm strategy:** hysteresis at `0.60 * threshold` (3% for a 5% threshold), direct reversal supported, no fixed cooldown initially; shadow validation required.
14. **20m TOP schedule behavior:** retain `minute=0,20,40`, `second=10`; read the rolling store and never trigger collection.
15. **OI collector concurrency approach:** reuse the synchronous client through an application-owned bounded executor, default 20 workers; never block the event loop.
16. **Rolling evaluation timing:** after each bounded full-market cycle in Phase 1; per-symbol immediate evaluation remains an optional later optimization.
17. **Rate-limit safety policy:** runtime limit parsing, normal usage capped at 70%, at least 30% reserve, primary collector priority, 429 backoff, and 418 protection stop.
18. **WebSocket stale-data policy:** stream stale/reconnect after 5 seconds without valid frames; per-symbol price attachable only when receipt and event timestamps are fresh against observation time; OI quantity remains valid without price.
19. **Shadow-mode cutover criteria:** at least 7 consecutive days and 30 candidates plus all nine documented quality/operational gates; **VALIDATION REQUIRED** in Task 13.
20. **Role of historical `openInterestHist`:** diagnostics, live probe, bounded shadow comparison, manual debugging, and explicitly labelled fallback only; never seed or drive the primary rolling signal after cutover.
