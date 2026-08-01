from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator

from serenity_monitor.private_daily_report import (
    JSON_SCHEMA_URI,
    LEGACY_SCHEMA_VERSION,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    PrivateDailyReportCanonicalizationError,
    PrivateDailyReportIdentityError,
    PrivateDailyReportSchemaError,
    canonical_json,
    compute_delivery_id,
    compute_report_id,
    compute_target_key_sha256,
    finalize_private_daily_report,
    validate_private_daily_report,
)


TARGET_HASH = "a" * 64


def _book() -> dict:
    return {
        "valuation_status": "carried_forward_display_only",
        "cash": Decimal("0.00"),
        "nav": Decimal("0"),
        "market_value": Decimal("0"),
        "total_economic_cost": Decimal("0"),
        "realized_pnl": Decimal("0"),
        "fees": Decimal("0"),
        "performance": {
            "valuation_session": "2026-07-31",
            "prior_nav": None,
            "prior_cumulative_twr": None,
            "net_external_flow": Decimal("0"),
            "weighted_external_flow": Decimal("0"),
            "daily_pnl": None,
            "daily_return": None,
            "cumulative_twr": None,
        },
        "positions": [],
    }


def report_draft() -> dict:
    return {
        "classification": "synthetic_example",
        "simulation": True,
        "report_status": "no_new_close",
        "prepared_at": "2026-08-01T05:15:00Z",
        "delivery": {
            "delivery_date": "2026-08-01",
            "timezone": "Asia/Shanghai",
            "channel": "codex",
        },
        "calendar": {
            "calendar_id": "XNYS",
            "exchange_mic": "XNAS",
            "exchange_timezone": "America/New_York",
            "report_timezone": "Asia/Shanghai",
            "as_of": "2026-08-01T05:15:00Z",
            "mode": "none",
            "latest_completed_session": "2026-07-31",
            "last_settled_session_before_run": "2026-07-31",
            "unsettled_sessions": [],
            "provenance": [
                {
                    "instrument_mic": "XNAS",
                    "calendar_name": "XNYS",
                    "calendar_version": "4.13.2",
                    "exchange_timezone": "America/New_York",
                }
            ],
            "new_sessions_count": 0,
            "no_new_close": True,
        },
        "session_results": [],
        "portfolio": {
            "currency": "USD",
            "as_of_session": "2026-07-31",
            "ledger_last_event_hash": "0" * 64,
            "confirmed": _book(),
            "modeled": _book(),
        },
        "dca": {
            "plan_id": "demo-plan",
            "version": "v1",
            "currency": "USD",
            "funding_mode": "modeled_external_contribution",
            "items": [
                {
                    "symbol": "DEMO_EQ",
                    "configured": {"amount": Decimal("10.00")},
                    "proposed": {
                        "amount": Decimal("25"),
                        "action": "increase_review",
                        "rationale_codes": ["research_only"],
                        "automatic_execution": False,
                    },
                    "modeled": {
                        "execution_claim": False,
                        "sessions": [],
                    },
                    "broker_confirmed": {
                        "availability": "unavailable",
                        "status": "not_connected",
                        "amount": None,
                        "quantity": None,
                        "price": None,
                        "trade_id": None,
                    },
                }
            ],
        },
        "research": {
            "overall_view": "Synthetic test only.",
            "market_regime": "unknown",
            "risk_budget_multiplier": Decimal("0"),
            "fund_monitoring": [],
            "social_attention": [],
            "notes": [],
        },
        "source_health": [],
        "actions": [],
        "manual_trade_prompt": {
            "required": False,
            "prompt": None,
            "accepted_response_kinds": ["no_manual_trade"],
            "default_if_no_response": "no_new_owner_confirmed_event",
            "broker_execution_available": False,
        },
        "privacy": {
            "contains_private_portfolio_data": False,
            "contains_target_identifier": False,
            "github_persistence_allowed": False,
            "public_artifact_allowed": False,
            "gpt_owner_delivery_only": True,
            "redaction_status": "synthetic_only",
            "warnings": [],
        },
    }


def finalized_report() -> dict:
    return finalize_private_daily_report(report_draft(), target_key_sha256=TARGET_HASH)


def legacy_finalized_report() -> dict:
    report = finalized_report()
    report["schema_version"] = LEGACY_SCHEMA_VERSION
    report["delivery"]["delivery_id"] = compute_delivery_id(
        delivery_date=report["delivery"]["delivery_date"],
        timezone=report["delivery"]["timezone"],
        channel=report["delivery"]["channel"],
        target_key_sha256=TARGET_HASH,
        schema_version=LEGACY_SCHEMA_VERSION,
    )
    report["report_id"] = compute_report_id(report)
    return report


