from __future__ import annotations

import copy
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
    PrivateResearchProjection,
    build_private_research_projection,
    validate_private_research_projection,
)
from serenity_monitor.prediction_ledger import PredictionWeightState
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


def _weight_state(state: str, *, platform: str = "x") -> PredictionWeightState:
    return PredictionWeightState(
        platform=platform,
        topic="semiconductors",
        model_version="social-v1",
        market_regime="risk_on",
        horizon=20,
        state=state,
        sample_count=100,
        recent_sample_count=20,
        reasons=("calibration_healthy",) if state == "active" else ("recent_hit_rate_weak",),
    )


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
    assert first.research["social_decision"]["effective_contribution"] == Decimal("0")
    assert first.research["social_decision"]["calibration_state"] == "research_only"
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


@pytest.mark.parametrize(
    "tamper",
    (
        "overall_view",
        "market_regime",
        "risk_budget_multiplier",
        "social_topic",
        "fund_summary",
        "notes",
        "fund_reason",
        "calibration_model",
        "source_detail",
    ),
)
def test_detached_projection_accepts_only_closed_adapter_vocabulary(
    tamper: str,
) -> None:
    projection = build_private_research_projection(
        PrivateResearchInput(
            as_of=NOW,
            fund_results=(_fund(),),
            social_heat=_social(),
            prediction_weight_states=(_weight_state("active"),),
        ),
        prepared_at=NOW,
    )
    research = copy.deepcopy(projection.research)
    source_health = copy.deepcopy(projection.source_health)
    if tamper == "overall_view":
        research["overall_view"] = "Bearer SECRET-TOKEN author=@private https://invalid"
    elif tamper == "market_regime":
        research["market_regime"] = "risk_on"
    elif tamper == "risk_budget_multiplier":
        research["risk_budget_multiplier"] = Decimal("999")
    elif tamper == "social_topic":
        research["social_attention"][0]["topic"] = "semiconductors"
    elif tamper == "fund_summary":
        research["fund_monitoring"][0]["summary"] = "https://invalid/@private"
    elif tamper == "notes":
        research["notes"] = sorted({*research["notes"], "bearer_secret_token"})
    elif tamper == "fund_reason":
        research["fund_monitoring"][0]["reason_codes"] = [
            "sk-liveapikey123456789"
        ]
    elif tamper == "calibration_model":
        research["signal_calibration"][0]["model_version"] = (
            "sk-liveapikey123456789"
        )
    else:
        snapshot_health = next(
            item for item in source_health if item["source_id"] == "research.snapshot"
        )
        snapshot_health["detail_code"] = "sk-liveapikey123456789"

    with pytest.raises(PrivateResearchAdapterError):
        validate_private_research_projection(
            PrivateResearchProjection(research, source_health),
            prepared_at=NOW,
        )


@pytest.mark.parametrize(
    ("state", "multiplier"),
    (
        ("active", Decimal("1")),
        ("decayed", Decimal("0.25")),
        ("quarantined", Decimal("0")),
        ("research_only", Decimal("0")),
    ),
)
def test_prediction_calibration_conservatively_gates_social_candidate_score(
    state: str,
    multiplier: Decimal,
) -> None:
    social = _social()
    projection = build_private_research_projection(
        PrivateResearchInput(
            as_of=NOW,
            social_heat=social,
            prediction_weight_states=(_weight_state(state),),
        ),
        prepared_at=NOW,
    )

    decision = projection.research["social_decision"]
    assert decision["effective_contribution"] == social.decision_contribution * multiplier
    assert decision["calibration_state"] == state
    x_row = next(
        item
        for item in projection.research["social_attention"]
        if item["platform"] == "x"
    )
    assert x_row["effective_execution_weight"] == (
        x_row["candidate_execution_weight"] * multiplier
    )
    assert all(
        item["effective_execution_weight"] == Decimal("0")
        for item in projection.research["social_attention"]
        if item["platform"] == "xiaohongshu"
    )
    assert projection.research["signal_calibration"][0]["state"] == state


