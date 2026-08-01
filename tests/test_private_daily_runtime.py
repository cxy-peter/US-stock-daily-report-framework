from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from serenity_monitor.daily_outbox import (
    DailyReportOutbox,
    DeliveryAdapterCapabilities,
)
from serenity_monitor.portfolio_ledger import PortfolioLedger
from serenity_monitor.private_daily_report import validate_private_daily_report
from serenity_monitor.private_daily_runtime import (
    PrivateDailyIntegrityError,
    PrivateDailyRuntime,
    initialize_private_ledger,
)
from serenity_monitor.private_runtime_config import (
    PUBLIC_EXAMPLE_NAME,
    load_private_daily_runtime_config,
)
from serenity_monitor.provider_registry import (
    CloseObservation,
    ProviderRegistry,
)
from serenity_monitor.trading_calendar import ExchangeSessionResolver


ROOT = Path(__file__).resolve().parents[1]
TARGET = "synthetic-owner-target"
EXACTLY_ONCE = DeliveryAdapterCapabilities(
    supports_idempotency_key=True,
    supports_delivery_lookup=False,
    idempotency_scope="synthetic-test-receiver",
)


class MutableClock:
    def __init__(self, value: dt.datetime) -> None:
        self.value = value

    def __call__(self) -> dt.datetime:
        return self.value


