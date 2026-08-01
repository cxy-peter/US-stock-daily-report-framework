# Decision and Report Contract

## Contents

1. Depth modes
2. Status model
3. Confidence model
4. Evidence labels
5. Reason codes
6. Required report structure
7. Comparison format
8. Research-record schema

## 1. Depth modes

Choose the lightest mode that answers the request:

- `TRIAGE`: decision card, structure/role, hard gates, decisive unknowns, and next evidence.
- `FIRST_PASS`: compact product analysis, applicable branch checks, preliminary fit, and monitoring needs.
- `FULL_DD`: every section and evidence-ledger field in this contract, suitable for an investment-committee record.

Do not force an eleven-section memo into a simple first question. Do not omit a decisive hard gate merely because the user requested brevity.

## 2. Status model

Give three separate fields:

- `product_quality`: `PASS`, `WATCH`, `REJECT`, or `NEED_INFO`
- `portfolio_fit`: `PASS`, `WATCH`, `REJECT`, or `NEED_INFO`
- `overall_status`: the most decision-relevant combined state

Meanings:

- `PASS`: sufficient evidence supports ownership for the stated role and conditions.
- `WATCH`: credible candidate, but evidence, valuation, stability, capacity, or execution is not yet strong enough.
- `REJECT`: a verified issue makes the candidate unsuitable for the stated mandate/role.
- `NEED_INFO`: missing user or product information prevents the requested conclusion.

Do not average these states into a number. A product-quality `PASS` can still have portfolio-fit `REJECT` because it duplicates exposure or violates the risk budget.

`product_quality` means the vehicle's integrity and ability to execute its own stated objective. It does not mean suitability for the user's requested holding period or role; that belongs in `portfolio_fit`.

Precedence rule: if a verified hard mismatch cannot be reversed by any missing fact, return `REJECT` even when other information is missing. Use `NEED_INFO` when the missing fact could materially change the requested decision.

### Runtime-monitor extension

The deterministic scheduled monitor may additionally return `NOT_DUE`. This is
a cadence state only and is not available as a full due-diligence or
investment-committee verdict. When one monitoring dimension passes and the
other is not due, the combined status remains `NOT_DUE` and `summary_code` must
be `fund_monitor.overall.partial_not_due`; never present the partial result as
an overall `PASS`.

A machine-readable monitoring update should also preserve
`triggered_event_keys`, the per-category freshness cutoffs, missing/stale/
degraded coverage, and the next due time. Acknowledge an event key only after
the exact event review is durably recorded. See `runtime-monitoring.md`.

## 3. Confidence model

Report `HIGH`, `MEDIUM`, or `LOW` confidence based on evidence coverage and agreement:

- `HIGH`: current primary sources cover material facts; independent evidence streams agree; important unknowns are immaterial to the decision.
- `MEDIUM`: primary sources exist, but history, regime coverage, or one material interpretation remains uncertain.
- `LOW`: material facts are missing/stale, evidence streams conflict, or the conclusion depends heavily on assumptions.

Confidence is not probability of positive return.

## 4. Evidence labels

Label material statements:

- `FACT`: directly supported by an identified source.
- `CALCULATION`: reproducible transformation of identified data; state method and sample.
- `INFERENCE`: conclusion implied by multiple facts/calculations but not explicitly stated by a source.
- `JUDGMENT`: portfolio or process assessment under declared criteria.
- `SOCIAL_SIGNAL`: unverified anecdote, concern, or claim from a social source.

Also label attribution inputs/results and sponsor marketing claims by observation origin when relevant: `OBSERVED`, `ESTIMATED`, or `SELF_REPORTED`. This is separate from the evidence label: an estimated regression result can be a reproducible `CALCULATION`, but it is not an observed holding. Use `METHOD-NOT-DISCLOSED` when a marketed statistic lacks enough methodology to reproduce or interpret it.

For every material numeric `FACT` or `CALCULATION`, include source, as-of/end date, frequency, currency, and whether returns are total or price returns when applicable.

## 5. Reason codes

Use one or more stable codes so monitoring updates can be compared.

### Information and structure

- `INFO-MISSING-PROFILE`
- `INFO-STALE-DATA`
- `INFO-CONFLICTING-SOURCES`
- `STRUCTURE-UNVERIFIED`
- `STRUCTURE-COMPLEXITY`
- `STRUCTURE-COUNTERPARTY`
- `OPTION-COVERAGE-UNVERIFIED`
- `COUNTERPARTY-DISCLOSURE-LAG`
- `METHOD-NOT-DISCLOSED`

### Mandate and exposure

- `MANDATE-MISMATCH`
- `ROLE-UNCLEAR`
- `ANCHOR-MISMATCH`
- `EXPOSURE-DRIFT`
- `EXPOSURE-CONCENTRATION`
- `EXPOSURE-DUPLICATION`

### People and process

- `TEAM-CHANGE`
- `PROCESS-INCONSISTENT`
- `PROCESS-UNPROVEN`
- `GOVERNANCE-CONCERN`
- `CAPACITY-CONCERN`

### Performance and risk

