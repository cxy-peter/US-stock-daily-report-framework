# External Agent and Social-View Fallbacks

## Purpose

Use external financial/news agents and owner-downloaded social content to widen
claim discovery when direct Reddit, Quora, X or Xiaohongshu access is unreliable.
The fallback is a provenance and verification pipeline, not a replacement for
primary research.

## Architecture references

The project may borrow role decomposition and digest formats from public
financial-agent projects such as:

- FinRobot;
- TradingAgents and TradingAgents-CN;
- ai-hedge-fund;
- OpenBB and agents-for-openbb.

These repositories are references or secondary digest producers. Their outputs
are not trusted facts and never enter the report without original URLs and
independent verification.

For Xiaohongshu, the preferred route is the owner's own compliant export or
local downloader. The public framework does not contain login bypasses,
cookies, private posts or scraping credentials.

## Private input schema

Private deployment accepts base64 JSON or an owner-local JSON path. A record is:

```json
{
  "platform": "xiaohongshu | reddit | quora | github_agent | broker_research",
  "observed_at": "2026-08-03T08:00:00+08:00",
  "ticker": "MU",
  "topic": "memory_hbm_demand",
  "claim": "The author's exact compact investment claim.",
  "direction": 0.4,
  "horizon_days": 60,
  "author": "optional alias",
  "source_url": "https://original-post-or-report-entry",
  "origin_urls": [
    "https://issuer-or-regulator-source",
    "https://independent-source"
  ],
  "summary_agent": "optional agent/repository/version",
  "engagement": 120,
  "position_disclosed": true,
  "conflict_disclosed": true,
  "sponsored": false,
  "invalidation": "Observable condition that would falsify the view"
}
```

Accepted private channels:

```text
DAILY_RESEARCH_INPUTS_JSON_B64
NEWS_AGENT_DIGEST_JSON_B64
XHS_VIEWS_JSON_B64
BROKER_RESEARCH_DIGEST_JSON_B64

DAILY_RESEARCH_INPUTS_PATH
NEWS_AGENT_DIGEST_PATH
XHS_VIEWS_PATH
BROKER_RESEARCH_DIGEST_PATH
```

Base64 is transport encoding, not encryption. Repository secrets are suitable
for compact sanitized records, not large raw exports. Large exports belong in
an owner-local file outside Git and cloud sync.

## Verification states

| State | Meaning | Decision treatment |
|---|---|---|
| `verified` | primary source or at least two independent institutional groups support the claim | may inform explanation; social/agent direct ADD/OPEN weight still zero |
| `unverified_lead` | source link exists but corroboration is insufficient | context and research queue only |
| `context_only_no_origin` | no original link or evidence trail | do not use as a factual premise |

Agent-generated text is always secondary synthesis. A copied citation list is
not sufficient when the cited page does not support the claim.

## Independent re-verification

The daily bridge compares the claim against:

- issuer, company, regulator or government primary evidence;
- major/institutional media and independent research groups;
- fund/financial-company primary and secondary news;
- political complete-sentence claims;
- point-in-time market and factor observations.

Two articles syndicated from the same original story count as one independence
group. Reddit posts, Xiaohongshu reposts, Quora answers and LLM agents do not
become independent merely because they use different URLs.

## Weight boundaries

- Xiaohongshu, Reddit, Quora and other social platforms: direct `ADD/OPEN`
  weight = 0.
- External agent summaries: direct weight = 0 unless the item is non-social,
  original-source-backed and independently corroborated; even then it is an
  explanatory research input, not a trade trigger.
- Bearish crowding, manipulation or conflict signals may reduce the risk budget
  by at most 5%.
- Missing direct access is displayed as `error`, `blocked`, `not_configured` or
  `context_only`; it is never described as neutral sentiment.

## GitHub agent intake

A useful agent digest should expose:

```text
agent/repository and version
run timestamp and information cutoff
claim and affected ticker/topic
original source URLs
FACT / CALCULATION / INFERENCE / JUDGMENT labels
bull and bear cases
catalysts and horizon
invalidation condition
known missing sources
```

Do not ingest a final `BUY/SELL` label without the underlying claim/evidence
records. The daily system independently recomputes action gates from holdings,
factors, source health and transaction economics.
