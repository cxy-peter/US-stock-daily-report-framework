# US Stock Daily Research

A personal U.S. stock research system that turns portfolio facts, market data,
global events and factor tests into **one Chinese daily report**. It is designed
to answer three questions clearly:

1. What should be held, added, reduced or paused today?
2. What evidence supports that conclusion, and what would invalidate it?
3. How much of the result survives fees, costs and out-of-sample testing?

The framework has no broker order endpoint. Execution remains manual.

## What the daily report shows

```text
1. 今日结论
2. 加减仓说明
3. 当前持仓状态
4. 费用与损耗
5. 今日核心论点及反证条件
6. 1/5/20 日因子有效性
7. 全球事件与跨资产传导
8. 数据与测试状态（折叠）
```

Missing data is shown as `UNKNOWN`, `blocked`, `degraded` or `stale`; it is not
silently treated as neutral.

## Main capabilities

### Portfolio and broker facts

- read-only IBKR Flex account, position, cash, P&L, dividend, withholding and fee parsing;
- separate confirmed and modeled books;
- accepted-close validation from independent price sources;
- corporate-action and owner-confirmation checks;
- no automatic order or ledger mutation.

### Daily market research

- SEC, official company sources, White House/public policy sources and RSS;
- Al Jazeera oil/geopolitics context;
- SK hynix primary releases and Korean semiconductor media;
- Reddit as one correlated community-sentiment group;
- Quora/search snippets as zero-direct-weight discovery context;
- Polymarket as a liquidity/calibration-weighted research signal;
- VIX term structure, option-tail, overnight and premarket risk.

### Factor research

- market, momentum, volatility, breadth, semiconductor, memory, energy, rates,
  gold and defensive relative-strength proxies;
- purged and embargoed 1/5/20-session walk-forward tests;
- training-only scaling and future-only targets;
- turnover and transaction-cost deduction;
- multiple-testing control and factor quarantine;
- cross-horizon `active / watch / quarantined / blocked` states;
- version isolation so changed factor definitions never reuse old calibration.

See [`docs/FACTOR_RESEARCH_METHOD.md`](docs/FACTOR_RESEARCH_METHOD.md).

### Risk and attribution

- Barra-inspired public factor/covariance proxy;
- Kalman dynamic exposure;
- Brinson-Fachler and Carino attribution;
- constrained allocation proposals with turnover and group caps;
- manager skill/fragility and fund-product research;
- all outputs are proposals or diagnostics, not orders.

## Data flow

```text
portfolio/broker snapshot
+ point-in-time market and source data
+ global event transmission
+ purged OOS factor tests
+ portfolio constraints and costs
-> explicit action candidates
-> direct theses and invalidation conditions
-> one private report
-> owner decision and manual trade
```

A single headline, KOL, community consensus, prediction market, factor or
optimizer cannot independently create a trade.

## Repository map

```text
serenity_monitor/
  external_views.py                 public-source collection
  global_market_narratives.py       event and cross-asset transmission
  factor_backtest.py                purged OOS regression and admission
  institutional_factor_research.py  multi-horizon factor research
  daily_research_enrichment.py      thesis-first daily research
  ibkr_flex*.py                     read-only broker parsing/reconciliation
  pro_research/                     factor risk, policy and manager models

tests/                              point-in-time and regression tests
config/                             synthetic examples and factor registry
examples/                           synthetic report examples
docs/                               user, method and control references
```

The public repository contains code, tests and synthetic examples only. Real
holdings, credentials and reports belong in the separate private deployment.

## Run the synthetic checks

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

## Documentation

Start with [`docs/README.md`](docs/README.md). Detailed privacy, accounting and
activation controls remain available for maintenance, but they are deliberately
kept out of the main user flow.
