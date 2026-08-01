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
        +-- research-only social attention
              +-- Xiaohongshu
              +-- X
              +-- Reddit
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

Public topic rules stop at generic themes and sectors. An ignored private
portfolio configuration may add its own auditable asset mapping without
placing the owner's strategy fingerprint in the framework repository:

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

### Weight boundary

- Current status: `research_only`.
- Current execution weight: `0`.
- It cannot trigger `OPEN`, `ADD`, `TRIM` or `EXIT`.
- After at least 252 valid trading days and embargoed walk-forward validation,
  the candidate cap is 1%-2% of the model decision score.
- If any social signals are later promoted, the entire social-media group must
  remain capped at 5% of the decision score.
- These are model-score weights, never direct portfolio weights.

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
- Test 1-, 5- and 20-day sector/factor-residual returns, not raw returns only.
- Compare the base model against base-plus-overlay in embargoed walk-forward
  tests.
- Report incremental Rank IC, calibration, turnover, trading costs, maximum
  drawdown and stability by market regime.
- Automatically quarantine a signal when rolling efficacy turns negative,
  coverage collapses or the input distribution shifts materially.

## Preferred primary references

- [Cboe VIX products](https://www.cboe.com/en/tradable-products/vix/)
- [Cboe market statistics and put/call data](https://www.cboe.com/data/mktstat.aspx)
- [Chicago Fed NFCI](https://www.chicagofed.org/research/data/nfci/about)
- [CFTC Commitments of Traders](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm)
- [ICI fund-flow statistics](https://www.ici.org/research/stats/combined_flows)
- [FINRA margin statistics](https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics)
- [Nasdaq Golden Dragon China Index](https://indexes.nasdaq.com/Index/Overview/HXC)
- [Google Trends API](https://developers.google.com/search/apis/trends)
