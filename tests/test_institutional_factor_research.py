from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from serenity_monitor.daily_research_enrichment import (
    build_daily_research_enrichment,
    build_research_theses,
)
from serenity_monitor.factor_backtest import walk_forward_factor_backtest
from serenity_monitor.institutional_factor_research import (
    run_institutional_factor_research,
)


def test_walk_forward_purges_forward_label_overlap_and_controls_multiple_tests():
    rng = np.random.default_rng(42)
    dates = pd.bdate_range("2020-01-01", periods=900)
    signal = rng.normal(size=len(dates))
    target = 0.004 * signal + rng.normal(0, 0.01, len(dates))
    signals = pd.DataFrame(
        {
            "signal": signal,
            "noise_1": rng.normal(size=len(dates)),
            "noise_2": rng.normal(size=len(dates)),
        },
        index=dates,
    )
    result = walk_forward_factor_backtest(
        signals,
        forward_returns=pd.Series(target, index=dates),
        feature_version="purged-v1",
        horizon_sessions=5,
        train_size=252,
        test_size=63,
        step_size=63,
        purge_sessions=5,
        embargo_sessions=5,
        transaction_cost_bps=3.0,
    )
    positions = {value.isoformat(): index for index, value in enumerate(dates)}
    for fold in result.folds:
        assert positions[fold.test_start] - positions[fold.train_end] >= 6
        assert fold.purge_sessions == 5
        assert fold.embargo_sessions == 5
    by_factor = {item.factor: item for item in result.factor_diagnostics}
    assert by_factor["signal"].multiple_testing_q_value is not None
    assert by_factor["signal"].robustness_score > by_factor["noise_1"].robustness_score
    assert result.probabilistic_sharpe_ratio is not None


def test_multi_horizon_research_requires_cross_horizon_evidence():
    rng = np.random.default_rng(2026)
    dates = pd.bdate_range("2018-01-01", periods=1800)
    innovations = rng.normal(size=len(dates))
    signal = np.zeros(len(dates))
    for index in range(1, len(dates)):
        signal[index] = 0.94 * signal[index - 1] + 0.35 * innovations[index]
    noise = rng.normal(size=len(dates))
    # A close-t signal predicts t+1 and, through persistence, later daily returns.
    daily_values = np.zeros(len(dates))
    daily_values[1:] = 0.0045 * signal[:-1] + rng.normal(0, 0.003, len(dates) - 1)
    result = run_institutional_factor_research(
        pd.DataFrame({"signal": signal, "noise": noise}, index=dates),
        pd.Series(daily_values, index=dates),
        as_of=dt.date(2026, 8, 2),
        feature_version="institutional-test-v1",
        transaction_cost_bps=2.0,
    )
    diagnostics = {item.factor: item for item in result.factor_diagnostics}
    assert {item.horizon_sessions for item in result.horizon_summaries} == {1, 5, 20}
    assert diagnostics["signal"].admission_status in {"active", "watch"}
    assert len(diagnostics["signal"].active_horizons) + len(diagnostics["signal"].watch_horizons) >= 2
    assert diagnostics["signal"].effective_weight_multiplier > diagnostics["noise"].effective_weight_multiplier
    assert result.model_refit_policy.startswith("daily_append")
    assert not result.automatic_trading_permitted


def _prices() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2021-01-01", periods=1100)
    market = rng.normal(0.0003, 0.009, len(dates))
    semis = 0.9 * market + rng.normal(0.00025, 0.006, len(dates))
    memory = 1.05 * semis + rng.normal(0.0002, 0.007, len(dates))
    data = {
        "SPY": market,
        "IWM": 1.1 * market + rng.normal(0, 0.006, len(dates)),
        "SMH": semis,
        "MU": memory,
        "XLE": 0.4 * market + rng.normal(0, 0.01, len(dates)),
        "USO": 0.3 * market + rng.normal(0, 0.012, len(dates)),
        "TLT": -0.2 * market + rng.normal(0, 0.005, len(dates)),
        "GLD": -0.1 * market + rng.normal(0, 0.006, len(dates)),
        "SCHD": 0.7 * market + rng.normal(0, 0.004, len(dates)),
        "QQQ": 1.1 * market + 0.2 * semis + rng.normal(0, 0.004, len(dates)),
        "QQQM": 1.1 * market + 0.2 * semis + rng.normal(0, 0.004, len(dates)),
        "VOO": market + rng.normal(0, 0.001, len(dates)),
    }
    return pd.DataFrame(
        {key: 100 * np.cumprod(1 + value) for key, value in data.items()},
        index=dates,
    )


def test_daily_enrichment_exposes_institutional_factor_result_and_direct_theses():
    result = build_daily_research_enrichment(
        ["MU", "QQQM", "SMH", "VOO", "SCHD"],
        as_of=dt.datetime(2026, 8, 2, 12, tzinfo=dt.timezone.utc),
        network_enabled=False,
        price_history=_prices(),
    )
    assert result.institutional_factor_research is not None
    assert result.factor_validation is result.institutional_factor_research.primary_result
    theses = build_research_theses(result)
    assert theses
    assert all("论点" not in item for item in theses)
    assert not result.automatic_trading_permitted