def test_schema_is_valid_draft_2020_12_and_all_objects_are_closed():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == JSON_SCHEMA_URI
    Draft202012Validator.check_schema(schema)

    def assert_closed(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for value in node.values():
                assert_closed(value)
        elif isinstance(node, list):
            for value in node:
                assert_closed(value)

    assert_closed(schema)


def test_finalize_adds_versioned_identities_and_canonical_decimal_strings():
    report = finalized_report()
    assert report["$schema"] == JSON_SCHEMA_URI
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["report_id"] == compute_report_id(report)
    assert report["delivery"]["delivery_id"] == compute_delivery_id(
        delivery_date="2026-08-01",
        timezone="Asia/Shanghai",
        channel="codex",
        target_key_sha256=TARGET_HASH,
    )
    assert report["dca"]["items"][0]["configured"]["amount"] == "10"
    assert report["portfolio"]["confirmed"]["cash"] == "0"
    assert validate_private_daily_report(report) == report


def test_legacy_v1_report_replays_but_cannot_claim_v1_1_fields():
    legacy = legacy_finalized_report()
    assert validate_private_daily_report(legacy) == legacy

    incompatible = deepcopy(legacy)
    incompatible["research"]["social_decision"] = {
        "raw_contribution": "0",
        "effective_contribution": "0",
        "effective_execution_coverage": "0",
        "decision_weight_cap": "0",
        "calibration_state": "research_only",
        "research_only": True,
    }
    incompatible["report_id"] = compute_report_id(incompatible)
    with pytest.raises(PrivateDailyReportSchemaError):
        validate_private_daily_report(incompatible)


def test_target_hash_and_raw_target_never_enter_report_document():
    report = finalized_report()
    serialized = canonical_json(report)
    assert TARGET_HASH not in serialized
    assert "target_key_sha256" not in serialized
    assert "owner@example.com" not in serialized
    assert compute_target_key_sha256("owner@example.com") != TARGET_HASH


def test_finalize_rejects_target_digest_hidden_in_free_text():
    draft = report_draft()
    draft["research"]["notes"] = [TARGET_HASH]

    with pytest.raises(PrivateDailyReportIdentityError, match="target digest"):
        finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)


def test_delivery_identity_is_stable_and_target_scoped():
    first = compute_delivery_id(
        delivery_date="2026-08-01",
        timezone="Asia/Shanghai",
        channel="codex",
        target_key_sha256=TARGET_HASH,
    )
    replay = compute_delivery_id(
        delivery_date="2026-08-01",
        timezone="Asia/Shanghai",
        channel="codex",
        target_key_sha256=TARGET_HASH,
    )
    different_target = compute_delivery_id(
        delivery_date="2026-08-01",
        timezone="Asia/Shanghai",
        channel="codex",
        target_key_sha256="b" * 64,
    )
    assert first == replay
    assert first != different_target


def test_report_id_detects_any_content_tamper():
    report = finalized_report()
    tampered = deepcopy(report)
    tampered["research"]["overall_view"] = "Changed after finalization."
    with pytest.raises(PrivateDailyReportIdentityError, match="report_id"):
        validate_private_daily_report(tampered)


@pytest.mark.parametrize("value", [0.1, float("nan"), float("inf")])
def test_binary_float_and_nonfinite_values_are_recursively_forbidden(value: float):
    draft = report_draft()
    draft["research"]["notes"] = [{"nested": value}]
    with pytest.raises(PrivateDailyReportCanonicalizationError, match="floating point"):
        finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)


def test_schema_rejects_noncanonical_exponent_or_trailing_zero_decimal_text():
    report = finalized_report()
    report["dca"]["items"][0]["configured"]["amount"] = "1E+1"
    report["report_id"] = compute_report_id(report)
    with pytest.raises(PrivateDailyReportSchemaError):
        validate_private_daily_report(report)

    report = finalized_report()
    report["dca"]["items"][0]["configured"]["amount"] = "10.0"
    report["report_id"] = compute_report_id(report)
    with pytest.raises(PrivateDailyReportSchemaError):
        validate_private_daily_report(report)


def test_schema_rejects_negative_cash_for_non_margin_ledger_books():
    report = finalized_report()
    report["portfolio"]["confirmed"]["cash"] = "-0.01"
    report["report_id"] = compute_report_id(report)

    with pytest.raises(PrivateDailyReportSchemaError, match="cash"):
        validate_private_daily_report(report)


def test_additional_properties_fail_at_top_and_nested_levels():
    top = finalized_report()
    top["unexpected"] = True
    top["report_id"] = compute_report_id(top)
    with pytest.raises(PrivateDailyReportSchemaError, match="unexpected"):
        validate_private_daily_report(top)

    nested = finalized_report()
    nested["delivery"]["target_key_sha256"] = TARGET_HASH
    nested["report_id"] = compute_report_id(nested)
    with pytest.raises(PrivateDailyReportSchemaError, match="target_key_sha256"):
        validate_private_daily_report(nested)


def test_finalize_can_replay_a_prederived_delivery_id_without_target_hash():
    report = finalized_report()
    draft = report_draft()
    draft["delivery"]["delivery_id"] = report["delivery"]["delivery_id"]
    replay = finalize_private_daily_report(draft)
    assert replay == report
