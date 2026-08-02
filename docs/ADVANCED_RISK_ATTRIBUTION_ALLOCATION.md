# Advanced Risk, Attribution, Allocation, and Settlement

This layer extends the one-report framework with six additional research and
accounting controls. Every module is offline/testable, point-in-time where
applicable, and disconnected from broker execution.

## 1. Volatility surface

`advanced_market_risk.evaluate_volatility_surface` jointly evaluates:

- VIX1D;
- VIX9D;
- VIX;
- VIX3M;
- VIX6M;
- VVIX;
- Cboe SKEW;
- realized volatility;
- put/call volume and open-interest ratios.

The maturities, VVIX and SKEW share an SPX-options lineage. They are treated as
one correlated evidence group rather than multiple independent confirmations.
The result may only reduce the effective risk budget; missing observations do
not imply a calm market.

## 2. Option-chain tail risk

`advanced_market_risk.evaluate_option_tail_risk` derives:

- at-the-money implied volatility;
- 25-delta put and call implied volatility;
- 10-delta put implied volatility;
- downside skew and far-wing convexity;
- implied expected move;
- put/call volume and open-interest ratios;
- an approximate signed gamma profile;
- quote-liquidity quality.

The gamma estimate uses open interest and Black-Scholes gamma as a research
proxy. It is not observed dealer inventory. Wide quotes reduce confidence and
all option-derived variables remain one correlated evidence group.

## 3. Overnight and premarket anomaly

`advanced_market_risk.evaluate_overnight_risk` separates close-to-open price
discovery from regular-session returns. A move is normalized against the same
asset's historical overnight distribution and cross-checked with:

- premarket volume;
- overnight high/low range;
- S&P 500, Nasdaq and small-cap futures;
- VIX change;
- credit confirmation.

The output distinguishes normal-range moves, thin-liquidity stretches,
confirmed gaps and unconfirmed anomalies. It changes opening caution inside the
single daily report; it never creates a second report or a trade.

## 4. Brinson-Fachler and Carino

`performance_attribution.py` implements:

- single-period Brinson-Fachler allocation, selection and interaction;
- explicit reconciliation to portfolio minus benchmark return;
- Carino multi-period linking;
- a convenience wrapper for linked Brinson totals.

This is performance explanation, not return prediction. Group definitions and
benchmarks must be fixed before the return period to avoid retrospective
reclassification.

## 5. Constrained allocation research

`portfolio_optimizer.optimize_allocation` combines:

- a shrunk covariance matrix;
- optional expected returns;
- position minimums and maximums;
- overlapping group caps;
- turnover limits;
- symbol-specific transaction-cost estimates;
- marginal and percentage risk contributions.

The output is a proposal only. Tax lots, wash-sale rules, liquidity, account
restrictions, owner intent and broker confirmation remain downstream gates. No
order object or broker target is produced.

Expected-return inputs are optional because unstable forecasts can make an
optimizer less reliable than a risk-only allocation. Without them, the model is
minimum-variance and turnover aware.

## 6. Corporate-action reconciliation

`corporate_action_reconciliation.py` compares broker observations with
point-in-time issuer, SEC, exchange or fund-sponsor evidence for:

- cash and stock dividends;
- splits and reverse splits;
- spin-offs;
- mergers and acquisitions;
- ticker changes and delistings;
- rights issues, distributions and return of capital.

A broker event with no primary evidence is `NEED_PRIMARY_EVIDENCE`; conflicting
terms are `SOURCE_CONFLICT`; a primary announcement missing from the broker
snapshot is `MISSING_BROKER_ACTION`. Even a matched result cannot automatically
change quantity, cash or cost basis.

## 7. Factor-model-version isolation

`factor_residual_calibration.py` evaluates out-of-sample residual forecasts by
the exact tuple:

```text
signal model version
× factor model version
× horizon
× market regime
```

It calculates residual MAE/RMSE, net residual return, directional hit rate,
recent hit rate and rank IC. States are:

```text
active
 decayed
 quarantined
 research_only
```

Observations from `barra-v1` and `barra-v2`, for example, are never pooled. A
factor definition change creates a new calibration history rather than silently
borrowing the old model's record.

## 8. Prediction-ledger automatic settlement

`prediction_settlement_scheduler.py` identifies due 1/5/20/60-session outcomes.
A task is generated only when:

- the horizon target has passed;
- the accepted-close path covers every trading session in the horizon;
- accepted-close timestamps follow the original signal;
- required factor residual evidence exists;
- factor residual evidence uses the exact declared factor-model version;
- the residual becomes available after the final accepted close;
- the horizon has not already been settled or reversed.

Each task receives a stable content-derived idempotency key. Execution occurs
only through an injected append-only ledger callback; the scheduler has no
portfolio or broker API.

## Intended single-report sequence

```text
broker baseline and confirmed ledger
+ accepted-close consensus
+ corporate-action reconciliation
+ political / Polymarket / Social Heat research
+ volatility surface / option tail / overnight state
+ factor exposure and dynamic beta
+ exact-version prediction calibration
+ attribution and constrained allocation review
-> one private daily JSON
-> one deterministic Chinese Markdown report
-> owner manual decision and manual trade
```

The owner remains the only execution authority.
