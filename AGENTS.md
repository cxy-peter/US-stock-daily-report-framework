# Repository Operating Rules

## Architecture

The executable path is:

```text
portfolio + market data
-> SEC/news/X/Reddit/public-search collection
-> source and claim credibility scoring
-> evidence aggregation
-> deterministic research council
-> portfolio and risk-group sizing
-> recurring-investment review
-> private Markdown/CSV/JSON audit artifacts
```

Collectors may add evidence but may not emit portfolio actions. Research rules
may emit a candidate action but may not bypass portfolio capacity, cash, turnover
or risk-group limits. The report renderer does not change decisions.

## Hard Gates

- One KOL, one social account, or multiple accounts in one independence group
  cannot support ADD, OPEN or EXIT.
- ADD/OPEN require a primary source, at least two independent evidence groups,
  minimum evidence coverage, acceptable manipulation/crowding risk, and
  available portfolio/risk-group capacity.
- EXIT requires an explicit failed thesis check. External opinion can trigger
  REVIEW, not an automatic exit.
- High manager leverage, concentration, liquidity days or prime-broker
  concentration must set `copy_trade_allowed=false`.
- Tracking positions are not mechanically rebalanced because they are small or
  have short-term gains/losses.
- Missing X credentials are `blocked`; failed SEC calls are `error` or `partial`.
  Neither condition may be reported as an empty successful search.
- Recurring-investment changes are proposals for manual review only.

## Testing

Before committing:

```bash
python -m compileall -q .
python scripts/check_public_privacy.py
pytest -q
python run_report.py --mock --no-external \
  --config config/portfolio.example.yaml \
  --out-dir out_public_smoke \
  --date 2026-01-02
```

Inspect both Markdown and CSV outputs. Any collector, decision rule, risk gate,
security identity, or report-field change requires a focused test.

## Execution Boundary

This repository must not contain broker credentials, order-entry code or
automatic execution. It may calculate model and executable quantities for
audit, but a person must reconcile live positions, cash, taxes and costs before
making any external change.

Tracked files must contain synthetic examples only. Real portfolio, source,
state and report data belong in ignored `.private.yaml` or `private/` paths.
Public CI must stay mock/offline and may not publish a report body, Summary,
artifact or reports commit.

## Public Release Provenance

- A public release must start from a privacy-audited export with fresh Git
  history. Do not publish an operational repository merely because its current
  tree is clean.
- Never add a private operational repository as a remote of the public
  framework, and never fork, import, mirror or copy its branches, tags, commit
  identifiers, reflogs or unreachable objects into the public repository.
- The public root commit may contain only reviewed framework files and
  synthetic examples. Private runtime data must be supplied after cloning via
  ignored paths.
