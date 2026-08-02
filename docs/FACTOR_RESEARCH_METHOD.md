# Factor Research Method

## Objective

The daily system does not ask, “Which factor had the best recent backtest?” It
asks a stricter question:

> Does this versioned factor still have economically plausible, cost-adjusted,
> out-of-sample evidence across more than one horizon, and is that evidence
> relevant to the current portfolio?

A factor may explain risk without forecasting return. An academically published
factor is a research candidate, not an automatic live signal.

## Research foundations

The initial library follows transparent public foundations rather than claiming
access to any private fund's internal model:

- Fama/French market, size, value, profitability and investment factors;
- AQR value, momentum and Quality Minus Junk research/data;
- MSCI's value, size, low-volatility, yield, quality and momentum taxonomy;
- Moreira–Muir volatility-managed risk scaling;
- trend and systematic research practices publicly described by firms such as
  AQR and Man AHL;
- machine-learning asset-pricing evidence from Gu, Kelly and Xiu;
- multiple-testing concerns from Harvey, Liu and Zhu;
- backtest-overfitting and Sharpe-inference controls associated with Bailey,
  Borwein and López de Prado.

The implementation uses a small portfolio-relevant proxy set first. New factors
must enter through `config/factor_registry.example.yaml` with a definition,
economic rationale, data lineage, expected horizon and implementation role.

## Daily, weekly and monthly responsibilities

### Every daily run

- append the latest point-in-time signal and return observations;
- rerun versioned 1/5/20-session purged walk-forward tests;
- deduct turnover-based transaction costs;
- update OOS IC, hit rate, drawdown, Sharpe/PSR and factor status;
- compare factor direction across horizons;
- report active, watch and quarantined factors;
- never change the economic definition because of one new day.

### Weekly review

- inspect coefficient drift, signal decay, turnover and source coverage;
- compare rolling and expanding-window results;
- check exposure concentration and portfolio relevance;
- investigate material status changes rather than automatically trading them.

### Monthly or data-definition-change review

- admit, redefine or retire a factor version;
- review the economic rationale and source provenance;
- run broader robustness checks, alternative costs and subperiods;
- increment `feature_version` whenever definitions, data timing or preprocessing
  change.

## Point-in-time validation

For a signal known at close `t`, the target is the compounded return over
`t+1 ... t+h`. The engine then applies:

```text
training data
-> purge h sessions before the test block
-> training-only mean/std estimation
-> ridge fit
-> future OOS block
-> sample every h sessions to avoid overlapping targets
-> embargo h sessions before the next fold
-> bounded exposure proxy
-> turnover and cost deduction
```

Changing a future target must not alter an earlier OOS prediction. Tests enforce
this property.

## Factor-level admission

Each factor is evaluated using:

- OOS rank Information Coefficient;
- coefficient-direction-adjusted OOS IC;
- coefficient sign consistency across folds;
- OOS sample size;
- Spearman p-value;
- Benjamini–Hochberg q-value across the tested factor family;
- a robustness score combining IC, stability, statistical evidence and sample
  shrinkage;
- implementation cost and turnover at the ensemble level.

Single-horizon states:

| State | Meaning |
|---|---|
| `active` | positive OOS IC, stable coefficient direction and FDR threshold passed |
| `watch` | positive evidence, but weak, unstable or multiple-testing marginal |
| `quarantined` | negative/unstable/cost-ineffective evidence; weight is zero |
| `blocked` | insufficient or invalid data; weight is zero |

A factor becomes multi-horizon `active` only when it is active in at least two
of 1/5/20 sessions, has consistent sign and passes the multiple-testing gate.
Otherwise it remains watch or quarantined.

## Portfolio-level metrics

For each horizon the report includes:

- OOS observations;
- gross and net annualized return;
- annualized volatility;
- Sharpe and probabilistic Sharpe ratio;
- prediction IC and hit rate;
- OOS R-squared;
- maximum drawdown;
- average turnover;
- total modeled cost drag.

These are calibration diagnostics, not promises of expected performance.
Probabilistic Sharpe does not replace minimum track record, capacity, liquidity,
tax, borrow or tail-risk review.

## Cross-sectional and event factors

The current daily proxy is primarily time-series/portfolio-level because it uses
public ETF/stock histories. A later cross-sectional branch should use
point-in-time fundamentals and implement:

- neutralized characteristic portfolios;
- Fama–MacBeth or equivalent cross-sectional regressions;
- Newey–West/HAC inference;
- sector/size/beta neutralization;
- long-short and long-only implementation variants;
- capacity and transaction-cost curves;
- characteristic decay and turnover;
- independent holdout periods.

Global news, policy and social narratives are treated as event/transmission
factors. They require source independence, timestamp preservation and settlement
calibration. They are not mixed with structural value/quality/momentum premia as
though they were the same phenomenon.

## Anti-overfitting boundaries

- no same-period signal/target use;
- purge and embargo equal to the forecast horizon by default;
- no training statistics estimated from the test block;
- no overlapping OOS target records;
- explicit cost deduction;
- FDR control across the factor family;
- exact feature/model version isolation;
- no cross-version residual pooling;
- future-data mutation regression test;
- no automatic promotion from a visually attractive chart;
- no automatic order or ledger mutation.

Probability of Backtest Overfitting, Deflated Sharpe Ratio, White's Reality
Check/Hansen SPA, block bootstrap and capacity curves remain valid extensions
for larger research libraries. They should be added before testing hundreds of
candidate variants, not used to decorate a small library after selection.

## Public references

- Kenneth French Data Library: https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library.html
- AQR Quality Minus Junk: https://www.aqr.com/Insights/Research/Working-Paper/Quality-Minus-Junk
- AQR Value and Momentum Everywhere: https://www.aqr.com/insights/research/journal-article/value-and-momentum-everywhere
- MSCI Foundations of Factor Investing: https://www.msci.com/research-and-insights/paper/foundations-of-factor-investing
- Moreira–Muir Volatility Managed Portfolios: https://www.nber.org/papers/w22208
- Harvey–Liu–Zhu multiple testing: https://www.nber.org/papers/w20592
- Gu–Kelly–Xiu Empirical Asset Pricing via Machine Learning: https://www.nber.org/papers/w25398
- Bailey et al. backtest overfitting: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2308659
