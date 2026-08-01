# US Stock Daily Investment Research Agent v2.3

An auditable U.S. investment-research framework with deterministic market
analysis, primary-source collection, KOL credibility gates, portfolio-capacity
checks, recurring-investment review, accepted-close validation, manual-event
accounting and Markdown/CSV/JSON output.

The public repository contains framework code and synthetic examples only. It
does not contain the repository owner's account value, holdings, share counts,
cash, buying power, cost basis, tax lots, broker credentials or private reports.
The system has no broker order endpoint.

> **Implementation boundary:** the verified code is the v2.3 research baseline
> plus the modules explicitly listed as implemented; remaining Pro modules are
> roadmap items. See
> [Verified Implementation Status](docs/IMPLEMENTATION_STATUS.md).

## Public/private boundary

```text
public repository
├── framework source and tests
├── config/portfolio.example.yaml       synthetic fixture
├── config/*_views.example.yaml         empty/generic templates
├── config/source_profiles.example.yaml generic source templates
└── public CI                           mock + offline only

private local runtime (gitignored)
├── config/portfolio.private.yaml
├── config/manual_external_views.private.yaml
├── config/source_profiles.private.yaml
├── config/strategy-profile.private.yaml
├── config/xiaohongshu_authorized.csv
└── private/reports/
```

Private files are ignored by Git. Live execution fails closed unless the
configuration declares `runtime.data_classification: private`, opts in to live
reporting and uses an ignored private path. The synthetic public example is
accepted only with `--mock --no-external`.

This framework is publishable only from a history-free, privacy-audited root.
Any operational repository that previously contained private runtime data must
remain private; copying or deleting files does not sanitize its Git history.

## Decision path

```text
private portfolio + market data
-> SEC/news/X/Reddit/authorized-social collectors
-> source, claim, fragility and manipulation scoring
-> evidence and independence gates
-> deterministic research council
-> objective market and portfolio-risk constraints
-> recurring-investment review
-> private Markdown/CSV/JSON audit output
```

ADD or OPEN requires primary evidence, at least two independent evidence
groups, sufficient coverage, acceptable manipulation risk and portfolio
capacity. A single KOL or social-media consensus cannot satisfy that gate.
Explicit failed thesis checks are required for an EXIT candidate.

## Objective market confirmation

The SPY regime is cross-checked by three provisional groups:

- volatility: VIX level and VIX/VIX3M term ratio (45%);
- credit proxy: HYG relative to LQD (30%);
- breadth: RSP and IWM relative to SPY (25%).

At least two healthy and two confirming groups are required. The overlay is
downside-only and may reduce the risk budget by at most 30%. Mock, stale and
future-dated inputs are excluded before scoring. HXC/USD-CNH and the explicitly
labelled KWEB fallback are China/ADR context only.

## Accepted-close boundary

Research/display quotes and settlement-grade closes are separate data paths.
`serenity_monitor/provider_registry.py` can collect raw daily observations from
Twelve Data and Alpha Vantage, validate exact session/security/currency/price
semantics, and require two independent sources before it emits an
`AcceptedClose`. It selects the validated primary value and never averages a
conflict.

Agreement at or below 30 bps passes the price gate. A 30--75 bps warning is
blocked from settlement by default, and a difference above 75 bps is blocked.
Single-source, adjusted, mock, snapshot, stale or wrong-session observations
remain display-only. This registry does not mutate holdings. Exchange-calendar
completion, corporate actions and the private atomic ledger are separate
downstream gates. See [Provider Registry](docs/PROVIDER_REGISTRY.md).

## Private manual ledger

`serenity_monitor/portfolio_ledger.py` provides an offline, append-only SQLite
ledger with separate `confirmed` and `modeled` books. Owner-reported fills,
cash, income, fees and splits enter both books. The modeled book additionally
posts the configured base DCA after the calendar, accepted-close,
corporate-action and funding gates all pass. Silence never creates a manual
trade, and no broker login or order method exists.

