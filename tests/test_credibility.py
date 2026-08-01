from serenity_monitor.credibility import (
    Claim, Disclosure, MarketContext, SourceProfile, SourceType, TrackRecord,
    aggregate_gate, assess_opinion, assess_source,
)


def test_unverified_kol_without_track_record_is_context_only():
    profile = SourceProfile(
        source_id="kol", label="KOL", source_type=SourceType.INDEPENDENT_KOL,
        independence_group="kol", identity_verified=False, audited_performance=False,
        position_disclosure=Disclosure.UNKNOWN,
        conflict_disclosure=Disclosure.UNKNOWN,
        leverage_disclosure=Disclosure.UNKNOWN,
    )
    claim = Claim(
        claim_id="1", source_id="kol", ticker="XYZ", text="XYZ will 10x, must buy",
        direction="bullish", horizon_days=None, primary_evidence_count=0,
        position_disclosed=None,
    )
    result = assess_opinion(profile, claim, MarketContext(avg_dollar_volume_20d=3_000_000, ret_1m=0.45, volume_ratio=3.0))
    assert result.research_weight < 0.15
    assert not result.copy_trade_allowed
    assert result.manipulation_risk_score >= 60


def test_small_sample_hit_rate_is_shrunk():
    perfect_tiny = TrackRecord(observations=2, hits=2)
    assert perfect_tiny.shrunk_hit_rate < 0.8
    assert perfect_tiny.sample_reliability < 0.1


def test_credible_manager_can_still_be_unsafe_to_copy_due_to_fragility():
    profile = SourceProfile(
        source_id="fund", label="Fund", source_type=SourceType.SMALL_FUND_MANAGER,
        independence_group="fund", identity_verified=True, audited_performance=True,
        position_disclosure=Disclosure.ALWAYS, conflict_disclosure=Disclosure.ALWAYS,
        leverage_disclosure=Disclosure.ALWAYS,
        track_record=TrackRecord(observations=80, hits=52, brier_score=0.19),
        reported_gross_leverage=3.5, top10_concentration=0.90,
        estimated_liquidity_days=12, prime_broker_concentration=0.8,
    )
    claim = Claim(
        claim_id="2", source_id="fund", ticker="AI", text="AI demand remains strong",
        direction="bullish", horizon_days=365, primary_evidence_count=3,
        invalidation_condition="revenue growth below 20%", position_disclosed=True,
        conflict_disclosed=True,
    )
    result = assess_opinion(profile, claim, MarketContext(avg_dollar_volume_20d=500_000_000))
    assert result.source_score >= 55
    assert result.manager_fragility_score >= 60
    assert not result.copy_trade_allowed
    assert result.can_inform_research


def test_two_independent_groups_plus_primary_can_support_research():
    base = dict(
        source_score=80, claim_score=85, manager_fragility_score=10,
        manipulation_risk_score=5, research_weight=0.45,
        can_inform_research=True, copy_trade_allowed=True,
        red_flags=(), positives=(),
    )
    from serenity_monitor.credibility import CredibilityAssessment
    a = CredibilityAssessment(**base, independence_group="group_a")
    b = CredibilityAssessment(**base, independence_group="group_b")
    gate = aggregate_gate([a, b], primary_source_present=True)
    assert gate.decision == "support"
    assert gate.independent_groups == 2


def test_repeated_accounts_from_same_fund_do_not_count_as_independent():
    from serenity_monitor.credibility import CredibilityAssessment
    base = dict(
        source_score=80, claim_score=80, manager_fragility_score=10,
        manipulation_risk_score=5, research_weight=0.45,
        can_inform_research=True, copy_trade_allowed=True,
        red_flags=(), positives=(), independence_group="same_fund",
    )
    gate = aggregate_gate([CredibilityAssessment(**base), CredibilityAssessment(**base)], primary_source_present=True)
    assert gate.independent_groups == 1
    assert gate.decision != "support"
