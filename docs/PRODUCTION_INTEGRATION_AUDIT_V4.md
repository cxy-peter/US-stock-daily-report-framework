# Production Integration Audit v4

## Decision

The earlier project generations implemented many advanced libraries, but the
live private daily report executed only global narrative scoring and 1/5/20-day
factor validation.  v4 closes that orchestration gap.  It does not describe a
class or test as “production-complete” unless current data reaches the one daily
report.

## State model

Every capability is evaluated through four separate states:

1. **Library** — typed model code exists.
2. **Test** — point-in-time and failure-path tests exist.
3. **Live input** — a production adapter supplies timestamped data.
4. **Daily report** — the model appears in and explains the one owner report.

`requirements/DAILY_RESEARCH_REQUIREMENTS.yaml` is the machine-readable source
of truth.  `skills/maintain-daily-research-context/SKILL.md` requires future
Codex/agent changes to update that ledger rather than relying on old PR prose.

## Reconciliation of early and later requirements

| Capability | Library before v4 | Daily input before v4 | v4 production path | Remaining boundary |
|---|---:|---:|---|---|
| Complete political-policy claims | Yes | No | White House public collector -> claim model -> report | X API requires owner credential; media never replaces original |
| Trump Policy Transmission Index | Yes | No | Validated claims/private events -> TPTI -> asset/risk overlay | No valid claim means `no_data/blocked`, not neutral |
| Live Polymarket | Yes | No | Gamma/CLOB discovery, history, spread, book and liquidity -> report | One correlated group; never an objective probability |
| Resolved Polymarket study | Yes | No | Private point-in-time event ledger -> 1/5/20/60 study | Requires persisted pre-resolution history |
| VIX/VVIX/SKEW surface | Yes | No | Current public close proxies or private snapshot -> one downside group | Missing VIX blocks calm inference |
| Option tail/gamma | Yes | No | Optional private point-in-time chain -> report | Requires reliable strike/quote coverage |
| Overnight/premarket anomaly | Yes | No | Optional private snapshot -> same 08:30 report | Never a second user-facing report |
| Barra-inspired risk proxy | Yes | No | Current research price history -> factor covariance and contributions | Public proxy, not commercial Barra |
| Kalman dynamic exposure | Yes | No | Current portfolio proxy returns -> dynamic exposures | Return-inferred, not disclosed holdings |
| Manager skill/fragility | Yes | No | Private manager/fund series or named active-fund proxy -> report | Named-manager attribution and fragility inputs still required |
| Reddit/Quora/Xiaohongshu | Partial | Reddit failed live | Private opinion inbox + public evidence re-verification | Direct `ADD/OPEN` weight remains zero |
| External news agents | No common contract | No | Provenance-gated private agent digest inbox | Agent prose is secondary synthesis |
| Fund/financial-company news | Limited ticker news | No dedicated view | asset managers, fund sponsors, banks, brokers and exchanges | Paid reports remain private inputs |
| Buy-side thesis structure | Shallow topic sentences | Partial | stance, change, variant view, evidence, catalysts, horizon and invalidation | Forecast remains conditional |
| Routine fee section | Yes | Yes | Removed in private v4 renderer | Costs shown only for actual add/trim candidates |
| Requirement/context persistence | Project contract only | Drift occurred | requirement ledger + maintenance Skill + CI check | Human/private facts still require owner updates |

## Daily orchestration

```text
broker snapshot and current holdings
+ public market and factor history
+ official policy claims -> TPTI
+ live and resolved Polymarket
+ VIX/option/overnight risk
+ Barra/Kalman exposures
+ manager/fund research
+ fund and financial-company news
+ private social/news-agent/broker-research inbox
-> independent-source and point-in-time gates
-> structured buy-side theses
-> conditional position actions
-> one private 08:30 local report
```

The base factor model remains the production action backbone.  Advanced models
may tighten risk or strengthen an explanation, but no political, social,
prediction-market, manager, volatility, allocation or agent result can place an
order.

## Social and agent fallback

When Reddit, Quora or Xiaohongshu cannot be fetched directly:

1. a user export, search result or external agent may supply a **claim lead**;
2. the item must include timestamp, platform/agent, claim, ticker/topic,
   direction, horizon, invalidation and original source URLs;
3. a primary source or two independent institutional source groups must
   corroborate the material claim;
4. otherwise it stays `unverified_lead` or `context_only_no_origin`;
5. all social/agent items have zero direct `ADD/OPEN` weight;
6. verified bearish crowding/manipulation may only reduce the risk budget.

## Manager and fund evaluation

Manager/fund research separates performance from repeatability and fragility:

- risk-adjusted alpha and factor exposures;
- bootstrap skill probability;
- Treynor-Mazuy and Henriksson-Merton timing;
- up/down capture;
- rolling-alpha persistence;
- tracking error and style consistency;
- leverage, concentration, liquidity, prime-broker concentration, tenure and
  product age;
- manager/team change and return-attribution boundaries.

A fund shell's historical record is not assigned to a new manager.  Missing
fragility inputs preserve a conservative `NEED_INFO/WATCH` outcome.

## Report depth

The report is no longer a list of headlines.  Each material thesis states:

- conclusion and action implication;
- change from the previous comparable score;
- market consensus and variant view;
- evidence chain with source hierarchy;
- near-term and medium-term catalysts;
- applicable horizon;
- falsification/exit conditions;
- confidence and affected assets.

## Remaining private-input requirements

The public framework deliberately does not contain:

- raw Xiaohongshu/Reddit/Quora exports;
- paid broker/FOF research documents;
- named-manager proprietary return histories and fragility data;
- private option-chain and overnight snapshots;
- real holdings, tax lots, reports or credentials.

Those inputs are accepted through private JSON/base64/local-path adapters and
must remain outside public Git history.
