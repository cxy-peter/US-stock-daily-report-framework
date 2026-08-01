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

`synthetic_example` remains test-only. A live daily configuration must use a
`.private.yaml` name and live outside Git worktrees and common cloud-sync
folders; `.gitignore` alone is not a security boundary. Missing or ambiguous
classification fails before data collection.
The source-profile, manual-KOL and authorized Xiaohongshu inputs are checked at
their resolved paths before reading; Git-tracked files and symlinks to tracked
files are rejected.

## Data placement

Tracked public files may contain framework logic, generic source templates and
fixed, clearly labelled synthetic fixtures. Outside those synthetic fixtures,
they may not contain account value, cash, buying power, positions, share
counts, cost basis, P/L, tax lots, broker exports, user-authorized social
records, prediction ledgers or private reports.

Legacy research inputs may still use ignored paths:

```text
config/*.private.yaml
config/xiaohongshu_authorized.csv
private/
```

The daily ledger, outbox, reports and live daily configuration require a fixed
local owner-only directory outside Git and cloud sync. Network/removable
drives, symlinks, junctions, reparse points and hard links are rejected.
Windows uses a protected DACL containing only the current user's full-control
ACE; POSIX requires current ownership and mode `0700`/`0600`. Credentials and
the GPT target remain environment-only.

## Public CI

The public workflow is deliberately limited to compilation, privacy scanning,
tests and a deterministic synthetic smoke report written to the runner's
temporary directory. It has no schedule, live source credentials, report body
in stdout, Actions Summary, artifact upload or report persistence job.
Mock providers are accepted only in explicit `--mock --no-external` mode;
their reports are labelled simulation-only and they do not read or write live
state.

## Implemented private prepare boundary

The private prepare runtime reconstructs confirmed and modeled books from the
append-only ledger, settles only the immutable base DCA plan at accepted
official closes, creates one report slot per receiver/day and writes only to
owner-only storage. User silence means no new owner-confirmed event; it does
not infer a broker trade. The framework never connects to a broker or claims
that modeled DCA entries are broker-confirmed executions.

GPT transmission remains a separate deployment boundary. The recurring task
must stay paused until the receiver exposes a verified stable idempotency key
or delivery lookup; an ambiguous send is never blindly retried.

## Repository provenance

A public deployment must begin from a history-free export of a privacy-audited
tree. Deleting sensitive files from an older repository does not remove them
from Git history, clones, caches or pull-request references; that operational
repository must remain private. This source policy does not authorize history
rewrites or force-pushes.
