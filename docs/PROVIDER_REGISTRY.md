# Accepted-Close Provider Registry

## Purpose

The provider registry creates an auditable boundary between prices that are
useful for research or display and prices that may later be used to settle a
configured recurring-investment plan. A quote being available does **not** make
it an accepted close.

```text
research/display chain
-> best-effort quotes, fallbacks and stale snapshots
-> valuation context only

accepted-close chain
-> independent raw daily observations
-> identity, session and currency validation
-> cross-provider agreement gate
-> AcceptedClose audit record
-> downstream corporate-action and ledger gates
```

The existing research/display providers remain available for analysis and
report rendering. Their fallback behavior is deliberately not reused for
settlement: a fallback chain is one observation path, not independent
confirmation.

## Providers and credentials

The accepted-close chain supports two independent observations:

- Twelve Data `time_series`, requested as a one-day, regular-session bar with
  `adjust=none` and `prepost=false`;
- Alpha Vantage `TIME_SERIES_DAILY`, using the raw/as-traded daily close rather
  than an adjusted endpoint.

Credentials are read only from environment variables:

```text
TWELVE_DATA_API_KEY
ALPHA_VANTAGE_API_KEY
```

API keys must never be stored in YAML, committed files, report artifacts,
request diagnostics or logs. Provider errors are normalized before they enter
audit output so a request URL or secret cannot leak through an exception.
Registry audit errors use a fixed vocabulary even for custom provider adapters;
adapter-supplied error text is not trusted.

The request and timing rules follow the providers' published documentation:

- [Twelve Data adjustment modes](https://support.twelvedata.com/en/articles/5179064-are-the-prices-adjusted)
  and [U.S. equities availability](https://support.twelvedata.com/en/articles/9935903-us-equities-market-data);
- [Alpha Vantage daily-series contract](https://www.alphavantage.co/documentation/)
  and [published request limits](https://www.alphavantage.co/support/).

Provider documentation is evidence for request semantics, not a guarantee that
a particular response is final. Exact-session presence and cross-source
agreement remain runtime gates.

## Observation eligibility

An observation is eligible for accepted-close consensus only when all of the
following are true:

- the canonical instrument and provider symbol map to the intended security;
- the exchange/MIC and market calendar match the configured instrument;
- the bar date equals the exact expected U.S. trading session;
- the value is the regular-session close, not an intraday, premarket,
  after-hours or stale value;
- the price is raw/unadjusted, denominated in USD and uses the configured unit
  multiplier;
- the provider is settlement-eligible and belongs to a distinct independence
  group;
- the payload is parseable, finite and accompanied by source and retrieval
  provenance.

Mock values, broker snapshots, emergency fallbacks, adjusted-price series,
wrong-session bars and observations with ambiguous currency or units are
display-only or rejected. They cannot become an accepted settlement close.

## Consensus policy

At least two eligible, independent observations are required. Agreement is
measured in basis points from the observed close range. The registry preserves
every observation and selects the configured primary provider's validated raw
close; it never averages conflicting prices.

| Independent-source result | Accepted-close state | Price-gate result |
| --- | --- | --- |
| Two or more; difference `<= 30 bps` | accepted | price gate passes |
| Two or more; difference `> 30` and `<= 75 bps` | warning | blocked by default |
| Two or more; difference `> 75 bps` | blocked | blocked |
| One eligible source only | degraded/provisional | blocked |
| Display-only sources only | display-only | blocked |

The 30--75 bps warning band may be exposed for investigation, but the default
policy does not settle it. Enabling warning-band settlement must be an explicit
private-runtime policy choice and remains subject to every downstream gate.
Price disagreement is never resolved by silently averaging observations.

Provider identity, upstream independence, trust tier and eligibility are frozen
when the registry is constructed. An observation cannot self-promote by
claiming a different independent upstream or settlement tier.

An `AcceptedClose` therefore means that the **price-validation gate** passed.
It intentionally exposes `price_gate_permitted`, not final settlement
authorization. It is not, by itself, permission to mutate a portfolio.

## Corporate actions and atomic settlement

Raw prices avoid mixing incompatible adjustment conventions, but they do not
solve splits, distributions, symbol changes or other corporate actions. A
separate downstream corporate-action reconciliation gate must validate the
instrument and session before any ledger mutation.

The future private ledger must also apply an atomic batch rule: either every
configured ticker for the session passes the required calendar, accepted-close
and corporate-action checks, or no automatic recurring-investment entry is
posted for that batch. Partial price availability must not create a partially
settled daily plan. A blocked batch marks every child close as ineligible for
ledger input, including symbols whose individual price gate passed.

Decision IDs are derived from stable target-session identity and close fields.
The hash of the complete provider response is retained separately as evidence,
because an expanding historical response must not change the ID of an unchanged
target-session close. The future ledger must additionally enforce a unique
session/plan/event constraint rather than relying on a batch hash alone.

## Scheduling and finality

Provider delivery time is not the same as the exchange close time. In
particular, Twelve Data's confirmed U.S. end-of-day bar may not be reliably
available by **08:30 Asia/Shanghai**. A formal private daily run should therefore
target **13:15 Asia/Shanghai**, while still verifying that the exact intended
session is present. Wall-clock time alone never proves finality.

If the target session is missing, provisional or disagrees beyond policy, the
system may render source health and display-only valuation context, but it must
not claim successful settlement. The runtime should retry on a later run rather
than substitute the previous close.

## Explicit non-goals and current release boundary

The registry:

- does not write a portfolio ledger or change holdings, cash or cost basis;
- does not create, submit or simulate broker orders;
- does not infer that a proposed DCA increase was executed;
- does not turn social, KOL or research scores into settlement amounts;
- does not make stale snapshots settlement-eligible.

This release adds only the accepted-close provider registry and its validation
contract. The private manual-trade/daily-DCA ledger, corporate-action
reconciliation, idempotent session settlement and private GPT daily-report
delivery belong to subsequent auditable pull requests.
