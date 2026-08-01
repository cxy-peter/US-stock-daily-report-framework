from __future__ import annotations

import hashlib
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest

from serenity_monitor.social_heat import (
    SOCIAL_DECISION_WEIGHT_CAP,
    EngagementBreakdown,
    SocialHeatSettings,
    SocialHeatValidationError,
    SocialObservation,
    SOCIAL_TOPIC_TAXONOMY,
    analyze_social_heat,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
ZERO = Decimal("0")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _engagement(
    *,
    likes: str = "10",
    comments: str = "2",
    shares: str = "1",
    saves: str = "3",
    views: str = "100",
) -> EngagementBreakdown:
    return EngagementBreakdown(
        likes=Decimal(likes),
        comments=Decimal(comments),
        shares=Decimal(shares),
        saves=Decimal(saves),
        views=Decimal(views),
    )


def _observation(
    key: str,
    *,
    platform: str = "x",
    when: datetime | None = None,
    rights: str = "authorized",
    source_health: str = "healthy",
    author: str | None = None,
    topic: str = "semiconductors",
    ticker: str = "DEMO",
    sentiment: str = "0.5",
    engagement: EngagementBreakdown | None = None,
    is_ad: bool = False,
    is_duplicate: bool = False,
    is_coordinated: bool = False,
    cluster: str | None = None,
) -> SocialObservation:
    return SocialObservation(
        platform=platform,
        rights_status=rights,
        source_health=source_health,
        observed_at=when or NOW - timedelta(hours=1),
        author_id_hash=_hash(author or f"author-{key}"),
        content_id_hash=_hash(f"content-{key}"),
        topic=topic,
        ticker=ticker,
        sentiment=Decimal(sentiment),
        engagement=engagement or _engagement(),
        is_ad=is_ad,
        is_duplicate=is_duplicate,
        is_coordinated=is_coordinated,
        cross_platform_cluster_hash=_hash(cluster) if cluster else None,
    )


def _platform(result, name: str):
    return next(item for item in result.platforms if item.platform == name)


def test_healthy_present_sources_are_renormalized_and_missing_is_not_zero_sentiment():
    result = analyze_social_heat(
        [
            _observation("x", platform="x", sentiment="0.8"),
            _observation("reddit", platform="reddit", sentiment="0.2"),
        ],
        as_of=NOW,
    )

    assert result.status == "ok"
    assert result.coverage == Decimal("0.500000000000")
    assert dict(result.platform_weights) == {
        "x": Decimal("0.700000000000"),
        "reddit": Decimal("0.300000000000"),
    }
    assert result.sentiment_mean == Decimal("0.620000000000")
    assert result.sentiment_mean != Decimal("0.31")
    assert result.clean_current_count == 2


def test_ad_duplicate_and_coordinated_records_are_audited_but_isolated_from_score():
    clean = _observation("clean", platform="xiaohongshu", sentiment="1")
    rows = [
        clean,
        _observation("ad", platform="xiaohongshu", sentiment="-1", is_ad=True),
        _observation(
            "duplicate",
            platform="xiaohongshu",
            sentiment="-1",
            is_duplicate=True,
        ),
        _observation(
            "coordinated",
            platform="xiaohongshu",
            sentiment="-1",
            is_coordinated=True,
        ),
    ]
    result = analyze_social_heat(rows, as_of=NOW)
    xhs = _platform(result, "xiaohongshu")

    assert xhs.raw_current_count == 4
    assert xhs.clean_current_count == 1
    assert xhs.ad_rate == Decimal("0.250000000000")
    assert xhs.duplicate_rate == Decimal("0.250000000000")
    assert xhs.coordinated_rate == Decimal("0.250000000000")
    assert xhs.sentiment_mean == Decimal("1.000000000000")
    assert result.sentiment_mean == Decimal("1.000000000000")
    assert not xhs.quarantine


def test_manipulated_platform_is_quarantined_without_poisoning_healthy_source():
    rows = [
        _observation("xhs-ad-1", platform="xiaohongshu", is_ad=True),
        _observation("xhs-ad-2", platform="xiaohongshu", is_ad=True),
        _observation("reddit-clean", platform="reddit", sentiment="0.4"),
    ]
    result = analyze_social_heat(rows, as_of=NOW)
    xhs = _platform(result, "xiaohongshu")
    reddit = _platform(result, "reddit")

    assert xhs.quarantine
    assert xhs.manipulation_risk == Decimal("1.000000000000")
    assert xhs.normalized_attention_weight == Decimal("0")
    assert not reddit.quarantine
    assert reddit.normalized_attention_weight == Decimal("1.000000000000")
    assert result.coverage == Decimal("0.150000000000")
    assert result.sentiment_mean == Decimal("0.400000000000")
    assert result.manipulation_risk > Decimal("0")
    assert result.status == "ok"


def test_relative_thirty_day_baseline_uses_daily_average_and_preserves_first_seen():
    current = _observation("current", when=NOW - timedelta(hours=2))
    baseline = [
        _observation(
            f"baseline-{day}",
            when=NOW - timedelta(days=day, hours=2),
        )
        for day in range(1, 31)
    ]
    result = analyze_social_heat([current, *baseline], as_of=NOW)
    x = _platform(result, "x")

    assert x.baseline_growth_30d == Decimal("0E-12")
    assert result.baseline_growth_30d == Decimal("0E-12")
    assert result.first_seen_at == NOW - timedelta(days=30, hours=2)
    assert result.half_life_hours == Decimal("72")


def test_missing_baseline_is_none_instead_of_fabricated_zero_growth():
    result = analyze_social_heat([_observation("only-current")], as_of=NOW)
    assert result.baseline_growth_30d is None
    assert _platform(result, "x").baseline_growth_30d is None


def test_social_decision_contribution_is_hard_capped_and_cannot_trigger_trades():
    positive_x = analyze_social_heat(
        [_observation("positive-x", platform="x", sentiment="1")], as_of=NOW
    )
    xhs_only = analyze_social_heat(
        [_observation("positive-xhs", platform="xiaohongshu", sentiment="1")],
        as_of=NOW,
    )

    assert ZERO < positive_x.decision_contribution <= SOCIAL_DECISION_WEIGHT_CAP
    assert positive_x.decision_weight_cap == Decimal("0.05")
    assert not positive_x.can_trigger_open
    assert not positive_x.can_trigger_add
    assert not positive_x.can_trigger_trim
    assert not positive_x.can_trigger_exit
    assert not positive_x.can_increase_dca
    assert positive_x.research_only
    assert xhs_only.decision_contribution == Decimal("0E-12")
    assert _platform(xhs_only, "xiaohongshu").execution_multiplier == Decimal("0")
    assert _platform(xhs_only, "xiaohongshu").normalized_execution_weight == Decimal(
        "0"
    )

    with pytest.raises(SocialHeatValidationError, match="0.05"):
        SocialHeatSettings(final_social_decision_weight=Decimal("0.0500001"))
    with pytest.raises(SocialHeatValidationError, match="remain zero"):
        SocialHeatSettings(xhs_execution_weight=Decimal("0.001"))


def test_xhs_attention_never_dilutes_x_or_reddit_execution_weights():
    x_row = _observation("execution-x", platform="x", sentiment="0.8")
    xhs_row = _observation(
        "attention-xhs", platform="xiaohongshu", sentiment="-1"
    )
    reddit_row = _observation("execution-reddit", platform="reddit", sentiment="0.2")

    x_only = analyze_social_heat([x_row], as_of=NOW)
    with_xhs = analyze_social_heat([x_row, xhs_row], as_of=NOW)
    all_three = analyze_social_heat([x_row, xhs_row, reddit_row], as_of=NOW)

    assert with_xhs.decision_contribution == x_only.decision_contribution
    assert _platform(with_xhs, "xiaohongshu").normalized_attention_weight > ZERO
    assert _platform(with_xhs, "xiaohongshu").normalized_execution_weight == ZERO
    assert _platform(with_xhs, "x").normalized_execution_weight == Decimal(
        "1.000000000000"
    )
    assert dict(all_three.execution_platform_weights) == {
        "xiaohongshu": ZERO,
        "x": Decimal("0.700000000000"),
        "reddit": Decimal("0.300000000000"),
    }


def test_empty_unauthorized_and_unhealthy_records_never_enter_score():
    empty = analyze_social_heat([], as_of=NOW)
    assert empty.status == "no_eligible_data"
    assert empty.coverage == Decimal("0E-12")
    assert empty.sentiment_mean is None
    assert empty.platforms == ()

    valid = _observation("valid", sentiment="0.7")
    unknown_rights = _observation("unknown", rights="unknown", sentiment="-1")
    unavailable = _observation(
        "unavailable", source_health="unavailable", sentiment="-1"
    )
    baseline = analyze_social_heat([valid], as_of=NOW)
    result = analyze_social_heat([valid, unknown_rights, unavailable], as_of=NOW)

    assert result.excluded_rights_count == 1
    assert result.excluded_source_health_count == 1
    assert result.authorized_healthy_count == 1
    assert result.eligible_input_digest == baseline.eligible_input_digest
    assert result.attention_score == baseline.attention_score
    assert result.sentiment_mean == baseline.sentiment_mean


def test_future_naive_time_raw_ids_and_inconsistent_clusters_fail_closed():
    with pytest.raises(SocialHeatValidationError, match="future"):
        analyze_social_heat(
            [_observation("future", when=NOW + timedelta(microseconds=1))],
            as_of=NOW,
        )
    with pytest.raises(SocialHeatValidationError, match="timezone-aware"):
        analyze_social_heat([], as_of=datetime(2026, 8, 1, 12, 0))
    with pytest.raises(SocialHeatValidationError, match="irreversible"):
        SocialObservation(
            platform="x",
            rights_status="authorized",
            source_health="healthy",
            observed_at=NOW,
            author_id_hash="raw-handle",
            content_id_hash=_hash("content"),
            topic="semiconductors",
            ticker="DEMO",
            sentiment=Decimal("0"),
            engagement=_engagement(),
            is_ad=False,
            is_duplicate=False,
            is_coordinated=False,
            cross_platform_cluster_hash=None,
        )

    cluster = "same-story"
    with pytest.raises(SocialHeatValidationError, match="inconsistent"):
        analyze_social_heat(
            [
                _observation("one", platform="x", cluster=cluster, ticker="AAA"),
                _observation(
                    "two", platform="reddit", cluster=cluster, ticker="BBB"
                ),
            ],
            as_of=NOW,
        )


@pytest.mark.parametrize(
    "topic",
    [
        "@creator",
        "https://example.test/post",
        "free text",
        "中文主题",
        "UpperCase",
        "topic ",
        "elonmusk",
    ],
)
def test_topic_must_be_a_controlled_lowercase_ascii_taxonomy_id(topic: str):
    with pytest.raises(SocialHeatValidationError, match="taxonomy"):
        _observation("unsafe-topic", topic=topic)


def test_topic_policy_can_only_narrow_the_built_in_taxonomy():
    assert SOCIAL_TOPIC_TAXONOMY == (
        "broad_market",
        "crypto_assets",
        "dividend_equity",
        "nasdaq_100",
        "semiconductors",
        "sp_500",
    )
    settings = SocialHeatSettings(allowed_topics=("sp_500",))
    with pytest.raises(SocialHeatValidationError, match="configured taxonomy"):
        analyze_social_heat(
            [_observation("not-allowed", topic="semiconductors")],
            as_of=NOW,
            settings=settings,
        )
    assert analyze_social_heat(
        [_observation("allowed", topic="sp_500")],
        as_of=NOW,
        settings=settings,
    ).status == "ok"
    with pytest.raises(SocialHeatValidationError, match="closed taxonomy"):
        SocialHeatSettings(allowed_topics=("private_theme",))


@pytest.mark.parametrize("ticker", ["PRIVATE_PATH", "A/B", ".SPY", "A\\B"])
def test_ticker_rejects_path_like_or_noncanonical_values(ticker: str):
    with pytest.raises(SocialHeatValidationError, match="ticker"):
        _observation("unsafe-ticker", ticker=ticker)


def test_quarantine_threshold_may_tighten_but_not_weaken_the_safety_default():
    assert SocialHeatSettings(
        quarantine_threshold=Decimal("0.50")
    ).quarantine_threshold == Decimal("0.50")
    with pytest.raises(SocialHeatValidationError, match="0.60"):
        SocialHeatSettings(quarantine_threshold=Decimal("0.600001"))


def test_trade_policy_result_fields_cannot_be_overridden_during_construction():
    result = analyze_social_heat([_observation("policy")], as_of=NOW)
    policy_fields = {
        item.name: item.init
        for item in fields(result)
        if item.name
        in {
            "research_only",
            "can_trigger_open",
            "can_trigger_add",
            "can_trigger_trim",
            "can_trigger_exit",
            "can_increase_dca",
        }
    }
    assert policy_fields == {
        "research_only": False,
        "can_trigger_open": False,
        "can_trigger_add": False,
        "can_trigger_trim": False,
        "can_trigger_exit": False,
        "can_increase_dca": False,
    }
    with pytest.raises(ValueError, match="init=False"):
        replace(result, can_trigger_add=True)


def test_float_inputs_are_rejected_at_every_numeric_boundary():
    with pytest.raises(SocialHeatValidationError, match="Decimal"):
        EngagementBreakdown(  # type: ignore[arg-type]
            likes=1.0,
            comments=Decimal("0"),
            shares=Decimal("0"),
            saves=Decimal("0"),
            views=Decimal("0"),
        )
    with pytest.raises(SocialHeatValidationError, match="integral Decimal count"):
        EngagementBreakdown(
            likes=Decimal("0.5"),
            comments=Decimal("0"),
            shares=Decimal("0"),
            saves=Decimal("0"),
            views=Decimal("0"),
        )
    with pytest.raises(SocialHeatValidationError, match="sentiment must be Decimal"):
        SocialObservation(  # type: ignore[arg-type]
            platform="x",
            rights_status="authorized",
            source_health="healthy",
            observed_at=NOW,
            author_id_hash=_hash("author"),
            content_id_hash=_hash("content"),
            topic="semiconductors",
            ticker="DEMO",
            sentiment=0.1,
            engagement=_engagement(),
            is_ad=False,
            is_duplicate=False,
            is_coordinated=False,
            cross_platform_cluster_hash=None,
        )
    with pytest.raises(SocialHeatValidationError, match="must be Decimal"):
        SocialHeatSettings(  # type: ignore[arg-type]
            final_social_decision_weight=0.01
        )
    with pytest.raises(SocialHeatValidationError, match="must be Decimal"):
        SocialHeatSettings(  # type: ignore[arg-type]
            platform_priors=(
                ("xiaohongshu", 0.4),
                ("x", Decimal("0.35")),
                ("reddit", Decimal("0.15")),
                ("other", Decimal("0.10")),
            )
        )


def test_metrics_include_entropy_concentration_overlap_and_independent_content():
    rows = [
        _observation(
            "one",
            author="author-one",
            topic="broad_market",
            ticker="AAA",
            engagement=_engagement(likes="10", comments="0", shares="0", saves="0", views="0"),
        ),
        _observation(
            "two",
            author="author-two",
            topic="sp_500",
            ticker="BBB",
            engagement=_engagement(likes="10", comments="0", shares="0", saves="0", views="0"),
        ),
    ]
    result = analyze_social_heat(rows, as_of=NOW)

    assert result.author_count == 2
    assert result.author_entropy == Decimal("1.000000000000")
    assert result.independent_content_count == 2
    assert result.topic_concentration == Decimal("0.500000000000")
    assert len(result.topics) == 2
    assert sum((item.attention_share for item in result.topics), Decimal("0")) == Decimal(
        "1.000000000000"
    )

    overlapped = analyze_social_heat(
        [
            _observation("cross-x", platform="x", cluster="cross-story"),
            _observation("cross-r", platform="reddit", cluster="cross-story"),
        ],
        as_of=NOW,
    )
    assert _platform(overlapped, "x").cross_platform_overlap == Decimal(
        "1.000000000000"
    )
    assert _platform(overlapped, "reddit").cross_platform_overlap == Decimal(
        "1.000000000000"
    )
    assert overlapped.status == "quarantined"


def test_result_is_deterministic_under_input_reordering_and_decimal_context_changes():
    rows = [
        _observation("x", platform="x", sentiment="0.2"),
        _observation("r", platform="reddit", sentiment="-0.1"),
        _observation(
            "old",
            platform="x",
            when=NOW - timedelta(days=5),
            sentiment="0.4",
        ),
    ]
    forward = analyze_social_heat(rows, as_of=NOW)
    with localcontext() as context:
        context.prec = 6
        reverse = analyze_social_heat(reversed(rows), as_of=NOW)

    assert forward == reverse
    assert forward.eligible_input_digest == reverse.eligible_input_digest
