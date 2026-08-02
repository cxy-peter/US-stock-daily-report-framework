# US Stock Daily Investment Research Framework

A privacy-safe, auditable framework for one owner-only post-close report. It
keeps broker facts, accounting, modeled recurring investments, research signals
and owner decisions separate, and it has no broker order endpoint.

## What makes it different

Typical market agents combine headlines, technical indicators and an LLM score.
This framework instead uses explicit evidence and accounting boundaries:

```text
point-in-time sources
+ accepted-close consensus
+ confirmed and modeled books
+ read-only broker and corporate-action reconciliation
+ claim-level political communication analysis
+ live and resolved prediction-market research
+ Social Heat and exact-version signal calibration
+ volatility surface, option-tail and overnight risk
+ factor risk, dynamic exposure and fund research
+ Brinson/Carino attribution and constrained allocation
-> one private JSON contract
-> one deterministic Chinese Markdown report
-> owner manual decision and owner manual trade
```

A KOL, social-media consensus, political statement, prediction-market result,
optimizer or factor model cannot independently create `OPEN`, `ADD`, `TRIM` or
`EXIT`. Missing data is reported as `UNKNOWN`, `blocked`, `degraded` or `stale`;
it is never silently converted to neutral.

## Repository boundary

This public repository contains code, synthetic fixtures, tests and methodology
only. Real holdings, cash, costs, reports, credentials and runtime databases
must remain outside Git and cloud-sync directories.

```text
public framework repository
├── source and tests
├── synthetic configurations
├── report schemas
└── public CI: offline/mock only

owner-only runtime
├── private configuration and broker snapshots
├── accepted-close and corporate-action evidence
├── portfolio and prediction ledgers
├── delivery outbox
└── private reports
```

`PROJECT_CONTRACT.yaml` is the persistent machine-readable scope and handoff
contract. `docs/REPOSITORY_ROLES.md` defines the canonical public framework and
the separate private deployment role.

## Implemented core

- SEC/X/Reddit and authorized-social evidence boundaries;
- KOL source, claim, manager-fragility and manipulation scoring;
- deterministic portfolio and risk-group gates;
- Twelve Data plus Alpha Vantage accepted-close consensus;
- U.S. exchange calendar with holidays, DST and early closes;
- append-only confirmed/modeled portfolio ledger;
- owner-attested fills, cash flows, income, fees, splits and DCA skips;
- read-only IBKR Flex v3 client, parser and reconciliation result model;
- versioned private daily-report JSON and deterministic Chinese Markdown;
- local immutable delivery outbox and readiness audit;
- Social Heat with separate attention/execution weights and a 5% total cap;
- append-only 1/5/20/60-session prediction research ledger;
- institutional U.S. fund/product Skill and offline fund monitor.

The IBKR layer can parse account summaries, positions, trades, cash
transactions, fees and corporate actions and compare positions/cash with the
confirmed book. It cannot change the ledger or place an order. See
[`docs/IBKR_FLEX_RECONCILIATION.md`](docs/IBKR_FLEX_RECONCILIATION.md).

## Political and policy communications

`serenity_monitor/political_communications.py` extracts complete economically
material claims from official actions, speeches, interviews, press briefings,
official X posts and media interpretation. It evaluates actor authority,
implementation stage, dates/quantities/agencies, novelty, holdings relevance and
media disagreement. It does not use word frequency as the policy signal.

Public adapters are available for White House pages, official X API v2 timelines
and public RSS/Atom feeds. A refreshable example registry covers 20 policy roles
and portfolio-industry roles. The President receives the highest actor prior;
other officials and executives remain topic-specific evidence. See
[`docs/POLITICAL_COMMUNICATIONS.md`](docs/POLITICAL_COMMUNICATIONS.md).

## Live and resolved Polymarket research

`serenity_monitor/polymarket_live.py` reads public Gamma/CLOB metadata, price
history, spread and order-book depth. An unresolved market is treated as a noisy
forecast and sentiment state whose weight depends on liquidity, spread,
time-to-resolution and historical calibration. The correlated live group is
capped at 3% of the decision score and has no wallet or order method.

The separate resolved-event study freezes the last probability observed before
a 24-hour embargo and measures 1/5/20/60-session returns. It never backfills a
post-resolution probability into a predictor. See
[`docs/LIVE_POLYMARKET.md`](docs/LIVE_POLYMARKET.md).

## Pro research suite

`serenity_monitor/pro_research/` includes:

- Trump Policy Transmission Index;
- point-in-time Polymarket settlement studies;
- Barra-inspired public factor/covariance proxy;
- Kalman-filtered return-inferred dynamic exposure;
- manager alpha, Bootstrap, timing, capture, persistence and fragility;
- bounded one-report research orchestration.

These modules do not claim commercial MSCI Barra output, disclosed holdings or
automatic execution. Academic and institutional motivation, caveats and
factor-admission rules are recorded in
[`docs/ACADEMIC_EVIDENCE_MAP.md`](docs/ACADEMIC_EVIDENCE_MAP.md).

## Advanced risk, attribution and allocation

The additional research libraries implement:

- VIX1D/VIX9D/VIX/VIX3M/VIX6M, VVIX and SKEW term-surface stress;
- option-chain skew, convexity, expected move, put/call and approximate gamma;
- overnight and premarket anomaly classification using own-history and futures
  confirmation;
- Brinson-Fachler allocation/selection/interaction;
- Carino multi-period contribution linking;
- covariance-shrunk allocation proposals with costs, turnover and group caps;
- point-in-time corporate-action reconciliation;
- factor-model-version-isolated residual calibration;
- automatic planning of due 1/5/20/60-session prediction settlements.

All of these are research or accounting controls. They cannot submit an order,
modify the confirmed ledger, or automatically apply a corporate action. See
[`docs/ADVANCED_RISK_ATTRIBUTION_ALLOCATION.md`](docs/ADVANCED_RISK_ATTRIBUTION_ALLOCATION.md).

## Synthetic validation

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python -m compileall -q .
python scripts/check_public_privacy.py
python scripts/check_project_contract.py
pytest -q

python run_report.py --mock --no-external \
  --config config/portfolio.example.yaml \
  --out-dir out_public_smoke \
  --date 2026-01-02

python run_pro_daily.py \
  --config examples/pro_daily_config.example.yaml \
  --out-dir out_pro_demo
```

All public values are fictional and all automatic-execution fields are fixed to
`false`.

## Private daily runtime

The guarded target sequence is:

```text
readiness audit
-> owner opening attestation
-> ledger initialization
-> dual-source accepted close
-> read-only IBKR and corporate-action reconciliation
-> political / prediction / social / volatility / factor enrichment
-> attribution and allocation review
-> one private report
-> verified receiver delivery
-> same-day replay without duplication
```

Production activation remains blocked until a private broker reconciliation
trial, an idempotent or queryable receiver, and a persisted live end-to-end
report trial succeed. Read
[`docs/PRIVATE_DAILY_ACTIVATION.md`](docs/PRIVATE_DAILY_ACTIVATION.md) before
using any mutating private-runtime command.

## Still not production-complete

- owner-only credentials and persisted live IBKR/Flex reconciliation;
- verified GPT receiver and recurring private delivery;
- private live political, Polymarket and Social Heat snapshot persistence;
- wiring the advanced libraries into the single ledger-backed private report;
- production corporate-action source adapters and owner-confirmation queue;
- live option-surface and overnight data adapters.

The exact boundary is maintained in
[`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) and the next
work order in
[`docs/CODEX_HANDOFF_PRO_SUITE.md`](docs/CODEX_HANDOFF_PRO_SUITE.md).
