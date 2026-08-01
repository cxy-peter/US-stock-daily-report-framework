from __future__ import annotations

from copy import deepcopy
from decimal import Context, Decimal, localcontext

import pytest

from serenity_monitor.private_daily_report import (
    PrivateDailyReportSchemaError,
    PrivateDailyReportSemanticError,
    compute_report_id,
    finalize_private_daily_report,
    validate_private_daily_report,
)
from test_private_daily_report_schema import TARGET_HASH, finalized_report, report_draft


def _rehash(report: dict) -> dict:
    report["report_id"] = compute_report_id(report)
    return report


def _performance(session: str | None) -> dict:
    return {
        "valuation_session": session,
        "prior_nav": None,
        "prior_cumulative_twr": None,
        "net_external_flow": Decimal("0"),
        "weighted_external_flow": Decimal("0"),
        "daily_pnl": None,
        "daily_return": None,
        "cumulative_twr": None,
    }


def _position(
    symbol: str,
    *,
    modeled_quantity: str = "0",
    valued: bool = True,
    price_session: str = "2026-07-31",
) -> dict:
    return {
        "symbol": symbol,
        "quantity": "1",
        "modeled_quantity": modeled_quantity,
        "accepted_close": "10" if valued else None,
        "accepted_close_id": "c" * 64 if valued else None,
        "selected_provider_id": "twelve_data" if valued else None,
        "price_session": price_session if valued else None,
        "market_value": "10" if valued else None,
        "economic_cost": "9",
        "average_economic_cost": "9",
        "unrealized_pnl": "1" if valued else None,
        "portfolio_weight": "1" if valued else None,
    }


def _session_result(
    session: str,
    *,
    status: str,
    is_backfill: bool,
    gates: tuple[str, str, str, str],
    dca_status: str | None = None,
    valuation_status: str = "fresh",
    reasons: list[str] | None = None,
) -> dict:
    fresh = valuation_status == "fresh"
    settled = status in {"settled", "already_settled"}
    return {
        "session_date": session,
        "status": status,
        "is_backfill": is_backfill,
        "close_batch_id": "b" * 64 if settled or gates[1] != "not_attempted" else None,
        "ledger_batch_id": f"batch-{session}" if settled else None,
        "calendar_gate": gates[0],
        "price_gate": gates[1],
        "corporate_action_gate": gates[2],
        "funding_gate": gates[3],
        "dca_status": dca_status or status,
        "confirmed_valuation_status": valuation_status,
        "modeled_valuation_status": valuation_status,
        "confirmed_valuation_id": f"confirmed-{session}" if fresh else None,
        "modeled_valuation_id": f"modeled-{session}" if fresh else None,
        "reason_codes": reasons or [],
    }


def _modeled_session(
    session: str,
    status: str,
    *,
    settled: bool = False,
) -> dict:
    return {
        "session_date": session,
        "status": status,
        "amount": Decimal("10") if settled else Decimal("0"),
        "spend": Decimal("10") if settled else Decimal("0"),
        "residual": Decimal("0"),
        "quantity": Decimal("0.1") if settled else Decimal("0"),
        "accepted_close": Decimal("100") if settled else None,
        "accepted_close_id": "c" * 64 if settled else None,
        "settlement_event_id": f"event-{session}" if settled else None,
    }


