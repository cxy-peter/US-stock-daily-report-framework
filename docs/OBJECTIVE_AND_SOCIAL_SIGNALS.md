# Objective and Social Signal Policy

This document defines which signals may affect portfolio risk and which signals
are research context only. A high number of inputs is not a substitute for
independence, data lineage or out-of-sample evidence.

## Decision hierarchy

```text
portfolio constraints and thesis hard gates
        |
        +-- objective price/risk groups (downside-only sizing overlay)
        |     +-- volatility: VIX level and VIX/VIX3M term ratio
        |     +-- credit: HYG relative to LQD
        |     +-- breadth: RSP and IWM relative to SPY
        |
        +-- China/ADR cross-asset context
        |     +-- Nasdaq Golden Dragon China Index (HXC)
        |     +-- USD/CNH
        |
        +-- bounded social research group
              +-- Xiaohongshu attention only
              +-- X candidate score
              +-- Reddit candidate score
```

The objective overlay can only reduce the base regime's risk multiplier. It
cannot increase risk, select a security or override liquidity, cash, risk-group,
thesis or evidence gates. A lower portfolio-wide multiplier may produce an
auditable `REBALANCE` research recommendation, but the system never sends a
broker order. Mock inputs are excluded before scoring and have zero decision
impact.

## Objective group weights

The initial configuration is explicit and provisional:

| Group | Composite weight | Portfolio effect |
|---|---:|---|
| Volatility | 45% | Downside-only risk-budget cap |
| Credit | 30% | Downside-only risk-budget cap |
| Breadth | 25% | Downside-only risk-budget cap |

These percentages are weights inside the objective stress score, not portfolio
NAV weights. At least two healthy groups and two groups above the configured
stress threshold are required. The maximum risk-budget reduction is 30%.
Missing or stale groups reduce coverage rather than being imputed.

The initial transforms are intentionally simple and auditable:

- VIX level: 15 or below maps to zero stress and 35 or above to full stress.
- VIX/VIX3M: a ratio of 0.90 maps to zero and 1.10 to full stress.
- Credit: a 21-day HYG-minus-LQD relative return of -3% maps to full stress.
- Breadth: 21-day RSP-minus-SPY and IWM-minus-SPY relative returns are combined.

These transforms must be recalibrated with point-in-time data and walk-forward
tests. They are not presented as proven alpha factors.

## Xiaohongshu / `china_retail_attention`

Xiaohongshu is useful for discovering Chinese consumer attention, product
adoption and narrative changes. It is not a stable, exchange-published index.
The implementation therefore accepts only data the user is entitled to use:

1. an authorized official or commercial export;
2. a user-owned export with documented rights basis; or
3. a licensed vendor feed with provenance and deletion terms.

The project does not automate login, cookies, captchas, mobile APIs, proxy
pools or anti-bot workarounds. If an authorized file is absent, rights metadata
is missing, or data is stale, source health is `blocked` and execution weight is
zero.

The analysis uses hashed record/content/author identifiers, publication and
observation times, theme mapping, freshness, capped engagement and advertising
status. Exact/normalized duplicates count once; engagement is log-scaled and
capped; advertising, repetition bursts and source concentration reduce the
research score. Private-runtime JSON output contains only aggregate/topic
output: record-level hashes, timestamps and engagement are discarded before
persistence. Public CI does not upload or commit report artifacts.

Xiaohongshu, X and Reddit belong to one `social_media` independence group.
They cannot be counted as several independent confirmations of the same story.

`serenity_monitor/social_heat.py` is an offline model boundary, not a
collector. It accepts only authorized, already-sanitized observations with
irreversible author/content identifiers and controlled topic taxonomy IDs. It
does not accept post bodies, account handles, URLs or free-text topics and has
no browser, login, cookie, API-reversal or scraping capability.

The built-in Social Heat taxonomy is closed to `broad_market`,
`crypto_assets`, `dividend_equity`, `nasdaq_100`, `semiconductors` and
`sp_500`. Runtime policy may narrow that list but cannot add arbitrary labels;
the separate ticker field is restricted to a canonical public-symbol shape.

The model calculates author breadth and entropy, independent content count,
30-day baseline growth, capped log engagement, sentiment and disagreement,
topic concentration, advertising/duplicate/coordinated rates,
cross-platform overlap, first-seen time, decay half-life, manipulation risk,
coverage and quarantine state. Missing or unhealthy platforms are omitted;
they are not converted into neutral sentiment.

