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

private local runtime (outside Git and cloud sync)
├── private_daily_runtime.private.yaml
├── portfolio-ledger.sqlite3
├── daily-outbox.sqlite3
└── reports/
```

The production daily runtime requires these files to be outside every Git
worktree and common cloud-sync folder, even if a path is ignored. It rejects
network/removable drives, links, junctions, reparse points and hard-linked
files. POSIX ownership/mode and Windows protected owner-only ACLs are verified
before use. The synthetic example is test-only and the production entrypoints
reject it.

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

## Private daily-report contract

`schemas/private_daily_report.v1.schema.json` is the stable owner-only JSON
contract for one daily report. It separates completed, blocked and no-new-close
runs; multi-session backfill; confirmed versus modeled books; and configured,
proposed, modeled and broker-confirmed DCA states. Decimal strings, UTC
timestamps, calendar/ledger watermarks and report identities are validated
fail-closed.

`serenity_monitor/private_daily_markdown.py` produces the deterministic Chinese
view without recalculating accounting. `serenity_monitor/daily_outbox.py`
stores one immutable private report per receiver/day and refuses a delivery
adapter that has neither idempotency-key nor receiver-lookup support. It does
not send a message itself.

`serenity_monitor/private_daily_runtime.py` wires the calendar, accepted
closes, corporate-action attestations, immutable DCA plan, dual-book ledger,
report contract, content-addressed local files and outbox. It replays from the
last successfully delivered report checkpoint, recovers partial/idempotent
sessions oldest first and stops later sessions after the first failed gate.
No broker API, order endpoint or automatic position change is present. See
[Private Daily Report Contract](docs/PRIVATE_DAILY_REPORT.md).

## Social research boundary

X, Reddit and Xiaohongshu belong to one `social_media` evidence group. The
authorized Xiaohongshu path accepts only a user-owned export, authorized API
export or licensed dataset with an explicit rights attestation. It performs no
login bypass, cookie extraction, API reversal or anti-bot workaround.

`serenity_monitor/social_heat.py` provides the deterministic offline aggregation
layer. It calculates breadth, entropy, independent-content counts, relative
30-day heat, log engagement, sentiment disagreement, concentration, overlap,
decay and manipulation quarantine. Platform priors start at Xiaohongshu 40%, X
35%, Reddit 15% and other authorized sources 10%, with healthy-source
re-normalization. Attention and candidate execution-score weights are separate:
Xiaohongshu's execution weight is hard-coded to zero, and all social sources
together are capped at 5% of the model score.

All social output remains research-only and cannot independently trigger
`OPEN`, `ADD`, `TRIM`, `EXIT` or an increased DCA. The module accepts controlled
topic taxonomy IDs from a closed built-in list and irreversible identifiers,
not raw posts, handles or URLs. Runtime policy may narrow the list but cannot
add private labels. Public output never persists record-level social hashes,
timestamps or engagement data. The owner-only runtime can now project one
sanitized aggregate row per available platform into the private report; it
does not collect production data, persist topic-level records or turn the
result into an action.

## Prediction research ledger

`serenity_monitor/prediction_ledger.py` is a separate private/local SQLite event
ledger for settling sanitized signals at 1, 5, 20 and 60 trading-session
horizons. It uses immutable events and explicit reversals, accepted-close
lineage and Decimal-only calculations for raw/factor-residual returns, hit,
MFE, MAE, Brier calibration and grouped Rank IC. Rolling results yield
`active`, `decayed`, `quarantined` or `research_only`; none of those states
permits an automatic trade. See [Prediction Ledger](docs/PREDICTION_LEDGER.md).

## Fund research Skill

`skills/evaluate-us-funds/` contains the institutional fund/product research
workflow. It assesses legal/economic structure, true style, manager tenure,
repeatable return sources, investor constraints, portfolio fit, implementation
and current timing. It returns `PASS`, `WATCH`, `REJECT` or `NEED_INFO`; it is
not an order-entry module.

`serenity_monitor/fund_monitor.py` adds the offline scheduled/event monitoring
boundary used by that Skill. It keeps product quality separate from portfolio
fit, applies fixed point-in-time freshness cutoffs, preserves explicit unknowns,
uses fund-scoped material-event acknowledgements and returns `NOT_DUE` when no
complete review is due. Social evidence can open a question but cannot close a
required category. Every trade, order, position and DCA capability remains
hard-coded off. A pure aggregate adapter now carries the fund status, controlled
reason codes and the separate product-quality/portfolio-fit states into the
private report summary. Production evidence collection remains owner-supplied
and is not enabled by the public framework.

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

## Prepare the private daily accounting runtime

Use `config/private_daily_runtime.example.yaml` only as a schema reference.
Create an owner-only file named `*.private.yaml` in a fixed local directory
that is outside Git and cloud-sync software. Set its runtime classification to:

```yaml
runtime:
  data_classification: private
  allow_live_report: true
  example_only: false
  execution_mode: modeled_manual_only
