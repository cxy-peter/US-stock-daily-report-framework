from __future__ import annotations

from dataclasses import replace

import pandas as pd

from serenity_monitor.data import Quote
from serenity_monitor.evidence import EvidenceAssessment
from serenity_monitor.indicators import compute
from serenity_monitor.regime import MarketRegime
from serenity_monitor.rules import ResearchAction, ResearchRecommendation
from serenity_monitor.sizing import PortfolioAction, PortfolioSettings, build_position_plans


def quote(ticker: str, price: float = 100.0, seed: int = 0) -> Quote:
    dates = pd.bdate_range("2025-01-01", periods=252)
    closes = pd.Series([price * (1 + 0.0005 * i) for i in range(252)], index=dates)
    volumes = pd.Series([1_000_000 + seed * 1000] * 252, index=dates)
    return Quote(ticker, float(closes.iloc[-1]), 5e9, closes, volumes, asset_type="stock", source="test")


def rec(ticker: str, action: ResearchAction, q: Quote) -> ResearchRecommendation:
    return ResearchRecommendation(
        ticker=ticker,
        name=ticker,
        action=action,
        reasons=["test"],
        confidence=80,
        evidence=EvidenceAssessment(ticker, "neutral", 50, 0.8, 2),
        indicators=compute(q.closes, q.volumes),
        market_cap=q.market_cap,
        asset_type=q.asset_type,
    )


def test_unknown_cash_preserves_model_buy_but_blocks_executable_buy():
    qa, qb = quote("A"), quote("B", 100, 1)
    holdings = [
        {"ticker": "A", "name": "A", "shares": 10, "conviction": "high"},
        {"ticker": "B", "name": "B", "shares": 100, "conviction": "high", "max_weight_pct": 1.0},
    ]
    plans, _ = build_position_plans(
        holdings, [], {"A": qa, "B": qb},
        {"A": rec("A", ResearchAction.ADD, qa), "B": rec("B", ResearchAction.HOLD, qb)},
        MarketRegime("risk_on", 1.0, 3, ()),
        PortfolioSettings(cash_usd=None, daily_turnover_limit_pct=1.0),
    )
    plan = next(p for p in plans if p.ticker == "A")
    assert plan.action == PortfolioAction.ADD
    assert plan.model_delta_usd > 0
    assert plan.executable_delta_usd is None
    assert plan.trade_shares is None


def test_known_cash_constrains_buy():
    q = quote("A")
    holdings = [{"ticker": "A", "name": "A", "shares": 10, "conviction": "high"}]
    settings = PortfolioSettings(
        cash_usd=500.0,
        cash_reserve_pct=0.0,
        daily_turnover_limit_pct=1.0,
        default_max_weights={"high": 0.8, "medium": 0.5, "low": 0.2},
        add_step_weights={"high": 0.5, "medium": 0.2, "low": 0.1},
    )
    plans, _ = build_position_plans(
        holdings, [], {"A": q}, {"A": rec("A", ResearchAction.ADD, q)},
        MarketRegime("risk_on", 1.0, 3, ()), settings,
    )
    plan = plans[0]
    assert plan.executable_delta_usd is not None
    assert 0 <= plan.executable_delta_usd <= 500.0


def test_overweight_hold_becomes_rebalance():
    qa, qb = quote("A", 100), quote("B", 100, 1)
    holdings = [
        {"ticker": "A", "name": "A", "shares": 90, "conviction": "low"},
        {"ticker": "B", "name": "B", "shares": 10, "conviction": "high"},
    ]
    recommendations = {
        "A": rec("A", ResearchAction.HOLD, qa),
        "B": rec("B", ResearchAction.HOLD, qb),
    }
    plans, _ = build_position_plans(
        holdings, [], {"A": qa, "B": qb}, recommendations,
        MarketRegime("risk_on", 1.0, 3, ()),
        PortfolioSettings(cash_usd=None, daily_turnover_limit_pct=1.0),
    )
    plan = next(p for p in plans if p.ticker == "A")
    assert plan.action == PortfolioAction.REBALANCE
    assert plan.model_delta_usd < 0
    assert plan.trade_shares is not None and plan.trade_shares < 0


def test_turnover_limit_scales_discretionary_trades():
    qa, qb = quote("A", 100), quote("B", 100, 1)
    holdings = [
        {"ticker": "A", "name": "A", "shares": 90, "conviction": "low"},
        {"ticker": "B", "name": "B", "shares": 10, "conviction": "high"},
    ]
    recommendations = {
        "A": rec("A", ResearchAction.HOLD, qa),
        "B": rec("B", ResearchAction.ADD, qb),
    }
    settings = PortfolioSettings(
        cash_usd=20_000,
        cash_reserve_pct=0.0,
        daily_turnover_limit_pct=0.01,
    )
    plans, equity = build_position_plans(
        holdings, [], {"A": qa, "B": qb}, recommendations,
        MarketRegime("risk_on", 1.0, 3, ()), settings,
    )
    gross = sum(abs(p.model_delta_usd) for p in plans if p.action in {PortfolioAction.REBALANCE, PortfolioAction.ADD})
    assert gross <= equity * settings.daily_turnover_limit_pct + 1e-6
