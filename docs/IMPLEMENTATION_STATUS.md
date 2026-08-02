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
- objective volatility, credit and breadth downside overlay;
- authorized Xiaohongshu context and cross-platform Social Heat;
- append-only prediction research ledger;
- institutional `evaluate-us-funds` Skill and offline fund monitor.

### Global market narratives and factor validation

- source-role separation for issuer/government/regulatory primary evidence,
  major international media, Korean regional/wire media, KOL/community and
  public-search context;
- default public-source coverage for Al Jazeera oil/geopolitics, the SK hynix
  newsroom, Yonhap, Korea Herald, Korea Times, Reddit and Quora/search context;
- English/Korean topic detection for oil supply, Middle East escalation,
  shipping, HBM demand, memory oversupply, export controls, Korean chip policy,
  tariffs, China demand and rates/inflation;
- explicit cross-asset transmission to MU, SMH, broad/Nasdaq equity, energy,
  gold, Treasuries and related proxies;
- repeated-headline and syndication collapse by independence group and topic;
- Quora/search-only zero direct weight and Reddit/community as one correlated
  social evidence group;
- media disagreement, community sentiment and crowding diagnostics;
- downside-only global-narrative risk reduction capped at 10% and research-score
  contribution bounded to -4%/+1%, with no independent trade authority;
- strict walk-forward ridge regression with train-before-test ordering;
- training-only standardization, horizon-spaced non-overlapping OOS records and
  turnover-cost deduction;
- OOS return/volatility/Sharpe, hit rate, rank IC, R-squared, drawdown, turnover
  and cost-drag reporting;
- per-factor coefficient stability, directional OOS IC and
  `active/watch/quarantined/blocked` admission;
- exact feature/model-version isolation and a regression test proving that a
  future-target mutation cannot change an earlier OOS prediction;
- one daily enrichment orchestrator that may refresh public research every day
  independently of less-frequent IBKR reconciliation.

These libraries use public or synthetic research data only. Adjusted price
history is explicitly non-settlement-grade and cannot replace the accepted-close
path.

### Political communication and live prediction-market research

- complete-sentence policy-claim extraction rather than keyword counts;
- actor-specific authority, with the President receiving the highest prior and
  other officials/industry executives receiving topic-specific weights;
- implementation-stage, specificity, novelty, horizon, holdings relevance and
  recency scoring;
- independent media consensus, disagreement and uncertainty overlays that do
  not replace the direct source;
- public White House article/listing collector, official X API v2 collector and
  RSS/Atom media collector;
- public read-only Polymarket Gamma/CLOB client for active markets, probability
  history, spread and order book;
- unresolved-market probability velocity, entropy, depth, spread,
  time-to-resolution and calibration scoring;
- political group cap of 8%, live Polymarket group cap of 3%, no automatic
  execution and no Polymarket wallet/order capability;
- refreshable example registry for 20 policy roles and portfolio-industry roles.

Collectors and models are tested with synthetic data. No production X token,
private source configuration or owner report is part of the public repository.

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

### Read-only IBKR Flex reconciliation

- Flex Web Service v3 `SendRequest`/`GetStatement` client with bounded polling;
- HTTPS host validation, sanitized errors and token-redacted client output;
- parsing for account summary, open positions, trades, cash transactions, fees
  and corporate actions;
- account-ID redaction and statement evidence digest;
- read-only comparison of broker positions/cash with the confirmed local book;
- structured `MATCHED`, `MISSING_LOCAL_EVENT`, `UNEXPECTED_BROKER_EVENT`,
  `QUANTITY_MISMATCH`, `CASH_MISMATCH` and `NEED_OWNER_CONFIRMATION` results;
- `automatic_ledger_mutation_permitted=false` in every result.

The library is tested with synthetic XML. No real token, query or private broker
statement is part of the public framework.

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

