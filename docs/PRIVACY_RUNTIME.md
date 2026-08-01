# Public Framework and Private Runtime

## Classification

Every runtime configuration must declare one of two classifications:

```yaml
runtime:
  data_classification: synthetic_example
  allow_live_report: false
```

or:

```yaml
runtime:
  data_classification: private
  allow_live_report: true
```

`synthetic_example` is accepted only with `--mock --no-external`. A private
configuration inside the repository must use a `.private.yaml` name and be
matched by `.gitignore`. Private report output inside the repository must also
be ignored. Missing or ambiguous classification fails before data collection.
The source-profile, manual-KOL and authorized Xiaohongshu inputs are checked at
their resolved paths before reading; Git-tracked files and symlinks to tracked
files are rejected.

## Data placement

Tracked public files may contain framework logic, generic source templates and
fixed, clearly labelled synthetic fixtures. Outside those synthetic fixtures,
they may not contain account value, cash, buying power, positions, share
counts, cost basis, P/L, tax lots, broker exports, user-authorized social
records, prediction ledgers or private reports.

Recommended ignored paths:

```text
config/*.private.yaml
config/xiaohongshu_authorized.csv
private/
```

An external encrypted directory is also valid. Environment variables or a
secret manager should hold credentials; configuration files should reference
variable names rather than embed tokens.

## Public CI

The public workflow is deliberately limited to compilation, privacy scanning,
tests and a deterministic synthetic smoke report written to the runner's
temporary directory. It has no schedule, live source credentials, report body
in stdout, Actions Summary, artifact upload or report persistence job.
Mock providers are accepted only in explicit `--mock --no-external` mode;
their reports are labelled simulation-only and they do not read or write live
state.

## Private delivery roadmap

A daily private report should run in a local or otherwise access-controlled
environment. It should reconstruct the modeled portfolio from a private,
append-only manual-trade and DCA ledger, write only to private storage, and send
one user-visible post-close report. The framework never connects to a broker or
claims that modeled DCA entries are broker-confirmed executions.

## Repository provenance

A public deployment must begin from a history-free export of a privacy-audited
tree. Deleting sensitive files from an older repository does not remove them
from Git history, clones, caches or pull-request references; that operational
repository must remain private. This source policy does not authorize history
rewrites or force-pushes.