def test_persisted_projection_rejects_effective_weight_above_candidate() -> None:
    projection = build_private_research_projection(
        PrivateResearchInput(
            as_of=NOW,
            social_heat=_social(),
            prediction_weight_states=(_weight_state("active"),),
        ),
        prepared_at=NOW,
    )
    research = copy.deepcopy(projection.research)
    x_row = next(
        item for item in research["social_attention"] if item["platform"] == "x"
    )
    x_row["candidate_execution_weight"] = Decimal("0.1")
    x_row["effective_execution_weight"] = Decimal("0.9")

    with pytest.raises(
        PrivateResearchAdapterError,
        match="projection_semantics_invalid",
    ):
        validate_private_research_projection(
            PrivateResearchProjection(research, projection.source_health),
            prepared_at=NOW,
        )


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    (
        ("status", "PASS"),
        ("coverage_ratio", Decimal("2")),
        ("next_due", "2026-08-01T11:59:59Z"),
    ),
)
def test_persisted_projection_rejects_inconsistent_fund_semantics(
    field_name: str,
    bad_value: object,
) -> None:
    projection = build_private_research_projection(
        PrivateResearchInput(as_of=NOW, fund_results=(_fund(),)),
        prepared_at=NOW,
    )
    research = copy.deepcopy(projection.research)
    research["fund_monitoring"][0][field_name] = bad_value

    with pytest.raises(
        PrivateResearchAdapterError,
        match="projection_semantics_invalid",
    ):
        validate_private_research_projection(
            PrivateResearchProjection(research, projection.source_health),
            prepared_at=NOW,
        )


def test_stale_projection_disables_social_score_and_downgrades_fund() -> None:
    projection = build_private_research_projection(
        PrivateResearchInput(
            as_of=NOW,
            fund_results=(_fund(),),
            social_heat=_social(),
            prediction_weight_states=(_weight_state("active"),),
        ),
        prepared_at=NOW,
    )

    stale = validate_private_research_projection(
        projection,
        prepared_at=NOW + timedelta(days=3),
    )

    assert stale.research["fund_monitoring"][0]["status"] == "NEED_INFO"
    assert stale.research["social_decision"]["effective_contribution"] == "0"
    assert stale.research["signal_calibration"][0]["state"] == "research_only"
    assert all(
        item["effective_execution_weight"] == "0"
        for item in stale.research["social_attention"]
    )


def test_stale_social_source_cannot_be_refreshed_by_new_container_time() -> None:
    old_social = replace(_social(), as_of=NOW - timedelta(days=10))
    projection = build_private_research_projection(
        PrivateResearchInput(
            as_of=NOW,
            fund_results=(_fund(),),
            social_heat=old_social,
            prediction_weight_states=(_weight_state("active"),),
        ),
        prepared_at=NOW,
    )

    validated = validate_private_research_projection(
        projection,
        prepared_at=NOW,
    )

    assert validated.research["social_decision"]["effective_contribution"] == "0"
    assert validated.research["social_decision"]["effective_execution_coverage"] == "0"
    assert validated.research["signal_calibration"][0]["state"] == "research_only"
    assert all(
        item["status"] in {"degraded", "quarantined"}
        and item["effective_execution_weight"] == "0"
        for item in validated.research["social_attention"]
    )
    assert "social_research_snapshot_stale_candidate_score_disabled" in (
        validated.research["notes"]
    )
    assert validated.research["fund_monitoring"][0]["status"] == "NOT_DUE"
    fund_health = next(
        item
        for item in validated.source_health
        if item["source_id"] == "research.fund.DEMO_ETF"
    )
    assert fund_health["detail_code"] == "fund_monitor_not_due"


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
    assert row["product_quality_status"] == "PASS"
    assert row["portfolio_fit_status"] == "NOT_DUE"
    assert row["next_due"].endswith("Z")
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