```

The following environment variables are fixed by the contract; their values
must never be embedded in YAML or command-line arguments:

```text
SERENITY_PRIVATE_CONFIG
SERENITY_PRIVATE_ROOT
CODEX_DAILY_TARGET_KEY
TWELVE_DATA_API_KEY
ALPHA_VANTAGE_API_KEY
```

Audit the activation boundary before any mutating command:

```bash
python scripts/check_private_daily_readiness.py
```

This command performs no network request, creates no runtime file and prints
one redacted JSON object. Its `operational_state` and `next_safe_action`
separate initialization, report preparation, pending delivery, reconciliation
and an already-complete local day. Exit code `0` is reserved for
`workflow_activation_allowed=true`; a valid but blocked audit exits `2`.
Prepared/retryable delivery is intentionally independent of market-data and
ledger readiness. See `docs/PRIVATE_DAILY_ACTIVATION.md` for the fixed contract
and the gates that still prevent recurring activation.

After reviewing the owner-only opening snapshot, create its one-time interactive
claim from a real terminal. Pipes, redirected input and unattended flags are
rejected:

```bash
python scripts/attest_private_opening.py
```

The claim contains hashes and timestamps only, remains private, and expires in
30 minutes. A durable intent can resume an interrupted commit only inside that
same window; afterward the attestation command requires a new terminal
confirmation before initialization can continue. Run the read-only readiness
command again; when it reports
`operational_state=needs_initialization`, initialize the opening snapshot and
both opening valuations explicitly once:

```bash
python scripts/initialize_private_daily.py
```

Then prepare one private report/outbox item after the official close:

```bash
python scripts/run_private_daily.py
```

All three commands accept no CLI arguments. Normal stdout is empty; failures
emit only a fixed error code, never a path, target, holding, amount, credential
or traceback. The attestation command prints only a random challenge, fixed
instructions and a fixed success marker to its terminal. The daily command
does not create owner-confirmed fills: if the owner reports no trade, the
ledger records no manual event and only the fixed base DCA plan is modeled at
accepted closes.

The opening initializer is deliberately not source-compatible with the older
unattested helper call: its internal Python API now requires the validated
runtime paths, exact configuration-byte digest and an aware clock. Operators
should use the guarded script rather than call that internal function.

`serenity_monitor/private_research_adapter.py` is the no-I/O bridge for
already-computed `FundMonitorResult` and `SocialHeatResult` aggregates. The
runtime validates this input before any ledger mutation. It exposes only
controlled aggregate rows and optional source-health status; it cannot modify
the ledger, configured DCA, report actions or manual-trade prompt. The current
v1.0 report intentionally omits prediction-ledger and social-topic detail until
a new versioned report contract is introduced.

## Public CI

`.github/workflows/public-framework-ci.yml` is intentionally a public-framework CI
workflow only. It compiles, runs the privacy scanner and tests, then produces a
synthetic offline smoke report in the runner's temporary directory. It has:

- no schedule;
- no live credentials or network report;
- no Actions Summary containing a report;
- no artifact upload/download;
- no report commit or write permission.

The private prepare runtime is implemented separately from legacy
`run_report.py`. A verified GPT receiver adapter and recurring delivery job are
still not enabled; the existing task must remain paused until receiver lookup
or stable idempotency semantics are proven.

## Main files

```text
config/portfolio.example.yaml           fictional public configuration
scripts/check_public_privacy.py          tracked-tree privacy gate
serenity_monitor/data.py                 market-data providers
serenity_monitor/provider_registry.py    accepted-close price validation
serenity_monitor/trading_calendar.py     exchange-session completion
serenity_monitor/portfolio_ledger.py     private manual/DCA accounting
schemas/private_daily_report.v1.schema.json owner-only report contract
serenity_monitor/private_daily_report.py report validation and identities
serenity_monitor/private_daily_markdown.py deterministic private Markdown
serenity_monitor/daily_outbox.py          private delivery state machine
serenity_monitor/private_daily_runtime.py private close/DCA/report orchestration
serenity_monitor/private_runtime_config.py strict private runtime configuration
serenity_monitor/private_runtime_paths.py external storage and privacy gates
serenity_monitor/private_windows_security.py Windows owner-only ACL boundary
serenity_monitor/private_runtime_cli.py    silent production entrypoints
serenity_monitor/external_views.py       source collection and health
serenity_monitor/credibility.py          source/claim/copy-trade scoring
serenity_monitor/evidence.py             evidence and independence gates
serenity_monitor/objective_signals.py    downside-only risk overlay
serenity_monitor/china_retail_attention.py authorized social research
serenity_monitor/social_heat.py           offline cross-platform Social Heat
serenity_monitor/prediction_ledger.py      private signal outcome/calibration ledger
serenity_monitor/fund_monitor.py           offline fund cadence/event monitor
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
