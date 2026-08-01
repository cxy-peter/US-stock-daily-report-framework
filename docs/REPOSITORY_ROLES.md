# Repository Roles

## Canonical framework

`cxy-peter/US-stock-daily-report-framework` is the canonical public source.
Feature development, tests, methodology and synthetic examples belong here.

## Owner deployment

`cxy-peter/US-stock-daily-report` has historical operational references and
must remain private. It should not continue as a full manually synchronized
code mirror.

Target simplification:

```text
framework repository
-> tagged/package release

private deployment repository
-> pinned framework version
-> deployment scripts and receiver adapter only

owner-only local runtime outside Git
-> configuration
-> ledger/outbox
-> accepted-close cache
-> IBKR reconciliation
-> private reports
```

Until packaging and migration are complete, code synchronization may continue,
but the framework repository remains the source of truth. Every sync PR must
state the source commit and prove tree or patch equivalence.
