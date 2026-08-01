from __future__ import annotations

from datetime import datetime, timezone

import pytest

from serenity_monitor.china_retail_attention import (
    DEFAULT_TOPIC_RULES,
    SOCIAL_MEDIA_INDEPENDENCE_GROUP,
    ChinaRetailAttentionSettings,
    TopicRule,
    UsageAuthorization,
    analyze_authorized_csv,
    analyze_authorized_records,
)


NOW = datetime(2026, 8, 1, 4, 0, tzinfo=timezone.utc)
AUTH = UsageAuthorization(
    has_right_to_use=True,
    basis="user_owned_export",
    declared_by="portfolio owner",
)
RULE = TopicRule(
    topic="hbm_memory",
    keywords=("HBM", "美光"),
    sector="Semiconductors",
    etfs=("DEMO_SECTOR",),
    tickers=("DEMO_STOCK",),
    base_confidence=0.70,
)


def test_public_default_topic_rules_are_asset_agnostic():
    assert all(not rule.etfs and not rule.tickers for rule in DEFAULT_TOPIC_RULES)


def row(
    record_id: str,
    platform: str,
    author: str,
    text: str,
    published_at: str,
    engagement: int = 10,
    **extra,
):
    return {
        "record_id": record_id,
        "platform": platform,
        "author_id": author,
        "text": text,
        "published_at": published_at,
        "engagement": engagement,
        **extra,
    }


def test_missing_file_and_unclear_permission_are_blocked(tmp_path):
    missing = analyze_authorized_csv(tmp_path / "missing.csv", AUTH, now=NOW)
    assert missing.status == "blocked"
    assert missing.execution_weight == 0
    assert missing.research_only
    assert not missing.can_trigger_trade

    unclear = analyze_authorized_records(
        [row("1", "小红书", "a", "HBM", "2026-08-01T03:00:00Z")],
        UsageAuthorization(True, "public", "portfolio owner"),
        now=NOW,
    )
    assert unclear.status == "blocked"
    assert "Permission basis" in unclear.detail
    assert unclear.execution_weight == 0


def test_exact_and_normalized_duplicates_are_removed_before_scoring():
    rows = [
        row("1", "xiaohongshu", "a", "HBM 热度上升", "2026-08-01T03:00:00Z"),
        row("2", "x", "b", "HBM 热度上升", "2026-08-01T03:10:00Z", 20),
        row("3", "reddit", "c", "hbm...热度上升!!!", "2026-08-01T03:20:00Z", 30),
        row("4", "小红书", "d", "美光 HBM 订单观察", "2026-08-01T02:00:00Z"),
    ]
    result = analyze_authorized_records(rows, AUTH, topic_rules=(RULE,), now=NOW)
    assert result.status == "ok"
    assert result.accepted_count == 4
    assert result.exact_duplicate_count == 1
    assert result.normalized_duplicate_count == 1
    assert result.unique_count == 2
    assert result.topics[0].sector == "Semiconductors"
    assert result.topics[0].etfs == ("DEMO_SECTOR",)
    assert result.topics[0].tickers == ("DEMO_STOCK",)
    assert "Matched" in result.topics[0].reason
    assert all(item.record_id not in {"1", "2", "3", "4"} for item in result.records)


def test_time_decay_log_winsor_and_manipulation_penalties_are_auditable():
    rows = [
        row(
            "1", "xiaohongshu", "same", "HBM 广告合作", "2026-08-01T03:50:00Z",
            1_000_000_000, sponsored=True,
        ),
        row("2", "x", "same", "HBM 广告合作", "2026-08-01T03:51:00Z", 5),
        row("3", "reddit", "same", "hbm 广告合作!", "2026-08-01T03:52:00Z", 5),
        row("4", "xiaohongshu", "same", "美光 HBM 旧观察", "2026-07-26T04:00:00Z", 20),
    ]
    settings = ChinaRetailAttentionSettings(
        engagement_winsor_quantile=0.50,
        validation_passed=True,
        execution_weight=0.02,
    )
    result = analyze_authorized_records(
        rows, AUTH, settings=settings, topic_rules=(RULE,), now=NOW
    )
    assert result.engagement_winsor_cap < 1_000_000_000
    assert max(item.capped_engagement for item in result.records) <= result.engagement_winsor_cap
    freshness = sorted(item.freshness_weight for item in result.records)
    assert freshness[0] < freshness[-1]
    assert result.ad_ratio >= 0.75
    assert result.duplicate_burst_score >= 0.75
    assert result.source_concentration > 0.5
    assert result.manipulation_penalty > 0.5
    assert result.execution_weight == 0
    assert any("forced to zero" in warning for warning in result.warnings)
    assert all(not item.can_trigger_trade for item in result.records)


def test_x_reddit_and_xiaohongshu_share_one_group_and_weight_is_model_only():
    rows = [
        row("1", "小红书", "a", "美光 HBM", "2026-08-01T03:00:00Z"),
        row("2", "twitter", "b", "HBM demand", "2026-08-01T02:30:00Z"),
        row("3", "reddit", "c", "HBM cycle", "2026-08-01T02:00:00Z"),
    ]
    settings = ChinaRetailAttentionSettings(
        validation_passed=True,
        execution_weight=0.02,
    )
    result = analyze_authorized_records(
        rows, AUTH, settings=settings, topic_rules=(RULE,), now=NOW
    )
    assert {item.independence_group for item in result.records} == {
        SOCIAL_MEDIA_INDEPENDENCE_GROUP
    }
    assert result.independence_group == SOCIAL_MEDIA_INDEPENDENCE_GROUP
    assert 0 < result.execution_weight <= 0.02
    assert result.weight_semantics == "model_blend_only_not_position_or_order"
    assert all(topic.model_weight_contribution <= 0.02 for topic in result.topics)
    assert result.research_only
    assert not result.can_trigger_trade


def test_unvalidated_or_over_cap_weight_cannot_leak_into_signal():
    unvalidated = analyze_authorized_records(
        [row("1", "x", "a", "HBM", "2026-08-01T03:00:00Z")],
        AUTH,
        settings=ChinaRetailAttentionSettings(execution_weight=0.02),
        topic_rules=(RULE,),
        now=NOW,
    )
    assert unvalidated.execution_weight == 0

    with pytest.raises(ValueError, match="0.02"):
        ChinaRetailAttentionSettings(candidate_weight_cap=0.03)

    with pytest.raises(ValueError, match="candidate_weight_cap"):
        ChinaRetailAttentionSettings(execution_weight=0.021)


def test_authorized_csv_export_is_supported_without_network(tmp_path):
    csv_path = tmp_path / "xhs-export.csv"
    csv_path.write_text(
        "platform,author_id,text,published_at,likes,comments\n"
        "小红书,creator-1,美光 HBM 观察,2026-08-01T03:00:00Z,12,3\n",
        encoding="utf-8",
    )
    result = analyze_authorized_csv(
        csv_path,
        AUTH,
        topic_rules=(RULE,),
        now=NOW,
    )
    assert result.status == "ok"
    assert result.unique_count == 1
    assert result.records[0].raw_engagement == 18
    assert result.records[0].platform == "xiaohongshu"
    assert result.records[0].record_id != "creator-1"
    assert result.records[0].author_hash != "creator-1"
