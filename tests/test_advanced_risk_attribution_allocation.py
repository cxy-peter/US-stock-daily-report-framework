from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal

import numpy as np
import pytest

from serenity_monitor.advanced_market_risk import (
    OptionChainSnapshot,
    OptionQuote,
    OvernightSnapshot,
    VolatilitySurfaceSnapshot,
    evaluate_option_tail_risk,
    evaluate_overnight_risk,
    evaluate_volatility_surface,
)
from serenity_monitor.corporate_action_reconciliation import (
    CorporateActionObservation,
    reconcile_corporate_actions,
)
from serenity_monitor.factor_residual_calibration import (
    ResidualForecastObservation,
    calibrate_factor_residuals,
)
from serenity_monitor.performance_attribution import (
    CarinoPeriodInput,
    brinson_fachler,
    carino_link,
    carino_link_brinson,
)
from serenity_monitor.portfolio_optimizer import (
    AllocationConstraints,
    optimize_allocation,
)
from serenity_monitor.prediction_settlement_scheduler import (
    AcceptedCloseReference,
    FactorResidualReference,
    SignalSettlementState,
    build_settlement_plan,
    execute_settlement_plan,
)


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 2, 13, 0, tzinfo=UTC)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_volatility_surface_uses_term_structure_vvix_and_skew_as_one_group():
    result = evaluate_volatility_surface(
        VolatilitySurfaceSnapshot(
            observed_at=NOW,
            vix1d=42,
            vix9d=36,
            vix=31,
            vix3m=25,
            vix6m=23,
            vvix=145,
            skew=151,
            realized_vol_20d=0.19,
            put_call_volume_ratio=1.45,
            put_call_open_interest_ratio=1.30,
        )
    )
    assert result.status == "ok"
    assert result.regime in {"elevated", "stress"}
    assert result.backwardation_score > 0.5
    assert result.vol_of_vol_score > 0.5
    assert result.tail_skew_score > 0.5
    assert 0.68 <= result.risk_budget_multiplier < 0.90
    assert not result.automatic_trading_permitted


def test_option_chain_tail_risk_applies_liquidity_haircut_and_has_no_execution():
    quotes = (
        OptionQuote("put", 80, 0.72, -0.10, 4.8, 5.2, volume=900, open_interest=2500),
        OptionQuote("put", 90, 0.58, -0.25, 4.2, 4.5, volume=1300, open_interest=4200),
        OptionQuote("put", 100, 0.42, -0.50, 3.8, 4.0, volume=2000, open_interest=5000),
        OptionQuote("call", 100, 0.40, 0.50, 3.7, 3.9, volume=1500, open_interest=4500),
        OptionQuote("call", 110, 0.36, 0.25, 2.9, 3.2, volume=800, open_interest=3000),
    )
    result = evaluate_option_tail_risk(
        OptionChainSnapshot(
            symbol="DEMO",
            observed_at=NOW,
            spot=100,
            days_to_expiry=30,
            quotes=quotes,
            risk_free_rate=0.04,
        )
    )
    assert result.status in {"ok", "partial"}
    assert result.downside_skew is not None and result.downside_skew > 0
    assert result.wing_convexity is not None and result.wing_convexity > 0
    assert result.expected_move is not None and result.expected_move > 0
    assert 0 <= result.tail_risk_score <= 1
    assert 0.78 <= result.risk_budget_multiplier <= 1
    assert not result.automatic_trading_permitted


def test_overnight_model_requires_own_history_and_cross_asset_confirmation():
    result = evaluate_overnight_risk(
        OvernightSnapshot(
            symbol="DEMO",
            observed_at=NOW,
            previous_close=100,
            premarket_price=93,
            overnight_high=98,
            overnight_low=92,
            historical_mean=0.0,
            historical_std=0.02,
            premarket_volume_ratio=1.8,
            es_return=-0.025,
            nq_return=-0.035,
            rty_return=-0.020,
            vix_change=0.18,
            credit_confirmation=-0.015,
        )
    )
    assert result.status == "ok"
    assert result.overnight_z_score is not None and result.overnight_z_score < -3
    assert result.classification == "confirmed_gap_down"
    assert result.risk_budget_multiplier < 1
    assert not result.automatic_trading_permitted


