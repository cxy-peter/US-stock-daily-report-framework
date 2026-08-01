# Private Daily Activation Readiness

## Purpose

`scripts/check_private_daily_readiness.py` is a read-only preflight for the
owner-only daily runtime. It does not initialize a ledger, fetch a close,
prepare a report, claim an outbox lease, deliver a message, create a directory
or change an automation. It emits exactly one redacted JSON object under
`private_daily_activation_readiness/v1.0.0`.

Private paths, symbols, positions, amounts, credentials, receiver targets,
target digests, report IDs and delivery IDs are excluded from the contract.
The command accepts no arguments. It reads the same fixed environment boundary
as the private runtime and returns:

- `0` only when `workflow_activation_allowed` is true;
- `2` when a valid audit is not yet safe to activate;
- `70` if the guarded import or execution boundary fails; or
- `130` when interrupted during either guarded boundary.

Delivery workers must parse `ready_for_delivery`; they must not treat the
activation exit code as permission to send.

## Independent decisions

The contract deliberately separates four decisions:

- `ready_for_initialize`: initialization is still needed and its preconditions
  are satisfied. A prior accepted-close probe is not required because the
  initializer performs that gate itself.
- `ready_for_prepare`: the initialized ledger and current preparation gates are
  valid, and no prior delivery blocks new ledger mutation.
- `ready_for_delivery`: the current receiver has one prepared/retryable item,
  the outbox is valid and the verified adapter can provide a compatible
  exactly-once path. Provider, ledger and corporate-action availability do not
  block delivery of content that is already persisted.
- `workflow_activation_allowed`: recurring deployment is safe only after all
  owner, ingestion, receiver, product-automation and persisted live-trial gates
  pass. It is not derived from any one operational decision.

The closed operational states and actions are:

| `operational_state` | `next_safe_action` | Meaning |
| --- | --- | --- |
| `blocked` | `operator_review` | A trust, completeness or activation prerequisite is missing. |
| `needs_initialization` | `initialize` | The opening ledger still needs an explicit initialization/resume. |
| `ready_for_prepare` | `prepare` | A new report may be prepared explicitly. |
| `pending_delivery` | `deliver` or `operator_review` | A prepared/retryable item takes precedence; delivery additionally needs compatible receiver capability. |
| `reconciliation_required` | `reconcile` | A sending/ambiguous attempt must be resolved before any resend. |
| `already_complete` | `none` | The current report-timezone date is already delivered and no earlier item remains unresolved. |

`overall` describes recurring activation only: it is `ready` exactly when
`workflow_activation_allowed` is true.

## Read-only integrity boundary

The audit validates the existing external owner-only storage root and known
database identities without creating or tightening them. It refuses a nonempty
SQLite WAL or rollback journal. The database and sidecars are fingerprinted
before and after an immutable read so concurrent mutation fails closed.

For both stores it runs SQLite quick and foreign-key checks and verifies the
required append-only schema protections. Ledger events, valuation chains and
the configured opening identity are checked. The outbox validates every row
and delivery-attempt chain before it filters in memory by the current target
digest and channel. This ordering prevents a corrupted target digest from
hiding an unsafe row.

For the current receiver:

- one `prepared` or `retryable` row becomes `pending_delivery`;
- one `sending` or `delivery_unknown` row becomes
  `reconciliation_required`;
- an already-delivered current local date becomes `already_complete`;
- only historical delivered rows become `empty`; and
- multiple unresolved rows, a future row or a delivered row that overtook an
  earlier unresolved row becomes a conflict and requires operator review.

A retry authorized by an idempotency key is ready only when the current adapter
uses the original receiver-scope identity. Neither raw scope is rendered.

## Current activation blockers

The public framework intentionally reports the following gates as incomplete
until separately implemented and verified:

- opening-snapshot owner attestation;
- safe owner-confirmed manual-event ingestion;
- a receiver adapter with verified idempotency or delivery lookup;
- a persisted live accepted-close/end-to-end trial; and
- product-level confirmation that one paused automation is uniquely targeted
  before it is activated.

Therefore this checker does not itself authorize or enable a daily task. A
future deployment adapter must provide those proofs through an auditable,
owner-only mechanism rather than environment booleans or public configuration.