class StubProvider:
    provider_version = "test-v1"
    source_tier = "primary"
    settlement_eligible = True

    def __init__(
        self,
        provider_id: str,
        clock: MutableClock,
        *,
        disagreement_after: dt.date | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.independence_group = provider_id
        self.clock = clock
        self.disagreement_after = disagreement_after
        self.calls: list[tuple[str, dt.date]] = []

    def fetch_close(self, instrument, expected_session: dt.date) -> CloseObservation:
        self.calls.append((instrument.canonical_symbol, expected_session))
        base = Decimal("100") if instrument.canonical_symbol == "DEMO_EQ" else Decimal("50")
        if (
            self.provider_id == "alpha_vantage"
            and self.disagreement_after is not None
            and expected_session >= self.disagreement_after
        ):
            base += Decimal("10")
        return CloseObservation(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            independence_group=self.independence_group,
            source_tier=self.source_tier,
            settlement_eligible=True,
            canonical_symbol=instrument.canonical_symbol,
            provider_symbol=instrument.symbol_for(self.provider_id),
            asset_type=instrument.asset_type,
            exchange_mic=instrument.exchange_mic,
            exchange_mic_provenance="provider_meta",
            calendar_id=instrument.calendar_id,
            session_date=expected_session,
            raw_close=base,
            currency="USD",
            currency_provenance="provider_meta",
            exchange_timezone="America/New_York",
            bar_kind="regular_session_close",
            adjustment_mode="none",
            price_unit_multiplier=Decimal("1"),
            retrieved_at=self.clock(),
            payload_sha256=("1" if self.provider_id == "twelve_data" else "2") * 64,
        )


def _config():
    return load_private_daily_runtime_config(
        ROOT / "config" / PUBLIC_EXAMPLE_NAME,
        allow_synthetic=True,
    )


def _registry(config, clock, *, disagreement_after=None):
    first = StubProvider("twelve_data", clock)
    second = StubProvider(
        "alpha_vantage",
        clock,
        disagreement_after=disagreement_after,
    )
    return (
        ProviderRegistry(
            (first, second),
            policy=config.close_policy,
            clock=clock,
        ),
        first,
        second,
    )


def _state(tmp_path: Path, config, clock):
    calendar = ExchangeSessionResolver()
    ledger = PortfolioLedger(
        tmp_path / "ledger.sqlite3",
        policy=config.ledger_policy,
        calendar_resolver=calendar,
    )
    outbox = DailyReportOutbox(tmp_path / "outbox.sqlite3")
    reports = tmp_path / "reports"
    reports.mkdir()
    registry, first, second = _registry(config, clock)
    initialize_private_ledger(
        config,
        ledger=ledger,
        close_registry=registry,
        calendar=calendar,
        as_of=clock(),
    )
    return calendar, ledger, outbox, reports, registry, first, second


def _runtime(config, calendar, ledger, outbox, reports, registry, clock):
    return PrivateDailyRuntime(
        config,
        calendar=calendar,
        close_registry=registry,
        ledger=ledger,
        outbox=outbox,
        report_directory=reports,
        clock=clock,
    )


def _deliver(outbox: DailyReportOutbox, delivery_id: str, now: dt.datetime) -> None:
    claim = outbox.claim(delivery_id, EXACTLY_ONCE, now=now)
    outbox.mark_delivered(
        delivery_id,
        claim.lease_token,
        delivered_at=now + dt.timedelta(seconds=1),
    )


def test_initialize_and_single_session_fixed_dca_report(tmp_path: Path) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, first, second = _state(
        tmp_path, config, clock
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    result = _runtime(
        config, calendar, ledger, outbox, reports, registry, clock
    ).prepare(TARGET)

    assert result.status == "prepared"
    assert result.report_status == "complete"
    assert result.processed_sessions == (dt.date(2026, 1, 5),)
    content = outbox.load_validated_content(result.delivery_id)
    report = validate_private_daily_report(content.report)
    assert report["session_results"][0]["status"] == "settled"
    assert [item["configured"]["amount"] for item in report["dca"]["items"]] == ["10", "10"]
    assert all(item["broker_confirmed"]["status"] == "not_connected" for item in report["dca"]["items"])
    assert all(item["proposed"]["automatic_execution"] is False for item in report["dca"]["items"])
    assert all(item["automatic_execution"] is False for item in report["actions"])
    assert report["manual_trade_prompt"]["default_if_no_response"] == "no_new_owner_confirmed_event"
    assert report["portfolio"]["modeled"]["nav"] != report["portfolio"]["confirmed"]["nav"]
    assert len(first.calls) == len(second.calls) == 4


def test_same_day_slot_reuses_outbox_without_provider_or_ledger_mutation(tmp_path: Path) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, first, second = _state(
        tmp_path, config, clock
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    runtime = _runtime(config, calendar, ledger, outbox, reports, registry, clock)
    first_result = runtime.prepare(TARGET)
    calls = (len(first.calls), len(second.calls))
    events = ledger.project("modeled").event_count

    replay = runtime.prepare(TARGET)

    assert replay.status == "existing"
    assert replay.delivery_id == first_result.delivery_id
    assert (len(first.calls), len(second.calls)) == calls
    assert ledger.project("modeled").event_count == events


def test_prior_prepared_delivery_blocks_next_day_before_provider_or_ledger(tmp_path: Path) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, first, second = _state(
        tmp_path, config, clock
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    runtime = _runtime(config, calendar, ledger, outbox, reports, registry, clock)
    runtime.prepare(TARGET)
    calls = (len(first.calls), len(second.calls))
    events = ledger.project("modeled").event_count
    clock.value = dt.datetime(2026, 1, 7, 5, tzinfo=dt.timezone.utc)

    blocked = runtime.prepare(TARGET)

    assert blocked.status == "pending_prior_delivery"
    assert (len(first.calls), len(second.calls)) == calls
    assert ledger.project("modeled").event_count == events


def test_weekend_no_new_close_carries_valuation_without_provider_calls(tmp_path: Path) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, first, second = _state(
        tmp_path, config, clock
    )
    clock.value = dt.datetime(2026, 1, 10, 5, tzinfo=dt.timezone.utc)
    runtime = _runtime(config, calendar, ledger, outbox, reports, registry, clock)
    friday = runtime.prepare(TARGET)
    _deliver(outbox, friday.delivery_id, clock.value + dt.timedelta(minutes=1))
    calls = (len(first.calls), len(second.calls))
    clock.value = dt.datetime(2026, 1, 11, 5, tzinfo=dt.timezone.utc)

    weekend = runtime.prepare(TARGET)
    report = outbox.load_validated_content(weekend.delivery_id).report

    assert weekend.report_status == "no_new_close"
    assert report["session_results"] == []
    assert report["portfolio"]["confirmed"]["performance"]["daily_pnl"] is None
    assert report["portfolio"]["confirmed"]["performance"]["daily_return"] is None
    assert report["portfolio"]["modeled"]["performance"]["daily_pnl"] is None
    assert report["portfolio"]["modeled"]["performance"]["daily_return"] is None
    assert (len(first.calls), len(second.calls)) == calls


def test_delivered_checkpoint_not_latest_valuation_drives_recovery(tmp_path: Path) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, first, second = _state(
        tmp_path, config, clock
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    runtime = _runtime(config, calendar, ledger, outbox, reports, registry, clock)
    day_one = runtime.prepare(TARGET)
    _deliver(outbox, day_one.delivery_id, clock.value + dt.timedelta(minutes=1))

    # Simulate a crash after both 2026-01-06 valuations but before enqueue.
    crash_session = dt.date(2026, 1, 6)
    universe = tuple(config.instruments)
    valuation_batch = registry.resolve_batch(universe, crash_session)
    plan_batch = registry.resolve_batch(
        tuple(config.by_symbol[symbol] for symbol in config.dca_plan.base_amounts),
        crash_session,
    )
    actions = config.corporate_action_statuses(
        crash_session,
        as_of=dt.datetime(2026, 1, 7, 5, tzinfo=dt.timezone.utc),
        symbols=tuple(item.canonical_symbol for item in universe),
    )
    ledger.settle_modeled_dca_batch(
        config.dca_plan,
        plan_batch,
        dt.datetime(2026, 1, 7, 5, tzinfo=dt.timezone.utc),
        {symbol: actions[symbol] for symbol in config.dca_plan.base_amounts},
    )
    ledger.record_valuation("confirmed", valuation_batch)
    ledger.record_valuation("modeled", valuation_batch)
    clock.value = dt.datetime(2026, 1, 8, 5, tzinfo=dt.timezone.utc)

    recovered = runtime.prepare(TARGET)
    report = outbox.load_validated_content(recovered.delivery_id).report

    assert report["calendar"]["last_settled_session_before_run"] == "2026-01-05"
    assert [item["session_date"] for item in report["session_results"]] == [
        "2026-01-06",
        "2026-01-07",
    ]
    assert report["session_results"][0]["status"] == "already_settled"
    assert report["session_results"][1]["status"] == "settled"
    assert all(
        item["modeled"]["sessions"][0]["amount"] == "10"
        for item in report["dca"]["items"]
    )


def test_complete_valuations_without_dca_evidence_fail_closed(tmp_path: Path) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, _, _ = _state(
        tmp_path, config, clock
    )
    session = dt.date(2026, 1, 5)
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    valuation_batch = registry.resolve_batch(tuple(config.instruments), session)
    ledger.record_valuation("confirmed", valuation_batch)
    ledger.record_valuation("modeled", valuation_batch)

    with pytest.raises(
        PrivateDailyIntegrityError,
        match="recovered_dca_evidence_missing",
    ):
        _runtime(
            config, calendar, ledger, outbox, reports, registry, clock
        ).prepare(TARGET)


def test_explicit_owner_skip_recovers_as_skipped_by_owner(tmp_path: Path) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, _, _ = _state(
        tmp_path, config, clock
    )
    session = dt.date(2026, 1, 5)
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    valuation_batch = registry.resolve_batch(tuple(config.instruments), session)
    ledger.record_dca_override(
        session,
        config.dca_plan.plan_id,
        config.dca_plan.version,
        reason="owner intentionally skipped the modeled base plan",
    )
    ledger.record_valuation("confirmed", valuation_batch)
    ledger.record_valuation("modeled", valuation_batch)

    result = _runtime(
        config, calendar, ledger, outbox, reports, registry, clock
    ).prepare(TARGET)
    report = outbox.load_validated_content(result.delivery_id).report

    assert report["session_results"][0]["status"] == "skipped_by_owner"
    assert report["portfolio"]["confirmed"]["nav"] == report["portfolio"]["modeled"]["nav"]


def test_price_gate_blocks_first_session_and_stops_later_sessions(tmp_path: Path) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, healthy, _, _ = _state(
        tmp_path, config, clock
    )
    bad_registry, _, _ = _registry(
        config,
        clock,
        disagreement_after=dt.date(2026, 1, 5),
    )
    clock.value = dt.datetime(2026, 1, 7, 5, tzinfo=dt.timezone.utc)

    result = _runtime(
        config, calendar, ledger, outbox, reports, bad_registry, clock
    ).prepare(TARGET)
    report = outbox.load_validated_content(result.delivery_id).report

    assert result.report_status == "blocked"
    assert [item["status"] for item in report["session_results"]] == [
        "blocked",
        "not_attempted_prior_session_blocked",
    ]
    assert report["actions"][0]["action"] == "BLOCK_NEW_RISK"
    assert ledger.latest_common_valuation_session() == config.opening.session


def test_missing_corporate_attestation_blocks_without_dca_mutation(tmp_path: Path) -> None:
    original = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, _, _ = _state(
        tmp_path, original, clock
    )
    attestations = tuple(
        replace(item, valid_through_session=original.opening.session)
        for item in original.corporate_action_attestations
    )
    config = replace(original, corporate_action_attestations=attestations)
    clock.value = dt.datetime(2026, 1, 7, 5, tzinfo=dt.timezone.utc)

    result = _runtime(
        config, calendar, ledger, outbox, reports, registry, clock
    ).prepare(TARGET)
    report = outbox.load_validated_content(result.delivery_id).report

    assert [item["status"] for item in report["session_results"]] == [
        "blocked",
        "not_attempted_prior_session_blocked",
    ]
    assert report["session_results"][0]["corporate_action_gate"] == "blocked"
    assert ledger.latest_common_valuation_session() == original.opening.session


def test_missing_provider_preflight_never_calls_provider_or_mutates_ledger(tmp_path: Path) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, first, second = _state(
        tmp_path, config, clock
    )
    calls = (len(first.calls), len(second.calls))
    events = ledger.project("modeled").event_count
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)

    result = _runtime(
        config, calendar, ledger, outbox, reports, registry, clock
    ).prepare(TARGET, preflight_block_reason="provider_credentials_missing")
    report = outbox.load_validated_content(result.delivery_id).report

    assert result.report_status == "blocked"
    assert report["session_results"][0]["price_gate"] == "blocked"
    assert (len(first.calls), len(second.calls)) == calls
    assert ledger.project("modeled").event_count == events


def test_partial_valuation_crash_recovers_idempotently(tmp_path: Path) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, _, _ = _state(
        tmp_path, config, clock
    )
    session = dt.date(2026, 1, 5)
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    valuation_batch = registry.resolve_batch(tuple(config.instruments), session)
    plan_instruments = tuple(
        config.by_symbol[symbol] for symbol in config.dca_plan.base_amounts
    )
    plan_batch = registry.resolve_batch(plan_instruments, session)
    actions = config.corporate_action_statuses(
        session,
        as_of=clock(),
        symbols=tuple(item.canonical_symbol for item in config.instruments),
    )
    ledger.settle_modeled_dca_batch(
        config.dca_plan,
        plan_batch,
        clock(),
        {symbol: actions[symbol] for symbol in config.dca_plan.base_amounts},
    )
    ledger.record_valuation("confirmed", valuation_batch)
    assert ledger.session_audit(session).valuation_state == "partial"

    result = _runtime(
        config, calendar, ledger, outbox, reports, registry, clock
    ).prepare(TARGET)
    report = outbox.load_validated_content(result.delivery_id).report

    assert report["session_results"][0]["status"] == "already_settled"
    assert ledger.session_audit(session).valuation_state == "complete"
    assert all(
        item["modeled"]["sessions"][0]["amount"] == "10"
        for item in report["dca"]["items"]
    )


def test_live_call_budget_blocks_large_backfill_without_network(tmp_path: Path) -> None:
    synthetic = _config()
    live = replace(synthetic, classification="private", simulation=False)
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, first, second = _state(
        tmp_path, live, clock
    )
    calls = (len(first.calls), len(second.calls))
    clock.value = dt.datetime(2026, 1, 28, 5, tzinfo=dt.timezone.utc)

    result = _runtime(
        live, calendar, ledger, outbox, reports, registry, clock
    ).prepare(TARGET)
    report = outbox.load_validated_content(result.delivery_id).report

    assert result.report_status == "blocked"
    assert report["session_results"][0]["reason_codes"] == [
        "live_provider_call_budget_exceeded"
    ]
    assert (len(first.calls), len(second.calls)) == calls
