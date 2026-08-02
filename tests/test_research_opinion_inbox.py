from __future__ import annotations

import datetime as dt

from serenity_monitor.research_opinion_inbox import (
    assess_opinion_inbox,
    parse_opinion_records,
)


def test_social_and_agent_views_require_independent_verification_and_never_add():
    now = dt.datetime(2026, 8, 3, 1, tzinfo=dt.timezone.utc)
    records, rejected = parse_opinion_records(
        [
            {
                "platform": "xiaohongshu",
                "observed_at": "2026-08-03T00:00:00Z",
                "ticker": "MU",
                "claim": "Micron HBM demand remains strong but export controls are a risk",
                "direction": 0.5,
                "source_url": "https://example.invalid/xhs/1",
                "origin_urls": ["https://issuer.example.com/release"],
            },
            {
                "platform": "github_agent",
                "summary_agent": "synthetic-agent/v1",
                "observed_at": "2026-08-03T00:10:00Z",
                "ticker": "SMH",
                "claim": "Semiconductor export controls may tighten",
                "direction": -0.7,
                "source_url": "https://example.invalid/agent/run",
                "origin_urls": ["https://government.example.gov/order"],
            },
            {
                "platform": "quora",
                "observed_at": "2026-08-03T00:20:00Z",
                "ticker": "MU",
                "claim": "Micron valuation is too high",
                "direction": -0.3,
            },
        ],
        as_of=now,
    )
    evidence = [
        {
            "source_class": "company_primary",
            "independence_group": "micron_primary",
            "source": "Micron",
            "url": "https://issuer.example.com/release",
            "title": "Micron says HBM demand remains strong and discusses export controls",
        },
        {
            "source_class": "major_media",
            "independence_group": "wire_a",
            "source": "Wire A",
            "url": "https://wire-a.example.com/story",
            "title": "Semiconductor export controls may tighten for advanced chips",
        },
        {
            "source_class": "independent_research",
            "independence_group": "research_b",
            "source": "Research B",
            "url": "https://research-b.example.com/note",
            "title": "Advanced semiconductor export controls may tighten",
        },
    ]
    result = assess_opinion_inbox(
        records,
        evidence_items=evidence,
        portfolio_tickers=["MU", "SMH"],
        rejected_count=rejected,
    )
    by_platform = {row.platform: row for row in result.assessments}
    assert by_platform["xiaohongshu"].verification_status == "verified"
    assert by_platform["github_agent"].verification_status == "verified"
    assert by_platform["quora"].verification_status == "context_only_no_origin"
    assert all(row.direct_decision_weight == 0 for row in result.assessments)
    assert by_platform["github_agent"].downside_overlay_weight > 0
    assert result.risk_budget_multiplier <= 1.0
    assert not result.automatic_trading_permitted


def test_missing_social_data_is_no_data_not_neutral():
    records, rejected = parse_opinion_records(
        [],
        as_of=dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc),
    )
    result = assess_opinion_inbox(records, rejected_count=rejected)
    assert result.status == "no_data"
    assert result.record_count == 0
    assert any("Missing community data" in warning for warning in result.warnings)
