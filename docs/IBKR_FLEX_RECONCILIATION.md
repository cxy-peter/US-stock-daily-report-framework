# Read-only IBKR Flex reconciliation

## Boundary

`serenity_monitor/ibkr_flex.py` requests a pre-configured Interactive Brokers
Flex Query, parses selected broker facts and compares them with the
owner-confirmed local ledger. It cannot place, modify or cancel an order and it
never silently mutates the confirmed book.

The live credentials remain environment-only:

```text
IBKR_FLEX_TOKEN
IBKR_FLEX_QUERY_ID
```

Flex Web Service v3 uses two requests:

1. `SendRequest` returns a reference code and an approved response URL;
2. `GetStatement` returns the generated XML statement.

The client validates HTTPS/host identity, sends a technology/version User-Agent,
normalizes failures, uses bounded polling and keeps the token out of returned
objects and `repr` output.

## Recommended Flex Query sections

Because Flex reports are configurable, the owner query should include, when
available:

- Account Information;
- Equity Summary in Base;
- Cash Report;
- Open Positions;
- Trades;
- Cash Transactions;
- Fees;
- Corporate Actions.

Missing fields remain `None` and produce `NEED_OWNER_CONFIRMATION`; they are not
converted to zero. Cost basis is parsed only from cost-basis fields, never from
realized-P/L fields.

## Reconciliation output

The adapter can emit:

- `MATCHED`;
- `MISSING_LOCAL_EVENT`;
- `UNEXPECTED_BROKER_EVENT`;
- `QUANTITY_MISMATCH`;
- `CASH_MISMATCH`;
- `NEED_OWNER_CONFIRMATION`.

The raw account identifier is replaced by a digest before a summary is returned.
Raw XML, full snapshots and reconciliation evidence belong only in the
owner-controlled private runtime outside Git and cloud sync.

## Intended daily sequence

```text
IBKR Flex statement
-> parse and redact
-> compare with confirmed local ledger
-> MATCHED or owner-confirmation queue
-> accepted-close valuation and research
-> exactly one private daily report
```

A mismatch prevents the report from claiming broker synchronization. It does
not prevent an explicitly labelled accounting/research report from being
prepared. A later integration may create an owner-confirmation request, but it
must not auto-apply a broker event or submit an order.

## Current release boundary

The parser, client and read-only reconciliation library are implemented and
covered by synthetic tests. A real token/query, persisted private reconciliation
snapshot and end-to-end daily-runtime adapter are not enabled by the public
framework.