`serenity_monitor/trading_calendar.py` uses a pinned exchange calendar for DST,
holidays and early closes. One session can contain only one atomic modeled-DCA
batch, and repeated runs are content-checked for idempotency. Valuations require
current accepted closes for every non-zero position and calculate Decimal-only
P/L and time-weighted return. The database and derived output must stay in an
ignored private runtime. See [Manual Ledger](docs/MANUAL_LEDGER.md).

## Social research boundary

X, Reddit and Xiaohongshu belong to one `social_media` evidence group. The
authorized Xiaohongshu path accepts only a user-owned export, authorized API
export or licensed dataset with an explicit rights attestation. It performs no
login bypass, cookie extraction, API reversal or anti-bot workaround.

Current Xiaohongshu model-blending weight is zero. Social observations are
research-only and cannot independently trigger `OPEN`, `ADD`, `TRIM` or `EXIT`.
Public output never persists record-level social hashes, timestamps or
engagement data.

## Fund research Skill

`skills/evaluate-us-funds/` contains the institutional fund/product research
workflow. It assesses legal/economic structure, true style, manager tenure,
repeatable return sources, investor constraints, portfolio fit, implementation
and current timing. It returns `PASS`, `WATCH`, `REJECT` or `NEED_INFO`; it is
not an order-entry module.

## Run the synthetic public smoke test

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m compileall -q .
python scripts/check_public_privacy.py
pytest -q
python run_report.py --mock --no-external \
  --config config/portfolio.example.yaml \
  --out-dir out_public_smoke \
  --date 2026-01-02
```

All values and positions in `portfolio.example.yaml` are fictional.
Mock output is labelled `SIMULATION ONLY` and never loads or writes the live
`state.json` path.

## Create a private local runtime

Copy, do not rename, the public templates and then enter private information
only in the ignored copies:

```powershell
Copy-Item config/portfolio.example.yaml config/portfolio.private.yaml
Copy-Item config/manual_external_views.example.yaml config/manual_external_views.private.yaml
Copy-Item config/source_profiles.example.yaml config/source_profiles.private.yaml
```

Set the private portfolio file to:

```yaml
runtime:
  data_classification: private
  allow_live_report: true
```

Then run locally with an ignored output directory:

```bash
python run_report.py \
  --config config/portfolio.private.yaml \
  --out-dir private/reports
```

The command writes files but does not print the report body to stdout. Review
`private/reports/latest.md` locally. Keep credentials in local environment
variables or a private secret store, never in YAML.

## Public CI

`.github/workflows/public-framework-ci.yml` is intentionally a public-framework CI
workflow only. It compiles, runs the privacy scanner and tests, then produces a
synthetic offline smoke report in the runner's temporary directory. It has:

- no schedule;
- no live credentials or network report;
- no Actions Summary containing a report;
- no artifact upload/download;
- no report commit or write permission.

A future private daily delivery runtime must still be deployed separately.
The accepted-close, calendar and manual-ledger contracts are implemented and
tested, but they are not yet wired into `run_report.py` or a recurring delivery
job in this release.

## Main files

```text
config/portfolio.example.yaml           fictional public configuration
scripts/check_public_privacy.py          tracked-tree privacy gate
serenity_monitor/data.py                 market-data providers
serenity_monitor/provider_registry.py    accepted-close price validation
serenity_monitor/trading_calendar.py     exchange-session completion
serenity_monitor/portfolio_ledger.py     private manual/DCA accounting
serenity_monitor/external_views.py       source collection and health
serenity_monitor/credibility.py          source/claim/copy-trade scoring
serenity_monitor/evidence.py             evidence and independence gates
serenity_monitor/objective_signals.py    downside-only risk overlay
serenity_monitor/china_retail_attention.py authorized social research
serenity_monitor/rules.py                deterministic research council
serenity_monitor/sizing.py               portfolio and risk-group sizing
serenity_monitor/dca_review.py            recurring-plan review
serenity_monitor/report.py                private report renderer
run_report.py                             fail-closed orchestration
skills/evaluate-us-funds/                 fund-research Skill
```

## Safety boundary

This is a research and discipline tool, not investment advice or a broker
connection. Before acting on any candidate change, reconcile actual positions,
cash, tax lots, spreads, costs, primary evidence and personal risk constraints
inside the private runtime.
