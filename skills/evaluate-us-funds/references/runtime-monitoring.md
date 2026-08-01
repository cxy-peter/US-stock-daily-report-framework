# Runtime Fund Monitoring Contract

Use this reference for a scheduled or event-triggered update after a fund has
already been classified. It complements the full due-diligence workflow; it
does not replace current product documents, branch-specific checks, or the
portfolio-fit decision.

The implementation is `serenity_monitor.fund_monitor`. It is deterministic and
offline. It does not fetch filings, scrape social platforms, read a portfolio,
connect to a broker, or submit an order. A private adapter must normalize and
privacy-minimize current evidence before constructing these records.

## Input objects

- `FundSource`: controlled source ID, tier, health, and point-in-time health
  timestamp.
- `FundEvidence`: controlled category and dimension, evidence label,
  assessment, point-in-time timestamp, and optional material-event flag.
- `FundMetric`: a finite `Decimal` observation or explicit `unknown` /
  `not_applicable`; missing values are never zero.
- `LastCompleted`: last daily, monthly, quarterly, and annual review times.
- `FundMonitorRequest`: fund key, legal/economic structure, portfolio role,
  local review timezone, inputs, and current event acknowledgements.

Only `FACT` and `CALCULATION` can close a required coverage category. Structure
requires official `FACT` evidence. `INFERENCE` and `JUDGMENT` may downgrade a
fully covered assessment to `WATCH`, but cannot fill a gap or independently
produce `REJECT`. `SOCIAL_SIGNAL` is only a `lead` or `unknown`: it may trigger
research but never satisfy a required category.

## Cadence and freshness

| Layer | Scheduled use | Inclusive freshness window |
|---|---|---:|
| Daily | structure, source health, manager, fees, prospectus | 2 days |
| Event | material manager/fee/index/prospectus/structure change | 7 days |
| Monthly | exposure, style, factor, liquidity, capacity, role and overlap | 45 days |
| Quarterly | holdings, attribution, thesis, manager skill and implementation | 120 days |
| Annual | full product and portfolio-fit review | 400 days |

Source-health evidence always uses the two-day window. A monthly category does
not become annual-current merely because an annual review is due. Scheduling
uses the request's allowlisted review timezone (`Asia/Shanghai`,
`America/New_York`, or `UTC`) and stores aware UTC instants.

## Result contract

`monitor_fund()` returns separate `product_quality_status` and
`portfolio_fit_status`, plus a conservative combined `status`:

- `PASS`: every due product and fit dimension passes;
- `WATCH`: current, complete evidence supports continued observation;
- `REJECT`: a qualifying verified failure is decisive;
- `NEED_INFO`: required evidence is missing, stale, unknown, or degraded;
- `NOT_DUE`: no complete decision is due. This also remains the combined status
  when one dimension passes but the other is not due; `summary_code` records
  `fund_monitor.overall.partial_not_due` so it cannot be mistaken for approval.

The result exposes required/category cutoffs, covered/missing/unknown/degraded/
stale/social-only categories, review schedules, next due time, and
`triggered_event_keys`. All trade and execution capability fields are immutable
`false`.

## Material-event acknowledgement

For every current material event, calculate the canonical token with
`compute_event_acknowledgement_key(fund_key, evidence)`. The digest commits to
the fund and every structured event field, including timestamp and assessment.

1. Run the monitor without pre-acknowledging a new event.
2. Persist the returned `triggered_event_keys` only after the research update is
   durably recorded.
3. On the next run, pass acknowledgements only for the still-current event set.
4. Drop expired event tokens; unknown, stale, cross-fund, or pre-confirmed
   acknowledgements fail closed.

Changing any event field creates a new token and a new review obligation. The
acknowledgement means “this exact event was processed,” not “this category is
permanently safe.”

## Skill routing

Use this monitor for a compact scheduled update. If structure, manager, index
methodology, strategy, tax treatment, or another thesis-bearing fact changed,
re-open the applicable product branches and produce a full or first-pass memo.
`NOT_DUE` is a runtime cadence state, not a fifth investment-committee verdict.
The full memo continues to use `PASS`, `WATCH`, `REJECT`, or `NEED_INFO`.

Production collection, durable cadence/event-acknowledgement state and GPT
delivery adapters remain separate deployment work. The framework can persist a
self-hashed, owner-only **sanitized aggregate projection** for the daily report;
that transport is not evidence collection and does not acknowledge an event.
All deployment adapters must reuse the owner-only non-Git path and ACL gates
and may not weaken this module's no-trade boundary.
