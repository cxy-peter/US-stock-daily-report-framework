from __future__ import annotations

import pandas as pd

from serenity_monitor.data import Quote
from serenity_monitor.objective_signals import (
    ObjectiveSignalSettings,
    apply_objective_overlay,
    build_objective_market_snapshot,
)
from serenity_monitor.regime import MarketRegime


def quote(
    ticker: str,
    start: float,
    end: float,
    *,
    source: str = "test-live",
) -> Quote:
    dates = pd.bdate_range("2026-01-02", periods=63)
    closes = pd.Series(
        [start + (end - start) * index / (len(dates) - 1) for index in range(len(dates))],
        index=dates,
        dtype=float,
    )
    return Quote(
        ticker=ticker,
        price=float(closes.iloc[-1]),
        market_cap=None,
        closes=closes,
        volumes=pd.Series(1_000_000.0, index=dates),
        asset_type="index",
        as_of="2026-03-31",
        source=source,
    )


def high_stress_quotes(source: str = "test-live") -> dict[str, Quote]:
    return {
        "vix": quote("^VIX", 20, 35, source=source),
        "vix3m": quote("^VIX3M", 24, 28, source=source),
        "spy": quote("SPY", 100, 100, source=source),
        "rsp": quote("RSP", 100, 94, source=source),
        "iwm": quote("IWM", 100, 92, source=source),
        "hyg": quote("HYG", 100, 95, source=source),
        "lqd": quote("LQD", 100, 100, source=source),
        "hxc": quote("^HXC", 100, 96, source=source),
        "cnh": quote("CNH=X", 7.0, 7.2, source=source),
    }


def test_independent_objective_groups_can_only_tighten_risk():
    settings = ObjectiveSignalSettings()
    snapshot = build_objective_market_snapshot(high_stress_quotes(), settings)

    assert snapshot.status == "ok"
    assert snapshot.healthy_groups == 3
    assert snapshot.confirming_groups >= 2
    assert snapshot.can_tighten_risk
    assert 0.70 <= snapshot.risk_budget_multiplier < 1.0
    assert not snapshot.can_increase_risk
    assert not snapshot.china_context.can_trigger_trade

    base = MarketRegime("risk_on", 1.0, 2, ("SPY trend is positive.",))
    adjusted = apply_objective_overlay(base, snapshot)
    assert adjusted.risk_multiplier < base.risk_multiplier
    assert adjusted.label in {"neutral", "risk_off"}


def test_missing_groups_are_visible_and_never_change_sizing():
    snapshot = build_objective_market_snapshot(
        {"vix": quote("^VIX", 16, 40)},
        ObjectiveSignalSettings(),
    )
    assert snapshot.status == "partial"
    assert snapshot.healthy_groups == 1
    assert not snapshot.can_tighten_risk
    assert snapshot.risk_budget_multiplier == 1.0

    base = MarketRegime("neutral", 0.85, 0, ())
    assert apply_objective_overlay(base, snapshot) == base


def test_mock_inputs_cannot_leak_into_portfolio_decisions():
    snapshot = build_objective_market_snapshot(
        high_stress_quotes(source="mock"),
        ObjectiveSignalSettings(),
    )
    assert snapshot.status == "mock"
    assert not snapshot.can_tighten_risk
    assert snapshot.risk_budget_multiplier == 1.0


def test_mock_leg_is_excluded_before_relative_group_scoring():
    quotes = high_stress_quotes()
    quotes["lqd"] = quote("LQD", 100, 100, source="mock")
    snapshot = build_objective_market_snapshot(quotes, ObjectiveSignalSettings())
    credit = next(item for item in snapshot.components if item.group == "credit")
    assert snapshot.status == "mixed_mock"
    assert credit.status == "unavailable"
    assert "credit" not in snapshot.group_scores
    assert "lqd" in snapshot.detail


def test_kweb_is_explicitly_labelled_when_hxc_is_unavailable():
    quotes = high_stress_quotes()
    quotes.pop("hxc")
    quotes["kweb"] = quote("KWEB", 100, 105)
    snapshot = build_objective_market_snapshot(quotes, ObjectiveSignalSettings())
    assert snapshot.china_context.china_equity_proxy == "KWEB ETF proxy"
    assert snapshot.china_context.hxc_return_1m is None
    assert snapshot.china_context.china_equity_return_1m is not None


def test_settings_clamp_risk_budget_impact():
    settings = ObjectiveSignalSettings.from_dict(
        {
            "max_risk_budget_reduction": 99,
            "confirmation_threshold": -2,
            "symbols": {"hxc": "KWEB"},
            "providers": {"hxc": "hybrid"},
        }
    )
    assert settings.max_risk_budget_reduction == 0.50
    assert settings.confirmation_threshold == 0.0
    assert settings.symbols["hxc"] == "KWEB"
    assert settings.providers["vix"] == "cboe"
    assert settings.providers["hxc"] == "hybrid"
