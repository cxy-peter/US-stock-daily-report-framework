# Academic and Institutional Evidence Map

This document records why a factor exists, what the cited evidence supports,
and what the code is permitted to infer. Publication does not guarantee that a
factor will remain effective. Every live factor still requires point-in-time
validation, costs, coverage, calibration and regime checks.

## Political communications and policy text

### Signal-in-the-noise text selection

- Filippou, Gozluklu, Nguyen and Viswanath-Natraj, *Signal in the Noise: Trump
  Tweets and the Currency Market*, SSRN 3754991.
  - Finding used: the economically informative subset is macro/trade content,
    not all posts.
  - Model implication: extract complete policy claims and classify topics;
    engagement alone cannot be the policy factor.

- Camous and Matveev, *Furor over the Fed: A President's Tweets and Central Bank
  Independence*, CESifo Economic Studies 67(1), 2021.
  - Finding used: monetary-policy messages can move federal-funds expectations
    in the direction communicated by the President.
  - Model implication: `monetary_rates` claims receive an explicit channel to
    rates-sensitive assets, while the Federal Reserve remains an independent
    evidence group.

- Zhang, Frömmel and Baidoo, *Donald Trump's Tweets, Political Value Judgment,
  and the Renminbi Exchange Rate*, SSRN 4606896.
  - Finding used: China-related political value judgments affect offshore FX
    and volatility differently by market regime.
  - Model implication: trade/China claims require asset- and regime-specific
    transmission rather than one global bullish/bearish label.

- Zheng and Lucey, *Make American Markets Gyrate Again*, SSRN 5341208/5637335.
  - Finding used: topic, sentiment and urgency can differentiate market impact
    across sectors.
  - Model implication: retain policy topic, direction, urgency/stage and sector
    exposure as separate fields.

- Aziz, Peng, Rahman and Bhambra, *Linguistic Uncertainty and Market Volatility:
  Evidence from Trump's Tariff War*, SSRN 5517660.
  - Finding used: linguistic uncertainty and media amplification correlate with
    VIX variation.
  - Model implication: independent media uncertainty/disagreement reduces
    policy confidence and may tighten risk, but does not overwrite the original
    statement.

### Boundary

Political communication is event information and uncertainty, not a permanent
risk premium. It is capped, decayed, versioned and settled in the prediction
ledger. Generic praise, insults, campaign repetition and unrelated viral posts
receive little or no policy weight.

## Prediction markets

- Wolfers and Zitzewitz, *Prediction Markets*, Journal of Economic Perspectives
  18(2), 2004; NBER 10504.
  - Finding used: prediction markets can aggregate dispersed information and
    often outperform moderately sophisticated benchmarks.

- Wolfers and Zitzewitz, *Prediction Markets in Theory and Practice*, NBER
  12083, 2006.
  - Finding used: market-generated forecasts are useful, but effectiveness
    depends on market design and application.

- Snowberg, Wolfers and Zitzewitz, *Prediction Markets for Economic
  Forecasting*, NBER 18222, 2012.
  - Finding used: prices can update quickly and can help uncover conditional
    economic beliefs.

- Manski, *Interpreting the Predictions of Prediction Markets*, Economics
  Letters 91(3), 2006; NBER 10359.
  - Finding used: a contract price does not mechanically reveal the full belief
    distribution and only partially identifies central beliefs under simple
    assumptions.

- Wolfers and Zitzewitz, *Interpreting Prediction Market Prices as
  Probabilities*, NBER 12200 / FRBSF 2006-11.
  - Finding used: under broad conditions prices may be close to mean beliefs,
    but can be biased by risk aversion and belief distributions.

### Model implication

Pre-resolution Polymarket prices are treated as noisy aggregate forecasts and
short-term sentiment. Weight depends on spread, depth, liquidity, time to
resolution, resolution source and historical calibration. Resolved-event
studies freeze a pre-resolution probability and never use post-resolution data
as a predictor.

## Volatility, options and risk scaling