def complete_draft(*, report_status: str = "complete") -> dict:
    draft = report_draft()
    draft["report_status"] = report_status
    draft["calendar"].update(
        {
            "mode": "single",
            "latest_completed_session": "2026-07-31",
            "last_settled_session_before_run": "2026-07-30",
            "unsettled_sessions": ["2026-07-31"],
            "new_sessions_count": 1,
            "no_new_close": False,
        }
    )
    draft["session_results"] = [
        _session_result(
            "2026-07-31",
            status="settled",
            is_backfill=False,
            gates=("passed", "passed", "passed", "passed"),
        )
    ]
    draft["portfolio"]["as_of_session"] = "2026-07-31"
    confirmed = draft["portfolio"]["confirmed"]
    confirmed.update(
        {
            "valuation_status": "fresh",
            "nav": Decimal("0"),
            "market_value": Decimal("0"),
            "performance": _performance("2026-07-31"),
        }
    )
    modeled = draft["portfolio"]["modeled"]
    modeled.update(
        {
            "valuation_status": "fresh",
            "nav": Decimal("10"),
            "market_value": Decimal("10"),
            "total_economic_cost": Decimal("10"),
            "performance": {
                **_performance("2026-07-31"),
                "net_external_flow": Decimal("10"),
            },
            "positions": [
                {
                    "symbol": "DEMO_EQ",
                    "quantity": Decimal("0.1"),
                    "modeled_quantity": Decimal("0.1"),
                    "accepted_close": Decimal("100"),
                    "accepted_close_id": "c" * 64,
                    "selected_provider_id": "twelve_data",
                    "price_session": "2026-07-31",
                    "market_value": Decimal("10"),
                    "economic_cost": Decimal("10"),
                    "average_economic_cost": Decimal("100"),
                    "unrealized_pnl": Decimal("0"),
                    "portfolio_weight": Decimal("1"),
                }
            ],
        }
    )
    draft["dca"]["items"][0]["modeled"]["sessions"] = [
        _modeled_session("2026-07-31", "settled", settled=True)
    ]
    if report_status == "complete_with_warnings":
        draft["privacy"]["warnings"] = ["synthetic_warning"]
    return draft


def blocked_first_run_draft() -> dict:
    draft = report_draft()
    draft["report_status"] = "blocked"
    draft["calendar"].update(
        {
            "mode": "single",
            "latest_completed_session": "2026-07-31",
            "last_settled_session_before_run": None,
            "unsettled_sessions": ["2026-07-31"],
            "new_sessions_count": 1,
            "no_new_close": False,
        }
    )
    draft["session_results"] = [
        _session_result(
            "2026-07-31",
            status="blocked",
            is_backfill=False,
            gates=("passed", "blocked", "not_attempted", "not_attempted"),
            dca_status="blocked",
            valuation_status="unavailable",
            reasons=["accepted_close_unavailable"],
        )
    ]
    draft["portfolio"].update(
        {"as_of_session": None, "ledger_last_event_hash": None}
    )
    for book_name in ("confirmed", "modeled"):
        book = draft["portfolio"][book_name]
        book.update(
            {
                "valuation_status": "unavailable",
                "cash": Decimal("100"),
                "nav": None,
                "market_value": None,
                "total_economic_cost": Decimal("9"),
                "performance": _performance(None),
                "positions": [_position("DEMO_EQ", valued=False)],
            }
        )
    draft["dca"]["items"][0]["modeled"]["sessions"] = [
        _modeled_session("2026-07-31", "blocked")
    ]
    return draft


def test_complete_and_complete_with_warnings_are_valid_but_partial_is_removed():
    finalize_private_daily_report(complete_draft(), target_key_sha256=TARGET_HASH)
    finalize_private_daily_report(
        complete_draft(report_status="complete_with_warnings"),
        target_key_sha256=TARGET_HASH,
    )
    draft = complete_draft()
    draft["report_status"] = "partial"
    with pytest.raises(PrivateDailyReportSchemaError):
        finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)


@pytest.mark.parametrize(
    ("status", "gates"),
    [
        ("already_settled", ("passed", "passed", "passed", "passed")),
        ("skipped_by_owner", ("passed", "passed", "passed", "not_attempted")),
    ],
)
def test_idempotent_and_owner_skip_sessions_do_not_claim_a_new_modeled_fill(
    status: str,
    gates: tuple[str, str, str, str],
):
    draft = complete_draft()
    draft["session_results"] = [
        _session_result(
            "2026-07-31",
            status=status,
            is_backfill=False,
            gates=gates,
        )
    ]
    draft["dca"]["items"][0]["modeled"]["sessions"] = [
        _modeled_session("2026-07-31", status)
    ]
    report = finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)
    modeled = report["dca"]["items"][0]["modeled"]["sessions"][0]
    assert modeled["amount"] == modeled["spend"] == modeled["quantity"] == "0"