def test_brinson_fachler_and_carino_reconcile_exactly():
    first = brinson_fachler(
        {"Tech": 0.60, "Broad": 0.40},
        {"Tech": 0.40, "Broad": 0.60},
        {"Tech": 0.10, "Broad": 0.02},
        {"Tech": 0.06, "Broad": 0.03},
    )
    second = brinson_fachler(
        {"Tech": 0.50, "Broad": 0.50},
        {"Tech": 0.40, "Broad": 0.60},
        {"Tech": -0.04, "Broad": 0.01},
        {"Tech": -0.02, "Broad": 0.00},
    )
    assert abs(first.active_return - first.allocation - first.selection - first.interaction) < 1e-10
    linked = carino_link_brinson([("p1", first), ("p2", second)])
    assert abs(sum(linked.linked_contributions.values()) - linked.cumulative_active_return) < 1e-9

    generic = carino_link(
        [
            CarinoPeriodInput("a", 0.03, 0.02, {"selection": 0.01}),
            CarinoPeriodInput("b", -0.01, -0.02, {"selection": 0.01}),
        ]
    )
    assert abs(sum(generic.linked_contributions.values()) - generic.cumulative_active_return) < 1e-9


def test_optimizer_respects_position_group_and_turnover_constraints():
    symbols = ("CORE", "TECH_A", "TECH_B", "DEFENSIVE")
    covariance = np.array(
        [
            [0.030, 0.018, 0.017, 0.006],
            [0.018, 0.080, 0.065, 0.004],
            [0.017, 0.065, 0.090, 0.004],
            [0.006, 0.004, 0.004, 0.012],
        ]
    )
    result = optimize_allocation(
        symbols=symbols,
        covariance=covariance,
        current_weights={"CORE": 0.35, "TECH_A": 0.25, "TECH_B": 0.20, "DEFENSIVE": 0.20},
        expected_returns={"CORE": 0.06, "TECH_A": 0.12, "TECH_B": 0.11, "DEFENSIVE": 0.035},
        constraints=AllocationConstraints(
            min_weights={"CORE": 0.25, "DEFENSIVE": 0.15},
            max_weights={"TECH_A": 0.25, "TECH_B": 0.25},
            group_members={"TECH": ("TECH_A", "TECH_B")},
            group_caps={"TECH": 0.40},
            max_turnover=0.12,
        ),
        transaction_cost_bps={"CORE": 5, "TECH_A": 15, "TECH_B": 18, "DEFENSIVE": 4},
        iterations=800,
    )
    proposed = result.proposed_weights
    assert abs(sum(proposed.values()) - 1) < 1e-8
    assert proposed["CORE"] >= 0.25 - 1e-8
    assert proposed["DEFENSIVE"] >= 0.15 - 1e-8
    assert proposed["TECH_A"] + proposed["TECH_B"] <= 0.40 + 1e-8
    assert result.turnover <= 0.12 + 1e-8
    assert not result.automatic_trading_permitted


def test_corporate_action_reconciliation_requires_primary_evidence_and_never_adjusts():
    broker = CorporateActionObservation(
        observation_id="broker-split",
        source_type="broker",
        source_id="ibkr-flex",
        observed_at=NOW,
        symbol="DEMO",
        action_type="split",
        effective_date="2026-08-01",
        ratio_numerator="2",
        ratio_denominator="1",
    )
    issuer = CorporateActionObservation(
        observation_id="issuer-split",
        source_type="issuer",
        source_id="issuer-ir",
        observed_at=NOW - dt.timedelta(hours=1),
        symbol="DEMO",
        action_type="split",
        effective_date="2026-08-01",
        ratio_numerator="2",
        ratio_denominator="1",
    )
    matched = reconcile_corporate_actions([broker], [issuer], as_of=NOW)
    assert matched.status == "MATCHED"
    assert matched.matched_count == 1
    assert not matched.automatic_adjustment_permitted

    conflict = CorporateActionObservation(
        observation_id="issuer-conflict",
        source_type="issuer",
        source_id="issuer-ir-2",
        observed_at=NOW - dt.timedelta(hours=1),
        symbol="DEMO",
        action_type="split",
        effective_date="2026-08-01",
        ratio_numerator="3",
        ratio_denominator="1",
    )
    blocked = reconcile_corporate_actions([broker], [conflict], as_of=NOW)
    assert blocked.status == "SOURCE_CONFLICT"
    assert blocked.issue_count == 1


