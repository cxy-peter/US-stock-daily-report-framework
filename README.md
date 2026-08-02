# US Stock Daily Research

A personal U.S. stock research system that turns broker facts, market data,
policy, prediction markets, fund/financial-company news, verified external
views and factor tests into **one Chinese 08:30 local-time decision report**.
It answers four questions:

1. What should be held, added, trimmed or blocked for the next session?
2. What changed from the previous comparable thesis?
3. What is the market consensus, what is the variant view, and what would
   invalidate it?
4. Does the action still make sense after security-specific transaction
   economics?

The framework has no broker order endpoint. Execution remains manual.

## What the daily report shows

```text
1. 今日结论
2. 下一交易日加减仓
3. 当前持仓状态
4. 买方研究结论
5. 高级模型、因子与风险预算
6. 基金公司、金融机构与经复核市场观点
7. 单票调仓成本（仅当存在 ADD_REVIEW / TRIM_REVIEW）
8. 数据源、测试与运行边界（折叠；无调仓时编号为 7）
```

There is no routine portfolio-wide fee section. Historical fees remain private
broker facts; commission, spread/slippage, holding friction and tax-lot
implications are estimated only for an actual add/trim candidate. Missing costs
remain `UNKNOWN` rather than zero.

## Main capabilities

### Portfolio and broker facts

- read-only IBKR Flex account, position, cash, P&L, dividend, withholding and
  historical-fee parsing;
- completed-XNYS-session snapshot freshness;
- separate confirmed and modeled books;
- accepted-close validation from independent price sources;
- corporate-action and owner-confirmation gates;
- no automatic order or ledger mutation.

### Buy-side thesis engine

Every material thesis includes:

- conclusion and action implication;
- change from yesterday's comparable versioned score;
- consensus and variant perception;
- evidence chain and source hierarchy;
- catalysts;
- horizon;
- invalidation/exit conditions;
- confidence and affected assets.

See [`docs/BUY_SIDE_DAILY_REPORT_STYLE.md`](docs/BUY_SIDE_DAILY_REPORT_STYLE.md).

### Daily market and policy research

- SEC, official company sources, White House/public policy sources and RSS;
- complete-sentence political claims and bounded Trump Policy Transmission
  Index rather than mention counts;
- live public Polymarket history, spread, liquidity, book/depth,
  time-to-resolution and calibration;
- private resolved-market event-study input;
- Al Jazeera oil/geopolitics, SK hynix primary releases and Korean media;
- dedicated asset-manager, fund-sponsor, bank, broker and exchange news;
- VIX1D/VIX9D/VIX/VIX3M/VIX6M, VVIX and SKEW as one correlated downside group;
- optional point-in-time option-chain and overnight/premarket inputs.

### External views and agent fallback

- one private inbox for Reddit, Quora, Xiaohongshu exports, broker-research
  digests and external financial/news agents;
- original URLs and first-observed timestamps;
- material-claim verification by a primary source or two independent
  institutional source groups;
- agent-generated prose is secondary synthesis, never original evidence;
- Xiaohongshu/Reddit/Quora/social and agent summaries have zero direct
  `ADD/OPEN` weight;
- verified bearish crowding may only tighten risk, capped at 5%;
- missing community access is explicit, never neutral.

See [`docs/EXTERNAL_AGENT_AND_SOCIAL_FALLBACKS.md`](docs/EXTERNAL_AGENT_AND_SOCIAL_FALLBACKS.md).

### Factor, exposure and manager research

- market, momentum, volatility, breadth, semiconductor, memory, energy, rates,
  gold and defensive relative-strength proxies;
- purged and embargoed 1/5/20-session walk-forward tests;
- training-only scaling, future-only targets, turnover/cost deduction, FDR,
  Probabilistic Sharpe and factor quarantine;
- Barra-inspired public covariance/risk proxy and Kalman dynamic exposures;
- manager/strategy alpha, bootstrap, Treynor-Mazuy and Henriksson-Merton timing,
  up/down capture, rolling persistence and fragility;
- manager-change attribution and no automatic copy-trade.

See [`docs/FACTOR_RESEARCH_METHOD.md`](docs/FACTOR_RESEARCH_METHOD.md) and the
institutional [`skills/evaluate-us-funds/SKILL.md`](skills/evaluate-us-funds/SKILL.md).

## Data flow

```text
read-only broker snapshot
+ point-in-time prices/factors
+ official policy claims -> TPTI
+ live/resolved Polymarket
+ VIX/option/overnight risk
+ Barra/Kalman/manager research
+ fund and financial-company news
+ private social/agent/research inbox
-> provenance and independence gates
-> buy-side theses and risk budget
-> explicit action candidates
-> conditional single-name transaction economics
-> one private report
-> owner decision and manual trade
```

A single headline, KOL, social consensus, agent, political statement,
Polymarket market, factor, manager score or optimizer cannot independently
create a trade.

## Context and requirement persistence

Future Codex/agent changes must read:

1. [`requirements/DAILY_RESEARCH_REQUIREMENTS.yaml`](requirements/DAILY_RESEARCH_REQUIREMENTS.yaml);
2. [`PROJECT_CONTRACT.yaml`](PROJECT_CONTRACT.yaml);
3. [`docs/PRODUCTION_INTEGRATION_AUDIT_V4.md`](docs/PRODUCTION_INTEGRATION_AUDIT_V4.md);
4. affected code/tests and the private deployment.

The [`maintain-daily-research-context`](skills/maintain-daily-research-context/SKILL.md)
Skill distinguishes **library**, **test**, **live data** and **daily-report
integration** so a class or design document cannot be mislabeled as production
complete.

## Repository map

```text
serenity_monitor/
  daily_advanced_research.py         production bridge and buy-side theses
  research_opinion_inbox.py          social/agent provenance and verification
  political_collectors.py            official policy collection
  political_communications.py        complete-sentence claim model
  polymarket_live.py                  public live prediction-market research
  advanced_market_risk.py             VIX, option tail and overnight risk
  external_views.py                   public-source/news collection
  global_market_narratives.py         event and cross-asset transmission
  factor_backtest.py                  purged OOS regression and admission
  institutional_factor_research.py    multi-horizon factor research
  pro_research/                        TPTI, Barra, Kalman, manager and studies
  ibkr_flex*.py                        read-only broker parsing/reconciliation

requirements/                         persistent machine-readable requirements
skills/                               context and fund-research workflows
tests/                                point-in-time and regression tests
examples/                             synthetic inputs and report examples
docs/                                 user, method and production-boundary notes
```

The public repository contains code, tests and synthetic examples only. Real
holdings, credentials, raw social exports, paid research and reports belong in
the separate private deployment or an owner-local path.

## Run the checks

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python -m compileall -q .
python scripts/check_public_privacy.py
python scripts/check_project_contract.py
python scripts/check_requirement_ledger.py
pytest -q
```

## Documentation

Start with [`docs/README.md`](docs/README.md). Detailed accounting, privacy and
maintenance controls remain available but are deliberately kept out of the
user-facing conclusion.
