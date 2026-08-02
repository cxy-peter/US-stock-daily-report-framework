#!/usr/bin/env python3
"""Validate the persistent project contract and critical safety invariants."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    path = ROOT / "PROJECT_CONTRACT.yaml"
    try:
        contract = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert contract["schema_version"] == "serenity_project_contract/v1.0.0"
        assert contract["repository_roles"]["US-stock-daily-report"]["visibility_required"] == "private"
        rules = set(contract["non_negotiables"])
        assert "no broker order endpoint" in rules
        assert "one user-visible daily report per date" in rules
        assert "political communications cannot independently open, add, trim, or exit" in rules
        assert "prediction-market prices are noisy forecasts, not objective probabilities" in rules
        assert "factor residual calibration may never pool model versions" in rules
        assert "corporate actions may never auto-adjust the confirmed ledger" in rules
        assert contract["private_daily_plan"]["five_tickers_required"] is True
        assert float(contract["private_daily_plan"]["base_amount_usd_each"]) == 20.0

        models = contract["advanced_models"]
        political = models["political_communication_brief"]
        assert political["raw_mention_count_signal"] is False
        assert political["complete_policy_claim_extraction"] is True
        assert political["media_can_replace_primary_source"] is False
        assert political["independent_trade_trigger"] is False
        live_poly = models["live_polymarket_sentiment"]
        assert live_poly["objective_probability_claim"] is False
        assert live_poly["order_capability"] is False
        assert float(live_poly["decision_score_cap"]) <= 0.03
        assert models["trump_policy_transmission_index"]["independent_trade_trigger"] is False
        assert models["polymarket_settlement_event_study"]["lookahead_permitted"] is False
        assert models["social_heat"]["xiaohongshu_execution_weight"] == 0

        volatility = models["volatility_surface_and_tail_risk"]
        assert volatility["correlated_option_surface_group"] is True
        assert volatility["downside_only_risk_control"] is True
        assert volatility["independent_trade_trigger"] is False
        overnight = models["overnight_and_premarket_anomaly"]
        assert overnight["own_history_normalization_required"] is True
        assert overnight["second_user_facing_report"] is False
        assert overnight["independent_trade_trigger"] is False
        attribution = models["brinson_fachler_and_carino"]
        assert attribution["accounting_reconciliation_required"] is True
        assert attribution["broker_order_capability"] is False
        allocation = models["constrained_asset_allocation"]
        assert allocation["turnover_and_cost_constraints"] is True
        assert allocation["broker_order_capability"] is False
        factor = models["factor_residual_calibration"]
        assert factor["factor_model_version_isolation"] is True
        assert factor["cross_version_pooling"] is False
        scheduler = models["prediction_settlement_scheduler"]
        assert scheduler["complete_close_path_required"] is True
        assert scheduler["idempotent_callback_only"] is True
        assert scheduler["broker_order_capability"] is False
        actions = models["corporate_action_reconciliation"]
        assert actions["primary_source_required"] is True
        assert actions["automatic_adjustment"] is False

        ibkr = models["ibkr_flex_readonly_reconciliation"]
        assert ibkr["implemented_library"] is True
        assert ibkr["automatic_ledger_mutation"] is False
        assert ibkr["broker_order_capability"] is False
    except (OSError, KeyError, TypeError, AssertionError, yaml.YAMLError) as exc:
        print(f"project contract check failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("project contract check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
