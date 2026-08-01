# U.S. Product-Specific Research Branches

## Contents

1. Universal structure check
2. Passive index ETF
3. Active mutual fund or active ETF
4. Non-transparent or semi-transparent active ETF
5. Fixed-income fund
6. Closed-end fund
7. Covered-call and option-income fund
8. Leveraged or inverse ETP
9. ETN
10. Commodity pool, grantor trust, and futures-based ETP
11. Tax, domicile, and account overlay
12. Trading and liquidity overlay
13. Names Rule and exposure verification
14. Social-media weak signals

This reference reflects sources checked through **2026-08-01**. Verify current rules, filings, product facts, and tax treatment at the analysis date.

Branches are composable, not mutually exclusive. Apply the universal check first, then every applicable vehicle and strategy branch, then tax and trading overlays. If a required current official fact cannot be found, record `UNKNOWN` and normally keep the product at `WATCH` unless a verified hard failure already requires `REJECT`.

## 1. Universal structure check

Before choosing a branch, identify:

- full legal name, ticker/share class, CIK/CUSIP if available;
- issuer, adviser, sub-adviser, custodian, distributor, and index provider;
- Investment Company Act status or other governing structure;
- whether investors own a portfolio interest, an unsecured promise, a trust interest, or a partnership/commodity-pool interest;
- creation/redemption and market-making mechanism;
- leverage, derivatives, collateral, counterparty, valuation, termination, and call provisions;
- expected U.S. tax reporting form and any investor eligibility limits.

FINRA distinguishes ETFs, ETNs, commodity pools, and other ETPs and notes that ETNs are unsecured issuer debt without an underlying portfolio. Use the prospectus, not a data vendor's category, as the structural source of truth: [FINRA ETP guide](https://www.finra.org/investors/investing/investment-products/exchange-traded-funds-and-products).

## 2. Passive index ETF

### Required questions

- What exactly does the index select, weight, rebalance, and delete?
- Is it broad beta, factor, theme, single country/sector, custom index, or disguised active design?
- Does the fund fully replicate, sample, optimize, or use derivatives?
- What are rolling tracking difference and tracking error after fees?
- What explains deviations: fee, tax, cash drag, index turnover, sampling, withholding, derivatives, corporate actions, securities lending, or rebalance execution?
- Are index changes predictable enough to invite front-running?
- How concentrated are issuers, sectors, countries, index providers, and authorized participants?
- What is closure/liquidation risk and the likely tax/transaction impact?

### Trading evidence

Review NAV versus market price, 30-day median spread, premium/discount history, underlying-basket liquidity, creation/redemption basket, market depth, and stress-period behavior. Average daily volume is not sufficient because ETFs have both primary- and secondary-market liquidity.

### Securities lending

Record gross lending revenue, borrower/agent arrangement, agent split, collateral, reinvestment risk, counterparty risk, indemnification, voting-right implications, and net benefit retained by the fund.

### Decision test

Prefer the fund that delivers the desired exposure with the best expected **total implementation outcome**, not automatically the lowest headline expense ratio.

## 3. Active mutual fund or active ETF

### Required questions

- Is the benchmark faithful to the actual opportunity set and risk budget?
- Who makes decisions, and is the team/process continuous across the claimed record?
- Is philosophy visible in holdings, trades, factor exposures, and sell decisions?
- Does alpha survive factors, fees, tax, turnover, capacity, and multiple-testing controls?
- Are Active Share and tracking error consistent with the claimed degree of activeness?
- Did asset growth, opportunity-set crowding, or product proliferation dilute the edge?
- How tax-efficient is implementation in the relevant account?
- Are capital-gains distributions or forced flows material?

### Evidence set

Use prospectus/SAI, N-CSR shareholder reports, N-PORT holdings, manager/team disclosures, audited financials, flows/assets, trading behavior, and N-PX when stewardship matters. The SEC's EDGAR guide describes core fund filings including N-1A/485, 497K, N-CSR, N-PX, and N-PORT: [Using EDGAR to Research Investments](https://www.investor.gov/introduction-investing/getting-started/researching-investments/using-edgar-research-investments).

### Decision test

Require a plausible repeatable edge and a role that cannot be obtained more reliably or cheaply through beta. A high past alpha estimate with weak process evidence belongs on `WATCH`, not automatic `PASS`.

## 4. Non-transparent or semi-transparent active ETF

Confirm the exact exemptive model and disclosure cycle. Review:

- proxy or tracking basket construction;
- information available to authorized participants and market makers;
- historical spread and premium/discount, especially in volatile periods;
- tracking-basket divergence and arbitrage impairment;
- holdings confidentiality benefit versus trading-cost cost;
- any special redemption, dissemination, or disruption provisions.

