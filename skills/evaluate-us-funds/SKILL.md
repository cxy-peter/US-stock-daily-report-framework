---
name: evaluate-us-funds
description: Institutional buy-side research and portfolio-fit evaluation for U.S.-listed or U.S.-domiciled funds and exchange-traded products. Use when researching, comparing, selecting, monitoring, or writing an investment memo for U.S. mutual funds, index or active ETFs, non-transparent ETFs, closed-end funds, covered-call or option-income funds, leveraged/inverse ETPs, ETNs, commodity pools, or grantor trusts; when assessing a manager's repeatable skill, style drift, factor exposures, fees, liquidity, tax and legal structure; or when integrating a candidate fund into a user's U.S. investment strategy, IPS, fund pool, watchlist, position sizing, rebalancing, and exit rules.
---

# Evaluate U.S. Funds

## Purpose

Treat “is this a good fund?” as a conditional portfolio decision:

`Good(fund | investor, mandate, role, alternatives, price, as_of_date)`

Do not produce a generic league table. Establish the investor's constraints, identify the product's legal and economic structure, isolate the return source, and test the candidate's marginal contribution to the existing portfolio. Keep product quality and portfolio suitability as separate conclusions.

## Required references

Read only the files needed for the case, but always read:

- `references/methodology.md` for the institutional workflow, evidence tests, and failure modes.
- `references/output-contract.md` for statuses, reason codes, evidence labels, and the report structure.

Also read:

- `references/us-product-branches.md` for any U.S. fund or ETP. After confirming legal structure, apply every relevant structure, strategy, tax, and trading branch in the prescribed order.
- `references/source-map.md` when explaining provenance, refreshing the framework, or tracing a rule back to cited broker research, official sources, or academic research.
- `references/runtime-monitoring.md` for a scheduled monitoring update, an event-triggered review, or when wiring the Skill to the deterministic fund-monitor module.

Use `assets/strategy-profile.yaml` as the editable schema when no strategy profile exists. Build a temporary in-memory profile for read-only questions. Copy it into the user's working directory only when the user asks to persist/customize a profile or the task otherwise authorizes that file write. Do not overwrite the installed template unless the user explicitly asks to change the reusable default.

## Non-negotiable rules

1. Put an `as_of_date` on the analysis and a source/date on every material number.
2. Distinguish `FACT`, `CALCULATION`, `INFERENCE`, `JUDGMENT`, and `SOCIAL_SIGNAL`.
3. Record missing facts as `UNKNOWN`; never convert missing data to zero, neutral, or a passing score.
4. Prefer regulator filings and official documents over aggregators. Use social media only to discover questions, failure cases, or claims to verify.
5. Identify the legal structure before scoring. A ticker is not proof that a product is a 1940 Act ETF.
6. Establish the investment role and comparison anchor before interpreting performance. Compare only like-for-like risk mandates.
7. Never let high past returns override a hard mandate, structure, liquidity, tax, governance, or data-quality failure.
8. Do not use one total-score formula across passive ETFs, active funds, CEFs, option-income funds, geared products, and ETNs.
9. Do not infer the user's U.S. strategy from generic research materials. If the profile is incomplete, finish a product-level memo and return `NEED_INFO` for personalized fit.
10. Do not turn research into an automatic trade. Give a conditional role, sizing range, execution rule, review date, and exit triggers.

## Workflow

### 0. Define the decision

Capture:

- candidate ticker, share class, CUSIP/CIK if available, and analysis date;
- investor tax residency, account type, base currency, time horizon, liquidity needs, and restrictions;
- objective, benchmark, risk budget, current holdings, cost basis if tax matters, target role, and alternatives;
- requested output: first-pass screen, full due diligence, comparison, portfolio fit, or monitoring update.

Search the workspace for an existing strategy profile before asking the user. If essential facts remain missing, state what can and cannot be concluded and proceed with product research.

### 1. Establish evidence freshness

Browse for current fund facts. Use this hierarchy:

