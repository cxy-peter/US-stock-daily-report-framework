from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from serenity_monitor.factor_backtest import (
    make_forward_returns,
    walk_forward_factor_backtest,
)
from serenity_monitor.global_market_narratives import score_global_narratives


def test_global_sources_are_bounded_and_transmitted():
    now = dt.datetime(2026, 8, 2, 12, tzinfo=dt.timezone.utc)
    items = [
        {
            "item_id": "aj-1",
            "source": "Al Jazeera",
            "source_kind": "news",
            "title": (
                "Tanker attack closes Strait of Hormuz and disrupts crude oil supply"
            ),
            "published": "2026-08-02T10:00:00Z",
            "url": "https://www.aljazeera.com/news/example",
            "credibility": 0.9,
        },
        {
            "item_id": "skh-1",
            "source": "SK hynix Newsroom",
            "source_kind": "news",
            "title": (
                "SK hynix signs multi-year HBM partnership and expands memory supply"
            ),
            "published": "2026-08-02T09:00:00Z",
            "url": "https://news.skhynix.com/example",
            "credibility": 1.0,
            "is_primary_source": True,
        },
        {
            "item_id": "reddit-1",
            "source": "Reddit r/stocks",
            "source_kind": "community",
            "title": "MU HBM demand looks extremely bullish",
            "published": "2026-08-02T08:00:00Z",
            "url": "https://www.reddit.com/r/stocks/example",
            "credibility": 0.3,
        },
        {
            "item_id": "quora-1",
            "source": "Public web search snippet",
            "source_kind": "kol",
            "title": "Quora: Is Micron overvalued after the HBM rally?",
            "published": "2026-08-02T08:00:00Z",
            "url": "https://www.quora.com/example",
            "credibility": 0.2,
        },
    ]
    result = score_global_narratives(
        items,
        as_of=now,
        portfolio_tickers=["MU", "SMH", "QQQM", "VOO", "SCHD"],
    )

    assert result.status == "healthy"
    assert result.independent_groups >= 3
    assert result.topic_scores["oil_supply"] > 0
    assert result.topic_scores["memory_hbm_demand"] > 0
    assert result.asset_scores["VOO"] < 0
    assert result.asset_scores["MU"] > 0
    assert 0.90 <= result.risk_budget_multiplier <= 1.0
    assert -0.04 <= result.decision_score_contribution <= 0.01
    quora = next(
        item
        for item in result.observations
        if item.event_id.startswith("quora-1")
    )
    assert quora.context_only
    assert quora.weight == 0
    assert not result.automatic_trading_permitted
    assert any("Reddit/community" in warning for warning in result.warnings)


def test_correlated_reposts_are_not_double_counted_and_future_is_excluded():
    now = dt.datetime(2026, 8, 2, 12, tzinfo=dt.timezone.utc)
    base = {
        "source": "Al Jazeera",
        "source_kind": "news",
        "title": "Oil tanker attack disrupts Strait of Hormuz supply",
        "published": "2026-08-02T10:00:00Z",
        "url": "https://www.aljazeera.com/news/example",
        "credibility": 0.9,
    }
    one = score_global_narratives(
        [{**base, "item_id": "one"}],
        as_of=now,
        portfolio_tickers=["VOO"],
    )
    repeated = score_global_narratives(
        [
            {**base, "item_id": "one"},
            {
                **base,
                "item_id": "two",
                "title": "Oil tanker attack halts Hormuz shipping",
            },
            {
                **base,
                "item_id": "future",
                "published": "2026-08-03T10:00:00Z",
            },
        ],
        as_of=now,
        portfolio_tickers=["VOO"],
    )

    assert repeated.topic_scores["oil_supply"] == one.topic_scores["oil_supply"]
    assert all(
        "future" not in item.event_id
        for item in repeated.observations
    )


def test_make_forward_returns_uses_only_future_sessions():
    dates = pd.bdate_range("2026-01-01", periods=6)
    returns = pd.Series(
        [0.01, 0.02, -0.01, 0.03, 0.04, 0.05],
        index=dates,
    )
    forward = make_forward_returns(returns, 2)
    assert forward.iloc[0] == pytest.approx((1.02 * 0.99) - 1.0)
    assert forward.iloc[1] == pytest.approx((0.99 * 1.03) - 1.0)


def test_walk_forward_regression_admits_predictive_factor_and_charges_costs():
    rng = np.random.default_rng(20260802)
    dates = pd.bdate_range("2024-01-01", periods=500)
    predictive = rng.normal(size=len(dates))
    noise = rng.normal(size=len(dates))
    forward = 0.006 * predictive + rng.normal(0, 0.010, len(dates))
    signals = pd.DataFrame(
        {
            "predictive": predictive,
            "noise": noise,
            "constant": np.ones(len(dates)),
        },
        index=dates,
    )
    result = walk_forward_factor_backtest(
        signals,
        forward_returns=pd.Series(forward, index=dates),
        feature_version="global-factor-v1",
        train_size=126,
        test_size=21,
        step_size=21,
        transaction_cost_bps=2.0,
    )

    diagnostics = {
        item.factor: item
        for item in result.factor_diagnostics
    }
    assert result.status == "active"
    assert result.oos_observations >= 300
    assert diagnostics["predictive"].admission_status == "active"
    assert (
        diagnostics["predictive"].directional_information_coefficient
        > 0.20
    )
    assert diagnostics["constant"].admission_status == "blocked"
    assert result.total_cost_drag > 0
    assert result.net_mean_return <= result.gross_mean_return
    assert not result.automatic_trading_permitted
    for fold in result.folds:
        assert pd.Timestamp(fold.train_end) < pd.Timestamp(fold.test_start)


def test_future_target_mutation_does_not_change_earlier_oos_predictions():
    rng = np.random.default_rng(8)
    dates = pd.bdate_range("2024-01-01", periods=360)
    signal = rng.normal(size=len(dates))
    target = pd.Series(
        0.004 * signal + rng.normal(0, 0.01, len(dates)),
        index=dates,
    )
    signals = pd.DataFrame({"signal": signal}, index=dates)
    first = walk_forward_factor_backtest(
        signals,
        forward_returns=target,
        feature_version="v1",
        train_size=100,
        test_size=20,
        step_size=20,
        transaction_cost_bps=1.0,
    )
    mutated = target.copy()
    mutated.iloc[-40:] = mutated.iloc[-40:] + 1.0
    second = walk_forward_factor_backtest(
        signals,
        forward_returns=mutated,
        feature_version="v1",
        train_size=100,
        test_size=20,
        step_size=20,
        transaction_cost_bps=1.0,
    )

    first_fold = [
        row
        for row in first.oos_records
        if row["fold_id"] == "fold-001"
    ]
    second_fold = [
        row
        for row in second.oos_records
        if row["fold_id"] == "fold-001"
    ]
    assert first_fold == second_fold
    assert first.model_version == second.model_version

    version_changed = walk_forward_factor_backtest(
        signals,
        forward_returns=target,
        feature_version="v2",
        train_size=100,
        test_size=20,
        step_size=20,
        transaction_cost_bps=1.0,
    )
    assert version_changed.model_version != first.model_version
