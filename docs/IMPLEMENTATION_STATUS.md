# Verified Implementation Status

This document separates four states: **library**, **test**, **live input**, and
**daily-report integration**. A class, design note, or synthetic test is not a
production-completion claim. The machine-readable source of truth is
`requirements/DAILY_RESEARCH_REQUIREMENTS.yaml`.

## Public framework boundary

The public repository contains code, synthetic fixtures, tests, methods,
requirements, and reusable Skills. Personal holdings, broker records, paid
research, raw social exports, and owner reports remain outside this repository.

## Integrated into the daily research path

### Portfolio and factor core

- global-event and cross-asset narrative scoring;
- source-independence and primary-source gates;
- repeated-headline collapse by independence group and topic;
- Quora/search context at zero direct weight;
- purged and embargoed 1/5/20-session walk-forward regression;
- future-only targets, training-only scaling, turnover/cost deduction, FDR,
  Probabilistic Sharpe, IC/stability, and factor quarantine;
- current research history feeding the daily factor result.

### Policy and prediction markets

- public White House collection plus optional private political documents;
- complete-sentence policy claims with authority, implementation stage,
  specificity, novelty, horizon, recency, and media disagreement;
- Trump Policy Transmission Index fed by validated claims/private events;
- public live Polymarket history, spread, liquidity, depth, book imbalance,
  time-to-resolution, and calibration;
- private resolved-market event-study input with pre-resolution freeze and
  1/5/20/60-session outcomes;
- political and prediction-market groups remain bounded research overlays.

### Market risk and exposure

- public VIX1D/VIX9D/VIX/VIX3M/VIX6M/VVIX/SKEW proxies or a private snapshot;
- one correlated option-surface downside group;
- optional point-in-time option-chain and overnight/premarket inputs;
- Barra-inspired public covariance/risk proxy from current price history;
- Kalman return-inferred dynamic exposure from current price history;
- no market-risk or exposure model independently creates a trade.

### Fund, manager, and financial-institution research

- dedicated public views for asset managers, fund sponsors, banks, brokers,
  exchanges, and market-structure companies;
- institutional `evaluate-us-funds` Skill;
- manager/strategy alpha, bootstrap, Treynor-Mazuy and Henriksson-Merton timing,
  capture, rolling persistence, tracking error, and fragility;
- named-manager attribution and manager/team change remain required.

### Social and external-agent fallback

- one private inbox for Reddit, Quora, Xiaohongshu, X, forums, broker-research
  digests, and financial/news-agent summaries;
- original URLs, first-observed timestamps, ticker/topic, direction, horizon,
  and invalidation;
- verification by one primary source or two independent institutional groups;
- agent prose is secondary synthesis, never original evidence;
- social and agent summaries have zero direct `ADD/OPEN` weight;
- verified bearish crowding may only tighten risk, capped at 5%;
- missing access remains `no_data`, `blocked`, `error`, or `not_configured`, not
  neutral.

### Buy-side report

Each material thesis states:

- action implication;
- change from the previous comparable score;
- consensus and variant perception;
- evidence chain;
- catalysts;
- horizon;
- invalidation/exit conditions;
- confidence and affected assets.

The private v4 renderer removes the routine portfolio-wide fee section. It
shows security-specific commission, spread/slippage, holding friction, and
lot/tax uncertainty only for an actual `ADD_REVIEW` or `TRIM_REVIEW` candidate.
Missing components remain `UNKNOWN`.

## Live path, private input still required

The following adapters execute but remain partial until valid point-in-time
owner inputs exist:

- Xiaohongshu/Reddit/Quora and external-agent digests;
- paid broker/FOF research digests and original links;
- resolved-Polymarket event history;
- reliable option-chain and overnight snapshots;
- named-manager/fund return and fragility data;
- product holding-cost, spread/slippage, and lot/tax data.

No missing input is fabricated or silently interpreted as neutral.

## Library/test only or not yet in the owner action path

- Brinson-Fachler/Carino require private period attribution inputs;
- constrained allocation requires private expectations, constraints, and
  implementation costs;
- factor-residual calibration and settlement require the private prediction
  ledger and complete accepted-close paths;
- full corporate-action reconciliation requires position-mapped issuer,
  regulator, exchange, or sponsor evidence;
- these remain diagnostics or input-dependent and cannot be described as fully
  active.

## Private deployment

The private deployment provides read-only IBKR data, completed-exchange-session
freshness, full public/private test preflight, one private Issue per report date,
and replay/update instead of duplicate creation. It has no report writeback or
broker order capability.

The report schedule is:

- through **2026-08-09**: 08:30 `Asia/Shanghai`;
- from **2026-08-10**: 08:30 `America/New_York`, with EDT/EST handled by a
  local-time gate.

## Context persistence

v4 adds:

- `requirements/DAILY_RESEARCH_REQUIREMENTS.yaml`;
- `skills/maintain-daily-research-context/SKILL.md`;
- `scripts/check_requirement_ledger.py`;
- a project contract that forbids treating library existence as production
  completion.

## Verification

```bash
python -m compileall -q .
python scripts/check_public_privacy.py
python scripts/check_project_contract.py
python scripts/check_requirement_ledger.py
pytest -q
```