Do not assume the daily portfolio-transparency rules of a standard Rule 6c-11 ETF apply to every active ETF.

## 5. Fixed-income fund

### Required questions

- effective duration, key-rate duration, convexity, curve exposure;
- yield-to-worst and income versus roll-down/carry assumptions;
- credit quality, spread duration, default/recovery assumptions, downgrade and fallen-angel exposure;
- securitized-product structure, prepayment/extension risk, and liquidity tiers;
- currency and hedge policy;
- derivatives, leverage, cash, and liquidity transformation;
- distribution yield versus portfolio yield and total return;
- benchmark duration/credit comparability.

Bond ETFs do not mature like an individual bond unless specifically designed as target-maturity vehicles. Stress both rates and spreads; historical equity-like calm is not proof of cash-like risk.

## 6. Closed-end fund

Separate four return engines:

1. underlying NAV total return;
2. discount/premium change;
3. leverage benefit or cost;
4. distributions and their economic/tax source.

### Required questions

- current discount/premium versus history and justified NAV quality;
- structural reason a discount might close—or remain permanent;
- leverage type, cost, maturity, covenants, and forced-deleveraging risk;
- distribution policy and source: income, realized gain, or return of capital;
- NAV erosion after distributions;
- rights offerings, tender offers, buybacks, managed distributions, and activist pressure;
- board/adviser incentives, fees on gross versus net assets, and liquidity.

A high distribution rate is not a return forecast. A discount is not free alpha. Use the SEC overview for structural checks: [SEC closed-end fund bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins/investor-bulletin-publicly-traded-closed-end-funds).

## 7. Covered-call and option-income fund

### Strategy decomposition

Record:

- underlying portfolio or synthetic exposure;
- option notional coverage, delta-adjusted coverage, and whether overwrite is systematic or discretionary;
- option/ELN asset value as a separate field from notional or delta coverage;
- index versus single-name options;
- strike/moneyness, maturity, roll schedule, settlement, and tax treatment;
- ELN, swap, or other counterparty exposure;
- distribution policy versus option premium actually earned;
- return of capital classification versus economic NAV erosion.

### Evaluation

Use total return and NAV, not distribution rate. Compare upside participation, downside capture, volatility, drawdown, tax outcome, and results across rising, falling, sideways, high-volatility, and low-volatility regimes. Option premium exchanges some upside for current cash flow; it is not free yield and does not remove downside.

Never infer option notional coverage or delta from the percentage of NAV invested in an ELN. If current terms, notional coverage, or counterparties are incomplete, use `OPTION-COVERAGE-UNVERIFIED` or `COUNTERPARTY-DISCLOSURE-LAG` and state exactly what was disclosed.

Compare with a transparent alternative: underlying equity plus an explicit spending/withdrawal rule, or a separately implemented option overlay.

## 8. Leveraged or inverse ETP

### Required questions

- stated objective period: daily, monthly, or other;
- leverage factor and reset/rebalance mechanism;
- swaps, futures, options, financing rate, collateral, and counterparties;
- path dependence under trend and choppy scenarios;
- gap risk, extreme-day mechanics, closure, and rebalance-market impact;
- realistic holding period and explicit exit/rebalance rule;
- portfolio-level leverage after all holdings, not only product label;
- tax and distribution effects.

### Minimum quantitative evidence

When data permit, report realized daily leverage relative to the stated daily target, daily-target tracking difference/error, explicit fee and estimated financing/derivative drag, maximum drawdown and recovery, and rolling multi-day/month/quarter holding outcomes. Label sponsor backtests or hypothetical tables `SELF_REPORTED` and disclose assumptions.

Run at least these path scenarios:

1. smooth rising trend;
2. smooth falling trend;
3. choppy round trip that leaves the unlevered index near flat;
4. abrupt loss and volatility jump, including the product's disclosed extreme-day mechanics;
5. historical stress and recovery with realistic fees/financing where available.

State each daily path, reset rule, financing assumption, and rebalance assumption so the result is reproducible.

