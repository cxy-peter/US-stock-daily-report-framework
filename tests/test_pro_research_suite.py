from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import jsonschema
import numpy as np
import pandas as pd

from serenity_monitor.pro_research.barra import fit_barra_proxy
from serenity_monitor.pro_research.cli import run
from serenity_monitor.pro_research.daily import build_pro_daily_report, render_pro_daily_markdown
from serenity_monitor.pro_research.io import demo_inputs
from serenity_monitor.pro_research.kalman import kalman_dynamic_exposures
from serenity_monitor.pro_research.manager_skill import ManagerFragility, evaluate_manager_skill
from serenity_monitor.pro_research.policy import compute_trump_policy_index
from serenity_monitor.pro_research.polymarket import study_resolved_markets


ROOT = Path(__file__).resolve().parents[1]


def test_trump_policy_index_is_bounded_and_stage_sensitive():
    now = dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc)
    events = [
        {
            "event_id": "official",
            "observed_at": (now - dt.timedelta(days=1)).isoformat(),
            "actor": "donald_trump",
            "source_tier": "signed_official_action",
            "stage": "signed",
            "policy_topic": "trade_tariff",
            "direction": -1,
            "magnitude": 0.8,
            "confidence": 0.9,
            "horizon_days": 90,
            "asset_impacts": {"SPY": 1.0},
        },
        {
            "event_id": "media",
            "observed_at": (now - dt.timedelta(days=1)).isoformat(),
            "actor": "donald_trump",
            "source_tier": "media_analysis",
            "stage": "media_interpretation",
            "policy_topic": "trade_tariff",
            "direction": 1,
            "magnitude": 0.8,
            "confidence": 0.9,
            "horizon_days": 90,
            "asset_impacts": {"SPY": 1.0},
        },
    ]
    result = compute_trump_policy_index(events, as_of=now)
    assert result.composite_score < 0
    assert -0.05 <= result.decision_score_contribution <= 0.05
    assert 0.95 <= result.risk_budget_multiplier <= 1.02
    assert not result.automatic_trading_permitted


def test_polymarket_uses_only_pre_embargo_probability():
    resolved = dt.datetime(2026, 1, 10, 20, tzinfo=dt.timezone.utc)
    prices = [
        {"session": (dt.date(2026, 1, 12) + dt.timedelta(days=index)).isoformat(), "close": 100 + index}
        for index in range(30)
        if (dt.date(2026, 1, 12) + dt.timedelta(days=index)).weekday() < 5
    ]
    event = {
        "market_id": "m1",
        "resolved_at": resolved.isoformat(),
        "outcome": 1,
        "probability_history": [
            {"observed_at": (resolved - dt.timedelta(hours=30)).isoformat(), "probability": 0.40},
            {"observed_at": (resolved + dt.timedelta(minutes=1)).isoformat(), "probability": 1.00},
        ],
        "asset_prices": {"SPY": prices},
    }
    result = study_resolved_markets([event], freeze_hours=24, min_samples=3)
    assert result.impacts[0].frozen_probability == 0.40
    assert result.impacts[0].surprise == 0.60
    assert not result.automatic_trading_permitted


def _factor_fixture(rows: int = 300):
    rng = np.random.default_rng(11)
    index = pd.bdate_range("2025-01-02", periods=rows)
    factors = pd.DataFrame(
        {
            "MARKET": rng.normal(0, 0.01, rows),
            "VALUE": rng.normal(0, 0.006, rows),
            "QUALITY": rng.normal(0, 0.005, rows),
        },
        index=index,
    )
    assets = pd.DataFrame(
        {
            "A": 1.1 * factors["MARKET"] + 0.4 * factors["QUALITY"] + rng.normal(0, 0.004, rows),
            "B": 0.7 * factors["MARKET"] + 0.5 * factors["VALUE"] + rng.normal(0, 0.005, rows),
        },
        index=index,
    )
    return assets, factors


