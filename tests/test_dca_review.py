from __future__ import annotations

import pandas as pd

from serenity_monitor.data import Quote
from serenity_monitor.dca_review import DcaReviewAction, build_dca_reviews
from serenity_monitor.evidence import EvidenceAssessment
from serenity_monitor.indicators import compute
from serenity_monitor.regime import MarketRegime
from serenity_monitor.rules import ResearchAction, ResearchRecommendation
from serenity_monitor.sizing import (
    PortfolioAction,
    PortfolioSettings,
    PositionPlan,
    build_position_plans,
)


def quote(ticker: str, price: float = 100.0) -> Quote:
    dates = pd.bdate_range("2025-01-01", periods=252)
    closes = pd.Series(
        [price * (1 + 0.0002 * index) for index in range(252)],
        index=dates,
    )
    volumes = pd.Series([1_000_000] * 252, index=dates)
    return Quote(
        ticker=ticker,
        price=float(closes.iloc[-1]),
        market_cap=10e9,
        closes=closes,
        volumes=volumes,
        asset_type="stock",
        source="test",
    )


def recommendation(
    ticker: str,
    action: ResearchAction,
    can_support_add: bool,
) -> ResearchRecommendation:
    market_quote = quote(ticker)
    evidence = EvidenceAssessment(
        ticker=ticker,
        stance="neutral",
        risk_score=50,
        coverage=0.8 if can_support_add else 0.1,
        item_count=2,
        primary_source_present=can_support_add,
        independent_groups=2 if can_support_add else 1,
        can_support_add=can_support_add,
    )
    return ResearchRecommendation(
        ticker=ticker,
        name=ticker,
        action=action,
        evidence=evidence,
        indicators=compute(market_quote.closes, market_quote.volumes),
    )


def plan(ticker: str = "DEMO_STOCK", risk_groups=("demo_risk_group",)) -> PositionPlan:
    return PositionPlan(
        ticker=ticker,
        name=ticker,
        research_action=ResearchAction.HOLD,
        action=PortfolioAction.HOLD,
        current_shares=5,
        current_price=100,
        current_value=500,
        current_weight=0.05,
        target_weight=0.05,
        adjusted_max_weight=0.1,
        model_delta_usd=0,
        executable_delta_usd=0,
        trade_shares=0,
        avg_correlation=0.2,
        volatility_multiplier=1,
        correlation_multiplier=1,
        regime_multiplier=1,
        confidence=70,
        risk_groups=risk_groups,
    )


def recurring_config():
    return {
        "enabled": True,
        "base_amount_usd_per_ticker": 10,
        "tickers": ["DEMO_STOCK"],
        "max_increase_multiple": 2,
    }


def test_single_kol_cannot_increase_dca():
    rec = recommendation("DEMO_STOCK", ResearchAction.ADD, can_support_add=False)
    reviews = build_dca_reviews(
        [plan()],
        {"DEMO_STOCK": rec},
        MarketRegime("neutral", 0.85, 0, ()),
        recurring_config(),
        {"demo_risk_group": 0.10},
        {"demo_risk_group": 0.32},
        1000,
    )
    assert reviews[0].action == DcaReviewAction.HOLD_BASE_NO_INCREASE
    assert reviews[0].proposed_daily_amount_usd == 10


def test_full_evidence_can_only_create_manual_increase_candidate():
    rec = recommendation("DEMO_STOCK", ResearchAction.ADD, can_support_add=True)
    reviews = build_dca_reviews(
        [plan()],
        {"DEMO_STOCK": rec},
        MarketRegime("neutral", 0.85, 0, ()),
        recurring_config(),
        {"demo_risk_group": 0.10},
        {"demo_risk_group": 0.32},
        1000,
    )
    assert reviews[0].action == DcaReviewAction.INCREASE_CANDIDATE
    assert reviews[0].proposed_daily_amount_usd == 20
    assert reviews[0].manual_confirmation_required
    assert not reviews[0].automatic_execution


def test_risk_group_cap_pauses_dca_for_manual_review():
    rec = recommendation("DEMO_STOCK", ResearchAction.HOLD, can_support_add=False)
    reviews = build_dca_reviews(
        [plan()],
        {"DEMO_STOCK": rec},
        MarketRegime("neutral", 0.85, 0, ()),
        recurring_config(),
        {"demo_risk_group": 0.32},
        {"demo_risk_group": 0.32},
        1000,
    )
    assert reviews[0].action == DcaReviewAction.PAUSE_FOR_REVIEW
    assert reviews[0].proposed_daily_amount_usd == 0


def test_tracking_position_is_not_mechanically_rebalanced():
    market_quote = quote("TRACK")
    holding = {
        "ticker": "TRACK",
        "name": "Tracking",
        "shares": 100,
        "tracking_position": True,
        "conviction": "low",
    }
    rec = recommendation("TRACK", ResearchAction.HOLD, can_support_add=False)
    plans, _ = build_position_plans(
        [holding],
        [],
        {"TRACK": market_quote},
        {"TRACK": rec},
        MarketRegime("risk_on", 1.0, 1, ()),
        PortfolioSettings(cash_usd=0, daily_turnover_limit_pct=1),
    )
    assert plans[0].action == PortfolioAction.HOLD
    assert plans[0].model_delta_usd == 0


def test_risk_group_cap_blocks_new_exposure():
    quote_a = quote("A")
    quote_b = quote("B")
    holdings = [
        {
            "ticker": "A",
            "name": "A",
            "shares": 1,
            "conviction": "high",
            "max_weight_pct": 1.0,
            "risk_groups": ["group"],
        },
        {
            "ticker": "B",
            "name": "B",
            "shares": 9,
            "conviction": "high",
            "max_weight_pct": 1.0,
            "risk_groups": ["group"],
        },
    ]
    rec_a = recommendation("A", ResearchAction.ADD, can_support_add=True)
    rec_b = recommendation("B", ResearchAction.HOLD, can_support_add=False)
    plans, _ = build_position_plans(
        holdings,
        [],
        {"A": quote_a, "B": quote_b},
        {"A": rec_a, "B": rec_b},
        MarketRegime("risk_on", 1.0, 1, ()),
        PortfolioSettings(
            cash_usd=0,
            daily_turnover_limit_pct=1,
            risk_group_caps={"group": 0.50},
        ),
    )
    plan_a = next(item for item in plans if item.ticker == "A")
    assert plan_a.action != PortfolioAction.ADD
    assert plan_a.model_delta_usd == 0
    assert any("风险组容量不足" in value for value in plan_a.constraints)
