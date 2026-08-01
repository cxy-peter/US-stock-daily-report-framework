# Private Manual-Event and Modeled-DCA Ledger

## Purpose

The private ledger replaces broker connectivity with an explicit local
accounting contract:

- actual trades and cash events enter only after the owner reports them;
- absence of a reported trade means no non-DCA trade is inferred;
- the configured base DCA plan may be modeled at an accepted official close;
- model entries calculate a private research portfolio, not broker execution;
- no order, login, account API or broker credential exists in this workflow.

The SQLite database and every derived snapshot belong in an ignored private
runtime path. The public repository contains only the engine, documentation and
synthetic tests.

## Two books

The same immutable event stream produces two projections:

```text
confirmed book
-> opening snapshot
-> owner-confirmed fills and cash events
-> confirmed income, fees and corporate actions

modeled book
-> confirmed book
-> configured base DCA entries not yet replaced by an owner-confirmed fill
```

The modeled projection is the default research view requested by the owner. It
must still expose modeled quantities separately and label them as not confirmed
by a broker. If a later owner-confirmed fill replaces a modeled entry, the
modeled projection uses the confirmed fill once; it never holds both copies.

## Immutable events

The MVP event vocabulary includes opening cash and positions, owner-confirmed
fills, confirmed cash flows, modeled DCA fills, explicit DCA session overrides,
splits and reversals. Economic records are appended; they are not updated or
deleted in place.

Every event has:

- a stable event and idempotency key;
- a source class such as `user_confirmed`, `modeled` or `system`;
- canonical JSON containing decimal strings;
- the previous event hash and its own hash.

Hash-chain verification detects accidental or manual mutation of stored event
payloads. It is an integrity check, not a substitute for backups or encryption.

## DCA price and atomicity gates

A modeled DCA batch may be posted only when all of the following hold:

1. the ledger resolves every instrument MIC against the pinned exchange
   calendar and verifies that `calendar_as_of` is at or after the official
   close;
2. the `AcceptedCloseBatch` covers exactly the active plan instruments;
3. every child close passed the independent-source price gate;
4. the whole accepted-close batch is eligible for ledger input;
5. each instrument's corporate-action state is `clear_none` or `reconciled`;
6. no explicit skip override exists for that plan and session;
7. the configured funding policy has sufficient capacity.

The ledger consumes only configured base amounts. A research engine's proposed
increase remains advisory until the owner explicitly creates a future plan
version. One failed instrument blocks the entire DCA batch and writes no child
event.