- Moreira and Muir, *Volatility-Managed Portfolios*, Journal of Finance 72(4),
  2017; NBER 22208.
  - Finding used: reducing exposure when factor volatility is high improved
    Sharpe ratios in their samples because expected returns did not rise in
    proportion to volatility.
  - Model implication: objective volatility can tighten risk; the implementation
    does not assume it should automatically increase risk after calm periods.

- Cboe VIX methodology and term-structure materials.
  - Institutional basis used: SPX options imply a forward-looking volatility
    term structure across maturities.
  - Model implication: use VIX1D/VIX9D/VIX/VIX3M/VIX6M jointly rather than a
    single VIX threshold.

- Research on the variance risk premium, VVIX and option-implied skew documents
  that volatility-of-volatility, term structure and tail prices carry different
  information from spot VIX.
  - Model implication: VVIX and SKEW are separate tail/convexity groups; they
    cannot be counted as independent confirmation when derived from the same
    SPX option surface without a correlation haircut.

## Overnight and premarket information

Research on the overnight-return anomaly and opening-price discovery finds that
important information can be incorporated outside regular trading hours, with
patterns differing by size, liquidity and prior sell-offs.

Model implication:

- separate close-to-open and open-to-close returns;
- normalize the overnight move against each asset's own historical distribution;
- require futures, volatility and breadth confirmation;
- flag thin-liquidity stretches instead of treating every large premarket move
  as fundamental information;
- do not create a second user-facing report.

## Cross-sectional factors

### Quality

- Asness, Frazzini and Pedersen, *Quality Minus Junk*, Review of Accounting
  Studies 24, 2019; AQR working-paper version.
  - Finding used: profitability, growth, safety and payout-based quality showed
    robust abnormal-return patterns across markets and periods.
  - Model implication: quality is an explanatory/selection factor and a stress
    discriminator, not a guaranteed return forecast.

### Momentum, value, profitability and investment

The public factor proxy can use established market, size, value, momentum,
profitability, investment and quality factors. Factors are retained only when:

- the definition is point-in-time and reproducible;
- exposures are stable enough for the intended horizon;
- multicollinearity and covariance shrinkage are handled;
- predictive residual tests remain out of sample;
- turnover and implementation costs do not erase the signal.

## Dynamic exposure

Kalman filtering is used to estimate time-varying return-implied exposures. It
is useful when a fixed-window beta averages over a changing business or market
regime. The estimate is not a disclosed holding and should be replaced or
quarantined when:

- the state is unstable relative to its standard error;
- the factor model omits a material theme;
- the out-of-sample residual forecast does not improve;
- the result is dominated by a short, unusual sample.

## Manager skill and fund research

The manager module implements established approaches rather than raw return
ranking:

- factor alpha and statistical reliability;
- residual Bootstrap to distinguish skill evidence from chance;
- Treynor-Mazuy and Henriksson-Merton timing specifications;
- up/down capture and rolling persistence;
- separate leverage, concentration, liquidity and funding fragility.

A strong research record can coexist with `copy_trade_allowed=false`.

## Policy uncertainty

- Baker, Bloom and Davis, *Measuring Economic Policy Uncertainty*, Quarterly
  Journal of Economics 131(4), 2016.
  - Finding used: policy uncertainty is associated with higher stock volatility
    and weaker policy-sensitive investment/employment.
  - Model implication: communication disagreement and unresolved policy stage
    are uncertainty features, distinct from the policy's expected directional
    effect.

## Factor admission protocol

An academic or institutional factor enters production only after:

1. an economic transmission hypothesis;
2. point-in-time data availability;
3. a predeclared horizon and target;
4. walk-forward and embargoed validation;
5. costs, liquidity and turnover checks;
6. regime and subperiod stability;
7. multiple-testing control or strong shrinkage;
8. prediction-ledger recording and automatic decay/quarantine.

This protocol deliberately rejects a large indicator library with no coherent
out-of-sample contract.