def test_blocked_first_run_requires_no_fabricated_nav_price_or_valuation():
    report = finalize_private_daily_report(
        blocked_first_run_draft(), target_key_sha256=TARGET_HASH
    )
    assert report["portfolio"]["ledger_last_event_hash"] is None
    for book_name in ("confirmed", "modeled"):
        book = report["portfolio"][book_name]
        assert book["valuation_status"] == "unavailable"
        assert book["nav"] is None
        assert book["market_value"] is None
        assert book["performance"]["valuation_session"] is None
        position = book["positions"][0]
        assert position["accepted_close"] is None
        assert position["market_value"] is None


def test_proposed_amount_is_informational_and_never_drives_modeled_sessions():
    report = finalize_private_daily_report(complete_draft(), target_key_sha256=TARGET_HASH)
    item = report["dca"]["items"][0]
    assert item["proposed"]["amount"] == "25"
    assert item["configured"]["amount"] == "10"
    assert item["modeled"]["sessions"][0]["amount"] == "10"

    item["modeled"]["sessions"][0]["amount"] = item["proposed"]["amount"]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="configured.amount"):
        validate_private_daily_report(report)


def test_settled_dca_validates_spend_residual_close_math_and_identities():
    report = finalize_private_daily_report(complete_draft(), target_key_sha256=TARGET_HASH)
    modeled = report["dca"]["items"][0]["modeled"]["sessions"][0]
    modeled["residual"] = "1"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="spend plus residual"):
        validate_private_daily_report(report)

    report = finalize_private_daily_report(complete_draft(), target_key_sha256=TARGET_HASH)
    modeled = report["dca"]["items"][0]["modeled"]["sessions"][0]
    modeled["quantity"] = "0.09"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="quantity times"):
        validate_private_daily_report(report)

    report = finalize_private_daily_report(
        complete_draft(), target_key_sha256=TARGET_HASH
    )
    report["portfolio"]["modeled"]["positions"][0][
        "average_economic_cost"
    ] = "999"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="average economic cost"):
        validate_private_daily_report(report)

    report = finalize_private_daily_report(
        complete_draft(), target_key_sha256=TARGET_HASH
    )
    report["portfolio"]["modeled"]["positions"][0]["portfolio_weight"] = "0.1"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="portfolio weight"):
        validate_private_daily_report(report)

    report = finalize_private_daily_report(complete_draft(), target_key_sha256=TARGET_HASH)
    modeled = report["dca"]["items"][0]["modeled"]["sessions"][0]
    modeled["accepted_close_id"] = None
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="identities"):
        validate_private_daily_report(report)


def test_dca_arithmetic_is_independent_of_ambient_decimal_precision():
    report = finalize_private_daily_report(complete_draft(), target_key_sha256=TARGET_HASH)
    configured = "1234567890" * 4
    spend = configured[:-2] + "89"
    item = report["dca"]["items"][0]
    item["configured"]["amount"] = configured
    session = item["modeled"]["sessions"][0]
    session.update(
        {
            "amount": configured,
            "spend": spend,
            "residual": "1",
            "quantity": spend,
            "accepted_close": "1",
        }
    )
    _rehash(report)

    for precision in (6, 28, 50):
        with localcontext(Context(prec=precision)):
            validate_private_daily_report(report)


def test_every_nonsettled_dca_session_uses_zero_and_null_operational_fields():
    report = finalize_private_daily_report(
        blocked_first_run_draft(), target_key_sha256=TARGET_HASH
    )
    modeled = report["dca"]["items"][0]["modeled"]["sessions"][0]
    modeled.update({"residual": "10", "accepted_close": "100"})
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="zero amounts"):
        validate_private_daily_report(report)


def test_broker_fields_remain_unavailable_and_null():
    report = finalized_report()
    broker = report["dca"]["items"][0]["broker_confirmed"]
    broker["availability"] = "available"
    broker["quantity"] = "0.1"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSchemaError):
        validate_private_daily_report(report)