- `ALPHA-NOT-ROBUST`
- `PERSISTENCE-WEAK`
- `DRAWDOWN-BREACH`
- `TAIL-RISK`
- `PATH-DEPENDENCE`
- `ATTRIBUTION-UNEXPLAINED`

### Implementation

- `LIQUIDITY-CONCERN`
- `PREMIUM-DISCOUNT-CONCERN`
- `TRACKING-CONCERN`
- `COST-CONCERN`
- `TAX-UNKNOWN`
- `TAX-CONCERN`
- `OPERATIONAL-CONCERN`

### Positive decision reasons

- `ROLE-FIT`
- `EXPOSURE-DIFFERENTIATED`
- `PROCESS-REPEATABLE`
- `ATTRIBUTION-CONSISTENT`
- `IMPLEMENTATION-EFFICIENT`
- `DIVERSIFICATION-BENEFIT`

## 6. Required report structure

Use the following order.

### 1. Decision card

- candidate and exact share class/structure
- analysis date and data cutoff
- one-sentence conclusion
- `product_quality`, `portfolio_fit`, `overall_status`, and confidence
- `portfolio_tier`, `economic_role`, and reason codes

### 2. What is known versus missing

Provide a compact table of essential investor, mandate, portfolio, and product inputs. Mark unknowns explicitly. State whether this is product-only or personalized analysis.

### 3. Product identity and economic exposure

- legal structure and governing documents
- actual exposure, benchmark, peer group, and role
- stated-versus-held-versus-realized consistency
- material derivatives, leverage, counterparties, or special mechanics

### 4. Evidence scorecard

Use branch-specific dimensions with `PASS/WATCH/REJECT/UNKNOWN`. Do not add them into a universal total score.

At minimum cover:

- mandate and structure;
- people/process or index design;
- portfolio/exposure;
- performance quality and attribution;
- risk and tail behavior;
- cost, liquidity, and capacity;
- tax/operational considerations;
- governance and monitoring readiness.

### 5. Return source and risk

Separate intended beta, factor tilts, selection/timing residual, option/leverage effects, costs, and unexplained residual. Include rolling/regime evidence and the most relevant stress cases.

### 6. Product-specific branch findings

Report every mandatory item from all applicable branches in `us-product-branches.md`.

### 7. Portfolio impact

Compare current portfolio, portfolio plus candidate, and funded/replacement portfolio where possible. Discuss exposure overlap, marginal risk, stress loss, cost, liquidity, tax, and behavioral burden.

### 8. Bull and bear cases

Give the strongest evidence for approval and the strongest disconfirming evidence. Do not use a straw-man bear case.

### 9. Decision, sizing, and execution

- status and reason codes
- approved role and conditional weight range, if justified
- funding source/replacement logic
- entry conditions and trading guardrails
- rebalance band and maximum exposure
- conditions that would upgrade or downgrade the status

If information is insufficient, state which new fact would change the decision most.

Set `approved_weight_range: UNKNOWN` whenever investor/portfolio inputs do not support sizing. Clearly label any sensitivity-test range as a scenario, not a recommendation.

### 10. Monitoring and exit

- thesis to preserve
- monitoring metrics and source cadence
- scheduled review date
- event triggers
- exit triggers and expected replacement process

### 11. Evidence ledger

List sources in authority order with title, issuer, link/path, publication date, data date, access date, and which claims each source supports. Put social sources in a separate weak-signal subsection.

## 7. Comparison format

When comparing funds:

1. confirm comparable legal structures and roles;
2. use a common data cutoff and aligned total-return period;
3. show branch-specific facts side by side;
4. distinguish absolute product quality from fit for the stated portfolio;
5. identify the funded alternative and switching costs;
6. allow “neither” or “different roles” as valid outcomes.

Do not force a winner when data quality or role mismatch makes the comparison false.

## 8. Research-record schema

Use this compact YAML shape when the user asks for a reusable machine-readable dossier:

```yaml
candidate:
  name: UNKNOWN
  ticker: UNKNOWN
  structure: UNKNOWN
  as_of_date: UNKNOWN
decision:
  product_quality: NEED_INFO
  portfolio_fit: NEED_INFO
  overall_status: NEED_INFO
  confidence: LOW
  portfolio_tier: UNKNOWN
  economic_role: UNKNOWN
  reason_codes: []
thesis:
  expected_return_sources: []
  why_now: UNKNOWN
  edge_or_exposure: UNKNOWN
  disconfirming_evidence: []
gates:
  - name: structure_verified
    status: UNKNOWN
    evidence: []
branch_checks: []
portfolio_fit:
  funded_by: UNKNOWN
  overlap: UNKNOWN
  marginal_risk: UNKNOWN
  tax_and_cost: UNKNOWN
implementation:
  approved_weight_range: UNKNOWN
  scenario_test_weights: []
  entry_rule: UNKNOWN
  rebalance_rule: UNKNOWN
monitoring:
  scheduled_review: UNKNOWN
  indicators: []
  downgrade_triggers: []
  exit_triggers: []
unknowns: []
evidence_ledger: []
```
