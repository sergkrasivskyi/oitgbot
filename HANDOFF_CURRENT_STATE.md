# Current project handoff

## Status

Task 27 production stabilization/observation is **PASS**. The completed live
observation period found no material runtime failures. Current tablet production,
before Task 28 deployment, remains **5m rolling OI IMPULSE + 15m OI ANOMALY**
with ALL/PROP publication. The legacy rolling 20m TOP scheduler remains disabled
through `ROLLING_OI_20M_TOP_ENABLED=0`; its implementation is rollback-only.

Task 28 is **CODE PASS / LIVE VALIDATION PENDING**. Its implementation has not
been deployed or restarted on the tablet. Do not describe the Spot filter as
live until the manual deployment checklist has passed.

## Task 28 universe contract

The canonical collector universe is:

```text
active Binance USDⓈ-M USDT perpetual futures
INTERSECT
active Binance Spot TRADING USDT markets
```

One bulk Futures exchange-info request and one bulk Spot exchange-info request
run on the existing symbol-cache refresh. Exact symbol matches win. Explicit
aliases are limited to `DODOX → DODO` and `LUNA2 → LUNA`. Only explicit `1000`
and `1000000` multiplier prefixes may be transformed, and only when the target
active Spot USDT pair exists. Everything unresolved is excluded without guessing.

The filter is upstream of current-OI collection, rolling windows, new research
telemetry, 5m alerts, 15m candidates, 60m/120m shadow analytics, and rollback
20m reports. A startup resolution failure yields no collection universe. A later
failure retains the last-known-good in-memory resolution. No disk cache or
telemetry schema migration was added; historical excluded-symbol rows remain
until normal retention removes them.

Internal identity and PROP membership remain the futures symbol. Telegram keeps
the existing CoinGlass futures link and adds plain text `· TOKEN (S)` only when
the resolved Spot base differs. The hint is not an additional hyperlink and has
no effect on math, eligibility, Z, NEW, ranking, or routing.

## Unchanged production contracts

5m IMPULSE retains its 5% trigger, 3% re-arm, state persistence, and direct
reversal behavior. The 15m product retains closed UTC alignment, `OI15% >= +1%`
eligibility, unchanged OI/PX formulas, same-symbol classic Z15 over the trailing
14 days with at least 96 prior valid observations, Z-first ranking, PX as context,
and NEW based on eligible appearances in `[T - 12h, T)`. Collector cadence,
current-OI endpoint behavior, mark-price WebSocket, SQLite schema/retention,
Telegram destinations, and legacy 20m production flag are unchanged.

## Validation and operations

The read-only live audit on 2026-09-10 observed 523 eligible futures, 488 active
Spot USDT markets, 363 retained futures (355 exact, 6 multiplier, 2 alias), and
160 unresolved exclusions. These are observations, not durable architecture
constants; rerun `python -m tools.spot_universe_audit` before deployment.

Task 28 still requires manual tablet deployment and live validation. FLASH
remains an unnumbered, unimplemented research direction requiring 1m telemetry,
shadow validation, and anti-spam trigger/re-arm design before publication.