def test_no_new_close_requires_carried_forward_books_and_no_daily_return():
    validate_private_daily_report(finalized_report())

    report = finalized_report()
    report["portfolio"]["modeled"]["valuation_status"] = "fresh"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="carried_forward"):
        validate_private_daily_report(report)

    report = finalized_report()
    report["portfolio"]["modeled"]["performance"]["daily_return"] = "0.01"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="non-fresh"):
        validate_private_daily_report(report)


def test_calendar_mode_unsettled_sessions_and_results_are_one_contract():
    report = finalize_private_daily_report(complete_draft(), target_key_sha256=TARGET_HASH)
    report["calendar"]["mode"] = "backfill"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="cardinality"):
        validate_private_daily_report(report)

    report = finalize_private_daily_report(complete_draft(), target_key_sha256=TARGET_HASH)
    report["calendar"]["unsettled_sessions"] = ["2026-07-30"]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="exactly match"):
        validate_private_daily_report(report)


def test_backfill_stops_all_later_sessions_after_first_block():
    draft = blocked_first_run_draft()
    draft["calendar"].update(
        {
            "mode": "backfill",
            "latest_completed_session": "2026-07-31",
            "last_settled_session_before_run": "2026-07-28",
            "unsettled_sessions": ["2026-07-29", "2026-07-30", "2026-07-31"],
            "new_sessions_count": 3,
        }
    )
    draft["session_results"] = [
        _session_result(
            "2026-07-29",
            status="settled",
            is_backfill=True,
            gates=("passed", "passed", "passed", "passed"),
        ),
        _session_result(
            "2026-07-30",
            status="blocked",
            is_backfill=True,
            gates=("passed", "blocked", "not_attempted", "not_attempted"),
            dca_status="blocked",
            valuation_status="unavailable",
            reasons=["price_gate_blocked"],
        ),
        _session_result(
            "2026-07-31",
            status="not_attempted_prior_session_blocked",
            is_backfill=False,
            gates=("not_attempted",) * 4,
            valuation_status="not_attempted",
            reasons=["prior_session_blocked"],
        ),
    ]
    draft["dca"]["items"][0]["modeled"]["sessions"] = [
        _modeled_session("2026-07-29", "settled", settled=True),
        _modeled_session("2026-07-30", "blocked"),
        _modeled_session("2026-07-31", "not_attempted_prior_session_blocked"),
    ]
    report = finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)
    validate_private_daily_report(report)

    report["session_results"][2]["status"] = "blocked"
    report["session_results"][2]["dca_status"] = "blocked"
    report["dca"]["items"][0]["modeled"]["sessions"][2]["status"] = "blocked"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="must not be attempted"):
        validate_private_daily_report(report)


def test_gate_pipeline_cannot_resume_after_a_block_or_not_attempted_gate():
    report = finalize_private_daily_report(
        blocked_first_run_draft(), target_key_sha256=TARGET_HASH
    )
    report["session_results"][0]["corporate_action_gate"] = "passed"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="after processing stopped"):
        validate_private_daily_report(report)


def test_blocked_session_requires_a_blocked_gate_matching_dca_and_no_ledger_batch():
    report = finalize_private_daily_report(
        blocked_first_run_draft(), target_key_sha256=TARGET_HASH
    )
    session = report["session_results"][0]
    session.update(
        {
            "price_gate": "passed",
            "corporate_action_gate": "passed",
            "funding_gate": "passed",
        }
    )
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="blocked gate"):
        validate_private_daily_report(report)

    report = finalize_private_daily_report(
        blocked_first_run_draft(), target_key_sha256=TARGET_HASH
    )
    report["session_results"][0]["dca_status"] = "settled"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="must agree"):
        validate_private_daily_report(report)

    report = finalize_private_daily_report(
        blocked_first_run_draft(), target_key_sha256=TARGET_HASH
    )
    report["session_results"][0]["ledger_batch_id"] = "false-ledger-batch"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="may not claim"):
        validate_private_daily_report(report)