The Barra module does not claim commercial MSCI Barra data; Kalman exposure is
return-inferred rather than disclosed holdings; manager skill does not bypass
the fragility/copy-trade gate.

### Advanced market risk

- VIX1D/VIX9D/VIX/VIX3M/VIX6M term-structure ratios;
- VVIX, SKEW, realized-volatility and put/call inputs;
- one correlated SPX-option-surface risk group rather than double-counted
  confirmations;
- option-chain ATM IV, 25-delta skew, 10-delta wing convexity, expected move,
  put/call ratios, quote-quality haircut and approximate signed gamma;
- overnight/premarket own-history z-score, range, volume, equity-index futures,
  VIX and credit confirmation;
- downside-only or caution-only risk multipliers with no trade authority.

### Attribution and constrained allocation

- Brinson-Fachler allocation, selection and interaction;
- exact single-period reconciliation;
- Carino multi-period linking and linked Brinson convenience wrapper;
- covariance shrinkage and positive-semidefinite repair;
- optional expected returns;
- position bounds, overlapping group caps, turnover limits and transaction-cost
  penalties;
- marginal and percentage risk contributions;
- proposal-only output with no broker target or order object.

### Corporate actions, calibration and settlement

- point-in-time broker versus issuer/SEC/exchange/fund-sponsor action
  reconciliation;
- matched, missing-primary, source-conflict and missing-broker-action states;
- no automatic quantity, cash or cost-basis adjustment;
- residual forecast calibration isolated by signal version, factor-model
  version, horizon and market regime;
- MAE, RMSE, net residual, directional hit rate, recent hit rate and rank IC;
- active, decayed, quarantined and research-only weight states;
- no cross-version pooling;
- due 1/5/20/60-session settlement planning with a complete accepted-close
  path, exact factor lineage and stable idempotency keys;
- callback-only settlement execution disconnected from brokers.

## Implemented but not yet connected end-to-end

- global narratives and walk-forward factor validation have a daily enrichment
  orchestrator and deterministic Markdown, but are not yet embedded as a
  versioned object in the ledger-backed private report contract;
- political, live Polymarket, volatility, option, overnight, attribution,
  allocation and calibration outputs are not yet embedded as versioned
  enrichment objects inside the single ledger-backed private daily report;
- the IBKR Flex library is not yet wired to owner-only credentials, persisted
  reconciliation evidence or the private ledger confirmation queue;
- Social Heat and fund-monitor aggregates can enter the private report, but
  production source credentials and owner-only snapshots are incomplete;
- the prediction scheduler exists, but the private daily runtime has not yet
  resolved accepted-close objects and called the append-only ledger callback;
- corporate-action reconciliation lacks production issuer/SEC/exchange adapters
  and an owner-confirmation queue;
- the delivery outbox exists, but no verified GPT receiver is deployed.

## Production blockers

- `US-stock-daily-report` must remain private because its old history is not safe
  for public release;
- private configuration and databases must be outside Git and cloud sync;
- one real dual-source accepted-close run must be persisted;
- the opening ledger must be owner-attested and initialized;
- one private IBKR reconciliation must be executed and reviewed;
- official political/X/Polymarket/option source configuration must be reviewed
  and recorded without exposing credentials;
- a receiver must prove stable idempotency or delivery lookup;
- one same-day replay must prove no duplicate delivery;
- exactly one recurring product-level daily task must be identified before
  activation.

## Remaining production roadmap

- private-runtime integration and one live trial for broker, global-market,
  political, Polymarket, Social Heat, volatility and corporate-action layers;
- a new private-report schema version containing structured research enrichment;
- production option-surface, overnight and corporate-action data adapters;
- automatic due-settlement callback integration;
- verified GPT delivery and local/private-GitHub fallback delivery;
- tax-lot and wash-sale review fields before an allocation proposal becomes an
  owner action candidate.

Every production item requires point-in-time fixtures, source-health semantics,
focused tests and a live-safe integration test before it can be marked active.
