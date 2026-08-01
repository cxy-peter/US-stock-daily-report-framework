# Public Release Manifest

## Delivery identity

- Repository class: history-free public framework
- Delivery label: `v2.3 privacy-audited framework root`

## What is verified in source

- existing v2.2 policy, market-state, portfolio, KOL, SEC, X and Reddit controls;
- an authorized-file Xiaohongshu research overlay that cannot create trades;
- objective volatility, credit and breadth confirmation groups;
- downside-only risk-budget tightening with mock and stale-data isolation;
- Cboe official VIX/VIX3M history plus market-data fallbacks;
- HXC context with an explicitly labelled KWEB ETF fallback;
- private-runtime China retail attention and objective-market JSON audit artifacts;
- raw-close consensus from Twelve Data and Alpha Vantage with an atomic
  accepted-close price gate;
- pinned U.S. exchange-calendar resolution for holidays, DST and early closes;
- an offline append-only confirmed/modeled portfolio ledger with atomic base
  DCA, owner-event corrections, accepted-close valuation and TWR;
- the complete `evaluate-us-funds` Skill and quantitative self-test.

## Verification performed

- Python compile check: passed;
- public-tree privacy check: passed;
- regression suite: passed on Linux public CI;
- fund-research Skill self-test: passed;
- deterministic mock report smoke test: passed;
- no live account report or private runtime input was used for this delivery.

## Safety boundaries

- Xiaohongshu ingestion requires a user-authorized export and a rights
  attestation. Missing authorization is reported as blocked with zero weight.
- Xiaohongshu, X and Reddit share one `social_media` correlation group.
- Social data is research-only and cannot trigger a trade or directly size a
  position.
- The public workflow does not publish reports, summaries or artifacts. Private
  outputs retain aggregate/topic social output only; record-level social
  hashes, timestamps and engagement are removed before persistence.
- Public configuration and tests are synthetic; real portfolio, KOL and source
  configuration paths are gitignored and fail closed when misclassified.
- The initial Xiaohongshu execution weight is zero. A future validated
  candidate is hard-capped at 2% of the decision score, not 2% of NAV.
- Objective signals may only reduce risk after independent confirmation; they
  may never increase risk or create an order.
- No broker execution or unrestricted external-social collection was run.
- Modeled DCA is explicitly not a broker-confirmed execution. One active batch
  is allowed per ledger session, and either book's valuation freezes that and
  all earlier sessions against silent backfill.

## Not implemented and not claimed complete

The larger Pro modules listed in the implementation roadmap are not part of
this verified baseline. Trump/Polymarket event studies, expanded
volatility surfaces, overnight models, Barra-style factor risk, Kalman beta,
manager-skill attribution and the prediction ledger remain roadmap items. See
`docs/IMPLEMENTATION_STATUS.md` for the complete boundary.