def test_failed_price_gate_cannot_claim_fresh_session_or_portfolio_valuation():
    draft = complete_draft()
    draft["report_status"] = "blocked"
    session = draft["session_results"][0]
    session.update(
        {
            "status": "blocked",
            "price_gate": "blocked",
            "corporate_action_gate": "not_attempted",
            "funding_gate": "not_attempted",
            "dca_status": "blocked",
            "ledger_batch_id": None,
            "reason_codes": ["accepted_close_blocked"],
        }
    )
    draft["dca"]["items"][0]["modeled"]["sessions"] = [
        _modeled_session("2026-07-31", "blocked")
    ]

    with pytest.raises(PrivateDailyReportSemanticError, match="cannot claim fresh"):
        finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)


def test_fresh_valuation_requires_ids_and_all_position_price_provenance():
    report = finalize_private_daily_report(complete_draft(), target_key_sha256=TARGET_HASH)
    report["session_results"][0]["modeled_valuation_id"] = None
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="identity"):
        validate_private_daily_report(report)

    report = finalize_private_daily_report(complete_draft(), target_key_sha256=TARGET_HASH)
    report["portfolio"]["modeled"]["positions"][0]["selected_provider_id"] = None
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="price provenance"):
        validate_private_daily_report(report)


def test_confirmed_positions_never_claim_modeled_quantity():
    draft = complete_draft()
    draft["portfolio"]["confirmed"].update(
        {
            "nav": Decimal("10"),
            "market_value": Decimal("10"),
            "total_economic_cost": Decimal("9"),
            "positions": [_position("DEMO_EQ", modeled_quantity="0.1")],
        }
    )
    with pytest.raises(PrivateDailyReportSemanticError, match="must be zero"):
        finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)


def test_modeled_book_cannot_drop_or_undercount_confirmed_positions():
    draft = complete_draft()
    confirmed_position = {
        "symbol": "DEMO_EQ",
        "quantity": Decimal("2"),
        "modeled_quantity": Decimal("0"),
        "accepted_close": Decimal("100"),
        "accepted_close_id": "c" * 64,
        "selected_provider_id": "twelve_data",
        "price_session": "2026-07-31",
        "market_value": Decimal("200"),
        "economic_cost": Decimal("200"),
        "average_economic_cost": Decimal("100"),
        "unrealized_pnl": Decimal("0"),
        "portfolio_weight": Decimal("1"),
    }
    draft["portfolio"]["confirmed"].update(
        {
            "nav": Decimal("200"),
            "market_value": Decimal("200"),
            "total_economic_cost": Decimal("200"),
            "positions": [confirmed_position],
        }
    )
    with pytest.raises(PrivateDailyReportSemanticError, match="below confirmed"):
        finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)

    draft["portfolio"]["modeled"]["positions"] = []
    draft["portfolio"]["modeled"].update(
        {
            "nav": Decimal("0"),
            "market_value": Decimal("0"),
            "total_economic_cost": Decimal("0"),
        }
    )
    with pytest.raises(PrivateDailyReportSemanticError, match="must contain"):
        finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)


def test_modeled_book_must_expose_projected_quantity_source():
    draft = complete_draft()
    draft["portfolio"]["modeled"]["positions"][0]["modeled_quantity"] = Decimal("0")

    with pytest.raises(
        PrivateDailyReportSemanticError,
        match="must expose a non-zero modeled-versus-confirmed quantity difference",
    ):
        finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)


