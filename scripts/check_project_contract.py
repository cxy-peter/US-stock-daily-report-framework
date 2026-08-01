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
        assert contract["private_daily_plan"]["five_tickers_required"] is True
        assert float(contract["private_daily_plan"]["base_amount_usd_each"]) == 20.0
        assert contract["advanced_models"]["trump_policy_transmission_index"]["independent_trade_trigger"] is False
        assert contract["advanced_models"]["polymarket_settlement_event_study"]["lookahead_permitted"] is False
        assert contract["advanced_models"]["social_heat"]["xiaohongshu_execution_weight"] == 0
        ibkr = contract["advanced_models"]["ibkr_flex_readonly_reconciliation"]
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
