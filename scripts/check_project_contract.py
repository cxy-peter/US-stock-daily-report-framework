#!/usr/bin/env python3
"""Validate the persistent project contract and critical invariants."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    try:
        contract = yaml.safe_load(
            (ROOT / "PROJECT_CONTRACT.yaml").read_text(encoding="utf-8")
        ) or {}
        assert contract["schema_version"] == "serenity_project_contract/v1.0.0"
        assert contract["repository_roles"]["US-stock-daily-report"]["visibility_required"] == "private"
        rules = set(contract["non_negotiables"])
        required_rules = {
            "no broker order endpoint",
            "one user-visible daily report per date",
            "conclusion and action explanation appear before technical evidence",
            "fees and modeled cost drag are displayed separately from returns",
            "political communications cannot independently open, add, trim, or exit",
            "global narratives cannot independently open, add, trim, or exit",
            "prediction-market prices are noisy forecasts, not objective probabilities",
            "walk-forward training must end before every out-of-sample test block",
            "forward-label overlap must be purged before every test block",
            "multiple factor tests require false-discovery-rate control",
            "daily monitoring may not silently redefine a factor",
            "factor residual calibration may never pool model versions",
            "corporate actions may never auto-adjust the confirmed ledger",
        }
        assert required_rules <= rules

        daily = contract["private_daily_plan"]
        assert daily["five_tickers_required"] is True
        assert float(daily["base_amount_usd_each"]) == 20.0
        assert daily["automatic_execution"] is False
        assert daily["daily_test_preflight"] is True
        sections = daily["report_sections"]
        assert sections[0] == "conclusion"
        assert sections.index("fees_and_cost_drag") < sections.index("multi_horizon_factor_evidence")

        models = contract["advanced_models"]
        political = models["political_communication_brief"]
        assert political["raw_mention_count_signal"] is False
        assert political["complete_policy_claim_extraction"] is True
        assert political["media_can_replace_primary_source"] is False
        assert political["independent_trade_trigger"] is False

        global_model = models["global_market_narratives"]
        assert global_model["quora_direct_weight"] == 0
        assert global_model["independent_trade_trigger"] is False

        validation = models["walk_forward_factor_validation"]
        assert validation["horizons_sessions"] == [1, 5, 20]
        assert validation["strict_train_before_test"] is True
        assert validation["purge_equal_to_horizon_default"] is True
        assert validation["embargo_equal_to_horizon_default"] is True
        assert validation["multiple_testing_method"] == "benjamini_hochberg"
        assert validation["cross_horizon_admission_required"] is True
        assert validation["daily_factor_definition_change"] is False
        assert validation["automatic_factor_quarantine"] is True
        assert validation["broker_order_capability"] is False

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
        assert models["brinson_fachler_and_carino"]["broker_order_capability"] is False
        assert models["constrained_asset_allocation"]["broker_order_capability"] is False
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