Exchange-session resolution is pinned to
[`exchange-calendars` 4.13.2](https://pypi.org/project/exchange-calendars/4.13.2/).
The engine preserves an instrument's canonical `XNAS`, `XNYS`, NYSE Arca
`ARCX`, or Cboe BZX `BATS` MIC while using the library's XNYS schedule for these U.S. equity
venues. It compares the timezone-aware run instant with the calendar's UTC
close, so DST, holidays and half days are not approximated with weekdays or
fixed clock offsets. Exchange close completion does not prove provider EOD
availability; the accepted-close gate remains separate. Calendar upgrades
must be reviewed against the
[NYSE hours calendar](https://www.nyse.com/markets/hours-calendars) and
[Nasdaq Trader calendar](https://www.nasdaqtrader.com/trader.aspx?id=Calendar).

The transaction permits only one active settled DCA batch per
`(ledger, session)`.
The plan identifier and version are part of that batch's immutable input. An
identical rerun returns the committed result; a changed plan, version, close or
gate input for the same session is an idempotency conflict and cannot create a
second purchase.

A `(plan_id, version)` also has one immutable definition across sessions,
including base amounts, funding mode, currency and effective share scale. A
changed definition requires a new version. A wrong unvalued batch can be
reversed only through one aggregate batch-reversal event, which deactivates the
marker, contribution and every child fill together. Individual modeled-DCA
children cannot be reversed. If a child already has an active owner-confirmed
replacement, that replacement must be resolved first.

## Decimal and funding rules

Binary floating-point inputs are rejected. Quantities, prices, cash and returns
use `Decimal` internally and canonical decimal strings in SQLite and JSON.

Modeled shares are rounded down to the configured instrument scale:

```text
quantity = floor(configured amount / accepted raw close, share scale)
spend = quantity * accepted raw close
residual = configured amount - spend
```

Two funding modes are explicit:

- `existing_cash`: spend and fees reduce modeled cash; an insufficient balance
  blocks the whole batch;
- `modeled_external_contribution`: the configured contribution enters at the
  close, the purchase consumes `spend`, and the rounding residual remains cash.

Owner-confirmed buys and sells use weighted-average economic carrying cost for
research P/L. That value is not a tax-lot or tax-reporting calculation.

An owner cash flow with no explicit time is defined as a close-time flow with
Modified-Dietz weight zero. If an intraday `occurred_at` is supplied, the caller
must also supply a weight from zero through one; neither field may be guessed
independently. Economic events replay by session, occurrence time and append
sequence under a fixed 50-digit Decimal context, so late input before valuation
and a caller's global Decimal settings cannot change the result.

## Performance semantics

A final valuation requires an accepted close for every non-zero position. A
stale or missing price makes the portfolio return incomplete instead of being
silently carried forward.

```text
NAV_t = cash_t + sum(quantity_i,t * accepted_close_i,t)
daily_pnl_t = NAV_t - NAV_(t-1) - net_external_flow_t
daily_return_t = daily_pnl_t / weighted_starting_capital_t
cumulative_TWR_t = product(1 + daily_return) - 1
```

A modeled external contribution and its DCA purchase occur at that session's
close, so the cash-flow weight is zero and the new shares earn no same-day
return. Existing-cash purchases are internal transfers and are not external
flows. The first stored valuation has no daily return; it establishes the
performance baseline.

Recording either book's valuation establishes a global session-finality
watermark. No owner event, DCA settlement, override or reversal may later be
backfilled on or before that session. The private runtime must therefore apply
all owner events and DCA batches first, then value the books. Valuation
restatement is intentionally outside this MVP; a late correction fails closed
instead of silently changing historical TWR.

## Owner interaction contract

The opening snapshot has a separate one-time TTY attestation and receipt. That
proof authorizes only the initial ledger checkpoint; it is not a reusable
confirmation for later fills, cash flows, DCA overrides or reversals.

When the research report suggests a rebalance, GPT asks the owner to report the
actual fill. The ledger changes only after receiving unambiguous side, quantity,
price, fees and effective time. A suggestion, target amount or silence is not an
actual trade.

Later events use a fixed owner-only staging path; they are never accepted from
arguments, stdin JSON, environment JSON or natural-language conversation:

```text
<SERENITY_PRIVATE_ROOT>/manual-event.request.json
```

The request is a closed `manual_owner_event_request/v1.0.0` JSON object with a
fresh 32-character lowercase hexadecimal nonce, one exchange session, optional
UTC occurrence time, one supported event kind and decimal strings for every
economic number. Supported kinds are `confirmed_fill`, `cash_flow`, `fee`,
`income`, `split` and `skip_dca`. Symbols and DCA plan identities must match the
current private configuration. The owner then runs, from a real terminal:

```bash
python scripts/attest_private_event.py
```

The random challenge binds confirmation to the exact request-byte digest. The
command holds the private-runtime lock, rechecks the config, ledger head,
valuation finality and every unresolved outbox scope, and publishes an
immutable self-hashed approval. It does not mutate the ledger and cannot place
an order. `run_private_daily.py` later audits every approval and receipt, writes
the event through the existing ledger API and publishes a receipt bound to the
resulting event ID and hash. A crash after ledger commit but before receipt
publication is recovered by the next idempotent run.

Each event commit is atomic and replay-safe; the entire pending queue is not
claimed as one database transaction. The runtime nevertheless refuses to value
or prepare a report until all eligible events for the session have been
processed. DCA replacements are staged before the run but consumed only after
that session's modeled DCA settlement and before either book is valued.

An explicit session override can skip the base DCA for a date. A separate
manual trade does not cancel the base plan by implication. Before valuation,
an owner-confirmed fill may explicitly replace the matching modeled DCA child.
Lower-level ledger APIs retain owner-event and aggregate DCA-batch reversal
support, but the v1 staging command intentionally exposes only the six event
kinds above. After valuation, historical corrections require a future
restatement/supersession workflow and are rejected.

## Current boundary

This module is local accounting infrastructure. It does not scrape a broker,
confirm external execution, submit orders, infer dividends, provide tax
accounting or automatically resolve complex mergers and distributions. Those
unknowns remain visible and fail closed where they affect DCA or return
calculation.
