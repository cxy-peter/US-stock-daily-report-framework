# Private Daily Report Contract

## Scope

The private daily-report layer defines one versioned JSON document, one
deterministic Chinese Markdown view and one local SQLite delivery outbox. It
does not fetch prices, mutate the portfolio ledger, connect to a broker or send
a message by itself.

The machine contract is
`schemas/private_daily_report.v1.schema.json`. Runtime reports, rendered
Markdown and the outbox database belong under an ignored `private/` directory;
none is a public-repository artifact.

## JSON is the only source of truth

`serenity_monitor/private_daily_report.py` validates Draft 2020-12 schema,
cross-field semantics and content identity. Every object is closed with
`additionalProperties: false`. Binary floating point is rejected recursively;
money, quantities and rates use canonical non-exponent Decimal strings.
Book NAV, cash, position market value, economic cost, unrealized P/L and linked
daily performance must reconcile arithmetically under the same fixed Decimal
context as the ledger. The contract carries prior cumulative TWR so linked
cumulative return can also be recomputed. The modeled book must contain every
confirmed position at no lower total quantity. Its separately labeled modeled
quantity cannot exceed the modeled-versus-confirmed difference, and every
non-zero difference must remain visibly identified as modeled rather than
appearing broker-confirmed.

The contract distinguishes:

- `complete`, `complete_with_warnings`, `blocked` and `no_new_close` reports;
- current, multi-session backfill and no-new-session calendar runs;
- per-session calendar, accepted-close, corporate-action and funding gates;
- fresh, display-only carried-forward and unavailable valuations;
- confirmed and modeled books;
- configured, proposed, modeled and unavailable broker-confirmed DCA layers;
- informational research from executable facts.

No-new-close reports carry forward the last real valuation session for display
only. They cannot claim a daily profit, daily return or new modeled purchase.
A first blocked run may truthfully contain no ledger hash, NAV or price instead
of inventing a zero-valued portfolio.

`report_id` hashes the canonical JSON document with only its own top-level
field removed. `delivery_id` is derived from schema version, local delivery
date, timezone, channel and a SHA-256 target digest. Neither the raw delivery
target nor its digest appears in the report document.

## Manual-trade and DCA meaning

The default for user silence is exactly
`no_new_owner_confirmed_event`. It means the local ledger received no new
owner-confirmed event; it does not assert that the real-world account had no
trade.

Position changes are proposals only. An `ADD`, `REDUCE` or `EXIT` proposal
requires owner confirmation and a manual prompt. The schema fixes every
`automatic_execution` field to `false`, and broker-confirmed DCA remains
`not_connected` and `unavailable`.

Modeled DCA settlement records one row per required session and symbol. A
settled row must use the immutable configured amount, accepted close and
settlement identity. Research can propose a different amount, but that
proposal cannot silently replace the configured base plan.

## Markdown view

`serenity_monitor/private_daily_markdown.py` accepts only a fully validated
report and performs no accounting, clock, file, network or market-data work.
Rendering is byte-deterministic with LF endings. Free-text paths,
credential-shaped assignments and Markdown control characters are redacted or
escaped before presentation. Untrusted text cannot create Markdown images,
links or raw HTML.

`serenity_monitor/daily_outbox.py` renders Markdown internally from the JSON;
callers cannot enqueue an independently edited body.

## Delivery outbox

The outbox uses SQLite WAL mode, `synchronous=FULL`, immediate transactions,
immutable-content triggers and append-preserved attempts. One channel, target
digest and local date has one immutable report slot.

Delivery is leased once. An expired or ambiguous attempt becomes
`delivery_unknown`, not automatically retryable. A retry is allowed only after
one of these explicit resolutions:

- a receiver lookup in the same recorded receiver namespace proves the
  delivery was not found; or
- both the original and current adapter support the same stable idempotency
  key in the same receiver namespace and an idempotent retry is explicitly
  authorized.

If an adapter supports neither receiver lookup nor idempotency keys, the
outbox rejects it because only at-most-once delivery is possible. This module
does not claim network-level exactly-once semantics without a receiver
capability that can enforce or verify deduplication.

The database retains only the target digest, idempotency-namespace digest,
lookup-namespace digest, lease-token digest and optional receiver-receipt
digest. Raw target, adapter namespaces, lease token, receipt and exception
text are not persisted. Enqueue time cannot precede `prepared_at`. Before
every claim, the outbox revalidates canonical JSON, re-renders Markdown and
recomputes report, delivery, ledger and content identities.

## Deployment boundary

The public CI compiles and tests these contracts with synthetic data only. It
does not schedule a report, create a private database, upload an artifact or
send a GPT message.

A later private-runtime orchestrator must still:

1. resolve all completed exchange sessions oldest first;
2. obtain accepted closes and stop at the first failed gate;
3. settle the modeled ledger and value both books;
4. build and finalize this JSON document;
5. enqueue it in an ignored private outbox; and
6. use a receiver adapter whose deduplication capability has been verified.

Until that orchestration and replay verification exist, this contract is
implemented but daily GPT delivery is not deployed.
