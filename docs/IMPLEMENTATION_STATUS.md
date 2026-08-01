# Verified Implementation Status

## Why this note exists

This note separates implemented, tested source code from the larger Pro
roadmap. Documentation and design proposals are not treated as deployed code,
and missing modules are never represented as tested or production-ready.

## Verified base

The public framework starts from a privacy-audited, history-free source
snapshot. Its verified baseline contains the auditable v2.2 pipeline, KOL
credibility and manipulation gates, SEC/X/Reddit collectors, authorized-file
Xiaohongshu context, DCA review and portfolio constraints. No private source
repository commit or branch is part of this provenance.

## Implemented and verified in this baseline

- the complete `evaluate-us-funds` Skill and its quantitative self-test;
- an offline deterministic fund monitor linked from the Skill, with separate
  product-quality/portfolio-fit states, native cadence freshness, structured
  event acknowledgements, explicit `NOT_DUE`, and immutable no-trade gates;
- authorized-file `china_retail_attention` research overlay;
- explicit deduplication, freshness, engagement and manipulation controls;
- objective volatility, credit and breadth confirmation groups;
- HXC and USD/CNH context for China/ADR themes;
- downside-only risk-budget integration with mock-data isolation;
- a separate accepted-close provider registry for raw Twelve Data and Alpha
  Vantage daily observations, with exact-session identity gates, independent
  source consensus, deterministic audit IDs and atomic batch price gating;
  Twelve Data uses the documented exact start/end date range and structurally
  rejected observations cannot appear as healthy source rows;
- pinned U.S. exchange-session resolution with DST, holiday and early-close
  handling while preserving each instrument's MIC identity;
- an offline append-only SQLite ledger with confirmed and modeled books,
  owner-confirmed events, atomic base-DCA settlement, exact Decimal accounting,
  accepted-close valuation and time-weighted returns;
- a versioned private daily-report JSON schema with deterministic Decimal and
  identity rules, multi-session/backfill state, truthful blocked/no-new-close
  semantics and separate confirmed/modeled/DCA layers;
- a deterministic Chinese Markdown view plus a local immutable SQLite outbox
  that fails closed unless a delivery adapter supports receiver lookup or a
  stable idempotency key;
- a private daily prepare runtime that recovers from the last delivered ledger
  checkpoint, performs oldest-first settlement, stops after the first failed
  gate, persists content-addressed JSON/Markdown and enqueues one receiver/day
  slot without any broker or order capability;
- strict environment-only production entrypoints, external non-cloud/non-Git
  storage gates, same-handle config reads, POSIX ownership/modes and protected
  Windows owner-only ACL verification;
- private-runtime JSON audit artifacts and focused regression tests;
- a non-mutating, redacted private-daily activation audit with whole-database
  ledger/outbox verification, current receiver scoping, explicit operational
  states and a separate recurring-workflow activation decision;
- a one-time interactive opening-owner attestation with exact config-byte and
  stable opening-identity binding, a 30-minute claim/commit window, durable
  intent, explicit re-confirmation after expiry, receipt recovery, read-only
  readiness states and fail-closed replay/rollback detection;
- owner-confirmed manual-event staging through a fixed owner-only JSON request,
  exact-byte TTY approval, immutable approval/receipt controls, full outbox and
  ledger-head gates, idempotent crash recovery, pre-DCA skips/events and strict
  settle-then-replace-before-valuation handling without broker connectivity;
- an offline deterministic Social Heat model with authorization/source-health
  gates, separate attention and candidate execution-score weights, provisional
  cross-platform priors, 30-day baselines, manipulation quarantine and a hard
  5% combined score cap; Xiaohongshu execution weight remains zero;
- a private append-only prediction research ledger with 1/5/20/60-session
  accepted-close settlement, raw/factor-residual outcomes, MFE/MAE, Brier,
  grouped Rank IC, explicit reversals and automatic research-only/decay/
  quarantine states;
- a pure private-report research adapter that accepts only aggregate fund,
  Social Heat and prediction-weight-state outputs, validates all no-trade flags
  before ledger work, conservatively applies calibration to social candidate
  scores and preserves structured fund quality/fit, cadence, coverage and event
  keys;
- an atomic, self-hashed, owner-only sanitized research snapshot transport with
  a no-argument publisher, shared pre-ledger v1.1 semantic validation,
  monotonic replacement and stale runtime downgrades; the normal private daily
  entrypoint loads it when present while leaving DCA, accounting actions and
  the manual-trade prompt unchanged;
- this status record, so later work can distinguish code from proposals.

## Still roadmap, not current implementation

- an automated corporate-action source and reconciliation workflow (the ledger
  already enforces an explicit per-symbol reconciliation gate);
- integration of the legacy research council and objective-risk layer into the
  new ledger-backed daily runtime; the fund/Social Heat aggregate projection is
  implemented and owner-only snapshot transport is connected, while production
  evidence collection adapters are still missing;
- authorized production social-data adapters and persistent private Social Heat
  observation history (the latest aggregate transport is implemented, but the
  model itself performs no collection);
- automatic scheduling and settlement of prediction-ledger horizons from the
  private daily runtime, plus durable raw topic evidence; the daily report now
  carries structured aggregate prediction weight states only;
- durable fund-monitor cadence state and exact event acknowledgements after the
  corresponding report/research record is committed;
- a verified GPT receiver adapter and recurring private delivery deployment;
- persisted live end-to-end activation evidence for the private daily readiness
  contract;
- Trump Policy Transmission Index and White House event lifecycle;
- point-in-time Polymarket event settlement studies;
- VIX1D/VIX9D/VVIX/SKEW and options-chain ingestion;
- overnight futures and premarket anomaly models;
- Barra-inspired exposure/covariance decomposition;
- Kalman-filtered dynamic beta;
- manager-skill bootstrap and timing models;
- Brinson/Carino attribution and asset-allocation optimizer;

Each roadmap item should be implemented as a separate auditable module with
point-in-time fixtures and focused tests. It must not be marked complete from a
document description alone.

The accepted-close registry, calendar, private ledger and daily-report/outbox
are connected by a separate private prepare runtime rather than legacy
`run_report.py`. A local verified GPT receiver adapter is not yet enabled by
this repository. None of these
modules connects to a broker or submits an order.
