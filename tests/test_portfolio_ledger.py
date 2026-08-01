from __future__ import annotations

import datetime as dt
import hashlib
import json
import socket
import sqlite3
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_UP, localcontext

import pytest

from serenity_monitor.portfolio_ledger import (
    DcaPlan,
    LedgerAlreadyInitializedError,
    LedgerIdempotencyConflict,
    LedgerInsufficientCash,
    LedgerIntegrityError,
    LedgerPolicy,
    LedgerSettlementBlocked,
    LedgerValidationError,
    OpeningCheckpoint,
    OpeningPosition,
    PortfolioLedger,
    PortfolioLedgerError,
)
from serenity_monitor.provider_registry import (
    AcceptedClose,
    AcceptedCloseBatch,
    CloseObservation,
    InstrumentRef,
)


SESSION_0 = dt.date(2026, 7, 29)
SESSION_1 = dt.date(2026, 7, 30)
SESSION_2 = dt.date(2026, 7, 31)
AFTER_SESSION_2 = dt.datetime(2026, 8, 1, 5, 15, tzinfo=dt.timezone.utc)


def test_opening_checkpoint_keeps_the_original_public_constructor_shape() -> None:
    checkpoint = OpeningCheckpoint(
        "1" * 64,
        "2" * 64,
        SESSION_0,
        "USD",
        Decimal("100"),
        (),
    )

    assert checkpoint.session == SESSION_0
    assert checkpoint.idempotency_key == ""
    assert checkpoint.created_at == dt.datetime(
        1970,
        1,
        1,
        tzinfo=dt.timezone.utc,
    )


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _ledger_ratio(numerator: str | int, denominator: str | int) -> Decimal:
    """Match the ledger's fixed arithmetic context, never the caller's context."""
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        return Decimal(numerator) / Decimal(denominator)


def _accepted_batch(
    prices: dict[str, str | Decimal],
    session: dt.date = SESSION_1,
    *,
    permitted: bool = True,
    batch_salt: str = "",
) -> AcceptedCloseBatch:
    """Build an offline accepted-close contract without provider calls."""
    closes: list[AcceptedClose] = []
    for raw_symbol, raw_price in sorted(prices.items()):
        symbol = raw_symbol.upper()
        price = Decimal(str(raw_price))
        instrument = InstrumentRef(
            canonical_symbol=symbol,
            asset_type="etf",
            exchange_mic="XNAS",
            currency="USD",
            calendar_id="XNAS",
        )
        close_id = _digest(
            {
                "symbol": symbol,
                "session": session,
                "price": price,
                "permitted": permitted,
                "salt": batch_salt,
            }
        )
        observations = tuple(
            CloseObservation(
                provider_id=f"test-{provider_name}",
                provider_version="fixture-v1",
                independence_group=f"fixture-{provider_name}",
                source_tier=provider_name,
                settlement_eligible=True,
                canonical_symbol=symbol,
                provider_symbol=symbol,
                asset_type="etf",
                exchange_mic="XNAS",
                session_date=session,
                raw_close=price,
                currency="USD",
                exchange_timezone="America/New_York",
                bar_kind="regular_session_close",
                adjustment_mode="none",
                price_unit_multiplier=Decimal("1"),
                retrieved_at=dt.datetime.combine(
                    session,
                    dt.time(22, 0),
                    tzinfo=dt.timezone.utc,
                ),
                payload_sha256=_digest(
                    {"provider": provider_name, "symbol": symbol, "price": price}
                ),
                finality="final",
                corporate_action_status="clear_none",
                provider_drift_status="healthy",
                calendar_id="XNAS",
            )
            for provider_name in ("primary", "secondary")
        )
        closes.append(
            AcceptedClose(
                accepted_close_id=close_id,
                instrument=instrument,
                expected_session=session,
                status="accepted" if permitted else "blocked",
                selected_observation_id=(
                    observations[0].observation_id if permitted else None
                ),
                selected_price=price if permitted else None,
                currency="USD",
                agreement_bps=Decimal("0"),
                independent_source_count=2 if permitted else 1,
                observations=observations,
                attempts=(),
                reasons=() if permitted else ("synthetic_price_gate_block",),
                valuation_permitted=permitted,
                price_gate_permitted=permitted,
                finality="confirmed" if permitted else "blocked",
                atomic_batch_permitted=permitted,
            )
        )
    batch_id = _digest(
        {
            "session": session,
            "closes": [item.accepted_close_id for item in closes],
            "permitted": permitted,
            "salt": batch_salt,
        }
    )
    return AcceptedCloseBatch(
        batch_id=batch_id,
        expected_session=session,
        closes=tuple(closes),
        status="accepted" if permitted else "blocked",
        price_gate_permitted=permitted,
        reasons=() if permitted else ("atomic_batch_blocked",),
    )


def _statuses(*symbols: str, value: str = "clear_none") -> dict[str, str]:
    return {symbol.upper(): value for symbol in symbols}


def _event_count(ledger: PortfolioLedger) -> int:
    return ledger.project("modeled").event_count


def test_binary_float_is_rejected_and_decimal_round_trip_is_exact(tmp_path):
    with pytest.raises(LedgerValidationError, match="binary floating point"):
        OpeningPosition("AAA", 0.1, Decimal("1"))
    with pytest.raises(LedgerValidationError, match="binary floating point"):
        DcaPlan("base", "v1", {"AAA": 20.0})
    with pytest.raises(LedgerValidationError, match="(?i)(two|2).*decimal"):
        DcaPlan("base", "v1", {"AAA": Decimal("20.001")})

    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    with pytest.raises(LedgerValidationError, match="binary floating point"):
        ledger.initialize(SESSION_0, 0.3)

    ledger.initialize(
        SESSION_0,
        Decimal("0.30"),
        [OpeningPosition("AAA", Decimal("0.1"), Decimal("0.2"))],
    )
    projection = ledger.project("modeled")
    assert projection.cash == Decimal("0.30")
    assert projection.by_symbol["AAA"].quantity == Decimal("0.1")
    assert projection.by_symbol["AAA"].average_economic_cost == Decimal("0.2")
    assert projection.by_symbol["AAA"].economic_cost == Decimal("0.02")