1. SEC/CFTC/FINRA/IRS and EDGAR filings;
2. current prospectus, SAI, audited report, shareholder report, holdings, and official index methodology;
3. sponsor factsheet, portfolio disclosures, tax supplement, and trading statistics;
4. independent databases and peer-reviewed research;
5. broker research for methodology and historical context;
6. Reddit, X, forums, and commentary only as weak signals.

Resolve conflicts in favor of the more authoritative and more recent source, while recording the conflict. Do not cite a search snippet when the underlying document is available.

### 2. Identify legal and economic structure

Confirm whether the candidate is a mutual fund, Rule 6c-11 ETF, exemptive-order/non-transparent ETF, CEF, ETN, commodity pool, grantor trust, or another ETP. Record regulator, issuer, assets held or promised, creation/redemption process, leverage/derivatives, and tax form expectations.

If structure is unresolved, stop any quality score and return `NEED_INFO / STRUCTURE-UNVERIFIED`.

### 3. Classify actual exposure and choose the anchor

Triangulate:

- mandate and prospectus language;
- current and historical holdings;
- return-based factor/style behavior.

Assign role on two axes:

- `portfolio_tier`: `CORE`, `SATELLITE`, or `TACTICAL`;
- `economic_role`: `BETA`, `ALPHA`, `FACTOR_TILT`, `HEDGE`, `INCOME`, or `LIQUIDITY`.

Select a benchmark and peer group that match the actual exposure, not merely the marketing category. Flag style drift, mandate drift, benchmark gaming, and stale holdings.

### 4. Apply hard gates

Test mandate fit, investor eligibility, legal/tax acceptability, operational integrity, disclosure availability, liquidity/capacity, strategy stability, and required downside limits. A gate is contextual: document the investor need and the evidence, rather than applying a universal AUM or tenure cutoff.

Use:

- `REJECT` when a verified failure makes the product unsuitable for the stated mandate;
- `WATCH` when the thesis is plausible but evidence, stability, valuation, capacity, or execution is not yet sufficient;
- `NEED_INFO` when a missing user or product fact prevents a fit decision;
- `PASS` only after product quality and portfolio fit both pass.

### 5. Run the product-specific branch

Open `references/us-product-branches.md` and compose all applicable branches in this order:

1. universal legal/economic structure check;
2. vehicle branch such as ETF, non-transparent ETF, CEF, ETN, or commodity vehicle;
3. strategy branch such as active, fixed income, option income, or leveraged/inverse;
4. tax/domicile and trading/liquidity overlays.

For example, an option-income active ETF requires the ETF, active, option-income, tax, and trading checks. Do not substitute a generic ETF checklist for a complex structure.

### 6. Test performance quality and repeatability

Use aligned total-return data and the correct benchmark. When the user provides a CSV of periodic returns, run:

```powershell
python scripts/compute_fund_metrics.py --input <returns.csv> --periods-per-year 252
```

The script accepts `date,fund_return,benchmark_return,risk_free_return`; benchmark and risk-free columns are optional. Read its warnings and do not report estimates the data cannot support.

If risk-free observations are missing, Sharpe and CAPM alpha/beta remain `null`. Use `--assume-zero-risk-free` only when the decision explicitly authorizes and discloses that assumption.

Evaluate:

- return, volatility, maximum drawdown and recovery, downside deviation, VaR/ES, Sharpe, Sortino, and Calmar;
- tracking difference for passive funds; active return, tracking error, information ratio, alpha/beta, and capture ratios where appropriate;
- rolling windows, bull/bear/sideways regimes, stress periods, and out-of-sample behavior;
- persistence after fees, factor exposure, capacity, and multiple testing;
- holdings-based attribution, return-based attribution, and qualitative explanations as separate evidence streams.

Treat sample weights and cutoffs in historical broker reports as examples, not universal constants. Reject look-ahead bias: respect filing publication lags and the actual information available on each decision date.

Any claim that a screen, label, or score predicts future returns must use a point-in-time universe and walk-forward or untouched holdout evidence. Include closed/liquidated products and departed managers where data permit, consolidate economically equivalent share classes, and deduct realistic fees, turnover, spread, slippage, tax, and capacity effects. If these controls are absent, label the result `DESCRIPTIVE_ONLY`.

