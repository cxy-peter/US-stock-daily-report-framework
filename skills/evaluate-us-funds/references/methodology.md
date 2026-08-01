# Institutional Fund-Research Methodology

## Contents

1. Core proposition
2. Decision inputs and completeness
3. Closed-loop research architecture
4. Classification and the comparison anchor
5. Hard gates and contextual screens
6. Quantitative evidence
7. Return-source and persistence tests
8. Qualitative diligence and historical replay
9. Portfolio construction and sizing
10. Pool governance and monitoring
11. Bias controls and common failure modes

## 1. Core proposition

A fund is not “good” in isolation. Institutional research asks four different questions:

1. **Integrity:** Is the vehicle what it says it is, and can it be owned safely?
2. **Quality:** Is the return source intelligible, repeatable, and net of realistic costs?
3. **Fit:** Does it improve this investor's portfolio relative to available alternatives?
4. **Governance:** Can the thesis be monitored, implemented, and exited with explicit rules?

The decision order matters. Do not rank historical returns first and explain the winner later.

## 2. Decision inputs and completeness

### Investor and mandate

Record objective, benchmark, horizon, liquidity, base currency, tax residency, account type, prohibited exposures, acceptable structures, maximum drawdown, volatility budget, concentration limits, and leverage/derivative permissions.

### Existing portfolio

Record current holdings, weights, cost basis when tax matters, realized/unrealized exposures, factor concentrations, turnover budget, cash flows, and rebalancing rules.

### Candidate and alternatives

Record exact share class/ticker, legal structure, intended role, viable substitutes, proposed funding source, and decision deadline.

### Completeness rule

Missing personal information does not block product research. It does block personalized tax, sizing, replacement, and overall-fit claims. Return `NEED_INFO` and distinguish:

- conclusions that remain valid at product level;
- conclusions conditional on an assumption;
- conclusions that cannot yet be made.

## 3. Closed-loop research architecture

### Layer A: Investment policy

Define the strategic objective and risk budget before product selection. Separate:

- **SAA:** long-term policy exposures and allowable bands;
- **TAA:** bounded, time-limited deviations with independent signals and exit rules.

Do not use frequent fund switching to conceal an unstable policy portfolio.

### Layer B: Product classification

Classify with three evidence streams:

1. mandate/prospectus;
2. disclosed holdings;
3. return-based factor/style behavior.

The three streams should explain each other. A mismatch is a research finding, not noise to discard.

### Layer C: Due diligence

Assess firm, people, philosophy, process, portfolio, performance, operations, fees, and capacity. Quantitative screening narrows the field; qualitative evidence tests repeatability and future relevance.

### Layer D: Portfolio construction

Evaluate marginal contribution, not standalone rank. Compare the candidate with the funded alternative and with the current portfolio before and after inclusion.

### Layer E: Implementation

Translate approval into a role, weight range, trade guardrails, rebalance bands, and liquidity/tax-aware funding plan.

### Layer F: Governance

Save the original thesis, disconfirming evidence, monitoring metrics, review schedule, downgrade triggers, and exit conditions. Attribute subsequent results to allocation, selection, implementation, fees, and taxes.

## 4. Classification and the comparison anchor

### Why the anchor comes first

Comparing unlike mandates converts beta into apparent alpha. Examples include:

- growth versus value funds during a style cycle;
- long-duration versus short-duration bond funds;
- broad equity versus concentrated themes;
- covered-call income versus uncapped equity;
- leveraged daily exposure versus an unlevered long-horizon benchmark.

### Classification procedure

1. Read the investment objective, principal strategies, 80% policy if any, derivatives permissions, concentration language, and benchmark.
2. Examine holdings across multiple dates, not only the latest snapshot.
3. Run return-based exposure analysis with plausible factors and rolling windows.
4. Compare stated, held, and realized exposures.
5. Assign confidence and label drift.

### Holdings-based evidence

Inspect sector, industry, country, market cap, valuation, profitability, duration, curve, credit quality, option notional, cash, derivatives, and concentration as relevant. Respect disclosure lags. Historical backtests must use the publication date, not the portfolio date, to avoid look-ahead bias.