Attention and candidate execution-score weights are separate. Healthy sources
are re-normalized inside each layer using the provisional priors below:

| Platform | Attention prior | Initial candidate execution eligibility |
|---|---:|---:|
| Xiaohongshu | 40% | 0% |
| X | 35% | Eligible inside the bounded social group |
| Reddit | 15% | Eligible inside the bounded social group |
| Other authorized source | 10% | Eligible inside the bounded social group |

Xiaohongshu is excluded from the execution-weight denominator, so its presence
cannot dilute or amplify X/Reddit execution contribution. It can still change
research attention, crowding flags and investigation priority.

The legacy authorized-file `china_retail_attention` mapper may keep an ignored
private keyword-to-asset mapping without placing the owner's strategy
fingerprint in the framework repository:

```yaml
china_retail_attention:
  topic_rules:
    - topic: private_theme
      keywords: ["authorized keyword"]
      sector: Private research sector
      etfs: [DEMO_ETF]
      tickers: [DEMO_STOCK]
      base_confidence: 0.60
```

Unknown keys, scalar keyword lists and invalid confidence values fail closed.
Before this legacy output enters Social Heat, the private adapter must translate
it to one of the closed generic taxonomy IDs above; private free-text labels do
not cross the model boundary.

### Weight boundary

- Every social output remains `research_only` and cannot independently trigger
  `OPEN`, `ADD`, `TRIM`, `EXIT` or an increased DCA amount.
- Xiaohongshu candidate execution weight is hard-coded to `0`.
- The combined social contribution is hard-capped at 5% of the decision score.
- Advertising, duplicate/coordinated activity, inconsistent cross-platform
  clusters, future observations and missing rights fail closed or quarantine
  the affected source.
- The default manipulation quarantine threshold is 0.60. Runtime policy may
  tighten it but cannot raise it.
- After at least 252 valid trading days and embargoed walk-forward validation,
  the candidate cap is 1%-2% of the model decision score.
- These are model-score weights, never direct portfolio weights.

The current implementation stops at deterministic model output. Authorized
X/Reddit/Xiaohongshu ingestion, private history storage and the adapter into the
ledger-backed daily report remain separate deployment work.

## China/ADR confirmation

HXC and USD/CNH are saved as context for China-related themes. When direct HXC
history is unavailable, KWEB is retained as an explicitly labelled tradable
proxy rather than being presented as the index itself. These readings do not
change the broad-market risk budget and do not validate an unrelated U.S.
security. A Xiaohongshu theme receives China/ADR relevance only when its
documented mapping explicitly links it to Chinese demand, ADRs or cross-border
revenue.

## Required validation before promotion

- Preserve the timestamp when information first became observable; do not
  backfill final engagement counts.
- Test 1-, 5-, 20- and 60-session raw and factor-residual returns.
- Compare the base model against base-plus-overlay in embargoed walk-forward
  tests.
- Report incremental Rank IC, calibration, turnover, trading costs, maximum
  drawdown and stability by market regime.
- Automatically quarantine a signal when rolling efficacy turns negative,
  coverage collapses or the input distribution shifts materially.

`serenity_monitor/prediction_ledger.py` implements the private local research
ledger for these tests. It stores sanitized signal metadata in an append-only
SQLite hash chain, settles against accepted-close lineage, calculates raw and
residual returns, hit rate, MFE, MAE, Brier score and Rank IC, and returns
`active`, `decayed`, `quarantined` or `research_only` weight state. Reversals
are new immutable events. The module has no broker, order or trade interface.
See [Prediction Ledger](PREDICTION_LEDGER.md).

## Preferred primary references

- [Cboe VIX products](https://www.cboe.com/en/tradable-products/vix/)
- [Cboe market statistics and put/call data](https://www.cboe.com/data/mktstat.aspx)
- [Chicago Fed NFCI](https://www.chicagofed.org/research/data/nfci/about)
- [CFTC Commitments of Traders](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm)
- [ICI fund-flow statistics](https://www.ici.org/research/stats/combined_flows)
- [FINRA margin statistics](https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics)
- [Nasdaq Golden Dragon China Index](https://indexes.nasdaq.com/Index/Overview/HXC)
- [Google Trends API](https://developers.google.com/search/apis/trends)
