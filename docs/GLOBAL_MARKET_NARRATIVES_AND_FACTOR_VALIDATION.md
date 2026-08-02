# Global Market Narratives and Walk-Forward Factor Validation

## Purpose

This layer closes two gaps in the daily research system:

1. global events and subjective market information were present as headlines but
   not translated into explicit cross-asset transmission channels;
2. candidate factors could be described or estimated in sample without one
   common point-in-time out-of-sample admission contract.

The layer is research-only. It cannot submit, modify or cancel an order and it
cannot automatically mutate the confirmed or modeled portfolio ledger.

## Source roles

The source registry separates source authority from popularity:

| Source class | Initial role | Direct decision authority |
|---|---|---:|
| Issuer/government/regulatory primary source | factual event evidence | bounded |
| Major international media | independent interpretation and event discovery | bounded |
| Korean regional/wire media | local semiconductor and policy context | bounded |
| Independent research/KOL | thesis context, subject to credibility and fragility | low |
| Reddit/community | crowding and disagreement only | no independent authority |
| Quora/search snippet | discovery lead only | zero |

The default public-search queries cover:

- Al Jazeera oil, OPEC, Hormuz, Red Sea and shipping developments;
- the SK hynix newsroom for HBM, DRAM, memory capacity and partnerships;
- Yonhap, Korea Herald and Korea Times semiconductor coverage;
- Quora semiconductor/Micron discussions as zero-weight context;
- a bounded public-search query for the configured Serenity/KOL context.

Reddit uses the existing public RSS search collector. All community observations
belong to correlated social-media evidence groups; many posts are not many
independent confirmations.

## Event topics and cross-market transmission

The implemented taxonomy includes:

- oil supply and Strait of Hormuz disruption;
- Middle East escalation;
- shipping and supply-chain disruption;
- HBM/memory demand;
- memory oversupply and inventory correction;
- semiconductor export controls;
- Korean semiconductor support policy;
- tariffs and trade conflict;
- China demand;
- rates and inflation.

Each topic has an explicit transmission map. Examples include oil and Middle
East shocks into energy, broad equities, technology, semiconductors, gold and
Treasuries; HBM demand or oversupply into MU/SMH; and export controls into
semiconductors and Nasdaq exposure.

A positive topic score means more of the named condition, not automatically a
positive equity return. For example, positive `oil_supply` means a stronger
supply shock, which is positive for oil/energy but generally negative for broad
and long-duration equity exposure.

Repeated headlines, syndication and reposts are collapsed by
`independence_group × topic`. Quora is context-only. The output reports media
disagreement and community crowding separately from directional transmission.

The combined layer is bounded:

- downside-only risk-budget reduction is capped at 10%;
- research decision contribution is bounded to -4% to +1%;
- it cannot independently create OPEN, ADD, TRIM or EXIT;
- missing or failed sources remain `blocked`, `disabled`, `partial`, `error` or
  `no_data`, never neutral by default.

## Walk-forward regression contract

`factor_backtest.py` validates candidate factors using the following sequence:

```text
factor values known at decision date t
-> training window ending before the test block
-> training-only standardization and ridge fit
-> prediction for a later non-overlapping OOS block
-> bounded position proxy
-> turnover and transaction-cost deduction
-> OOS metrics and per-factor admission
```

Key controls:

- forward returns use `t+1 ... t+h`; the same-day target is never used;
- training rows always end before the first test row;
- OOS records are sampled at the factor horizon to avoid overlapping returns;
- the model may use expanding or rolling windows;
- means, standard deviations and coefficients are estimated from training only;
- turnover costs are deducted before factor admission;
- exact `feature_version` and hashed `model_version` prevent cross-version
  residual pooling;
- changing future outcomes cannot alter an earlier fold's prediction;
- mock/synthetic tests never write private state or a live prediction ledger.

## Metrics and factor admission

The ensemble reports:

- OOS gross and net return;
- annualized return, volatility and Sharpe;
- hit rate;
- prediction information coefficient;
- OOS R-squared;
- maximum drawdown;
- average turnover and total cost drag.

Each factor receives:

- mean standardized coefficient;
- coefficient-sign consistency across folds;
- raw OOS rank IC;
- coefficient-direction-adjusted OOS IC;
- `active`, `watch`, `quarantined` or `blocked` status;
- a shrunk effective-weight multiplier.

An active factor needs sufficient OOS observations, positive
coefficient-direction-adjusted IC and stable coefficient direction. Negative or
unstable evidence is quarantined with zero factor weight. A non-positive net OOS
ensemble is also quarantined and can only tighten, not increase, the risk
budget.

## Daily proxy factors

The first transparent public proxy set uses historical adjusted prices for
research only, never for accepted-close settlement:

- SPY 21- and 63-session momentum;
- negative 21-session SPY volatility;
- SMH relative to SPY;
- MU relative to SMH;
- XLE relative to SPY;
- TLT relative to SPY;
- GLD relative to SPY;
- IWM relative to SPY;
- SCHD relative to SPY.

The target is an equal-weight return proxy for the configured private symbols.
The result is explicitly a factor-calibration reference, not a reconstruction
of the broker account and not a target portfolio.

## Daily report boundary

Global research may refresh every day. IBKR Flex is a separate reconciliation
source and may refresh less frequently or on demand. A stale broker snapshot
must be labelled stale and blocks active ADD/OPEN; it does not prevent the
system from publishing new policy, global-market, source-health and factor
research.

The private deployment should render these modules inside the same owner-only
report rather than generating a second visible report. A GitHub Issue can serve
as the private fallback receiver and email notification surface, while the full
body remains available to the connected GPT/GitHub reader.
