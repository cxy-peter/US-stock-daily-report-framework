# Verified Implementation Status

This file is the source-of-truth boundary between tested code, offline research
libraries and unfinished production deployment. A design document is not an
implementation claim.

## Verified public framework

The history-free public framework contains only source, synthetic fixtures,
tests and methodology. Public CI runs on Linux and Windows with no schedule,
live credentials, report artifact, Actions summary or repository writeback.

## Implemented and tested

### Evidence, risk and research core

- SEC/X/Reddit and authorized-social collection boundaries;
- source, claim, manager-fragility and manipulation scoring;
- evidence independence and primary-source gates;
- deterministic holding/watchlist research rules;
- portfolio, cash, turnover, position and risk-group constraints;
- VIX/VIX3M, credit and breadth downside-only objective overlay;
- authorized Xiaohongshu context and cross-platform Social Heat;
- append-only prediction research ledger with 1/5/20/60-session settlement;
- institutional `evaluate-us-funds` Skill and offline fund monitor.

### Accepted close and private accounting

- raw Twelve Data and Alpha Vantage close observations;
- exact session, MIC, currency, raw-price and finality validation;
- two-independent-source consensus with 30/75 bps gates;
- pinned exchange sessions, holidays, DST and early closes;
- append-only confirmed and modeled portfolio books;
- atomic base-DCA settlement and exact Decimal accounting;
- owner-attested opening and later fills/cash/income/fees/splits/DCA skips;
- versioned private daily-report JSON, deterministic Markdown and local outbox;
- activation readiness audit, crash recovery and replay/idempotency controls.

### Pro research suite v1

The following offline libraries are implemented under
`serenity_monitor/pro_research/` and covered by synthetic point-in-time tests:

- Trump Policy Transmission Index;
- Polymarket settlement event study with a pre-resolution embargo;
- Barra-inspired public factor/covariance risk proxy;
- Kalman-filtered dynamic alpha/beta exposure;
- manager alpha, Bootstrap, timing, capture, persistence and fragility;
- one-report research orchestrator with bounded risk multipliers;
- stable `pro_daily_report/v1.0.0` JSON and Chinese Markdown output.

These modules do not include production collectors and cannot submit orders.
The Barra module does not claim commercial MSCI Barra data; Kalman exposure is
return-inferred rather than disclosed holdings; manager skill does not bypass
the fragility/copy-trade gate.

## Implemented but not yet connected end-to-end

- the Pro suite is not yet embedded as a versioned enrichment object inside the
  ledger-backed private daily-report contract;
- Social Heat and fund-monitor aggregates can enter the private report, but
  production source adapters and persistent owner-only snapshots are incomplete;
- prediction-ledger settlement exists, but the private daily runtime does not
  yet schedule every due horizon automatically;
- the delivery outbox exists, but no verified GPT receiver is deployed;
- owner-confirmed accounting is available, but IBKR is not read automatically.

## Production blockers

- `US-stock-daily-report` must be private because its old history is not safe for
  public release;
- private configuration and databases must be outside Git and cloud sync;
- one real dual-source accepted-close run must be persisted;
- the opening ledger must be owner-attested and initialized;
- a receiver must prove stable idempotency or delivery lookup;
- one same-day replay must prove no duplicate delivery;
- exactly one recurring product-level daily task must be identified before
  activation.

## Remaining roadmap

- read-only IBKR Flex reconciliation;
- production White House/presidential-action and Polymarket adapters;
- automated corporate-action evidence and adjustment workflow;
- structured prediction/topic detail in a new private-report schema version;
- VIX1D/VIX9D/VIX6M/VVIX/SKEW and options-chain stress;
- overnight futures and premarket anomaly model;
- Brinson-Fachler and Carino attribution;
- asset-allocation and marginal-contribution optimizer;
- factor-model-version-isolated residual calibration.

Every remaining item requires point-in-time fixtures, source-health semantics,
focused tests and a live-safe integration test before it can move into the
implemented section.