Most geared ETPs target a stated multiple over a specified short period, frequently one day, so longer-horizon results can diverge substantially. Do not summarize this as “decay always” or “long-run return equals leverage times index return.” Model paths. See [FINRA leveraged and inverse ETP guide](https://www.finra.org/investors/insights/lowdown-leveraged-and-inverse-exchange-traded-products).

### Decision test

Approve only for a defined tactical or portfolio-engineering role with exposure limits, monitoring, rebalance rules, stress tests, and an investor who understands the path risk. Never treat a geared ticker as a drop-in core replacement based on a backtest alone.

Monitoring must include target-leverage tolerance, portfolio-level leverage cap, realized financing/tracking drag, volatility and drawdown triggers, derivative/counterparty change, and a contingency for periods when the investor cannot execute the planned rebalance.

## 9. ETN

Treat an ETN as unsecured issuer debt linked to an index or strategy, not as a fund portfolio.

Review:

- issuer and guarantor credit;
- maturity, call/accelerated-redemption provisions, issuance suspension, and indicative value;
- index calculation and embedded fees;
- premium/discount to indicative value and market-making dependence;
- tax characterization;
- liquidity under issuer stress;
- replacement exposure if called or closed.

An attractive index does not compensate for unacceptable issuer, call, or issuance risk.

## 10. Commodity pool, grantor trust, and futures-based ETP

Identify CFTC/SEC status, partnership or trust structure, physical versus futures exposure, collateral, storage/insurance where physical, and tax reporting.

For futures strategies, decompose:

- spot change;
- collateral yield;
- roll yield and term structure;
- contract-selection and roll rule;
- position limits, liquidity, and market impact;
- K-1 or other tax/reporting consequences where relevant.

Do not benchmark futures-product returns solely to spot commodity changes.

## 11. Tax, domicile, and account overlay

Before personalized tax conclusions, confirm:

- tax residency and citizenship/domicile where relevant;
- taxable, retirement, entity, trust, or other account type;
- fund domicile and legal structure;
- W-8BEN and treaty eligibility/status;
- distribution character, withholding, capital-gains distribution, PFIC/K-1 or other reporting issues where applicable;
- estate-tax exposure and situs characterization;
- cost basis and switching consequences.

For distribution-heavy products, use an evidence ladder: prospectus/SAI tax section, final sponsor year-end tax supplement, the investor's actual broker tax form (including Form 1042-S where applicable), then current IRS/treaty guidance. A Section 19(a) notice or sponsor estimate may not be the final tax character. Separate ordinary, qualified, capital-gain, interest-related, return-of-capital, and other categories only when current authoritative evidence supports the distinction.

U.S. mutual funds can distribute taxable capital gains even when an investor has not sold. ETF tax efficiency is not guaranteed. For a nonresident noncitizen, the IRS currently states a general Form 706-NA filing threshold when U.S.-situated assets exceed USD 60,000, while treaties and individual facts can materially alter treatment: [IRS nonresident estate-tax guidance](https://www.irs.gov/individuals/international-taxpayers/some-nonresidents-with-us-assets-must-file-estate-tax-returns).

Treat that threshold as an issue flag, not personal tax advice. Verify the current rule and obtain qualified advice where material.

## 12. Trading and liquidity overlay

### Rule 6c-11 ETF evidence

For eligible Rule 6c-11 ETFs, the SEC guide describes website disclosure of prior-business-day holdings used for NAV, NAV and market price, premium/discount history, extended >2% premium/discount disclosure, and the most recent 30-day median bid-ask spread. Verify the actual fund page and whether the product relies on the rule: [SEC Rule 6c-11 guide](https://www.sec.gov/investment/exchange-traded-funds-small-entity-compliance-guide).

### Execution questions

- Is trade size small relative to displayed depth and expected basket liquidity?
- Are underlying markets open at the same time?
- Is the quote near fair value/NAV and within the mandate's spread guardrail?
- Would limit orders, staged execution, RFQ, or an institutional trading desk reduce risk?
- Could creation/redemption taxes, custom baskets, holidays, halts, or stressed correlations impair arbitrage?
- What is the exit plan during a liquidity shock?

Do not give a universal “never use market orders” rule without context, but explain gap, spread, and fair-value risks.

## 13. Names Rule and exposure verification

The SEC staff's 2025–26 FAQ explains that many names suggesting an investment type, industry, geography, or particular characteristic require an 80% investment policy. The FAQ itself says staff statements are not rules and may be updated. Treat the name and 80% policy as a starting constraint, then inspect the 80% basket, derivatives, remaining assets, and realized exposure: [SEC Names Rule FAQ](https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/2025-26-names-rule-faqs).

## 14. Social-media weak signals

Use social sources only to generate questions and detect common misunderstandings. Label them `SOCIAL_SIGNAL`, preserve date and URL, and verify factual claims elsewhere.

Examples found during framework development:

- Reddit discussions show mutually contradictory explanations of leveraged-ETF “decay,” supporting a mandatory path-scenario test rather than a slogan: [Bogleheads discussion](https://www.reddit.com/r/Bogleheads/comments/1tmn6ik/volatility_decay_and_daily_resetting_in_letfs/).
- High-distribution discussions repeatedly confuse distribution rate, option premium, total return, and return of capital, supporting a mandatory NAV/total-return decomposition: [FEPI discussion](https://www.reddit.com/r/dividends/comments/1coi41w/fepi_etf_with_24_dividend/).

X results available during development were too difficult to verify and were not used for any product fact or decision rule.