def test_all_ordered_collections_are_canonicalized_by_contract():
    report = finalize_private_daily_report(complete_draft(), target_key_sha256=TARGET_HASH)
    report["calendar"]["provenance"] = [
        {
            "instrument_mic": "XNYS",
            "calendar_name": "XNYS",
            "calendar_version": "4.13.2",
            "exchange_timezone": "America/New_York",
        },
        *report["calendar"]["provenance"],
    ]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="provenance"):
        validate_private_daily_report(report)

    report = finalized_report()
    report["research"]["notes"] = ["z_note", "a_note"]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="research.notes"):
        validate_private_daily_report(report)

    report = finalized_report()
    report["source_health"] = [
        {"source_id": "z", "source_type": "other", "status": "ok", "required": False, "observed_at": None, "detail_code": None},
        {"source_id": "a", "source_type": "other", "status": "ok", "required": False, "observed_at": None, "detail_code": None},
    ]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="source_health"):
        validate_private_daily_report(report)

    report = finalized_report()
    report["research"]["social_attention"] = [
        {"platform": "x", "topic": "z", "direction": "neutral", "status": "healthy", "score": "0", "research_only": True, "summary": ""},
        {"platform": "reddit", "topic": "a", "direction": "neutral", "status": "healthy", "score": "0", "research_only": True, "summary": ""},
    ]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="social_attention"):
        validate_private_daily_report(report)

    report = finalized_report()
    report["research"]["fund_monitoring"] = [
        {"fund_key": "FUND_Z", "status": "WATCH", "summary": "", "reason_codes": []},
        {"fund_key": "FUND_A", "status": "WATCH", "summary": "", "reason_codes": []},
    ]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="fund_monitoring"):
        validate_private_daily_report(report)

    report = finalized_report()
    report["actions"] = [
        {"action_id": "z", "scope": "data", "symbol": None, "action": "REVIEW", "priority": "normal", "status": "informational", "owner_confirmation_required": False, "automatic_execution": False, "rationale_codes": []},
        {"action_id": "a", "scope": "data", "symbol": None, "action": "REVIEW", "priority": "normal", "status": "informational", "owner_confirmation_required": False, "automatic_execution": False, "rationale_codes": []},
    ]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="actions"):
        validate_private_daily_report(report)


def test_calendar_identity_and_session_watermarks_are_cross_checked():
    report = finalized_report()
    report["calendar"]["provenance"][0]["calendar_name"] = "XNAS"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="calendar identity"):
        validate_private_daily_report(report)

    report = finalized_report()
    report["calendar"]["last_settled_session_before_run"] = "2026-07-30"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="already settled"):
        validate_private_daily_report(report)

    report = finalize_private_daily_report(
        complete_draft(), target_key_sha256=TARGET_HASH
    )
    report["calendar"]["latest_completed_session"] = "2026-08-03"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="extend through"):
        validate_private_daily_report(report)


def test_portfolio_ledger_currency_and_as_of_identity_are_cross_checked():
    report = finalized_report()
    report["portfolio"]["ledger_last_event_hash"] = None
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="requires ledger"):
        validate_private_daily_report(report)

    report = finalized_report()
    report["dca"]["currency"] = "EUR"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="portfolio.currency"):
        validate_private_daily_report(report)

    report = finalized_report()
    report["portfolio"]["as_of_session"] = "2026-08-03"
    for book_name in ("confirmed", "modeled"):
        report["portfolio"][book_name]["performance"]["valuation_session"] = "2026-08-03"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="latest_completed"):
        validate_private_daily_report(report)


def test_book_amounts_and_linked_performance_must_reconcile():
    report = finalize_private_daily_report(
        complete_draft(), target_key_sha256=TARGET_HASH
    )
    report["portfolio"]["confirmed"]["nav"] = "999999"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="NAV"):
        validate_private_daily_report(report)

    report = finalize_private_daily_report(
        complete_draft(), target_key_sha256=TARGET_HASH
    )
    report["portfolio"]["modeled"]["positions"][0]["market_value"] = "11"
    report["portfolio"]["modeled"]["market_value"] = "11"
    report["portfolio"]["modeled"]["nav"] = "11"
    report["portfolio"]["modeled"]["positions"][0]["unrealized_pnl"] = "1"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="quantity times"):
        validate_private_daily_report(report)

    draft = complete_draft()
    draft["portfolio"]["confirmed"]["performance"].update(
        {
            "prior_nav": Decimal("1"),
            "daily_pnl": Decimal("-1"),
            "daily_return": Decimal("-1"),
            "cumulative_twr": Decimal("-1"),
        }
    )
    draft["portfolio"]["modeled"]["performance"].update(
        {
            "prior_nav": Decimal("10"),
            "net_external_flow": Decimal("0"),
            "daily_pnl": Decimal("0"),
            "daily_return": Decimal("0"),
            "cumulative_twr": Decimal("0"),
        }
    )
    report = finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)
    report["portfolio"]["modeled"]["performance"]["daily_pnl"] = "1"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="daily P/L"):
        validate_private_daily_report(report)

    report = finalize_private_daily_report(draft, target_key_sha256=TARGET_HASH)
    report["portfolio"]["modeled"]["performance"]["cumulative_twr"] = "999"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="cumulative TWR"):
        validate_private_daily_report(report)