### Return-based evidence

Use constrained style analysis or factor regressions where appropriate. Check stability across windows. Returns are timely but can be non-identifying: several portfolios can generate similar returns, and short samples can mistake regimes for skill.

### Classification output

State:

- official category and legal structure;
- observed economic exposure;
- benchmark and peer group;
- intended portfolio role;
- confidence and unresolved discrepancies.

Leaving a fund unclassified is preferable to forcing a false label.

## 5. Hard gates and contextual screens

Hard gates prevent compensating errors. High returns cannot offset a verified breach.

### Typical gates

- mandate or prohibited-structure conflict;
- investor ineligibility or unacceptable tax/legal treatment;
- missing or internally inconsistent official documents;
- unresolved counterparty, leverage, collateral, or valuation mechanism;
- operational or governance failure;
- liquidity/capacity below the intended trade size and exit need;
- material strategy or personnel change that invalidates the history;
- downside behavior beyond the stated risk budget;
- no credible benchmark or no usable data for the claimed strategy.

### Contextual, not universal, thresholds

AUM, manager tenure, track-record length, holdings concentration, turnover, spread, and fund age can be useful screens, but there is no universal passing value. Tie each threshold to the mandate and product:

- a small niche ETF may be tradable for a small account but not an institution;
- a new fund may inherit a demonstrable team/process record but not automatically a product record;
- a concentrated strategy can be legitimate as a satellite but unsuitable as core beta.

Record the rationale for every screen and allow documented exceptions.

## 6. Quantitative evidence

### Data discipline

- Use total returns with distributions reinvested unless the question explicitly requires price return.
- Align dates, currencies, valuation times, and frequencies.
- Use the actual investable share class and fee load.
- Record inception, survivorship, backfill, benchmark-change, and merger effects.
- Avoid annualizing a tiny sample without a prominent warning.
- Use multiple horizons and rolling windows; a single since-inception number can be regime-specific.
- For predictive claims, use a point-in-time investable universe, actual disclosure availability, walk-forward or untouched holdout testing, closed funds/departed managers where available, share-class consolidation, and realistic implementation costs. Otherwise mark the exercise `DESCRIPTIVE_ONLY`.

### Risk and outcome measures

Select metrics that match the mandate:

- arithmetic and geometric/annualized return;
- volatility, downside deviation, maximum drawdown, recovery time;
- historical VaR and expected shortfall as descriptive tail statistics;
- Sharpe, Sortino, Calmar, and hit/win rates;
- upside/downside capture and stress-period loss;
- liquidity, premium/discount, and transaction-cost measures.

Do not interpret a ratio without its numerator, denominator, sample, benchmark, and regime.

### Relative measures

For active risk, inspect active return, tracking error, information ratio, alpha/beta, factor residual, and rolling excess-win rate. For passive products, tracking **difference** is the investor outcome; tracking error describes variability around it. Explain the sources of both.

### Persistence

Test whether performance survives:

- fees and implementation costs;
- factor/style adjustment;
- rolling and non-overlapping windows;
- market regimes and stress periods;
- capacity and asset growth;
- manager/team changes;
- multiple-testing and selection-bias controls.

Persistence evidence is usually weaker than a league table implies. Treat it as one component of the evidence chain.

## 7. Return-source and persistence tests

### Attribution hierarchy

Choose the method that fits the product:

- **Brinson-type attribution:** allocation, selection, and interaction for holdings portfolios;
- **factor attribution:** market, size, value, profitability, investment, momentum, quality, low volatility, sector, country, rates, curve, credit, currency, commodity, and volatility/option exposures;
- **timing/selection models:** Treynor–Mazuy, Henriksson–Merton, or conditional variants only when assumptions and sample support them;
- **return gap:** compare reported return with the return implied by disclosed holdings to investigate interim trading, costs, and unobserved activity;
- **Davis-style decomposition or transaction replay:** when holdings histories support stock-selection and trading-behavior analysis.

No single attribution model proves skill. Compare holdings-based, return-based, and qualitative evidence.

For a holdings portfolio, a practical Brinson–Fachler decomposition is:

- allocation: `(wP_i - wB_i) * (rB_i - RB)`;
- selection, with interaction absorbed: `wP_i * (rP_i - rB_i)`.

Define all symbols and weight/return conventions in the report. Do not add single-period attribution effects mechanically across time; use a documented linking method such as GRAP and reconcile the linked result with the actual multi-period active return.

Tag every attribution input/result by observation origin as well as evidence type:

- `OBSERVED`: filed holding, audited value, or directly reported return;
- `ESTIMATED`: regression, Kalman/mimicking portfolio, inferred trade, or interpolated exposure;
- `SELF_REPORTED`: manager or sponsor explanation not independently verified.

Never describe a mimicking or Kalman-estimated portfolio as the fund's actual holding. Correlated securities and lagged disclosures can produce multiple near-equivalent solutions.

### Skill-versus-luck questions

- Is alpha concentrated in one regime, factor, name, or decision?
- Does alpha remain after realistic fees, spread, market impact, and taxes?
- Did assets grow beyond the strategy's opportunity set?
- Is the manager's claimed edge visible in holdings and decisions?
- Is the edge dependent on a stale benchmark or hidden option exposure?
- Are unfavorable funds, share classes, or time periods omitted?

### Active Share and concentration

Active Share shows distance from a benchmark, not quality. Interpret it jointly with tracking error, factor exposures, concentration, turnover, capacity, costs, and the stated process.

## 8. Qualitative diligence and historical replay

### Firm

Review ownership, incentives, governance, investment-culture stability, research resources, risk independence, compliance history, product proliferation, and treatment of capacity.

### People

Review actual decision-makers, tenure together, succession, analyst contribution, personal incentives/co-investment where disclosed, and whether the marketed manager truly controls the portfolio.

### Philosophy and process

Demand a falsifiable description of edge, opportunity set, decision rules, portfolio construction, sell discipline, risk controls, and circumstances where the strategy should underperform.

### Portfolio and operations

Review concentration, liquidity, valuation, collateral, counterparties, service providers, securities lending, leverage, cash management, trading controls, and business-continuity arrangements.

### Historical action replay

Select several material buys, sells, allocation shifts, drawdown responses, and missed opportunities. Reconstruct information available at the time. Evaluate:

- consistency between stated philosophy and action;
- willingness to express the claimed edge;
- trading style and implementation quality;
- allocation/position-sizing ability;
- learning behavior after error;
- whether the result was process-consistent even when the outcome was poor.

Outcome bias cuts both ways: a profitable mistake is not evidence of process quality, and a disciplined loss is not automatically process failure.

### Four-dimensional manager ability circle

Where holdings history permits, organize the evidence without collapsing it into one score:

1. **Security selection:** Brinson selection, industry-relative outcomes, long-term core holdings, new buys, hit rate, and factor-adjusted residual.
2. **Industry allocation:** Brinson allocation, active industry weight, stability, and whether concentrated research produced industry-relative value.
3. **Trading:** actual return versus a lagged disclosed-holdings simulation, turnover, price/position-change relationship, and contribution after costs.
4. **Portfolio management:** cash/exposure decisions, concentration, effective breadth, risk budgeting, style stability, and adaptation as AUM changes.

High turnover is not trading skill, high concentration is not conviction, and zero allocation alpha is not necessarily a weakness when the manager explicitly claims bottom-up selection. Judge each dimension against the stated process and intended role.

### Style drift versus ability-circle evolution

Decompose changes into manager trades and passive price movement where data allow. A changing exposure is defensible evolution only when the decision process is explicit and subsequent evidence supports a repeatable improvement; otherwise label it drift. Use rolling style-factor dispersion (including an SDS-type approach) as a diagnostic, not a universal cutoff.

### Interview cross-validation

An interview is not ground truth. Test claims against filings, holdings, transactions, return behavior, team history, and prior statements. Unresolved mismatch reduces confidence.

### Holder experience

Complement point-to-point returns with recovery time, longest underwater period, new-high frequency, rolling holding-period outcomes, random-entry one-year outcome distribution, and monthly/quarterly excess-win stability. These describe the experience the mandate actually had to endure.

