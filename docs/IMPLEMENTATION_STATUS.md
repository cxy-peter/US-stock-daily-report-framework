# Verified Implementation Status

## Why this note exists

This note separates implemented, tested source code from the larger Pro
roadmap. Documentation and design proposals are not treated as deployed code,
and missing modules are never represented as tested or production-ready.

## Verified base

The public framework starts from a privacy-audited, history-free source
snapshot. Its verified baseline contains the auditable v2.2 pipeline, KOL
credibility and manipulation gates, SEC/X/Reddit collectors, authorized-file
Xiaohongshu context, DCA review and portfolio constraints. No private source
repository commit or branch is part of this provenance.

## Implemented and verified in this baseline

- the complete `evaluate-us-funds` Skill and its quantitative self-test;
- authorized-file `china_retail_attention` research overlay;
- explicit deduplication, freshness, engagement and manipulation controls;
- objective volatility, credit and breadth confirmation groups;
- HXC and USD/CNH context for China/ADR themes;
- downside-only risk-budget integration with mock-data isolation;
- private-runtime JSON audit artifacts and focused regression tests;
- this status record, so later work can distinguish code from proposals.

## Still roadmap, not current implementation

- Trump Policy Transmission Index and White House event lifecycle;
- point-in-time Polymarket event settlement studies;
- VIX1D/VIX9D/VVIX/SKEW and options-chain ingestion;
- overnight futures and premarket anomaly models;
- Barra-inspired exposure/covariance decomposition;
- Kalman-filtered dynamic beta;
- manager-skill bootstrap and timing models;
- Brinson/Carino attribution and asset-allocation optimizer;
- versioned prediction ledger and automatic signal quarantine.

Each roadmap item should be implemented as a separate auditable module with
point-in-time fixtures and focused tests. It must not be marked complete from a
document description alone.
