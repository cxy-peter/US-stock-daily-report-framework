# Codex Handoff: Pro Suite and Private Daily Closure

Read `PROJECT_CONTRACT.yaml` first and do not infer missing owner data. This file
preserves the work order when conversation context or Codex quota is unavailable.

## Completed library and repository work

- canonical public framework / private operational-repository roles;
- Trump Policy Transmission Index;
- point-in-time Polymarket settlement event study;
- Barra-inspired public factor-risk proxy;
- Kalman dynamic exposure;
- manager skill, timing, persistence and fragility;
- one-report JSON/Markdown orchestrator;
- deterministic synthetic demo and focused tests;
- read-only IBKR Flex v3 request, parser and reconciliation library;
- persistent `PROJECT_CONTRACT.yaml` and framework pin in the private repo.

The operational repository is private. The IBKR adapter is a tested library but
has not used owner credentials or a real statement. Production collectors, the
GPT receiver and recurring activation remain deployment work.

## Next PRs

### 1. `feat/pro-suite-private-report-adapter`

- feed the ledger-backed private daily JSON and accepted-close state into the
  Pro suite;
- preserve accounting fields without recalculation;
- add a versioned `research_enrichment` object;
- ensure exactly one final report is produced per local date;
- keep configured, modeled and broker-confirmed DCA separate.

### 2. `feat/ibkr-flex-private-runtime-adapter`

- read `IBKR_FLEX_TOKEN` and `IBKR_FLEX_QUERY_ID` only from the private secret
  boundary;
- persist raw XML and parsed snapshots only in owner-controlled storage;
- compare NAV, cash, positions, fills, fees, income and corporate actions with
  the confirmed ledger;
- create owner-confirmation proposals for mismatches;
- do not silently mutate the confirmed ledger and do not submit orders;
- prove one real private reconciliation and safe same-statement replay.

### 3. `feat/verified-gpt-receiver`

- implement receiver lookup or a stable idempotency key;
- handle ambiguous sends without blind retry;
- persist receipt identity in the private outbox;
- prove one live send and one same-day replay without duplication;
- verify exactly one paused product-level automation before activation.

### 4. `feat/production-research-adapters`

- official White House/presidential-action collector with source health;
- resolved Polymarket point-in-time export adapter;
- authorized Social Heat snapshot persistence;
- scheduled prediction-ledger settlement;
- corporate-action source and reconciliation.

### 5. `refactor/private-deployment-package`

- pin a tagged framework release instead of manually mirroring the full tree;
- leave only deployment adapters and owner runbooks in the private repository;
- keep every private database, report and credential outside Git/cloud sync.

## Required live acceptance sequence

```text
readiness audit
-> opening owner attestation
-> ledger initialization
-> real dual-source accepted close
-> real read-only IBKR reconciliation
-> private daily report preparation with Pro enrichment
-> GPT receiver delivery
-> same-day replay proves no duplicate
-> next trading day proves one report per date
```

Do not activate recurring delivery before every step is evidenced in private
runtime state.

## Deferred extensions after the live loop works

- VIX1D/VIX9D/VIX6M/VVIX/SKEW and options-chain stress;
- overnight/premarket anomaly model;
- Brinson-Fachler and Carino attribution;
- asset-allocation optimizer;
- factor-version-isolated residual calibration;
- automated corporate-action adjustment paths.
