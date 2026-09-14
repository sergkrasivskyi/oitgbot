# AGENTS.md — OI TG Bot / CCS Foundation

## Scope

Permanent Codex execution contract for `sergkrasivskyi/oitgbot` (`main`).

Task prompts use **CCS (Codex Compact Spec)** and should contain only task-specific deltas. Apply this file automatically with every task. An explicit task instruction overrides this file only for that task.

## 1. Authority & pre-flight

Use sources by role:

- **Task** — requested delta: exact semantics, numbers, edge cases, non-goals, DONE.
- **Code + tests** — implemented behavior.
- **`HANDOFF_CURRENT_STATE.md`** — operational/deployment/LIVE state.
- **`README.md`** — concise product/operator overview.
- **Architecture/spec docs** — detailed subsystem contracts/rationale.

Before editing:

1. Check branch and `git status --short`.
2. Read the full current `HANDOFF_CURRENT_STATE.md`.
3. Read relevant README/spec/architecture sections.
4. Inspect affected code, tests, config, runtime wiring, persistence/state, formatter/publisher, and tools.

If sources disagree: preserve production behavior unless the task explicitly changes it; use code/tests for behavior, HANDOFF for LIVE/deployment state, and the task for the requested delta. Update stale docs when the correct state is clear. Never invent missing product semantics; resolve ambiguity from the repo when possible.

Never discard unrelated user changes or use destructive Git operations unless explicitly requested.

## 2. Change policy & production invariants

Implement the **smallest coherent production-safe change**.

- Reuse existing services/stores/writers/schedulers/formatters/safe-send/config/state/logging patterns.
- Avoid parallel infrastructure when an existing abstraction extends cleanly.
- No unrelated refactors, renames, formatting churn, dependency changes, or speculative roadmap work.
- Preserve backwards compatibility/config semantics unless explicitly changed.
- Preserve exact thresholds, inclusivity, timing, ordering, timestamps, edge cases, state and failure semantics.
- Prefer deterministic behavior/order; persisted/analytical time is UTC unless explicitly defined otherwise.

Existing products/routes are unchanged unless named by the task. Unless explicitly required:

- do not change signal math, eligibility, ranking, NEW/re-arm/cooldown, persistence, Telegram UX/destinations, or ALL/PROP routing;
- do not enable legacy/rollback products;
- do not add duplicate Binance current-OI polling, another mark-price WS, per-symbol Spot polling, or extra market-data load for convenience.

Research/shadow/parallel subsystems must fail independently from established production paths unless the task defines coupling.

## 3. Config, secrets, persistence, side effects

Follow existing config style and use safe defaults: new code must not publish/activate unexpectedly after deployment.

Never commit or hardcode tokens, chat IDs, credentials, `.env`, private exports, production DBs, or runtime SQLite/WAL/SHM/state artifacts. Do not modify the external tablet env/config automatically unless explicitly requested; document required manual env changes.

For state/DB work:

- reuse existing persistence/background-writer patterns where appropriate;
- keep latency-sensitive paths non-blocking where practical;
- make required state restart-safe and failure behavior explicit/tested;
- never silently replace required durable persistence with memory-only state;
- keep schema/index additions minimal.

Tests/verification must not publish to real Telegram destinations. Do not push, merge, deploy, restart production, perform tablet cutover, or publish live unless explicitly requested.

**CODE PASS != LIVE PASS.** Tests/local runs can establish CODE PASS only; LIVE PASS requires the specified real deployment/live validation.

## 4. Verification

Never weaken/delete/skip tests merely to pass a task.

For code changes, use focused tests during development, then normally run:

```text
python -m pytest <focused tests>
python -m pytest
python -m ruff check .
python -m compileall -q oitgbot tools
git diff --check
```

Adapt commands only to repository structure. For docs-only changes, run only relevant checks. If a check cannot run, report why; never claim PASS for an unsuccessful/unrun check.

Before completion inspect `git diff`, `git diff --check`, and `git status --short`.

## 5. Docs & Git

Behavior-changing tasks must leave docs consistent:

- keep `README.md` concise/current;
- update `HANDOFF_CURRENT_STATE.md` carefully;
- update architecture/spec docs when data flow or subsystem contracts change;
- replace stale statements instead of stacking contradictory history.

HANDOFF must clearly distinguish: **CODE PASS / not deployed / deployed but pending validation / LIVE PASS**. Never infer LIVE PASS from tests.

For normal implementation tasks, after required checks pass, create **one clean task commit** unless the task says otherwise. Use a concise conventional commit message. Do not push automatically. Read-only/investigation tasks do not commit unless requested.

## 6. Completion report

Keep it concise and evidence-based. Include as applicable:

- files changed and behavior delta;
- key architecture/state/persistence decisions;
- focused/full tests + lint/compile/diff-check;
- docs updated;
- known limitations/deferred items;
- deployment/LIVE state;
- `git status`;
- commit SHA.

Include live/deployment commands only when requested.

## 7. CCS task prompt contract

Future task prompts should contain only the delta:

- `GOAL`
- `SEMANTICS / MATH`
- `CONFIG`
- `DATA / STATE`
- `OUTPUT / UX`
- `FAILURE RULES`
- focused `TESTS`
- `NON-GOALS`
- `DONE`

Do not repeat this foundation in task prompts. Keep task specs compact, exact, and production-safe.