def test_cash_flow_is_external_but_income_and_standalone_fee_are_not(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    ledger.record_cash_flow(
        SESSION_1,
        Decimal("25"),
        description="owner contribution",
        idempotency_key="cash-flow-1",
    )
    ledger.record_income(
        SESSION_1,
        Decimal("3"),
        symbol="AAA",
        description="confirmed dividend",
        idempotency_key="income-1",
    )
    ledger.record_fee(
        SESSION_1,
        Decimal("2"),
        description="confirmed account fee",
        idempotency_key="fee-1",
    )

    projection = ledger.project("confirmed")
    assert projection.cash == Decimal("126")
    assert projection.net_external_flow == Decimal("25")
    assert projection.fees == Decimal("2")
    assert projection.realized_pnl == Decimal("1")


def test_opening_snapshot_is_idempotent_but_conflicting_openings_fail(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    positions = [OpeningPosition("AAA", Decimal("2"), Decimal("10"))]

    first = ledger.initialize(SESSION_0, Decimal("100"), positions)
    replay = ledger.initialize(SESSION_0, Decimal("100.00"), positions)

    assert replay == first
    assert _event_count(ledger) == 1
    with pytest.raises((LedgerAlreadyInitializedError, LedgerIdempotencyConflict)):
        ledger.initialize(SESSION_0, Decimal("101"), positions)
    with pytest.raises(LedgerAlreadyInitializedError):
        ledger.initialize(SESSION_0, Decimal("100"), positions, idempotency_key="other-opening")
    assert _event_count(ledger) == 1


def test_modeled_dca_changes_only_modeled_book(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    plan = DcaPlan("daily-base", "v1", {"AAA": Decimal("20")})
    result = ledger.settle_modeled_dca_batch(
        plan,
        _accepted_batch({"AAA": "10"}),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )

    confirmed = ledger.project("confirmed")
    modeled = ledger.project("modeled")
    assert result.total_spend == Decimal("20")
    assert "AAA" not in confirmed.by_symbol
    assert confirmed.cash == Decimal("100")
    assert modeled.by_symbol["AAA"].quantity == Decimal("2")
    assert modeled.by_symbol["AAA"].modeled_quantity == Decimal("2")
    assert modeled.cash == Decimal("80")


@pytest.mark.parametrize(
    ("batch", "calendar_as_of", "corporate_statuses"),
    [
        (_accepted_batch({"AAA": "10"}), AFTER_SESSION_2, _statuses("AAA", "BBB")),
        (
            _accepted_batch({"AAA": "10", "BBB": "20"}),
            dt.datetime(2026, 7, 30, 19, 59, 59, tzinfo=dt.timezone.utc),
            _statuses("AAA", "BBB"),
        ),
        (
            _accepted_batch({"AAA": "10", "BBB": "20"}),
            AFTER_SESSION_2,
            {"AAA": "clear_none", "BBB": "not_checked"},
        ),
        (
            _accepted_batch({"AAA": "10", "BBB": "20"}, permitted=False),
            AFTER_SESSION_2,
            _statuses("AAA", "BBB"),
        ),
    ],
    ids=["missing-plan-symbol", "calendar-incomplete", "corporate-action", "price-gate"],
)
def test_any_dca_gate_failure_blocks_the_whole_batch_without_partial_writes(
    tmp_path,
    batch,
    calendar_as_of,
    corporate_statuses,
):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    plan = DcaPlan(
        "daily-base",
        "v1",
        {"AAA": Decimal("20"), "BBB": Decimal("20")},
    )
    before = _event_count(ledger)

    with pytest.raises(LedgerSettlementBlocked):
        ledger.settle_modeled_dca_batch(
            plan,
            batch,
            calendar_as_of=calendar_as_of,
            corporate_action_statuses=corporate_statuses,
        )

    assert _event_count(ledger) == before
    assert ledger.project("modeled").cash == Decimal("100")
    assert ledger.project("modeled").positions == ()


def test_internally_inconsistent_price_contract_and_duplicate_action_keys_fail_closed(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    plan = DcaPlan("daily-base", "v1", {"AAA": Decimal("20")})
    valid = _accepted_batch({"AAA": "10"})

    inconsistent_batch = replace(valid, status="blocked")
    with pytest.raises(LedgerSettlementBlocked, match="price gate"):
        ledger.settle_modeled_dca_batch(
            plan,
            inconsistent_batch,
            calendar_as_of=AFTER_SESSION_2,
            corporate_action_statuses=_statuses("AAA"),
        )

    inconsistent_close = replace(valid.closes[0], status="blocked")
    inconsistent_child_batch = replace(valid, closes=(inconsistent_close,))
    with pytest.raises(LedgerSettlementBlocked, match="not eligible"):
        ledger.settle_modeled_dca_batch(
            plan,
            inconsistent_child_batch,
            calendar_as_of=AFTER_SESSION_2,
            corporate_action_statuses=_statuses("AAA"),
        )

    with pytest.raises(LedgerSettlementBlocked, match="duplicate corporate-action"):
        ledger.settle_modeled_dca_batch(
            plan,
            valid,
            calendar_as_of=AFTER_SESSION_2,
            corporate_action_statuses={"aaa": "clear_none", "AAA": "clear_none"},
        )

    assert _event_count(ledger) == 1


def test_same_session_rerun_is_idempotent_but_changed_plan_or_close_conflicts(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("200"))
    plan = DcaPlan("daily-base", "v1", {"AAA": Decimal("20")})
    batch = _accepted_batch({"AAA": "10"})

    first = ledger.settle_modeled_dca_batch(
        plan,
        batch,
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    replay = ledger.settle_modeled_dca_batch(
        plan,
        batch,
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )

    assert replay.idempotent_replay is True
    assert replay.batch_event_id == first.batch_event_id
    assert replay.fill_event_ids == first.fill_event_ids
    assert _event_count(ledger) == 3  # opening + immutable batch marker + modeled fill

    changed_plan = DcaPlan("daily-base", "v1", {"AAA": Decimal("25")})
    with pytest.raises(LedgerIdempotencyConflict):
        ledger.settle_modeled_dca_batch(
            changed_plan,
            batch,
        calendar_as_of=AFTER_SESSION_2,
            corporate_action_statuses=_statuses("AAA"),
        )

    changed_close = _accepted_batch({"AAA": "10.25"}, batch_salt="corrected")
    with pytest.raises(LedgerIdempotencyConflict):
        ledger.settle_modeled_dca_batch(
            plan,
            changed_close,
        calendar_as_of=AFTER_SESSION_2,
            corporate_action_statuses=_statuses("AAA"),
        )
    with pytest.raises(LedgerIdempotencyConflict):
        ledger.settle_modeled_dca_batch(
            DcaPlan("replacement-plan", "v2", {"AAA": Decimal("20")}),
            batch,
        calendar_as_of=AFTER_SESSION_2,
            corporate_action_statuses=_statuses("AAA"),
        )
    assert _event_count(ledger) == 3


def test_dca_rechecks_real_two_source_lineage_instead_of_gate_booleans(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    plan = DcaPlan("daily-base", "v1", {"AAA": Decimal("20")})
    valid = _accepted_batch({"AAA": "10"}, SESSION_1)
    close = valid.closes[0]
    duplicate_group_observations = (
        close.observations[0],
        replace(
            close.observations[1],
            independence_group=close.observations[0].independence_group,
        ),
    )
    research_observations = tuple(
        replace(item, source_tier="research_only") for item in close.observations
    )
    invalid_closes = (
        replace(
            close,
            observations=(),
            selected_observation_id="f" * 64,
            independent_source_count=2,
        ),
        replace(close, observations=duplicate_group_observations),
        replace(
            close,
            observations=research_observations,
            selected_observation_id=research_observations[0].observation_id,
        ),
    )
    before = _event_count(ledger)

    for invalid_close in invalid_closes:
        with pytest.raises(LedgerSettlementBlocked, match="(?i)(source|lineage)"):
            ledger.settle_modeled_dca_batch(
                plan,
                replace(valid, closes=(invalid_close,)),
                calendar_as_of=AFTER_SESSION_2,
                corporate_action_statuses=_statuses("AAA"),
            )
        assert _event_count(ledger) == before


def test_symbol_level_dca_receipts_are_immutable_and_identical_on_replay(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    plan = DcaPlan(
        "daily-base",
        "v1",
        {"AAA": Decimal("20"), "BBB": Decimal("20")},
        share_scale=2,
    )
    accepted = _accepted_batch({"AAA": "6", "BBB": "7"})

    first = ledger.settle_modeled_dca_batch(
        plan,
        accepted,
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA", "BBB"),
    )
    replay = ledger.settle_modeled_dca_batch(
        plan,
        accepted,
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA", "BBB"),
    )

    assert replay.idempotent_replay is True
    assert replay.fill_receipts == first.fill_receipts
    assert replay.fill_event_ids == first.fill_event_ids
    assert tuple(item.symbol for item in first.fill_receipts) == ("AAA", "BBB")
    aaa = first.receipts_by_symbol["AAA"]
    assert aaa.quantity == Decimal("3.33")
    assert aaa.price == Decimal("6")
    assert aaa.spend == Decimal("19.98")
    assert aaa.residual == Decimal("0.02")
    assert aaa.accepted_close_id == accepted.by_symbol["AAA"].accepted_close_id
    assert aaa.settlement_event_id == first.fill_event_ids[0]
    with pytest.raises(TypeError):
        first.receipts_by_symbol["AAA"] = aaa


def test_external_contribution_and_existing_cash_funding_conserve_cash(tmp_path):
    external = PortfolioLedger(tmp_path / "external.sqlite")
    external.initialize(SESSION_0, Decimal("0"))
    ext_plan = DcaPlan(
        "daily-base",
        "v1",
        {"AAA": Decimal("20")},
        funding_mode="modeled_external_contribution",
        share_scale=2,
    )
    ext_result = external.settle_modeled_dca_batch(
        ext_plan,
        _accepted_batch({"AAA": "6"}),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    ext_projection = external.project("modeled")
    assert ext_result.total_configured_amount == Decimal("20")
    assert ext_result.total_spend == Decimal("19.98")
    assert ext_result.total_residual == Decimal("0.02")
    assert ext_projection.cash == Decimal("0.02")
    assert ext_projection.net_external_flow == Decimal("20")

    existing = PortfolioLedger(tmp_path / "existing.sqlite")
    existing.initialize(SESSION_0, Decimal("20"))
    cash_plan = replace(ext_plan, funding_mode="existing_cash")
    cash_result = existing.settle_modeled_dca_batch(
        cash_plan,
        _accepted_batch({"AAA": "6"}),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    cash_projection = existing.project("modeled")
    assert cash_result.total_spend + cash_result.total_residual == Decimal("20")
    assert cash_projection.cash == Decimal("0.02")
    assert cash_projection.net_external_flow == Decimal("0")


def test_existing_cash_shortfall_is_atomic_and_rounding_is_always_down(tmp_path):
    insufficient = PortfolioLedger(tmp_path / "insufficient.sqlite")
    insufficient.initialize(SESSION_0, Decimal("19.97"))
    plan = DcaPlan(
        "daily-base",
        "v1",
        {"AAA": Decimal("20")},
        funding_mode="existing_cash",
        share_scale=2,
    )
    before = _event_count(insufficient)
    with pytest.raises(LedgerInsufficientCash):
        insufficient.settle_modeled_dca_batch(
            plan,
            _accepted_batch({"AAA": "6"}),
        calendar_as_of=AFTER_SESSION_2,
            corporate_action_statuses=_statuses("AAA"),
        )
    assert _event_count(insufficient) == before

    funded = PortfolioLedger(tmp_path / "funded.sqlite")
    funded.initialize(SESSION_0, Decimal("20"))
    result = funded.settle_modeled_dca_batch(
        plan,
        _accepted_batch({"AAA": "6"}),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    position = funded.project("modeled").by_symbol["AAA"]
    assert position.quantity == Decimal("3.33")
    assert position.quantity * Decimal("6") == Decimal("19.98")
    assert result.total_spend <= result.total_configured_amount
    assert result.total_residual == Decimal("0.02")


def test_owner_confirmed_fill_replaces_modeled_fill_instead_of_doubling_it(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    plan = DcaPlan("daily-base", "v1", {"AAA": Decimal("20")})
    modeled = ledger.settle_modeled_dca_batch(
        plan,
        _accepted_batch({"AAA": "10"}),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )

    confirmed_event = ledger.record_user_confirmed_fill(
        SESSION_1,
        "AAA",
        "buy",
        Decimal("1.9"),
        Decimal("10.25"),
        fees=Decimal("0.05"),
        idempotency_key="owner-fill-1",
        replaces_modeled_event_id=modeled.fill_event_ids[0],
    )
    assert confirmed_event

    confirmed = ledger.project("confirmed")
    projected = ledger.project("modeled")
    assert confirmed.by_symbol["AAA"].quantity == Decimal("1.9")
    assert projected.by_symbol["AAA"].quantity == Decimal("1.9")
    assert projected.by_symbol["AAA"].modeled_quantity == Decimal("0")
    assert projected.cash == confirmed.cash


def test_manual_buys_sells_fees_weighted_cost_and_realized_unrealized_pnl(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(
        SESSION_0,
        Decimal("1000"),
        [OpeningPosition("AAA", Decimal("10"), Decimal("10"))],
    )
    ledger.record_user_confirmed_fill(
        SESSION_1,
        "AAA",
        "buy",
        Decimal("5"),
        Decimal("20"),
        fees=Decimal("5"),
        idempotency_key="buy-1",
    )
    ledger.record_user_confirmed_fill(
        SESSION_2,
        "AAA",
        "sell",
        Decimal("6"),
        Decimal("30"),
        fees=Decimal("6"),
        idempotency_key="sell-1",
    )

    projection = ledger.project("confirmed")
    position = projection.by_symbol["AAA"]
    assert position.quantity == Decimal("9")
    assert position.economic_cost == Decimal("123")
    assert position.average_economic_cost == _ledger_ratio("123", "9")
    assert position.realized_pnl == Decimal("92")
    assert projection.realized_pnl == Decimal("92")
    assert projection.fees == Decimal("11")
    assert projection.cash == Decimal("1069")

    valuation = ledger.record_valuation("confirmed", _accepted_batch({"AAA": "25"}, SESSION_2))
    unrealized = valuation.securities_value - projection.total_economic_cost
    assert valuation.securities_value == Decimal("225")
    assert unrealized == Decimal("102")


def test_split_is_applied_before_same_session_dca_and_preserves_cost(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(
        SESSION_0,
        Decimal("100"),
        [OpeningPosition("AAA", Decimal("10"), Decimal("20"))],
    )
    ledger.record_split(SESSION_1, "AAA", Decimal("2"), idempotency_key="split-aaa")
    result = ledger.settle_modeled_dca_batch(
        DcaPlan("daily-base", "v1", {"AAA": Decimal("20")}),
        _accepted_batch({"AAA": "10"}),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses={"AAA": "reconciled"},
    )

    position = ledger.project("modeled").by_symbol["AAA"]
    assert result.total_spend == Decimal("20")
    assert position.quantity == Decimal("22")
    assert position.economic_cost == Decimal("220")
    assert position.average_economic_cost == Decimal("10")


def test_explicit_skip_records_no_modeled_fill(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    plan = DcaPlan("daily-base", "v1", {"AAA": Decimal("20")})
    override_id = ledger.record_dca_override(
        SESSION_1,
        plan.plan_id,
        plan.version,
        action="skip",
        reason="owner requested no base DCA",
    )
    result = ledger.settle_modeled_dca_batch(
        plan,
        _accepted_batch({"AAA": "10"}),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )

    assert override_id
    assert result.skipped is True
    assert result.fill_event_ids == ()
    assert ledger.project("modeled").positions == ()
    assert ledger.project("modeled").cash == Decimal("100")
    audit = ledger.session_audit(SESSION_1)
    assert audit.dca_settlement is None
    assert audit.has_owner_skip is True
    assert audit.owner_skip is not None
    assert audit.owner_skip.override_event_id == override_id
    assert audit.owner_skip.plan_id == plan.plan_id
    assert audit.owner_skip.plan_version == plan.version


def test_close_funded_dca_creates_no_same_day_return(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(
        SESSION_0,
        Decimal("100"),
        [OpeningPosition("AAA", Decimal("1"), Decimal("10"))],
    )
    baseline = ledger.record_valuation("modeled", _accepted_batch({"AAA": "10"}, SESSION_0))
    assert baseline.daily_return is None

    plan = DcaPlan(
        "daily-base",
        "v1",
        {"AAA": Decimal("20")},
        funding_mode="modeled_external_contribution",
    )
    ledger.settle_modeled_dca_batch(
        plan,
        _accepted_batch({"AAA": "20"}, SESSION_1),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    valued = ledger.record_valuation("modeled", _accepted_batch({"AAA": "20"}, SESSION_1))

    assert valued.nav == Decimal("140")
    assert valued.net_external_flow == Decimal("20")
    assert valued.weighted_external_flow == Decimal("0")
    assert valued.daily_pnl == Decimal("10")
    assert valued.daily_return == _ledger_ratio("10", "110")


def test_valuation_blocks_when_any_nonzero_position_lacks_a_final_close(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(
        SESSION_0,
        Decimal("0"),
        [
            OpeningPosition("AAA", Decimal("1"), Decimal("10")),
            OpeningPosition("BBB", Decimal("2"), Decimal("20")),
        ],
    )
    before = _event_count(ledger)
    with pytest.raises(LedgerSettlementBlocked):
        ledger.record_valuation("modeled", _accepted_batch({"AAA": "11"}, SESSION_1))
    assert _event_count(ledger) == before


def test_daily_returns_chain_into_cumulative_time_weighted_return(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(
        SESSION_0,
        Decimal("0"),
        [OpeningPosition("AAA", Decimal("1"), Decimal("100"))],
    )
    initial = ledger.record_valuation("modeled", _accepted_batch({"AAA": "100"}, SESSION_0))
    up = ledger.record_valuation("modeled", _accepted_batch({"AAA": "110"}, SESSION_1))
    down = ledger.record_valuation("modeled", _accepted_batch({"AAA": "99"}, SESSION_2))

    assert initial.daily_return is None
    assert initial.cumulative_twr is None
    assert up.daily_return == Decimal("0.1")
    assert up.cumulative_twr == Decimal("0.1")
    assert down.daily_return == Decimal("-0.1")
    assert down.cumulative_twr == Decimal("-0.01")


def test_valuation_lineage_prior_values_and_public_recovery_api(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(
        SESSION_0,
        Decimal("100"),
        [OpeningPosition("AAA", Decimal("1"), Decimal("10"))],
    )
    checkpoint = ledger.opening_checkpoint()
    assert checkpoint.session == SESSION_0
    assert checkpoint.cash == Decimal("100")
    assert checkpoint.positions == (
        OpeningPosition("AAA", Decimal("1"), Decimal("10")),
    )
    assert ledger.contains_event_hash(checkpoint.opening_event_hash) is True
    assert ledger.contains_event_hash("f" * 64) is False
    with pytest.raises(LedgerValidationError, match="SHA-256"):
        ledger.contains_event_hash("not-a-hash")

    opening_close = _accepted_batch({"AAA": "10"}, SESSION_0)
    confirmed_opening = ledger.record_valuation("confirmed", opening_close)
    modeled_opening = ledger.record_valuation("modeled", opening_close)
    assert confirmed_opening.prior_nav is None
    assert confirmed_opening.prior_cumulative_twr is None
    assert confirmed_opening.accepted_close_lineage[
        "AAA"
    ].accepted_close_id == opening_close.by_symbol["AAA"].accepted_close_id
    assert confirmed_opening.accepted_close_lineage[
        "AAA"
    ].selected_provider_id == "test-primary"
    with pytest.raises(TypeError):
        confirmed_opening.accepted_close_lineage["AAA"] = confirmed_opening.accepted_close_lineage[
            "AAA"
        ]
    with pytest.raises(TypeError):
        confirmed_opening.prices["AAA"] = Decimal("999")

    plan = DcaPlan(
        "daily-base",
        "v1",
        {"AAA": Decimal("20")},
        funding_mode="modeled_external_contribution",
    )
    session_close = _accepted_batch({"AAA": "20"}, SESSION_1)
    settled = ledger.settle_modeled_dca_batch(
        plan,
        session_close,
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    confirmed = ledger.record_valuation("confirmed", session_close)

    partial = ledger.session_audit(SESSION_1)
    assert partial.dca_settlement is not None
    assert partial.dca_settlement.fill_receipts == settled.fill_receipts
    assert partial.confirmed_valuation == confirmed
    assert partial.modeled_valuation is None
    assert partial.valuation_state == "partial"
    assert partial.has_partial_valuation is True
    assert ledger.latest_common_valuation_session() == SESSION_0

    modeled = ledger.record_valuation("modeled", session_close)
    assert confirmed.prior_nav == confirmed_opening.nav
    assert confirmed.prior_cumulative_twr == confirmed_opening.cumulative_twr
    assert modeled.prior_nav == modeled_opening.nav
    assert modeled.prior_cumulative_twr == modeled_opening.cumulative_twr
    assert ledger.valuation_at("confirmed", SESSION_1) == confirmed
    assert ledger.valuation_at("confirmed", SESSION_2) is None

    completed = ledger.session_audit(SESSION_1)
    assert completed.valuation_state == "complete"
    assert completed.has_partial_valuation is False
    assert ledger.contains_event_hash(completed.last_event_hash) is True
    common = ledger.latest_common_valuation()
    assert common is not None
    assert common.session == SESSION_1
    assert common.confirmed == confirmed
    assert common.modeled == modeled


def test_complete_valuations_without_marker_require_an_explicit_active_owner_skip(tmp_path):
    anomalous = PortfolioLedger(tmp_path / "anomalous.sqlite")
    anomalous.initialize(
        SESSION_0,
        Decimal("0"),
        [OpeningPosition("AAA", Decimal("1"), Decimal("10"))],
    )
    close = _accepted_batch({"AAA": "11"}, SESSION_1)
    anomalous.record_valuation("confirmed", close)
    anomalous.record_valuation("modeled", close)

    audit = anomalous.session_audit(SESSION_1)
    assert audit.valuation_state == "complete"
    assert audit.dca_settlement is None
    assert audit.owner_skip is None
    assert audit.has_owner_skip is False

    explicit = PortfolioLedger(tmp_path / "explicit.sqlite")
    explicit.initialize(
        SESSION_0,
        Decimal("0"),
        [OpeningPosition("AAA", Decimal("1"), Decimal("10"))],
    )
    override_id = explicit.record_dca_override(
        SESSION_1,
        "daily-base",
        "v1",
        reason="owner intentionally skipped",
    )
    explicit.record_valuation("confirmed", close)
    explicit.record_valuation("modeled", close)

    explicit_audit = explicit.session_audit(SESSION_1)
    assert explicit_audit.valuation_state == "complete"
    assert explicit_audit.dca_settlement is None
    assert explicit_audit.has_owner_skip is True
    assert explicit_audit.owner_skip is not None
    assert explicit_audit.owner_skip.override_event_id == override_id
    assert explicit_audit.owner_skip.plan_id == "daily-base"
    assert explicit_audit.owner_skip.plan_version == "v1"

    reversed_ledger = PortfolioLedger(tmp_path / "reversed.sqlite")
    reversed_ledger.initialize(SESSION_0, Decimal("100"))
    reversed_override = reversed_ledger.record_dca_override(
        SESSION_1,
        "daily-base",
        "v1",
        reason="temporary owner skip",
    )
    reversed_ledger.reverse_event(
        reversed_override,
        reason="owner restored the base plan",
        idempotency_key="reverse-owner-skip",
    )
    reversed_audit = reversed_ledger.session_audit(SESSION_1)
    assert reversed_audit.owner_skip is None
    assert reversed_audit.has_owner_skip is False


def test_valuation_requires_final_multi_source_atomic_close_lineage(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(
        SESSION_0,
        Decimal("0"),
        [OpeningPosition("AAA", Decimal("1"), Decimal("10"))],
    )
    valid = _accepted_batch({"AAA": "11"}, SESSION_1)
    close = valid.closes[0]
    research_observations = tuple(
        replace(item, source_tier="research_only") for item in close.observations
    )
    invalid_batches = (
        replace(valid, status="blocked"),
        replace(valid, closes=(replace(close, finality="provisional"),)),
        replace(valid, closes=(replace(close, independent_source_count=1),)),
        replace(valid, closes=(replace(close, observations=(close.observations[0],)),)),
        replace(
            valid,
            closes=(replace(close, reasons=("test-secondary:rejected_fixture",)),),
        ),
        replace(
            valid,
            closes=(
                replace(
                    close,
                    observations=research_observations,
                    selected_observation_id=research_observations[0].observation_id,
                ),
            ),
        ),
        replace(valid, closes=(replace(close, atomic_batch_permitted=False),)),
        replace(valid, closes=(replace(close, selected_observation_id="f" * 64),)),
    )
    before = _event_count(ledger)

    for batch in invalid_batches:
        with pytest.raises(LedgerSettlementBlocked):
            ledger.record_valuation("modeled", batch)
        assert _event_count(ledger) == before

    warned_final = replace(
        valid,
        closes=(replace(close, status="warning", finality="confirmed_with_warning"),),
    )
    valuation = ledger.record_valuation("modeled", warned_final)
    assert valuation.nav == Decimal("11")


def test_legacy_valuation_payload_fails_closed_with_explicit_migration_error(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    with ledger._transaction() as connection:
        ledger._append_event(
            connection,
            event_type="valuation",
            source_class="system",
            session=SESSION_1,
            occurred_at=f"{SESSION_1.isoformat()}T23:59:59Z",
            idempotency_key="legacy-valuation-fixture",
            payload={
                "book_kind": "modeled",
                "accepted_close_batch_id": "a" * 64,
                "currency": "USD",
                "cash": Decimal("100"),
                "securities_value": Decimal("0"),
                "nav": Decimal("100"),
                "prices": {},
                "daily_pnl": None,
                "daily_return": None,
                "cumulative_twr": None,
                "net_external_flow": Decimal("0"),
                "weighted_external_flow": Decimal("0"),
            },
        )

    assert ledger.verify_hash_chain() is True
    with pytest.raises(LedgerIntegrityError, match="legacy valuation payload"):
        ledger.valuation_at("modeled", SESSION_1)
    with pytest.raises(LedgerIntegrityError, match="legacy valuation payload"):
        ledger.latest_common_valuation_session()
    with pytest.raises(LedgerIntegrityError, match="legacy valuation payload"):
        ledger.project("modeled")
    with sqlite3.connect(ledger.database_path) as connection:
        before_count = connection.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
    with pytest.raises(LedgerIntegrityError, match="legacy valuation payload"):
        ledger.record_cash_flow(
            SESSION_2,
            Decimal("1"),
            idempotency_key="must-not-extend-legacy-ledger",
        )
    with sqlite3.connect(ledger.database_path) as connection:
        after_count = connection.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0]
    assert after_count == before_count


def test_hash_valid_v2_valuation_cannot_forge_its_actual_prior_chain(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(
        SESSION_0,
        Decimal("0"),
        [OpeningPosition("AAA", Decimal("1"), Decimal("100"))],
    )
    baseline = ledger.record_valuation(
        "modeled",
        _accepted_batch({"AAA": "100"}, SESSION_0),
    )
    with ledger._transaction() as connection:
        ledger._append_event(
            connection,
            event_type="valuation",
            source_class="system",
            session=SESSION_1,
            occurred_at=f"{SESSION_1.isoformat()}T23:59:59Z",
            idempotency_key="forged-v2-prior-fixture",
            payload={
                "contract_version": "ledger_valuation/v2",
                "input_hash": "d" * 64,
                "book_kind": "modeled",
                "accepted_close_batch_id": "b" * 64,
                "currency": "USD",
                "cash": Decimal("0"),
                "securities_value": Decimal("200"),
                "nav": Decimal("200"),
                "prices": {"AAA": Decimal("200")},
                "accepted_close_lineage": {
                    "AAA": {
                        "accepted_close_id": "c" * 64,
                        "selected_provider_id": "test-primary",
                    }
                },
                "prior_nav": Decimal("50"),
                "prior_cumulative_twr": None,
                "daily_pnl": Decimal("150"),
                "daily_return": Decimal("3"),
                "cumulative_twr": Decimal("3"),
                "net_external_flow": Decimal("0"),
                "weighted_external_flow": Decimal("0"),
                "cumulative_external_flow": Decimal("0"),
                "previous_valuation_event_id": baseline.valuation_event_id,
            },
        )

    assert ledger.verify_hash_chain() is True
    for read_only_api in (
        lambda: ledger.valuation_at("modeled", SESSION_1),
        lambda: ledger.session_audit(SESSION_1),
        ledger.latest_common_valuation_session,
        ledger.opening_checkpoint,
        lambda: ledger.project("modeled"),
        lambda: ledger.contains_event_hash("f" * 64),
    ):
        with pytest.raises(LedgerIntegrityError, match="prior NAV or TWR"):
            read_only_api()


def test_mid_transaction_failure_rolls_back_every_dca_event(tmp_path, monkeypatch):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    plan = DcaPlan(
        "daily-base",
        "v1",
        {"AAA": Decimal("20"), "BBB": Decimal("20")},
    )
    before = _event_count(ledger)

    original_append = ledger._append_event
    calls = 0

    def fail_during_second_child(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected transaction failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(ledger, "_append_event", fail_during_second_child)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        ledger.settle_modeled_dca_batch(
            plan,
            _accepted_batch({"AAA": "10", "BBB": "20"}),
        calendar_as_of=AFTER_SESSION_2,
            corporate_action_statuses=_statuses("AAA", "BBB"),
        )

    assert _event_count(ledger) == before
    assert ledger.project("modeled").positions == ()
    assert ledger.verify_hash_chain() is True


def test_hash_chain_detects_payload_tampering(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    ledger.record_cash_flow(
        SESSION_1,
        Decimal("25"),
        description="owner contribution",
        idempotency_key="cash-1",
    )
    assert ledger.verify_hash_chain() is True

    with sqlite3.connect(ledger.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE ledger_events SET payload_json = ? WHERE sequence_no = 2",
                ('{"amount":"999999"}',),
            )
        connection.rollback()
        # Remove that independent defense only to prove that hash verification
        # still detects an attacker editing the SQLite file offline.
        connection.execute("DROP TRIGGER ledger_events_no_update")
        connection.execute(
            "UPDATE ledger_events SET payload_json = ? WHERE sequence_no = 2",
            ('{"amount":"999999"}',),
        )
        connection.commit()

    with pytest.raises(LedgerIntegrityError):
        ledger.verify_hash_chain()
    for read_only_api in (
        ledger.opening_checkpoint,
        ledger.latest_common_valuation_session,
        lambda: ledger.valuation_at("modeled", SESSION_1),
        lambda: ledger.session_audit(SESSION_1),
        lambda: ledger.contains_event_hash("f" * 64),
    ):
        with pytest.raises(LedgerIntegrityError):
            read_only_api()


def test_all_ledger_workflows_are_offline_and_expose_no_broker_or_order_method(
    tmp_path,
    monkeypatch,
):
    def forbid_network(*_args, **_kwargs):
        raise AssertionError("portfolio ledger attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    ledger.settle_modeled_dca_batch(
        DcaPlan("daily-base", "v1", {"AAA": Decimal("20")}),
        _accepted_batch({"AAA": "10"}),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    ledger.record_valuation("modeled", _accepted_batch({"AAA": "10"}, SESSION_1))

    public_names = {name.lower() for name in dir(ledger) if not name.startswith("_")}
    assert not any("broker" in name or "order" in name or "execute" in name for name in public_names)
    assert ledger.verify_hash_chain() is True


def test_existing_database_cannot_be_reinterpreted_under_a_different_currency_policy(tmp_path):
    database_path = tmp_path / "ledger.sqlite"
    usd_ledger = PortfolioLedger(database_path, policy=LedgerPolicy(currency="USD"))
    usd_ledger.initialize(SESSION_0, Decimal("100"))
    before_hash = usd_ledger.project("modeled").last_event_hash
    before_count = _event_count(usd_ledger)

    eur_ledger = PortfolioLedger(database_path, policy=LedgerPolicy(currency="EUR"))
    with pytest.raises(PortfolioLedgerError, match="(?i)currency"):
        eur_ledger.project("modeled")
    with pytest.raises(PortfolioLedgerError, match="(?i)currency"):
        eur_ledger.record_cash_flow(
            SESSION_1,
            Decimal("10"),
            idempotency_key="must-not-reinterpret-currency",
        )

    assert _event_count(usd_ledger) == before_count
    assert usd_ledger.project("modeled").last_event_hash == before_hash
    assert usd_ledger.verify_hash_chain() is True


def test_confirmed_cash_events_cannot_make_the_modeled_book_negative(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    ledger.settle_modeled_dca_batch(
        DcaPlan("daily-base", "v1", {"AAA": Decimal("80")}),
        _accepted_batch({"AAA": "10"}),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    assert ledger.project("confirmed").cash == Decimal("100")
    assert ledger.project("modeled").cash == Decimal("20")
    before_count = _event_count(ledger)
    before_hash = ledger.project("modeled").last_event_hash

    with pytest.raises(PortfolioLedgerError, match="(?i)(negative|cash)"):
        ledger.record_cash_flow(
            SESSION_2,
            Decimal("-30"),
            description="confirmed-only view could afford this",
            idempotency_key="unsafe-withdrawal",
        )
    assert _event_count(ledger) == before_count
    assert ledger.project("modeled").last_event_hash == before_hash

    with pytest.raises(PortfolioLedgerError, match="(?i)(negative|cash)"):
        ledger.record_fee(
            SESSION_2,
            Decimal("30"),
            description="must also preserve modeled cash",
            idempotency_key="unsafe-fee",
        )
    assert _event_count(ledger) == before_count
    assert ledger.project("modeled").cash == Decimal("20")
    assert ledger.project("modeled").last_event_hash == before_hash
    assert ledger.verify_hash_chain() is True


def test_atomic_dca_children_cannot_be_reversed_individually(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("0"))
    settled = ledger.settle_modeled_dca_batch(
        DcaPlan(
            "daily-base",
            "v1",
            {"AAA": Decimal("20")},
            funding_mode="modeled_external_contribution",
        ),
        _accepted_batch({"AAA": "10"}),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    assert settled.contribution_event_id is not None
    before_count = _event_count(ledger)
    before_hash = ledger.project("modeled").last_event_hash

    for event_id in (settled.fill_event_ids[0], settled.contribution_event_id):
        with pytest.raises(LedgerValidationError, match="cannot be reversed directly"):
            ledger.reverse_event(event_id, reason="must reverse the aggregate, not one child")

    assert _event_count(ledger) == before_count
    assert ledger.project("modeled").last_event_hash == before_hash
    assert ledger.project("modeled").cash == Decimal("0")
    assert ledger.project("modeled").by_symbol["AAA"].quantity == Decimal("2")
    assert ledger.verify_hash_chain() is True


def test_late_reported_events_replay_by_session_and_occurred_at_before_any_valuation(tmp_path):
    def build(path, insertion_order):
        ledger = PortfolioLedger(path)
        ledger.initialize(
            SESSION_0,
            Decimal("1000"),
            [OpeningPosition("AAA", Decimal("10"), Decimal("10"))],
        )
        events = {
            "day1_buy": lambda: ledger.record_user_confirmed_fill(
                SESSION_1,
                "AAA",
                "buy",
                Decimal("10"),
                Decimal("10"),
                occurred_at=dt.datetime(2026, 7, 30, 14, 0, tzinfo=dt.timezone.utc),
                idempotency_key="day1-buy",
            ),
            "day1_sell": lambda: ledger.record_user_confirmed_fill(
                SESSION_1,
                "AAA",
                "sell",
                Decimal("1"),
                Decimal("15"),
                occurred_at=dt.datetime(2026, 7, 30, 19, 0, tzinfo=dt.timezone.utc),
                idempotency_key="day1-sell",
            ),
            "day2_buy": lambda: ledger.record_user_confirmed_fill(
                SESSION_2,
                "AAA",
                "buy",
                Decimal("2"),
                Decimal("20"),
                occurred_at=dt.datetime(2026, 7, 31, 15, 0, tzinfo=dt.timezone.utc),
                idempotency_key="day2-buy",
            ),
        }
        for name in insertion_order:
            events[name]()
        return ledger.project("confirmed")

    on_time = build(
        tmp_path / "on-time.sqlite",
        ("day1_buy", "day1_sell", "day2_buy"),
    )
    late = build(
        tmp_path / "late.sqlite",
        ("day2_buy", "day1_sell", "day1_buy"),
    )

    assert late.cash == on_time.cash == Decimal("875")
    assert late.realized_pnl == on_time.realized_pnl == Decimal("5")
    assert late.by_symbol["AAA"] == on_time.by_symbol["AAA"]
    assert late.by_symbol["AAA"].quantity == Decimal("21")
    assert late.by_symbol["AAA"].economic_cost == Decimal("230")
    assert late.event_count == on_time.event_count
    assert late.last_event_hash != on_time.last_event_hash  # append audit order remains immutable


@pytest.mark.parametrize("book_kind", ["confirmed", "modeled"])
def test_any_valuation_freezes_owner_history_reversals_and_modeled_dca(
    tmp_path,
    book_kind,
):
    ledger = PortfolioLedger(tmp_path / f"{book_kind}.sqlite")
    ledger.initialize(
        SESSION_0,
        Decimal("100"),
        [OpeningPosition("AAA", Decimal("1"), Decimal("10"))],
    )
    reversible = ledger.record_cash_flow(
        SESSION_0,
        Decimal("10"),
        idempotency_key="pre-valuation-flow",
    )
    close = _accepted_batch({"AAA": "11"}, SESSION_1)
    ledger.record_valuation(book_kind, close)
    before_count = _event_count(ledger)
    before_hash = ledger.project("modeled").last_event_hash

    owner_mutations = (
        lambda: ledger.record_user_confirmed_fill(
            SESSION_1,
            "AAA",
            "buy",
            Decimal("1"),
            Decimal("1"),
            idempotency_key="late-fill",
        ),
        lambda: ledger.record_cash_flow(
            SESSION_1,
            Decimal("1"),
            idempotency_key="late-flow",
        ),
        lambda: ledger.record_income(
            SESSION_0,
            Decimal("1"),
            idempotency_key="late-income",
        ),
        lambda: ledger.record_fee(
            SESSION_1,
            Decimal("1"),
            idempotency_key="late-fee",
        ),
        lambda: ledger.record_split(
            SESSION_1,
            "AAA",
            Decimal("2"),
            idempotency_key="late-split",
        ),
    )
    for mutate in owner_mutations:
        with pytest.raises(PortfolioLedgerError, match="(?i)(valuation|final|locked)"):
            mutate()
        assert _event_count(ledger) == before_count
        assert ledger.project("modeled").last_event_hash == before_hash

    with pytest.raises(PortfolioLedgerError, match="(?i)(valuation|final|locked)"):
        ledger.reverse_event(reversible, reason="too late after valuation")
    with pytest.raises(PortfolioLedgerError, match="(?i)(valuation|final|locked)"):
        ledger.settle_modeled_dca_batch(
            DcaPlan("late-plan", "v1", {"AAA": Decimal("20")}),
            close,
            calendar_as_of=AFTER_SESSION_2,
            corporate_action_statuses=_statuses("AAA"),
        )

    assert _event_count(ledger) == before_count
    assert ledger.project("modeled").last_event_hash == before_hash
    assert ledger.verify_hash_chain() is True


@pytest.mark.parametrize(
    ("occurred_at", "valuation_weight", "expected_weighted_flow", "expected_return"),
    [
        (None, None, Decimal("0"), Decimal("0.1")),
        (
            dt.datetime(2026, 7, 30, 13, 30, tzinfo=dt.timezone.utc),
            Decimal("1"),
            Decimal("100"),
            Decimal("0.05"),
        ),
        (
            dt.datetime(2026, 7, 30, 16, 45, tzinfo=dt.timezone.utc),
            Decimal("0.5"),
            Decimal("50"),
            _ledger_ratio("10", "150"),
        ),
    ],
    ids=["implicit-close-weight-zero", "opening-weight-one", "midday-half-weight"],
)
def test_cash_flow_weights_produce_deterministic_twr(
    tmp_path,
    occurred_at,
    valuation_weight,
    expected_weighted_flow,
    expected_return,
):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(
        SESSION_0,
        Decimal("0"),
        [OpeningPosition("AAA", Decimal("1"), Decimal("100"))],
    )
    ledger.record_valuation("confirmed", _accepted_batch({"AAA": "100"}, SESSION_0))
    ledger.record_cash_flow(
        SESSION_1,
        Decimal("100"),
        occurred_at=occurred_at,
        valuation_weight=valuation_weight,
        idempotency_key="weighted-flow",
    )
    valued = ledger.record_valuation(
        "confirmed",
        _accepted_batch({"AAA": "110"}, SESSION_1),
    )

    assert valued.nav == Decimal("210")
    assert valued.daily_pnl == Decimal("10")
    assert valued.weighted_external_flow == expected_weighted_flow
    assert valued.daily_return == expected_return
    assert valued.cumulative_twr == expected_return


def test_explicit_cash_flow_time_requires_an_explicit_valuation_weight(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    before_count = _event_count(ledger)
    before_hash = ledger.project("confirmed").last_event_hash

    with pytest.raises(LedgerValidationError, match="(?i)weight"):
        ledger.record_cash_flow(
            SESSION_1,
            Decimal("10"),
            occurred_at=dt.datetime(2026, 7, 30, 15, 0, tzinfo=dt.timezone.utc),
            idempotency_key="ambiguous-flow-time",
        )

    with pytest.raises(LedgerValidationError, match="supplied together"):
        ledger.record_cash_flow(
            SESSION_1,
            Decimal("10"),
            valuation_weight=Decimal("1"),
            idempotency_key="ambiguous-flow-weight",
        )

    assert _event_count(ledger) == before_count
    assert ledger.project("confirmed").last_event_hash == before_hash


@pytest.mark.parametrize(
    ("session", "calendar_as_of"),
    [
        (
            SESSION_1,
            dt.datetime(2026, 7, 30, 19, 59, 59, tzinfo=dt.timezone.utc),
        ),
        (
            dt.date(2026, 8, 1),
            dt.datetime(2026, 8, 2, 5, 0, tzinfo=dt.timezone.utc),
        ),
        (
            dt.date(2026, 7, 3),
            dt.datetime(2026, 7, 4, 5, 0, tzinfo=dt.timezone.utc),
        ),
    ],
    ids=["before-regular-close", "weekend", "market-holiday"],
)
def test_calendar_blocks_uncompleted_or_non_session_dca_without_writes(
    tmp_path,
    session,
    calendar_as_of,
):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(dt.date(2026, 6, 1), Decimal("100"))
    before_count = _event_count(ledger)

    with pytest.raises(LedgerSettlementBlocked, match="(?i)(calendar|session|close|complete)"):
        ledger.settle_modeled_dca_batch(
            DcaPlan("daily-base", "v1", {"AAA": Decimal("20")}),
            _accepted_batch({"AAA": "10"}, session),
            calendar_as_of=calendar_as_of,
            corporate_action_statuses=_statuses("AAA"),
        )

    assert _event_count(ledger) == before_count
    assert ledger.project("modeled").positions == ()


def test_calendar_as_of_must_be_timezone_aware(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    with pytest.raises(PortfolioLedgerError, match="(?i)timezone-aware"):
        ledger.settle_modeled_dca_batch(
            DcaPlan("daily-base", "v1", {"AAA": Decimal("20")}),
            _accepted_batch({"AAA": "10"}),
            calendar_as_of=dt.datetime(2026, 7, 30, 21, 0),
            corporate_action_statuses=_statuses("AAA"),
        )
    assert _event_count(ledger) == 1


@pytest.mark.parametrize(
    ("opening_session", "session", "calendar_as_of"),
    [
        (
            SESSION_0,
            SESSION_1,
            dt.datetime(2026, 7, 30, 20, 0, tzinfo=dt.timezone.utc),
        ),
        (
            dt.date(2025, 11, 25),
            dt.date(2025, 11, 28),
            dt.datetime(2025, 11, 28, 18, 0, tzinfo=dt.timezone.utc),
        ),
    ],
    ids=["regular-close", "black-friday-half-day-close"],
)
def test_calendar_allows_dca_at_or_after_official_regular_and_half_day_close(
    tmp_path,
    opening_session,
    session,
    calendar_as_of,
):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(opening_session, Decimal("100"))
    result = ledger.settle_modeled_dca_batch(
        DcaPlan("daily-base", "v1", {"AAA": Decimal("20")}),
        _accepted_batch({"AAA": "10"}, session),
        calendar_as_of=calendar_as_of,
        corporate_action_statuses=_statuses("AAA"),
    )

    assert result.status == "settled"
    assert ledger.project("modeled").by_symbol["AAA"].quantity == Decimal("2")


@pytest.mark.parametrize(
    ("changed_amount", "changed_funding", "changed_scale"),
    [
        (Decimal("25"), "existing_cash", 2),
        (Decimal("20"), "modeled_external_contribution", 2),
        (Decimal("20"), "existing_cash", 3),
    ],
    ids=["base-amount", "funding-mode", "share-scale"],
)
def test_plan_id_and_version_are_immutable_across_sessions(
    tmp_path,
    changed_amount,
    changed_funding,
    changed_scale,
):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("200"))
    baseline_plan = DcaPlan(
        "daily-base",
        "v1",
        {"AAA": Decimal("20")},
        funding_mode="existing_cash",
        share_scale=2,
    )
    ledger.settle_modeled_dca_batch(
        baseline_plan,
        _accepted_batch({"AAA": "10"}, SESSION_1),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    before_count = _event_count(ledger)
    before_hash = ledger.project("modeled").last_event_hash
    changed_plan = DcaPlan(
        "daily-base",
        "v1",
        {"AAA": changed_amount},
        funding_mode=changed_funding,
        share_scale=changed_scale,
    )

    with pytest.raises(LedgerIdempotencyConflict, match="(?i)(plan|version|contract|immutable)"):
        ledger.settle_modeled_dca_batch(
            changed_plan,
            _accepted_batch({"AAA": "10"}, SESSION_2),
            calendar_as_of=AFTER_SESSION_2,
            corporate_action_statuses=_statuses("AAA"),
        )

    assert _event_count(ledger) == before_count
    assert ledger.project("modeled").last_event_hash == before_hash


def test_unchanged_plan_version_can_settle_on_the_next_completed_session(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    plan = DcaPlan("daily-base", "v1", {"AAA": Decimal("20")}, share_scale=2)
    first = ledger.settle_modeled_dca_batch(
        plan,
        _accepted_batch({"AAA": "10"}, SESSION_1),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    second = ledger.settle_modeled_dca_batch(
        plan,
        _accepted_batch({"AAA": "10"}, SESSION_2),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )

    assert first.batch_event_id != second.batch_event_id
    assert ledger.project("modeled").by_symbol["AAA"].quantity == Decimal("4")


def test_skip_override_does_not_hide_plan_definition_drift(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("200"))
    baseline = DcaPlan("daily-base", "v1", {"AAA": Decimal("20")})
    ledger.settle_modeled_dca_batch(
        baseline,
        _accepted_batch({"AAA": "10"}, SESSION_1),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    ledger.record_dca_override(
        SESSION_2,
        baseline.plan_id,
        baseline.version,
        reason="owner skip",
    )

    with pytest.raises(LedgerIdempotencyConflict, match="immutable"):
        ledger.settle_modeled_dca_batch(
            DcaPlan("daily-base", "v1", {"AAA": Decimal("25")}),
            _accepted_batch({"AAA": "10"}, SESSION_2),
            calendar_as_of=AFTER_SESSION_2,
            corporate_action_statuses=_statuses("AAA"),
        )


def test_reverse_dca_batch_is_atomic_idempotent_and_allows_correct_resettlement(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("0"))
    plan = DcaPlan(
        "daily-base",
        "v1",
        {"AAA": Decimal("20")},
        funding_mode="modeled_external_contribution",
    )
    close = _accepted_batch({"AAA": "10"}, SESSION_1)
    settled = ledger.settle_modeled_dca_batch(
        plan,
        close,
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    assert settled.batch_event_id is not None
    assert settled.contribution_event_id is not None

    reversed_event = ledger.reverse_dca_batch(
        settled.batch_event_id,
        reason="owner corrected the modeled session",
        idempotency_key="reverse-dca-1",
    )
    after_reverse = ledger.project("modeled")
    assert after_reverse.cash == Decimal("0")
    assert "AAA" not in after_reverse.by_symbol
    reverse_count = after_reverse.event_count
    reverse_hash = after_reverse.last_event_hash

    replay = ledger.reverse_dca_batch(
        settled.batch_event_id,
        reason="owner corrected the modeled session",
        idempotency_key="reverse-dca-1",
    )
    assert replay == reversed_event
    assert _event_count(ledger) == reverse_count
    assert ledger.project("modeled").last_event_hash == reverse_hash
    with pytest.raises(LedgerIdempotencyConflict):
        ledger.reverse_dca_batch(
            settled.batch_event_id,
            reason="different correction",
            idempotency_key="reverse-dca-2",
        )

    corrected = ledger.settle_modeled_dca_batch(
        plan,
        close,
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    assert corrected.batch_event_id != settled.batch_event_id
    assert ledger.project("modeled").cash == Decimal("0")
    assert ledger.project("modeled").by_symbol["AAA"].quantity == Decimal("2")

    ledger.record_valuation("modeled", close)
    before_locked_reverse = ledger.project("modeled").last_event_hash
    with pytest.raises(PortfolioLedgerError, match="(?i)(valuation|final|locked)"):
        ledger.reverse_dca_batch(
            corrected.batch_event_id,
            reason="must not rewrite a valued session",
            idempotency_key="reverse-after-valuation",
        )
    assert ledger.project("modeled").last_event_hash == before_locked_reverse
    assert ledger.verify_hash_chain() is True


def test_decimal_context_precision_and_rounding_cannot_change_ledger_results(tmp_path):
    def run(path, precision, rounding):
        with localcontext() as context:
            context.prec = precision
            context.rounding = rounding
            ledger = PortfolioLedger(path)
            ledger.initialize(
                SESSION_0,
                Decimal("1000"),
                [OpeningPosition("AAA", Decimal("10"), Decimal("10"))],
            )
            ledger.record_valuation(
                "modeled",
                _accepted_batch({"AAA": "10"}, SESSION_0),
            )
            settlement = ledger.settle_modeled_dca_batch(
                DcaPlan(
                    "daily-base",
                    "v1",
                    {"AAA": Decimal("20")},
                    share_scale=12,
                ),
                _accepted_batch({"AAA": "6"}, SESSION_1),
                calendar_as_of=AFTER_SESSION_2,
                corporate_action_statuses=_statuses("AAA"),
            )
            ledger.record_user_confirmed_fill(
                SESSION_2,
                "AAA",
                "sell",
                Decimal("3"),
                Decimal("17"),
                fees=Decimal("0.01"),
                occurred_at=dt.datetime(2026, 7, 31, 15, 0, tzinfo=dt.timezone.utc),
                idempotency_key="context-independent-sell",
            )
            projection = ledger.project("modeled")
            valuation = ledger.record_valuation(
                "modeled",
                _accepted_batch({"AAA": "11"}, SESSION_2),
            )
            position = projection.by_symbol["AAA"]
            return (
                settlement.total_spend,
                settlement.total_residual,
                projection.cash,
                projection.realized_pnl,
                position.quantity,
                position.economic_cost,
                position.average_economic_cost,
                position.modeled_quantity,
                valuation.nav,
                valuation.daily_pnl,
                valuation.daily_return,
                valuation.cumulative_twr,
            )

    baseline = run(tmp_path / "baseline.sqlite", 50, ROUND_HALF_EVEN)
    hostile = run(tmp_path / "hostile.sqlite", 6, ROUND_UP)
    assert hostile == baseline


def test_modeled_replacement_requires_buy_same_session_and_active_batch(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    settled = ledger.settle_modeled_dca_batch(
        DcaPlan("daily-base", "v1", {"AAA": Decimal("20")}),
        _accepted_batch({"AAA": "10"}, SESSION_1),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    assert settled.batch_event_id is not None
    modeled_fill = settled.fill_event_ids[0]
    before_count = _event_count(ledger)
    before_hash = ledger.project("modeled").last_event_hash

    with pytest.raises(LedgerValidationError, match="(?i)buy"):
        ledger.record_user_confirmed_fill(
            SESSION_1,
            "AAA",
            "sell",
            Decimal("1"),
            Decimal("10"),
            idempotency_key="replacement-must-not-sell",
            replaces_modeled_event_id=modeled_fill,
        )
    with pytest.raises(LedgerValidationError, match="(?i)session"):
        ledger.record_user_confirmed_fill(
            SESSION_2,
            "AAA",
            "buy",
            Decimal("1"),
            Decimal("10"),
            idempotency_key="replacement-wrong-session",
            replaces_modeled_event_id=modeled_fill,
        )
    assert _event_count(ledger) == before_count
    assert ledger.project("modeled").last_event_hash == before_hash

    ledger.reverse_dca_batch(
        settled.batch_event_id,
        reason="make the source batch inactive",
        idempotency_key="reverse-before-invalid-replacement",
    )
    inactive_count = _event_count(ledger)
    inactive_hash = ledger.project("modeled").last_event_hash
    with pytest.raises(LedgerValidationError, match="(?i)(active|reversed|batch)"):
        ledger.record_user_confirmed_fill(
            SESSION_1,
            "AAA",
            "buy",
            Decimal("1"),
            Decimal("10"),
            idempotency_key="replacement-inactive-batch",
            replaces_modeled_event_id=modeled_fill,
        )
    assert _event_count(ledger) == inactive_count
    assert ledger.project("modeled").last_event_hash == inactive_hash
    assert ledger.verify_hash_chain() is True


def test_dca_batch_with_active_confirmed_replacement_cannot_be_reversed(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.sqlite")
    ledger.initialize(SESSION_0, Decimal("100"))
    settled = ledger.settle_modeled_dca_batch(
        DcaPlan("daily-base", "v1", {"AAA": Decimal("20")}),
        _accepted_batch({"AAA": "10"}, SESSION_1),
        calendar_as_of=AFTER_SESSION_2,
        corporate_action_statuses=_statuses("AAA"),
    )
    assert settled.batch_event_id is not None
    ledger.record_user_confirmed_fill(
        SESSION_1,
        "AAA",
        "buy",
        Decimal("1.95"),
        Decimal("10.20"),
        fees=Decimal("0.01"),
        idempotency_key="confirmed-replacement",
        replaces_modeled_event_id=settled.fill_event_ids[0],
    )
    before_projection = ledger.project("modeled")
    before_count = before_projection.event_count
    before_hash = before_projection.last_event_hash

    with pytest.raises(PortfolioLedgerError, match="(?i)(replacement|confirmed|active)"):
        ledger.reverse_dca_batch(
            settled.batch_event_id,
            reason="must not orphan a confirmed replacement",
            idempotency_key="unsafe-batch-reversal",
        )

    after_projection = ledger.project("modeled")
    assert after_projection.event_count == before_count
    assert after_projection.last_event_hash == before_hash
    assert after_projection == before_projection
    assert ledger.verify_hash_chain() is True