## 9. Portfolio construction and sizing

### Marginal-fit analysis

For both the current and proposed portfolio, compare:

- expected and historical factor exposures;
- volatility, drawdown, and stress losses;
- holdings and return correlations;
- concentration by issuer, sector, strategy, counterparty, and liquidity bucket;
- fee, spread, turnover, tax, and operational burden;
- the return source displaced by the funding trade.

Asset-label diversification is not necessarily risk-factor diversification.

### Similarity

Use multiple lenses:

- holdings overlap and active-weight overlap;
- return correlation and downside correlation;
- factor-exposure distance;
- common top contributors and common liquidity risks;
- behavior during stress and rebounds.

High similarity can still be acceptable for tax lots, trading convenience, or implementation redundancy, but it is not new diversification.

### Sizing

Use a range, not a single optimizer output. Constrain by:

- role and risk budget;
- marginal drawdown and concentration;
- liquidity and capacity;
- estimation uncertainty;
- tax and switching cost;
- behavioral ability to hold through the expected bad regime.

Optimization is a decision aid. Test covariance assumptions with a simple long-window baseline, shrinkage or EWMA alternative, and stressed correlations where relevant.

## 10. Pool governance and monitoring

### Pool states

- `BASIC_POOL`: passed minimum eligibility and data checks.
- `PREFERRED_POOL`: research supports a credible, differentiated thesis.
- `INVESTMENT_POOL`: approved for a specified role, mandate, and implementation range.
- `WATCH_POOL`: an exception, missing confirmation, or deterioration requires review.

### Monitoring layers

- **Daily/event:** suspension, premium/discount, spread, leverage, issuer/counterparty, key-person news, regulatory filing.
- **Weekly/monthly:** exposure drift, flows, capacity, tracking, factor and risk movement.
- **Quarterly:** holdings, attribution, thesis milestones, peer/benchmark fit, pool status.
- **Annual/full review:** firm, team, process, operations, fees, tax, alternatives, and IPS fit.

### Downgrade and exit triggers

- thesis or mandate violation;
- manager/team departure or incentive/governance change;
- persistent style/process drift;
- capacity, liquidity, spread, or premium/discount deterioration;
- leverage, collateral, derivative, distribution, index, or tax-structure change;
- unexplained attribution mismatch or data-quality issue;
- portfolio redundancy or a superior replacement after costs;
- risk-budget breach under a predeclared rule.

Do not use a fixed short-term underperformance threshold without diagnosing the source. If underperformance is exactly what the thesis predicted for the regime, it can strengthen rather than weaken process confidence.

## 11. Bias controls and common failure modes

### Research biases

- survivorship and backfill bias;
- look-ahead from holdings publication lag;
- stale or changed benchmark;
- factor mining and multiple comparisons;
- share-class and fee mismatch;
- cherry-picked inception date;
- ignoring distributions, taxes, or price/NAV divergence;
- extrapolating a manager's prior record without checking team and mandate continuity.
- copying a report number without reconciling its table, body text, summary, units, and neighboring fields.

### Decision failures

- ranking before classification;
- confusing beta with alpha;
- equating high distribution with high return;
- equating low expense ratio with low total ownership cost;
- treating ADV as complete ETF liquidity;
- treating a fund name as verified exposure;
- treating interview narrative as evidence without behavior checks;
- using social sentiment as product truth;
- declaring a good standalone fund suitable without portfolio analysis;
- encoding illustrative broker-report weights as permanent universal rules.

The preferred remedy is explicit evidence, an appropriate anchor, a falsifiable thesis, and a documented `UNKNOWN`—not extra decimal places.

One cited report contains a concrete warning: in Zhongtai Securities' 2022-09-14 Jinyuan Shunan Yuanqi attribution report, page 4's table reports annualized alpha of 21.88% and 27.47%, while the body text gives 82.62% and 85.72%; the latter figures align with the table's adjusted R-squared fields. Treat this as a likely field-copy conflict, not as an alpha fact. The skill must always cross-check tables, body text, and summaries before using a number.
