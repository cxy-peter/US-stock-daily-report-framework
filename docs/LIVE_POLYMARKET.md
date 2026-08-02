# Pre-resolution Polymarket Research

## Why use an unresolved market?

A liquid event market can aggregate dispersed beliefs more quickly than a poll,
news summary or single analyst. Before settlement, changes in the implied
probability may therefore provide a useful short-horizon indication of how
market participants are updating their expectations about a policy or event.

That does not make the displayed price an objective probability. Contract
prices can reflect risk aversion, heterogeneous beliefs, hedging demand,
position limits, liquidity, spread, market design and manipulation. The model
therefore treats unresolved Polymarket data as a **noisy forecast and sentiment
state**, not as a fact.

## Public data inputs

`serenity_monitor/polymarket_live.py` uses public read-only Gamma/CLOB endpoints
for:

- active-market metadata;
- current outcome prices;
- bid/ask spread;
- order-book depth and imbalance;
- historical probability path;
- market volume, liquidity and open interest when provided;
- time to resolution and declared resolution source.

No wallet, authentication, signing or order endpoint is implemented.

## Signal construction

Each market is evaluated with:

```text
current probability relative to baseline
+ 1h / 6h / 24h / 7d probability change
+ probability velocity
+ order-book imbalance
× spread quality
× liquidity/depth quality
× time-to-resolution quality
× historical calibration multiplier
```

Wide spreads, low depth, very near resolution, unclear resolution sources and
poor historical calibration reduce the weight. Markets with insufficient data
remain `research_only` or `blocked`.

## Portfolio interpretation

The unresolved-market group is capped at 3% of the total decision score. It can
support:

- event-risk monitoring;
- research prioritization;
- confirmation that public beliefs changed rapidly;
- modest risk-budget tightening when the event is adverse and other evidence
  confirms the transmission channel.

It cannot independently open, add, reduce or exit a position. Positive live
signals can expand the risk multiplier by at most 1%, while adverse signals may
reduce it by at most 5%.

## Relationship to post-settlement studies

The unresolved signal and the resolved event study are different modules:

```text
live signal
= what the market believed before the event was known

resolved study
= whether the pre-resolution probability and surprise historically explained
  1/5/20/60-session asset returns
```

The live model's future calibration multiplier should be supplied by the
append-only prediction ledger. Post-settlement probabilities must never be
backfilled into the live predictor.

## Evidence base

Prediction-market research finds that well-designed markets can aggregate
information and often outperform conventional benchmarks. Other work cautions
that prices only approximate average beliefs under specific conditions and may
be biased. The code implements both lessons: use probability paths as an
informative state, but condition their weight on microstructure and realized
calibration.

See `docs/ACADEMIC_EVIDENCE_MAP.md` for the research mapping.
