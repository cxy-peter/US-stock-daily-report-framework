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
        assert contract["schema_version"] == "serenity_project_contract/v1.1.0"
        assert contract["repository_roles"]["US-stock-daily-report"]["visibility_required"] == "private"
        assert contract["sources_of_truth"][0] == "requirements/DAILY_RESEARCH_REQUIREMENTS.yaml"

        rules = set(contract["non_negotiables"])
        required_rules = {
            "no broker order endpoint",
            "one user-visible daily report per local date",
            "conclusion and action explanation appear before technical evidence",
            "routine portfolio-wide fees do not appear in the main daily report",
            "per-security transaction economics appear only for add or trim candidates",
            "missing costs remain UNKNOWN and are never assumed to be zero",
            "external agent summaries are secondary synthesis and never original evidence",
            "political communications cannot independently open, add, trim, or exit",
            "global narratives cannot independently open, add, trim, or exit",
            "prediction-market prices are noisy forecasts, not objective probabilities",
            "walk-forward training ends before every out-of-sample test block",
            "forward-label overlap is purged before every test block",
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
        schedule = daily["schedule"]
        assert schedule == {
            "before_date": "2026-08-10",
            "timezone": "Asia/Shanghai",
            "local_time": "08:30",
            "from_date": "2026-08-10",
            "timezone_after": "America/New_York",
            "local_time_after": "08:30",
            "dst_aware": True,
        }
        sections = daily["report_sections"]
        assert sections[0] == "conclusion_and_next_session_actions"
        assert "conditional_security_level_transaction_economics" in sections
        assert "fees_and_cost_drag" not in sections

        models = contract["advanced_models"]
        political = models["political_communication_brief"]
        assert political["raw_mention_count_signal"] is False
        assert political["complete_policy_claim_extraction"] is True
        assert political["media_can_replace_primary_source"] is False
        assert political["daily_report_integrated"] is True
        assert political["independent_trade_trigger"] is False

        tpti = models["trump_policy_transmission_index"]
        assert tpti["daily_report_integrated"] is True
        assert tpti["independent_trade_trigger"] is False

        global_model = models["global_market_narratives"]
        assert global_model["quora_direct_weight"] == 0
        assert global_model["xiaohongshu_direct_add_open_weight"] == 0
        assert global_model["independent_trade_trigger"] is False

        opinion = models["external_agent_and_social_inbox"]
        assert opinion["agent_summary_is_original_evidence"] is False
        assert opinion["verification"] == "primary_source_or_two_independent_institutional_groups"
        assert opinion["social_direct_add_open_weight"] == 0
        assert float(opinion["downside_overlay_cap"]) <= 0.05

        validation = models["walk_forward_factor_validation"]
        assert validation["horizons_sessions"] == [1, 5, 20]
        assert validation["strict_train_before_test"] is True
        assert validation["purge_equal_to_horizon_default"] is True
        assert validation["embargo_equal_to_horizon_default"] is True
        assert validation["multiple_testing_method"] == "benjamini_hochberg"
        assert validation["cross_horizon_admission_required"] is True
        assert validation["daily_factor_definition_change"] is False
        assert validation["automatic_factor_quarantine"] is True
        assert validation["daily_report_integrated"] is True
        assert validation["broker_order_capability"] is False

        live_poly = models["live_polymarket_sentiment"]
        assert live_poly["objective_probability_claim"] is False
        assert live_poly["order_capability"] is False
        assert live_poly["daily_report_integrated"] is True
        assert float(live_poly["decision_score_cap"]) <= 0.03
        resolved_poly = models["polymarket_settlement_event_study"]
        assert resolved_poly["lookahead_permitted"] is False
        assert resolved_poly["horizons_sessions"] == [1, 5, 20, 60]

        volatility = models["volatility_surface_and_tail_risk"]
        assert volatility["correlated_option_surface_group"] is True
        assert volatility["downside_only_risk_control"] is True
        assert volatility["daily_report_integrated"] is True
        assert volatility["independent_trade_trigger"] is False
        overnight = models["overnight_and_premarket_anomaly"]
        assert overnight["own_history_normalization_required"] is True
        assert overnight["second_user_facing_report"] is False
        assert overnight["daily_report_integrated"] is True
        assert overnight["independent_trade_trigger"] is False

        assert models["barra_public_proxy"]["daily_report_integrated"] is True
        assert models["kalman_dynamic_exposure"]["daily_report_integrated"] is True
        manager = models["manager_skill_and_fragility"]
        assert manager["manager_change_attribution_required"] is True
        assert manager["copy_trade_requires_fragility_gate"] is True
        assert manager["daily_report_integrated"] is True

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
        assert models["social_heat"]["xiaohongshu_execution_weight"] == 0

        ibkr = models["ibkr_flex_readonly_reconciliation"]
        assert ibkr["implemented_library"] is True
        assert ibkr["daily_report_integrated"] is True
        assert ibkr["automatic_ledger_mutation"] is False
        assert ibkr["broker_order_capability"] is False

        persistence = contract["context_persistence"]
        assert persistence["requirement_ledger"] == "requirements/DAILY_RESEARCH_REQUIREMENTS.yaml"
        assert persistence["maintenance_skill"] == "skills/maintain-daily-research-context/SKILL.md"
        assert persistence["ci_check"] == "scripts/check_requirement_ledger.py"
        assert persistence["four_state_audit_required"] is True
        assert persistence["prohibited_shortcut"] == "implemented_library_is_not_production_complete"
    except (OSError, KeyError, TypeError, AssertionError, yaml.YAMLError) as exc:
        print(f"project contract check failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("project contract check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