@pytest.mark.parametrize(
    ("container_path", "field", "values", "message"),
    [
        (("dca", "items", 0, "proposed"), "rationale_codes", ["z", "a"], "rationale_codes"),
        (("research",), "notes", ["z", "a"], "research.notes"),
        (("privacy",), "warnings", ["z", "a"], "privacy.warnings"),
        (("manual_trade_prompt",), "accepted_response_kinds", ["no_manual_trade", "confirmed_fill"], "accepted_response_kinds"),
    ],
)
def test_set_semantics_arrays_must_be_sorted(
    container_path: tuple[object, ...],
    field: str,
    values: list[str],
    message: str,
):
    report = finalized_report()
    container: object = report
    for key in container_path:
        container = container[key]  # type: ignore[index]
    container[field] = values  # type: ignore[index]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match=message):
        validate_private_daily_report(report)


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        (("prepared_at",), "2026-08-01T05:15:00+00:00"),
        (("calendar", "as_of"), "2026-08-01T05:15:00+00:00"),
    ],
)
def test_operational_timestamps_require_rfc3339_utc_z(field_path: tuple[str, ...], value: str):
    report = finalized_report()
    target = report
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = value
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="UTC"):
        validate_private_daily_report(report)


def test_source_observed_at_requires_utc_z_and_collections_are_sorted():
    report = finalized_report()
    report["source_health"] = [
        {
            "source_id": "calendar",
            "source_type": "calendar",
            "status": "ok",
            "required": True,
            "observed_at": "2026-08-01T05:15:00+00:00",
            "detail_code": None,
        }
    ]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="UTC"):
        validate_private_daily_report(report)

    report = finalized_report()
    report["source_health"] = [
        {
            "source_id": "calendar",
            "source_type": "calendar",
            "status": "ok",
            "required": True,
            "observed_at": "2026-08-01T05:15:01Z",
            "detail_code": None,
        }
    ]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="prepared_at"):
        validate_private_daily_report(report)


def test_manual_default_does_not_claim_that_no_real_world_trade_occurred():
    report = finalized_report()
    assert (
        report["manual_trade_prompt"]["default_if_no_response"]
        == "no_new_owner_confirmed_event"
    )
    report["manual_trade_prompt"]["default_if_no_response"] = "no_manual_trade"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSchemaError):
        validate_private_daily_report(report)


def test_delivery_date_uses_prepared_at_utc_in_delivery_timezone():
    report = finalized_report()
    report["prepared_at"] = "2026-07-31T15:00:00Z"
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="delivery_date"):
        validate_private_daily_report(report)


def test_synthetic_reports_cannot_claim_private_portfolio_data():
    report = finalized_report()
    report["privacy"]["contains_private_portfolio_data"] = True
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="private portfolio"):
        validate_private_daily_report(report)


def test_private_reports_must_declare_private_data_classification():
    report = finalized_report()
    report["classification"] = "private_owner_only"
    report["simulation"] = False
    report["privacy"]["redaction_status"] = "private_owner_only"
    _rehash(report)

    with pytest.raises(PrivateDailyReportSemanticError, match="must declare"):
        validate_private_daily_report(report)


def test_proposed_position_changes_require_confirmation_and_a_prompt():
    action = {
        "action_id": "position-review",
        "scope": "position",
        "symbol": "DEMO_EQ",
        "action": "REDUCE",
        "priority": "high",
        "status": "proposed",
        "owner_confirmation_required": False,
        "automatic_execution": False,
        "rationale_codes": ["risk_review"],
    }
    report = finalized_report()
    report["actions"] = [action]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="owner confirmation"):
        validate_private_daily_report(report)

    report = finalized_report()
    action["owner_confirmation_required"] = True
    report["actions"] = [action]
    _rehash(report)
    with pytest.raises(PrivateDailyReportSemanticError, match="manual trade prompt"):
        validate_private_daily_report(report)
