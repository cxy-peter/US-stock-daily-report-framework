from __future__ import annotations

from serenity_monitor.external_views import ExternalSettings, collect_external_views
from serenity_monitor.china_retail_attention import analyze_authorized_records
from serenity_monitor.objective_signals import (
    ObjectiveSignalSettings,
    build_objective_market_snapshot,
)
from serenity_monitor.regime import MarketRegime
from serenity_monitor.report import render_markdown
from serenity_monitor.rules import ResearchAction
from serenity_monitor.sizing import PortfolioAction, PositionPlan


def plan(action=PortfolioAction.HOLD, model_delta=0.0):
    return PositionPlan(
        ticker="TEST",
        name="Test",
        research_action=ResearchAction.HOLD,
        action=action,
        current_shares=10,
        current_price=100,
        current_value=1000,
        current_weight=0.1,
        target_weight=0.1,
        adjusted_max_weight=0.2,
        model_delta_usd=model_delta,
        executable_delta_usd=0.0 if model_delta == 0 else model_delta,
        trade_shares=0.0 if model_delta == 0 else model_delta / 100,
        avg_correlation=0.2,
        volatility_multiplier=1.0,
        correlation_multiplier=1.0,
        regime_multiplier=1.0,
        confidence=70,
        reasons=["论点未破"],
        constraints=[],
    )


def test_missing_x_token_is_reported_as_blocked(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    settings = ExternalSettings.from_dict({
        "news": {"enabled": False},
        "stocktwits": {"enabled": False},
        "reddit": {"enabled": False},
        "x": {
            "enabled": True,
            "discovery_enabled": False,
            "handles": [{"username": "example_researcher"}],
        },
        "sec": {"enabled": False},
        "manual_kol": {"enabled": False},
        "public_web": {"enabled": False},
    })
    bundle = collect_external_views([{"ticker": "TEST", "name": "Test"}], [], settings)
    status = next(s for s in bundle.statuses if s.source == "X KOL")
    assert status.status == "blocked"
    assert "X_BEARER_TOKEN" in status.detail


def test_missing_sec_user_agent_is_reported_as_blocked(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    settings = ExternalSettings.from_dict({
        "news": {"enabled": False},
        "stocktwits": {"enabled": False},
        "reddit": {"enabled": False},
        "x": {"enabled": False, "discovery_enabled": False},
        "sec": {"enabled": True, "user_agent_env": "SEC_USER_AGENT"},
        "manual_kol": {"enabled": False},
        "public_web": {"enabled": False},
        "source_profiles_path": "config/source_profiles.example.yaml",
    })
    bundle = collect_external_views([{"ticker": "TEST", "name": "Test"}], [], settings)
    status = next(s for s in bundle.statuses if s.source == "SEC EDGAR")
    assert status.status == "blocked"
    assert "SEC_USER_AGENT" in status.detail


def test_sec_without_company_targets_does_not_require_user_agent(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    settings = ExternalSettings.from_dict({
        "news": {"enabled": False},
        "stocktwits": {"enabled": False},
        "reddit": {"enabled": False},
        "x": {"enabled": False, "discovery_enabled": False},
        "sec": {"enabled": True, "user_agent_env": "SEC_USER_AGENT"},
        "manual_kol": {"enabled": False},
        "public_web": {"enabled": False},
        "source_profiles_path": "config/source_profiles.example.yaml",
    })
    bundle = collect_external_views(
        [{"ticker": "DEMO_ETF", "name": "Demo ETF", "framework": "core_etf"}],
        [],
        settings,
    )
    status = next(s for s in bundle.statuses if s.source == "SEC EDGAR")
    assert status.status == "ok"
    assert status.detail == "No company-security targets configured"


def test_report_contains_explicit_continue_holding_message():
    settings = ExternalSettings.from_dict({"enabled": False})
    bundle = collect_external_views([{"ticker": "TEST"}], [], settings)
    text = render_markdown(
        [plan()], MarketRegime("neutral", 0.85, 0, ()), bundle, 10_000,
        portfolio_as_of="2026-06-14", cash_known=False,
    )
    assert "今日没有需要调整的持仓：继续持有" in text
    assert "持仓决策表" in text


def test_report_discloses_blocked_xhs_and_missing_objective_sources():
    settings = ExternalSettings.from_dict({"enabled": False})
    bundle = collect_external_views([{"ticker": "TEST"}], [], settings)
    objective = build_objective_market_snapshot({}, ObjectiveSignalSettings())
    xhs = analyze_authorized_records([], None)
    text = render_markdown(
        [plan()],
        MarketRegime("neutral", 0.85, 0, ()),
        bundle,
        10_000,
        objective_snapshot=objective,
        china_retail_attention=xhs,
    )
    assert "客观市场交叉确认" in text
    assert "小红书 / 中国零售注意力" in text
    assert "**blocked**" in text
    assert "执行权重：**0.00%**" in text
