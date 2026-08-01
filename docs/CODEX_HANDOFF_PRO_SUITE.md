# Codex Handoff: Pro Suite and Private Daily Closure

Read `PROJECT_CONTRACT.yaml` first and do not infer missing owner data. This file
preserves the work order when conversation context or Codex quota is unavailable.

## Completed library work

- Trump Policy Transmission Index;
- point-in-time Polymarket settlement event study;
- Barra-inspired public factor-risk proxy;
- Kalman dynamic exposure;
- manager skill, timing, persistence and fragility;
- one-report JSON/Markdown orchestrator;
- deterministic synthetic demo and focused tests.

These are research libraries. Production collectors, the GPT receiver, IBKR
reconciliation and recurring activation remain deployment work.

## Next PRs

### 1. `fix/operational-repository-role`

- make `US-stock-daily-report` private through repository settings;
- remove or archive obsolete branches;
- stop describing it as a safe public history;
- add a pinned framework version and deprecate manual full-tree sync.

### 2. `feat/pro-suite-private-report-adapter`

- feed the existing ledger-backed private daily JSON into the Pro suite;
- preserve accounting fields without recalculation;
- add a versioned `research_enrichment` object;
- ensure exactly one final report is produced per local date.

### 3. `feat/verified-gpt-receiver`

- implement receiver lookup or a stable idempotency key;
- handle ambiguous sends without blind retry;
- persist receipt identity in the private outbox;
- prove one live send and one same-day replay without duplication.

### 4. `feat/ibkr-flex-readonly-reconciliation`

- read NAV, cash, positions, fills, fees, income and corporate actions;
- compare them with the confirmed ledger;
- emit reconciliation proposals only;
- never submit orders or silently mutate owner-confirmed events.

### 5. `feat/production-research-adapters`

- official White House/presidential-action collector with source health;
- resolved Polymarket point-in-time export adapter;
- authorized Social Heat snapshot persistence;
- scheduled prediction-ledger settlement;
- corporate-action source and reconciliation.

## Required live acceptance sequence

```text
readiness audit
-> opening owner attestation
-> ledger initialization
-> real dual-source accepted close
-> private daily report preparation
-> GPT receiver delivery
-> same-day replay proves no duplicate
-> next trading day proves one report per date
```

Do not activate recurring delivery before every step is evidenced in private
runtime state.

## Deferred extensions after the live loop works

- VIX1D/VIX9D/VVIX/SKEW and options-chain stress;
- overnight/premarket anomaly model;
- Brinson-Fachler and Carino attribution;
- asset-allocation optimizer;
- factor-version-isolated residual calibration;
- automated corporate-action adjustment paths.
