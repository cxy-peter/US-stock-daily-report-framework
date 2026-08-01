# US Stock Daily Investment Research Framework

A privacy-safe, auditable framework for one owner-only post-close research
report. It keeps accounting facts, modeled recurring investments, research
signals and broker-confirmed events separate, and it has no broker order
endpoint.

## What makes it different

Typical market agents combine headlines, technical indicators and an LLM score.
This framework instead uses explicit evidence and accounting boundaries:

```text
point-in-time sources
+ source/claim/fragility/manipulation gates
+ accepted-close consensus
+ confirmed and modeled books
+ read-only broker reconciliation
+ objective market stress
+ Social Heat and prediction calibration
+ fund/product monitoring
+ policy, event-study and factor-risk models
-> one private JSON contract
-> one deterministic Chinese Markdown report
```

A KOL, social-media consensus, Trump statement or prediction-market result
cannot independently create `OPEN`, `ADD`, `TRIM` or `EXIT`. Missing data is
reported as `UNKNOWN`, `blocked`, `degraded` or `stale`; it is never silently
converted to neutral.

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

owner-only local runtime
├── private configuration
├── accepted-close cache
├── broker reconciliation evidence
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
- VIX/VIX3M, credit and breadth downside overlay;
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

The IBKR Flex library can parse account summaries, positions, trades, cash
transactions, fees and corporate actions and compare positions/cash with the
confirmed book. It cannot change the ledger or place an order. A private live
query and daily-runtime adapter remain deployment work. See
[`docs/IBKR_FLEX_RECONCILIATION.md`](docs/IBKR_FLEX_RECONCILIATION.md).

## Pro research suite

`serenity_monitor/pro_research/` adds tested offline libraries for:

- **Trump Policy Transmission Index**: source authority, policy stage,
  magnitude, horizon, recency and asset sensitivity;
- **Polymarket settlement studies**: freeze the last probability before a
  24-hour embargo and measure 1/5/20/60-session post-resolution returns;
- **Barra-inspired public proxy**: factor exposure, shrunk covariance,
  systematic/specific risk and marginal risk contribution;
- **Kalman dynamic exposure**: time-varying return-inferred alpha and beta;
- **Manager skill and fragility**: alpha, Bootstrap, timing, capture,
  persistence, leverage, concentration, liquidity and funding fragility;
- **One-report orchestration**: combine bounded model multipliers into
  `HOLD`, `RISK_REBALANCE` or `PAUSE_AND_VERIFY`.

These are not production data collectors and do not claim commercial Barra
output, disclosed holdings, or automatic trading. See
[`docs/PRO_RESEARCH_SUITE.md`](docs/PRO_RESEARCH_SUITE.md).

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

The Pro command writes exactly:

```text
pro_daily_report.json
pro_daily_report.md
```

All public values are fictional and all automatic-execution fields are fixed to
`false`.

## Private daily runtime

The existing owner-only runtime is deliberately separate from the synthetic
commands. Its guarded sequence is:

```text
readiness audit
-> owner opening attestation
-> ledger initialization
-> dual-source accepted close
-> read-only IBKR reconciliation
-> private report preparation
-> verified receiver delivery
-> same-day replay without duplication
```

Production activation remains blocked until a private IBKR reconciliation trial,
a receiver with idempotency or lookup, and a persisted live end-to-end report
trial succeed. Read
[`docs/PRIVATE_DAILY_ACTIVATION.md`](docs/PRIVATE_DAILY_ACTIVATION.md) before
using any mutating private-runtime command.

## Still not production-complete

- private live IBKR Flex query configuration and ledger-queue integration;
- verified GPT receiver and recurring private delivery;
- production White House/Trump and Polymarket collectors;
- automated corporate-action reconciliation;
- scheduled prediction settlement and structured topic detail in the private
  report contract;
- expanded VIX surface, overnight/premarket model, Brinson/Carino attribution
  and asset-allocation optimizer.

The exact boundary is maintained in
[`docs/IMPLEMENTATION_STATUS.md`](docs/IMPLEMENTATION_STATUS.md) and the next
work order in
[`docs/CODEX_HANDOFF_PRO_SUITE.md`](docs/CODEX_HANDOFF_PRO_SUITE.md).
