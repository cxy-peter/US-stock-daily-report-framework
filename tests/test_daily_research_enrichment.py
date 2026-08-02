from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from serenity_monitor.daily_research_enrichment import (
    build_daily_research_enrichment,
    build_factor_dataset,
    render_daily_research_markdown,
    validate_daily_factors,
)


def _synthetic_prices() -> pd.DataFrame:
    rng = np.random.default_rng(20260802)
    dates = pd.bdate_range("2021-01-04", periods=1000)
    market = rng.normal(0.0003, 0.009, len(dates))
    semis = 0.85 * market + rng.normal(0.0002, 0.006, len(dates))
    memory = 1.1 * semis + rng.normal(0.0001, 0.008, len(dates))
    oil = 0.35 * market + rng.normal(0.0001, 0.012, len(dates))
    rates = -0.25 * market + rng.normal(0.00005, 0.006, len(dates))
    gold = -0.10 * market + rng.normal(0.00005, 0.006, len(dates))
    dividend = 0.70 * market + rng.normal(0.0001, 0.005, len(dates))
    small = 1.15 * market + rng.normal(0.0001, 0.008, len(dates))
    qqq = 1.1 * market + 0.25 * semis + rng.normal(0, 0.004, len(dates))
    voo = market + rng.normal(0, 0.0015, len(dates))
    series = {
        "SPY": market,
        "IWM": small,
        "SMH": semis,
        "MU": memory,
        "XLE": oil,
        "USO": oil + rng.normal(0, 0.003, len(dates)),
        "TLT": rates,
        "GLD": gold,
        "SCHD": dividend,
        "QQQ": qqq,
        "QQQM": qqq + rng.normal(0, 0.0005, len(dates)),
        "VOO": voo,
    }
    return pd.DataFrame(
        {key: 100.0 * np.cumprod(1.0 + value) for key, value in series.items()},
        index=dates,
    )


def test_factor_dataset_is_lag_safe_and_has_proxy_coverage():
    prices = _synthetic_prices()
    signals, target = build_factor_dataset(
        prices,
        ["MU", "QQQM", "VOO", "SCHD", "SMH"],
    )
    assert "memory_relative_21" in signals
    assert "oil_relative_21" in signals
    assert "rates_relative_21" in signals
    assert signals.index.equals(target.index)
    expected = prices["SPY"].iloc[21] / prices["SPY"].iloc[0] - 1.0
    assert signals["market_momentum_21"].iloc[21] == expected


def test_daily_factor_validation_produces_versioned_oos_result():
    result = validate_daily_factors(
        _synthetic_prices(),
        ["MU", "QQQM", "VOO", "SCHD", "SMH"],
        transaction_cost_bps=4.0,
    )
    assert result.feature_version == "daily_global_proxy_factors/v2.0.0"
    assert result.oos_observations >= 50
    assert result.purge_sessions == 5
    assert result.embargo_sessions == 5
    assert result.total_cost_drag >= 0
    assert result.model_version.startswith("walk_forward_ridge:")
    assert not result.automatic_trading_permitted


def test_daily_enrichment_runs_without_network_and_preserves_blocked_sources():
    result = build_daily_research_enrichment(
        ["MU", "QQQM", "VOO", "SCHD", "SMH"],
        as_of=dt.datetime(2026, 8, 2, 12, tzinfo=dt.timezone.utc),
        network_enabled=False,
        price_history=_synthetic_prices(),
    )
    assert result.status in {"completed", "partial"}
    assert result.factor_validation is not None
    assert result.institutional_factor_research is not None
    assert result.global_narratives.status == "no_data"
    statuses = {item["source"]: item["status"] for item in result.source_health}
    assert statuses["Reddit"] == "disabled"
    assert statuses["Public Web KOL Discovery"] == "disabled"
    markdown = render_daily_research_markdown(result)
    assert "今日核心论点" in markdown
    assert "因子有效性" in markdown
    assert "事件与跨资产传导" in markdown
    assert "数据源健康与方法边界" in markdown
    assert not result.automatic_trading_permitted