def test_barra_proxy_decomposes_risk_without_commercial_claim():
    assets, factors = _factor_fixture()
    result = fit_barra_proxy(assets, factors, {"A": 0.6, "B": 0.4})
    assert result.status == "ok"
    assert 0 <= result.systematic_risk_share <= 1
    assert 0 <= result.specific_risk_share <= 1
    assert abs(result.systematic_risk_share + result.specific_risk_share - 1) < 1e-6
    assert 0.75 <= result.risk_budget_multiplier <= 1
    assert any("not commercial" in warning.lower() for warning in result.warnings)


def test_kalman_detects_dynamic_market_beta_shift():
    rng = np.random.default_rng(12)
    index = pd.bdate_range("2025-01-02", periods=260)
    market = rng.normal(0, 0.01, len(index))
    beta = np.concatenate([np.full(130, 0.7), np.full(130, 1.4)])
    asset = beta * market + rng.normal(0, 0.004, len(index))
    result = kalman_dynamic_exposures(
        pd.Series(asset, index=index),
        pd.DataFrame({"MARKET": market}, index=index),
        process_variance=5e-4,
    )
    assert result.latest_exposures["MARKET"] > 1.0
    assert result.exposure_changes["MARKET"] > 0.2
    assert not result.automatic_trading_permitted


def test_manager_skill_separates_skill_and_fragility():
    assets, factors = _factor_fixture(360)
    rng = np.random.default_rng(13)
    fund = (
        0.00015
        + 0.9 * factors["MARKET"]
        + 0.25 * factors["QUALITY"]
        + rng.normal(0, 0.003, len(factors))
    )
    result = evaluate_manager_skill(
        fund,
        factors,
        bootstrap_iterations=100,
        fragility=ManagerFragility(
            gross_leverage=3.2,
            top10_concentration=0.85,
            liquidity_days=15,
            prime_broker_concentration=0.9,
            tenure_months=36,
            fund_age_months=36,
        ),
    )
    assert result.skill_score is not None
    assert result.fragility_score > 0.6
    assert not result.copy_trade_allowed


def test_one_daily_report_has_five_non_executing_dca_rows_and_valid_schema():
    demo = demo_inputs(dt.date(2026, 1, 2))
    report = build_pro_daily_report(
        report_date=demo.report_date,
        portfolio_snapshot=demo.portfolio_snapshot,
        asset_returns=demo.asset_returns,
        factor_returns=demo.factor_returns,
        policy_events=demo.policy_events,
        polymarket_events=demo.polymarket_events,
        dca_plan={"A": 20, "B": 20, "C": 20, "D": 20, "E": 20},
        objective_risk_multiplier=demo.objective_risk_multiplier,
        accepted_close_status="healthy",
        social_heat=demo.social_heat,
        prediction_state=demo.prediction_state,
        manager_fund_returns=demo.manager_fund_returns,
        manager_factor_returns=demo.manager_factor_returns,
        manager_fragility=demo.manager_fragility,
        generated_at=dt.datetime(2026, 1, 2, 13, 15, tzinfo=dt.timezone.utc),
    )
    payload = report.to_dict()
    assert len(payload["dca"]) == 5
    assert all(item["configured_daily_usd"] == 20 for item in payload["dca"])
    assert all(item["automatic_execution"] is False for item in payload["dca"])
    assert payload["automatic_trading_permitted"] is False
    schema = json.loads((ROOT / "schemas" / "pro_daily_report.v1.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(payload)
    markdown = render_pro_daily_markdown(report)
    assert "今日结论" in markdown
    assert "Trump Policy Transmission Index" in markdown
    assert "Polymarket" in markdown
    assert "Barra" in markdown
    assert "Kalman" in markdown


def test_cli_demo_generates_one_json_and_one_markdown(tmp_path):
    json_path, markdown_path = run(ROOT / "examples" / "pro_daily_config.example.yaml", tmp_path)
    assert json_path.is_file()
    assert markdown_path.is_file()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "pro_daily_report/v1.0.0"
    assert payload["portfolio_action"] in {"HOLD", "RISK_REBALANCE", "PAUSE_AND_VERIFY"}
    assert not (tmp_path / "state.json").exists()
