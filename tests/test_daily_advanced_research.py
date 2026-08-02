from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from serenity_monitor.daily_advanced_research import (
    build_daily_advanced_research,
    load_private_research_inputs,
    render_buy_side_research_markdown,
)


def _prices() -> pd.DataFrame:
    rng = np.random.default_rng(20260803)
    dates = pd.bdate_range("2021-01-04", periods=1050)
    market = rng.normal(0.0003, 0.009, len(dates))
    semis = 0.90 * market + rng.normal(0.0002, 0.006, len(dates))
    memory = 1.05 * semis + rng.normal(0.00015, 0.007, len(dates))
    oil = 0.35 * market + rng.normal(0.00005, 0.010, len(dates))
    rates = -0.20 * market + rng.normal(0, 0.005, len(dates))
    gold = -0.10 * market + rng.normal(0.00005, 0.005, len(dates))
    dividend = 0.70 * market + rng.normal(0.0001, 0.004, len(dates))
    small = 1.12 * market + rng.normal(0, 0.007, len(dates))
    qqq = 1.10 * market + 0.20 * semis + rng.normal(0, 0.004, len(dates))
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
        "QQQM": qqq + rng.normal(0, 0.0004, len(dates)),
        "VOO": market + rng.normal(0, 0.001, len(dates)),
    }
    return pd.DataFrame(
        {key: 100.0 * np.cumprod(1.0 + value) for key, value in series.items()},
        index=dates,
    )


def _inputs() -> dict:
    return {
        "portfolio_tags": ["ai_semiconductor", "trade_tariff", "monetary_rates"],
        "portfolio_weights": {
            "MU": 0.20,
            "SMH": 0.20,
            "QQQM": 0.20,
            "VOO": 0.20,
            "SCHD": 0.20,
        },
        "active_fund_symbols": ["QQQM"],
        "political_documents": [
            {
                "document_id": "official-1",
                "actor_id": "white_house",
                "observed_at": "2026-08-02T20:00:00Z",
                "source_type": "signed_official_action",
                "title": "Synthetic semiconductor action",
                "body": (
                    "The President signed an executive order that will expand "
                    "support for domestic semiconductor investment this year."
                ),
                "source_url": "https://www.whitehouse.gov/presidential-actions/synthetic",
                "outlet": "The White House",
                "direct_quote": True,
            }
        ],
        "volatility_surface": {
            "vix1d": 17.0,
            "vix9d": 18.0,
            "vix": 19.0,
            "vix3m": 20.0,
            "vix6m": 21.0,
            "vvix": 95.0,
            "skew": 128.0,
            "realized_vol_20d": 16.0,
            "source_health": "healthy",
        },
        "option_chain": {
            "symbol": "SPY",
            "spot": 500.0,
            "days_to_expiry": 30,
            "source_health": "healthy",
            "quotes": [
                {
                    "option_type": "put",
                    "strike": 475.0,
                    "implied_volatility": 0.23,
                    "delta": -0.25,
                    "bid": 4.8,
                    "ask": 5.2,
                    "volume": 200,
                    "open_interest": 1000,
                },
                {
                    "option_type": "call",
                    "strike": 525.0,
                    "implied_volatility": 0.18,
                    "delta": 0.25,
                    "bid": 3.8,
                    "ask": 4.2,
                    "volume": 150,
                    "open_interest": 800,
                },
                {
                    "option_type": "put",
                    "strike": 450.0,
                    "implied_volatility": 0.28,
                    "delta": -0.10,
                    "bid": 2.3,
                    "ask": 2.7,
                    "volume": 100,
                    "open_interest": 600,
                },
                {
                    "option_type": "call",
                    "strike": 500.0,
                    "implied_volatility": 0.20,
                    "delta": 0.50,
                    "bid": 10.0,
                    "ask": 10.5,
                    "volume": 300,
                    "open_interest": 1200,
                },
            ],
        },
        "overnight_snapshots": [
            {
                "symbol": "MU",
                "previous_close": 110.0,
                "premarket_price": 108.0,
                "overnight_high": 110.5,
                "overnight_low": 107.5,
                "historical_mean": 0.0005,
                "historical_std": 0.012,
                "premarket_volume_ratio": 1.2,
                "es_return": -0.004,
                "nq_return": -0.006,
                "rty_return": -0.003,
                "vix_change": 0.05,
                "credit_confirmation": -0.002,
                "source_health": "healthy",
            }
        ],
        "xiaohongshu_views": [
            {
                "platform": "xiaohongshu",
                "observed_at": "2026-08-03T00:00:00Z",
                "ticker": "MU",
                "claim": "HBM demand remains strong but export controls are a risk",
                "direction": 0.3,
                "source_url": "https://example.invalid/xhs/1",
                "origin_urls": [
                    "https://www.whitehouse.gov/presidential-actions/synthetic"
                ],
            }
        ],
    }


def test_advanced_bridge_wires_existing_models_into_one_buy_side_report():
    result = build_daily_advanced_research(
        ["MU", "SMH", "QQQM", "VOO", "SCHD"],
        as_of=dt.datetime(2026, 8, 3, 1, tzinfo=dt.timezone.utc),
        network_enabled=False,
        price_history=_prices(),
        inputs=_inputs(),
    )
    assert result.political_brief.accepted_claim_count >= 1
    assert result.trump_policy.accepted_count >= 1
    assert result.live_polymarket.status == "blocked"
    assert result.volatility_surface.status in {"ok", "partial"}
    assert result.option_tail is not None
    assert result.overnight_risk
    assert result.barra is not None
    assert result.kalman is not None
    assert "strategy_proxy:QQQM" in result.manager_skill
    assert result.theses
    assert 0.40 <= result.effective_risk_budget <= 1.02
    assert not result.automatic_trading_permitted

    markdown = render_buy_side_research_markdown(result)
    assert "## 4. 买方研究结论" in markdown
    assert "共识与差异化判断" in markdown
    assert "反证/退出条件" in markdown
    assert "TPTI" in markdown
    assert "Polymarket" in markdown
    assert "Barra" in markdown
    assert "Kalman" in markdown
    assert "经理/策略 skill" in markdown
    assert "费用与损耗" not in markdown


def test_private_input_loader_reports_missing_optional_channels_explicitly():
    payload, health = load_private_research_inputs(environ={})
    assert payload == {}
    assert health
    assert all(row["status"] == "not_configured" for row in health)
