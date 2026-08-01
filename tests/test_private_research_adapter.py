from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from serenity_monitor.fund_monitor import (
    FundMonitorRequest,
    LastCompleted,
    monitor_fund,
)
from serenity_monitor.private_research_adapter import (
    PrivateResearchAdapterError,
    PrivateResearchInput,
    build_private_research_projection,
)
from serenity_monitor.social_heat import (
    EngagementBreakdown,
    SocialObservation,
    analyze_social_heat,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fund(fund_key: str = "DEMO_ETF"):
    return monitor_fund(
        FundMonitorRequest(
            fund_key=fund_key,
            as_of=NOW,
            legal_structure="open_end_fund",
            economic_structure="index_tracking",
            portfolio_role="core_equity",
            last_completed=LastCompleted(NOW, NOW, NOW, NOW),
            sources=(),
            evidence=(),
            metrics=(),
        )
    )


def _social():
    engagement = EngagementBreakdown(
        likes=Decimal("10"),
        comments=Decimal("2"),
        shares=Decimal("1"),
        saves=Decimal("3"),
        views=Decimal("100"),
    )
    observations = [
        SocialObservation(
            platform=platform,
            rights_status="authorized",
            source_health="healthy",
            observed_at=NOW - timedelta(hours=1),
            author_id_hash=_hash(f"author-{platform}"),
            content_id_hash=_hash(f"content-{platform}"),
            topic="semiconductors",
            ticker="DEMO",
            sentiment=sentiment,
            engagement=engagement,
            is_ad=False,
            is_duplicate=False,
            is_coordinated=False,
            cross_platform_cluster_hash=None,
        )
        for platform, sentiment in (
            ("xiaohongshu", Decimal("0.8")),
            ("x", Decimal("0.4")),
        )
    ]
    return analyze_social_heat(observations, as_of=NOW)


def test_projection_is_deterministic_sanitized_and_research_only() -> None:
    social = _social()
    value = PrivateResearchInput(
        as_of=NOW,
        fund_results=(_fund("ZZZ_ETF"), _fund("AAA_ETF")),
        social_heat=social,
    )

    first = build_private_research_projection(value, prepared_at=NOW)
    second = build_private_research_projection(value, prepared_at=NOW)

    assert first == second
    assert [item["fund_key"] for item in first.research["fund_monitoring"]] == [
        "AAA_ETF",
        "ZZZ_ETF",
    ]
    assert [
        (item["platform"], item["topic"])
        for item in first.research["social_attention"]
    ] == [("x", "platform_aggregate"), ("xiaohongshu", "platform_aggregate")]
    assert first.research["market_regime"] == "unknown"
    assert first.research["risk_budget_multiplier"] == Decimal("0")
    assert all(item["research_only"] for item in first.research["social_attention"])
    assert first.can_change_ledger is False
    assert first.can_change_dca is False
    assert first.can_create_trade_action is False
    assert [item["source_id"] for item in first.source_health] == sorted(
        item["source_id"] for item in first.source_health
    )

    serialized = json.dumps(
        {"research": first.research, "source_health": first.source_health},
        ensure_ascii=False,
        default=str,
        sort_keys=True,
    )
    assert social.eligible_input_digest not in serialized
    assert "author_id_hash" not in serialized
    assert "content_id_hash" not in serialized
    assert "http://" not in serialized and "https://" not in serialized
    assert "token" not in serialized.casefold()


def test_fund_partial_not_due_is_not_promoted_to_pass() -> None:
    result = replace(
        _fund(),
        status="NOT_DUE",
        summary_code="fund_monitor.overall.partial_not_due",
        product_quality_status="PASS",
        portfolio_fit_status="NOT_DUE",
    )
    projection = build_private_research_projection(
        PrivateResearchInput(as_of=NOW, fund_results=(result,)),
        prepared_at=NOW,
    )

    row = projection.research["fund_monitoring"][0]
    assert row["status"] == "NOT_DUE"
    assert "fund_monitor.overall.partial_not_due" in row["reason_codes"]
    assert "产品质量=PASS" in row["summary"]
    assert "组合适配=NOT_DUE" in row["summary"]


def test_xiaohongshu_nonzero_execution_weight_is_rejected() -> None:
    social = _social()
    platforms = tuple(
        replace(item, normalized_execution_weight=Decimal("0.1"))
        if item.platform == "xiaohongshu"
        else item
        for item in social.platforms
    )

    with pytest.raises(
        PrivateResearchAdapterError,
        match="xiaohongshu_execution_weight_must_be_zero",
    ):
        PrivateResearchInput(
            as_of=NOW,
            social_heat=replace(social, platforms=platforms),
        )


def test_quarantined_social_contribution_must_be_zero() -> None:
    with pytest.raises(
        PrivateResearchAdapterError,
        match="quarantined_social_heat_must_have_zero_contribution",
    ):
        PrivateResearchInput(
            as_of=NOW,
            social_heat=replace(
                _social(),
                status="quarantined",
                quarantine=True,
                decision_contribution=Decimal("0.001"),
            ),
        )


def test_future_snapshot_and_duplicate_fund_keys_fail_closed() -> None:
    with pytest.raises(
        PrivateResearchAdapterError,
        match="research_snapshot_may_not_be_from_the_future",
    ):
        build_private_research_projection(
            PrivateResearchInput(as_of=NOW + timedelta(seconds=1)),
            prepared_at=NOW,
        )

    fund = _fund()
    with pytest.raises(
        PrivateResearchAdapterError,
        match="fund_results_contains_duplicate_fund_key",
    ):
        PrivateResearchInput(as_of=NOW, fund_results=(fund, fund))


def test_fund_trade_permission_corruption_is_rejected() -> None:
    fund = _fund()
    object.__setattr__(fund, "can_trade", True)

    with pytest.raises(
        PrivateResearchAdapterError,
        match="fund_result_crosses_no_trade_boundary",
    ):
        PrivateResearchInput(as_of=NOW, fund_results=(fund,))


@pytest.mark.parametrize(
    "tampered_key",
    (
        "A" * 33,
        "DEMO_TOKEN",
        "DEMO/ETF",
    ),
)
def test_tampered_fund_key_is_revalidated_before_projection(
    tampered_key: str,
) -> None:
    value = PrivateResearchInput(as_of=NOW, fund_results=(_fund(),))
    object.__setattr__(value.fund_results[0], "fund_key", tampered_key)

    with pytest.raises(
        PrivateResearchAdapterError,
        match="fund_key_must_be_controlled_fund_key",
    ):
        build_private_research_projection(value, prepared_at=NOW)


def test_tampered_secret_shaped_reason_code_is_rejected() -> None:
    fund = _fund()
    object.__setattr__(
        fund.product_quality,
        "reason_codes",
        ("fund_monitor.token.secret",),
    )

    with pytest.raises(
        PrivateResearchAdapterError,
        match="reason_code_must_be_controlled_reason_code",
    ):
        PrivateResearchInput(as_of=NOW, fund_results=(fund,))


def test_aggregate_and_platform_quarantine_must_be_consistent() -> None:
    social = _social()
    forged_platforms = tuple(
        replace(item, quarantine=False) for item in social.platforms
    )

    with pytest.raises(
        PrivateResearchAdapterError,
        match="social_heat_platform_quarantine_inconsistent",
    ):
        PrivateResearchInput(
            as_of=NOW,
            social_heat=replace(
                social,
                status="quarantined",
                quarantine=True,
                decision_contribution=Decimal("0"),
                platforms=forged_platforms,
            ),
        )