### 7. Perform qualitative diligence and historical replay

Assess firm, people, philosophy, process, portfolio construction, risk controls, operations, fees, and capacity. Cross-check the manager's stated philosophy against holdings, trades, and returns. Reconstruct several consequential historical decisions and ask:

- What did the manager know at the time?
- Was the action consistent with the stated process?
- Did the result come from repeatable skill, intended beta, unintended exposure, or luck?
- Is the opportunity scalable at current assets and market liquidity?

Downgrade unresolved contradictions; do not invent a narrative to reconcile them.

Attribute performance to the actual decision-maker and decision period. Cut the record at manager or material team changes, exclude build-out periods when appropriate, and disclose co-manager attribution uncertainty. Do not assign a product shell's prior history to a new manager.

### 8. Integrate with the U.S. portfolio strategy

Compare the current portfolio with and without the candidate. Analyze:

- exposure overlap by holdings and return/factor similarity;
- marginal volatility, drawdown, concentration, liquidity, and currency risk;
- expected source of return and whether an existing holding already supplies it more cheaply;
- fee, spread, premium/discount, turnover, capital-gains distribution, withholding, and estate/tax constraints;
- bull, sideways, drawdown, volatility spike, rate shock, credit shock, and liquidity stress scenarios;
- replacement cost, implementation friction, and behavior risk.

Do not make personalized after-tax claims until tax residency, account type, domicile, and relevant treaty facts are known. Recommend professional tax/legal review where consequences are material.

### 9. Decide, size, and govern

Produce the report defined in `references/output-contract.md`. Give separate verdicts for:

- `product_quality`;
- `portfolio_fit`;
- `overall_status`.

When sizing is justified, give a range and connect it to a risk budget, not a precise false-optimal weight. Define entry conditions, order type or premium/discount guardrails when relevant, rebalance bands, review cadence, monitoring indicators, downgrade triggers, and exit triggers.

If sizing inputs are incomplete, set `approved_weight_range: UNKNOWN`. A scenario range used to test portfolio sensitivity is not a recommended weight.

### 10. Maintain the fund pool

Use a four-stage governance path: `BASIC_POOL -> PREFERRED_POOL -> INVESTMENT_POOL`, with `WATCH_POOL` for exceptions and deterioration. Re-evaluate on both schedule and event triggers. Manager departure, process change, style drift, capacity/liquidity deterioration, abnormal premium/discount, leverage or distribution-policy change, thesis failure, and risk-limit breach all require review.

Performance disappointment alone is not an exit rule; diagnose whether the original thesis is intact. Performance success alone is not evidence that a broken process should remain approved.

For scheduled or event-triggered updates, follow
`references/runtime-monitoring.md`. Build `FundSource`, `FundEvidence`,
`FundMetric`, `LastCompleted`, and `FundMonitorRequest` only from current,
privacy-minimized evidence, then call `monitor_fund()`. Treat `NOT_DUE` as a
runtime cadence state, not an approval verdict. Persist returned
`triggered_event_keys` only after the exact event review is durably recorded.
The monitor cannot fetch data, fill evidence gaps, alter a holding or change a
DCA plan.

## Updating this skill

When the user asks ChatGPT/Codex to modify the framework:

1. Read all affected files before editing and preserve product-specific exceptions and prior safety checks.
2. Put changing personal constraints in a copied `strategy-profile.yaml`, not in the universal methodology.
3. Put new product rules in `references/us-product-branches.md`, new report fields/reason codes in `references/output-contract.md`, and new provenance in `references/source-map.md`.
4. Keep `SKILL.md` as the routing and execution layer; avoid duplicating long reference material here.
5. Re-run the bundled metric-script self-test and the skill validator after any structural change. On Windows, run the validator in UTF-8 mode: `python -X utf8 <skill-creator>/scripts/quick_validate.py <skill-dir>`.
