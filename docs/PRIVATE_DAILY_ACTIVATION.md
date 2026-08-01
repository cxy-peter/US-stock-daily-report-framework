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

## Opening owner-attestation sequence

The opening ledger cannot be created from configuration alone. The operator
must use this order from the same owner-only environment:

```text
check_private_daily_readiness.py       # read-only baseline
attest_private_opening.py              # interactive TTY only
check_private_daily_readiness.py       # must say needs_initialization
initialize_private_daily.py            # provider/corporate-action gates + commit
check_private_daily_readiness.py       # confirms the next safe action
```

The claim is bound to the exact reviewed configuration bytes and to a stable
opening identity. It contains no path, symbol, quantity, cost or cash value.
Before the opening commit, the exact-byte binding also catches changes to DCA
or provider routing. After consumption, the permanent proof intentionally
checks only immutable opening economics and stable security identity; ongoing
DCA versions and provider aliases remain maintainable owner-only configuration
and must pass their own ledger/provider gates.

The 30-minute TTL covers both intent publication and the opening commit. A
fresh intent with no opening event is `resume_available`; the explicit
initializer may replay it with the real current commit time. Once the TTL has
expired it becomes `resume_requires_owner_reconfirmation`; rerunning
`attest_private_opening.py` from a TTY archives the unused intent and creates a
new claim. It does not alter an initialized ledger.

An opening event without its receipt is `recovery_available`; the initializer
may bind the existing event to a receipt and finish the two opening valuations.
`consumed_verified` is the only state accepted by normal live report
preparation. Config mismatch, unsafe files, a missing control in an initialized
runtime, and replay/rollback evidence fail closed. The mutating initializer
also requires the outbox to be absent or an exact empty schema, so stale report
state cannot be attached to a new ledger.

The three v1 sidecar readers are compatibility contracts: their contract,
confirmation-method, config-schema and opening-identity versions must remain
readable when a future v2 writer is added.

### Threat boundary

The random TTY challenge is an interlock against unattended or accidental
initialization, not cryptographic authentication of another process running as
the same OS account. The control hashes are self-hashes, not signatures or
HMACs, and must remain private because a low-entropy opening snapshot could be
guessed offline. Owner-only OS permissions, exact SQLite/hash-chain checks and
the three-file state machine protect against other ordinary users, accidental
scripts, single-file rollback and non-coordinated corruption. They do not
detect an administrator/same-user attacker that rewrites the ledger and all
sidecars together, a whole-directory/disk rollback, or system-clock control.

The storage root must therefore remain under an owner-controlled parent, not
merely have an owner-only leaf ACL. POSIX publication fsyncs file and directory
transitions. Windows hard-link recovery is tested for process interruption,
but directory-entry durability across sudden power loss is not claimed.

## Current activation blockers

Opening owner attestation is implemented and audited, but it does not by itself
authorize recurring delivery. The public framework still reports these gates
as incomplete until separately implemented and verified:

- safe owner-confirmed manual-event ingestion;
- a receiver adapter with verified idempotency or delivery lookup;
- a persisted live accepted-close/end-to-end trial; and
- product-level confirmation that one paused automation is uniquely targeted
  before it is activated.

Therefore this checker does not itself authorize or enable a daily task. A
future deployment adapter must provide those proofs through an auditable,
owner-only mechanism rather than environment booleans or public configuration.
