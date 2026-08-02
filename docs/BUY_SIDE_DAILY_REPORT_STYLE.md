# Buy-Side Daily Report Style

## Objective

Write a decision memo, not a news digest and not an institutional-audit log.
The reader should understand the action, variant view, evidence and failure
conditions before seeing model plumbing.

## Required thesis structure

Each material thesis uses the same fields:

```text
Title / affected assets
Conclusion and action implication
Change from yesterday
Consensus and variant perception
Evidence chain
Catalysts
Horizon
Invalidation / exit conditions
Confidence
```

### Conclusion and action implication

Use direct language:

- `HOLD` — the thesis is intact but no independent evidence justifies changing
  exposure;
- `ADD_REVIEW` — the thesis, cross-horizon factor evidence, fresh holdings and
  transaction economics all pass manual-review gates;
- `TRIM_REVIEW` — thesis deterioration, concentration or downside risk supports
  reducing exposure after tax-lot and execution review;
- `BLOCK_ADD` — maintain the existing position but do not add because a data,
  corporate-action, fee, liquidity or risk gate is unresolved;
- `PAUSE_AND_VERIFY` — public/private tests or critical account facts failed.

Never replace a decision with “continue monitoring” unless the exact monitored
condition and next decision threshold are stated.

### Change from yesterday

Compare only like-for-like versioned scores.  Use:

- first observation / no comparable prior score;
- strengthened;
- weakened;
- stable;
- reversed.

Do not infer a change from publication volume.  A new syndicated article is not
a new independent data point.

### Consensus and variant perception

Separate:

1. what the market or sell-side broadly appears to believe;
2. what this system believes is underappreciated or mispriced;
3. why the difference may persist;
4. what evidence would show the variant view is wrong.

Examples:

- “HBM demand remains a positive industry consensus, but the variant question is
  whether pricing and supply discipline survive capacity additions.”
- “Index momentum remains positive, but the variant risk is that actual
  transaction costs and factor crowding eliminate the apparent signal.”

### Evidence chain

Order evidence by authority:

1. regulator, official action, filing, issuer primary disclosure;
2. audited report, prospectus, index methodology or official holdings;
3. direct management comments and investor materials;
4. high-quality independent institutional research and major media;
5. model calculation with point-in-time input;
6. social or agent-generated claim lead.

Label every material statement as one of:

- `FACT`;
- `CALCULATION`;
- `INFERENCE`;
- `JUDGMENT`;
- `SOCIAL_SIGNAL`.

A lower-level source cannot overrule a contradictory primary source without an
explicit conflict note.

### Catalysts and horizon

Use at least two time scales where relevant:

- next session / 1—5 trading days;
- 1—4 weeks;
- 1—2 quarters;
- structural 1—3 years.

A catalyst must be an observable event: earnings, pricing, inventory, fund
flow, policy implementation, manager change, index rebalance, VIX term-structure
normalization or corporate action.  “Better sentiment” is not a sufficient
catalyst.

### Invalidation and exit

State conditions that would overturn the thesis rather than merely lower
confidence.  Examples:

- issuer disclosure contradicts the assumed demand/price path;
- 5-day and 20-day OOS factor evidence turns negative or is quarantined;
- manager/team change breaks return attribution;
- fund liquidity, premium/discount or capacity deteriorates;
- policy remains rhetoric and fails to reach implementation;
- a company action cannot be reconciled to holdings/cost basis;
- estimated all-in transaction cost consumes the expected edge.

## Fund and manager research

A fund or manager paragraph must distinguish:

- product quality;
- portfolio fit;
- manager skill;
- strategy/vehicle fragility;
- implementation cost;
- monitoring/exit governance.

Use alpha, bootstrap, Treynor-Mazuy/Henriksson-Merton timing, capture ratios,
rolling persistence, factor/style drift and fragility.  Do not assign a product
shell's older history to a new manager.  Do not let recent returns override a
structure, capacity, liquidity or governance failure.

## Transaction cost presentation

There is no recurring portfolio-wide fee section in the main report.
Transaction economics appear only for `ADD_REVIEW` or `TRIM_REVIEW` and are
security-specific:

```text
reference price and timestamp
proposed shares and notional
commission estimate or UNKNOWN
spread/slippage estimate or UNKNOWN
holding-cost estimate where material
known tax-lot implication or UNKNOWN
minimum expected edge required to cover costs
```

Actual historical fees and tax records remain available in collapsed broker
facts, not in the daily decision flow.

## What not to write

Avoid:

- a source-by-source headline list without a thesis;
- “institutional-grade” as a substitute for evidence;
- long safety/control repetition in every conclusion;
- raw mention counts as political or social signals;
- model scores without economic interpretation;
- precise target weights when inputs are incomplete;
- “no data” presented as a neutral market view;
- recommendations based solely on an agent-generated summary.