def _residual_observations(version: str, *, good: bool) -> list[ResidualForecastObservation]:
    rows = []
    for index in range(24):
        predicted = 0.01 + 0.0005 * index
        realized = predicted * (0.9 if good else -0.8)
        rows.append(
            ResidualForecastObservation(
                observation_id=f"{version}-{index}",
                signal_model_version="social-v2",
                factor_model_version=version,
                horizon_sessions=5,
                market_regime="neutral",
                first_observed_at=NOW - dt.timedelta(days=90 - index),
                target_session=(NOW.date() - dt.timedelta(days=60 - index)),
                settled_at=NOW - dt.timedelta(days=30 - index),
                predicted_residual_return=predicted,
                realized_residual_return=realized,
                implementation_cost_return=0.0002,
                direction="bullish",
            )
        )
    return rows


def test_factor_residual_calibration_never_pools_model_versions():
    result = calibrate_factor_residuals(
        [*_residual_observations("barra-v1", good=True), *_residual_observations("barra-v2", good=False)],
        as_of=NOW,
        minimum_samples=20,
        recent_window=10,
        minimum_recent_samples=5,
    )
    assert len(result.summaries) == 2
    states = {item.factor_model_version: item.state for item in result.summaries}
    assert states["barra-v1"] == "active"
    assert states["barra-v2"] == "quarantined"
    assert not result.pooled_across_factor_versions
    assert not result.automatic_trading_permitted


def _signal() -> SignalSettlementState:
    start = dt.date(2026, 1, 2)
    path = {offset: start + dt.timedelta(days=offset) for offset in range(1, 61)}
    return SignalSettlementState(
        signal_id="signal-1",
        valuation_symbol="SPY",
        observation_session=start,
        first_observed_at=dt.datetime(2026, 1, 2, 23, 0, tzinfo=UTC),
        session_path=path,
        signal_model_version="social-v2",
        factor_model_version="barra-v2",
        settled_horizons=(1,),
        require_factor_residual=True,
    )


def test_prediction_settlement_scheduler_requires_complete_close_path_and_exact_factor_version():
    signal = _signal()
    closes = [
        AcceptedCloseReference(
            symbol="SPY",
            session=signal.session_path[offset],
            accepted_close_id=_sha(f"close-{offset}"),
            accepted_at=dt.datetime.combine(
                signal.session_path[offset], dt.time(23, 30), tzinfo=UTC
            ),
        )
        for offset in range(1, 6)
    ]
    wrong_factor = FactorResidualReference(
        signal_id="signal-1",
        horizon_sessions=5,
        target_session=signal.session_path[5],
        factor_model_version="barra-v1",
        evidence_id=_sha("residual-wrong"),
        available_at=dt.datetime.combine(signal.session_path[5], dt.time(23, 45), tzinfo=UTC),
    )
    blocked = build_settlement_plan(
        [signal], closes, [wrong_factor], as_of=dt.datetime.combine(signal.session_path[5], dt.time(23, 59), tzinfo=UTC)
    )
    assert blocked.task_count == 0
    assert {item.reason_code for item in blocked.blocked} == {"factor_model_version_mismatch"}

    matching = FactorResidualReference(
        signal_id="signal-1",
        horizon_sessions=5,
        target_session=signal.session_path[5],
        factor_model_version="barra-v2",
        evidence_id=_sha("residual-right"),
        available_at=dt.datetime.combine(signal.session_path[5], dt.time(23, 45), tzinfo=UTC),
    )
    plan = build_settlement_plan(
        [signal], closes, [matching], as_of=dt.datetime.combine(signal.session_path[5], dt.time(23, 59), tzinfo=UTC)
    )
    assert plan.task_count == 1
    assert plan.tasks[0].horizon_sessions == 5
    assert len(plan.tasks[0].close_path_ids) == 5
    assert plan.tasks[0].factor_model_version == "barra-v2"
    assert plan.tasks[0].idempotency_key.startswith("prediction-settlement:")
    assert not plan.automatic_trading_permitted

    calls = []
    execution = execute_settlement_plan(plan, lambda task: calls.append(task.idempotency_key) or "event-1")
    assert execution.succeeded_count == 1
    assert calls == [plan.tasks[0].idempotency_key]
    assert not execution.automatic_trading_permitted
