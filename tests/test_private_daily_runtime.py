from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import serenity_monitor.opening_owner_attestation as opening_attestation
import serenity_monitor.manual_owner_event as manual_owner_event
import serenity_monitor.private_report_store as private_report_store
from serenity_monitor.daily_outbox import (
    DailyReportOutbox,
    DeliveryAdapterCapabilities,
)
from serenity_monitor.opening_owner_attestation import (
    create_opening_owner_claim,
    interactive_owner_presence,
)
from serenity_monitor.manual_owner_event import (
    REQUEST_CONTRACT_VERSION,
    approve_manual_event,
    interactive_manual_event_presence,
    load_manual_event_queue,
    load_manual_event_request,
)
from serenity_monitor.portfolio_ledger import PortfolioLedger
from serenity_monitor.private_daily_report import validate_private_daily_report
from serenity_monitor.private_daily_runtime import (
    PrivateDailyIntegrityError,
    PrivateDailyRuntime,
    PrivateDailyRuntimeError,
    initialize_private_ledger,
)
from serenity_monitor.private_daily_runtime import _attempt_source_health
from serenity_monitor.private_research_adapter import (
    PrivateResearchInput,
    PrivateResearchProjection,
    build_private_research_projection,
)
from serenity_monitor.private_runtime_config import (
    PUBLIC_EXAMPLE_NAME,
    load_private_daily_runtime_config,
)
from serenity_monitor.private_runtime_paths import (
    PrivateRuntimePaths,
    ensure_private_storage,
    tighten_private_file,
)
from serenity_monitor.provider_registry import (
    CloseObservation,
    ProviderRegistry,
    ProviderAttempt,
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


def test_rejected_close_attempt_is_blocked_in_source_health() -> None:
    row = _attempt_source_health(
        dt.date(2026, 1, 5),
        "DEMO_EQ",
        ProviderAttempt(
            provider_id="twelve_data",
            status="rejected",
            detail="twelve_data: observation rejected by acceptance policy",
            observed_at="2026-01-06T01:00:00Z",
            observation_id="a" * 64,
        ),
    )

    assert row["status"] == "blocked"
    assert row["detail_code"] == "rejected"


@pytest.fixture(autouse=True)
def _allow_synthetic_temp_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        opening_attestation,
        "validate_existing_private_storage_root",
        lambda paths: paths.root,
    )
    monkeypatch.setattr(
        opening_attestation,
        "validate_existing_private_runtime_file",
        lambda _paths, path: Path(path),
    )
    monkeypatch.setattr(
        private_report_store,
        "validate_private_report_directory",
        lambda paths, directory: Path(directory),
    )
    monkeypatch.setattr(
        manual_owner_event,
        "validate_existing_private_storage_root",
        lambda paths: paths.root,
    )


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
    root = tmp_path / "private-runtime"
    paths = PrivateRuntimePaths(
        root=root,
        ledger_database=root / "portfolio-ledger.sqlite3",
        outbox_database=root / "daily-outbox.sqlite3",
        report_directory=root / "reports",
        lock_file=root / "private-daily-runtime.lock",
    )
    ensure_private_storage(paths)
    input_stream = io.StringIO("CONFIRM 23456789AB\n")
    output_stream = io.StringIO()
    input_stream.isatty = lambda: True
    output_stream.isatty = lambda: True
    presence = interactive_owner_presence(
        input_stream,
        output_stream,
        challenge_factory=lambda: "23456789AB",
    )
    config_digest = hashlib.sha256(
        (ROOT / "config" / PUBLIC_EXAMPLE_NAME).read_bytes()
    ).hexdigest()
    create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=config_digest,
        owner_presence=presence,
        clock=clock,
    )
    calendar = ExchangeSessionResolver()
    ledger = PortfolioLedger(
        paths.ledger_database,
        policy=config.ledger_policy,
        calendar_resolver=calendar,
    )
    reports = paths.report_directory
    registry, first, second = _registry(config, clock)
    initialize_private_ledger(
        config,
        runtime_paths=paths,
        config_bytes_sha256=config_digest,
        ledger=ledger,
        close_registry=registry,
        calendar=calendar,
        clock=clock,
    )
    outbox = DailyReportOutbox(paths.outbox_database)
    return calendar, ledger, outbox, reports, registry, first, second


def _runtime(config, calendar, ledger, outbox, reports, registry, clock):
    runtime_paths = None
    config_digest = None
    if not config.simulation:
        root = ledger.database_path.parent
        runtime_paths = PrivateRuntimePaths(
            root=root,
            ledger_database=ledger.database_path,
            outbox_database=outbox.database_path,
            report_directory=reports,
            lock_file=root / "private-daily-runtime.lock",
        )
        config_digest = hashlib.sha256(
            (ROOT / "config" / PUBLIC_EXAMPLE_NAME).read_bytes()
        ).hexdigest()
    return PrivateDailyRuntime(
        config,
        calendar=calendar,
        close_registry=registry,
        ledger=ledger,
        outbox=outbox,
        report_directory=reports,
        clock=clock,
        runtime_paths=runtime_paths,
        config_bytes_sha256=config_digest,
    )


def _approve_runtime_event(
    config,
    paths: PrivateRuntimePaths,
    body: dict,
    clock: MutableClock,
):
    payload = json.dumps(
        body,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8") + b"\n"
    paths.manual_event_request_file.write_bytes(payload)
    tighten_private_file(paths.manual_event_request_file)
    request = load_manual_event_request(config, paths)
    challenge = "23456789AB"
    prefix = hashlib.sha256(payload).hexdigest()[:8]
    input_stream = io.StringIO(f"CONFIRM {challenge} {prefix}\n")
    output_stream = io.StringIO()
    input_stream.isatty = lambda: True
    output_stream.isatty = lambda: True
    proof = interactive_manual_event_presence(
        request,
        input_stream,
        output_stream,
        challenge_factory=lambda: challenge,
    )
    return approve_manual_event(config, paths, request, proof, clock)


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


def test_live_owner_skip_is_consumed_before_modeled_dca(tmp_path: Path) -> None:
    config = replace(_config(), classification="private", simulation=False)
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, _, _ = _state(
        tmp_path,
        config,
        clock,
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    runtime = _runtime(config, calendar, ledger, outbox, reports, registry, clock)
    assert runtime.runtime_paths is not None
    _approve_runtime_event(
        config,
        runtime.runtime_paths,
        {
            "contract_version": REQUEST_CONTRACT_VERSION,
            "event_kind": "skip_dca",
            "event_nonce": "a" * 32,
            "occurred_at": None,
            "payload": {
                "plan_id": config.dca_plan.plan_id,
                "plan_version": config.dca_plan.version,
                "reason": "owner explicit skip",
            },
            "session": "2026-01-05",
        },
        clock,
    )

    result = runtime.prepare(TARGET)

    report = validate_private_daily_report(
        outbox.load_validated_content(result.delivery_id).report
    )
    assert report["session_results"][0]["status"] == "skipped_by_owner"
    assert ledger.session_audit(dt.date(2026, 1, 5)).owner_skip is not None
    assert load_manual_event_queue(
        config,
        runtime.runtime_paths,
        ledger.event_checkpoint,
        ledger.latest_valuation_watermark(),
    ) == ()


def test_live_dca_replacement_is_applied_after_settlement_before_valuation(
    tmp_path: Path,
) -> None:
    config = replace(_config(), classification="private", simulation=False)
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, _, _ = _state(
        tmp_path,
        config,
        clock,
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    runtime = _runtime(config, calendar, ledger, outbox, reports, registry, clock)
    assert runtime.runtime_paths is not None
    _approve_runtime_event(
        config,
        runtime.runtime_paths,
        {
            "contract_version": REQUEST_CONTRACT_VERSION,
            "event_kind": "confirmed_fill",
            "event_nonce": "b" * 32,
            "occurred_at": "2026-01-05T18:30:00Z",
            "payload": {
                "fees": "0",
                "modeled_dca_replacement": True,
                "plan_id": config.dca_plan.plan_id,
                "plan_version": config.dca_plan.version,
                "price": "101",
                "quantity": "0.11",
                "side": "buy",
                "symbol": "DEMO_EQ",
            },
            "session": "2026-01-05",
        },
        clock,
    )

    result = runtime.prepare(TARGET)

    assert result.report_status == "complete"
    confirmed = ledger.project("confirmed", dt.date(2026, 1, 5)).by_symbol
    modeled = ledger.project("modeled", dt.date(2026, 1, 5)).by_symbol
    assert confirmed["DEMO_EQ"].quantity == Decimal("10.11")
    assert modeled["DEMO_EQ"].quantity == Decimal("10.11")
    assert modeled["DEMO_EQ"].modeled_quantity == Decimal("0")
    assert modeled["DEMO_BOND"].modeled_quantity == Decimal("0.2")
    assert load_manual_event_queue(
        config,
        runtime.runtime_paths,
        ledger.event_checkpoint,
        ledger.latest_valuation_watermark(),
    ) == ()


def test_unapproved_private_request_blocks_before_provider_or_ledger_mutation(
    tmp_path: Path,
) -> None:
    config = replace(_config(), classification="private", simulation=False)
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, first, second = _state(
        tmp_path,
        config,
        clock,
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    runtime = _runtime(config, calendar, ledger, outbox, reports, registry, clock)
    assert runtime.runtime_paths is not None
    request = {
        "contract_version": REQUEST_CONTRACT_VERSION,
        "event_kind": "fee",
        "event_nonce": "c" * 32,
        "occurred_at": None,
        "payload": {"amount": "1", "description": "owner fee"},
        "session": "2026-01-05",
    }
    runtime.runtime_paths.manual_event_request_file.write_text(
        json.dumps(request),
        encoding="utf-8",
    )
    tighten_private_file(runtime.runtime_paths.manual_event_request_file)
    chain_head = ledger.last_event_hash()
    provider_calls = (list(first.calls), list(second.calls))

    with pytest.raises(
        PrivateDailyIntegrityError,
        match="manual_owner_event_queue_invalid",
    ):
        runtime.prepare(TARGET)

    assert ledger.last_event_hash() == chain_head
    assert (first.calls, second.calls) == provider_calls


def test_aggregate_research_projection_cannot_change_dca_or_actions(tmp_path: Path) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, first, second = _state(
        tmp_path, config, clock
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)

    result = _runtime(
        config, calendar, ledger, outbox, reports, registry, clock
    ).prepare(
        TARGET,
        research_input=PrivateResearchInput(as_of=clock.value),
    )

    report = validate_private_daily_report(
        outbox.load_validated_content(result.delivery_id).report
    )
    assert [item["configured"]["amount"] for item in report["dca"]["items"]] == [
        "10",
        "10",
    ]
    assert all(
        item["proposed"]["automatic_execution"] is False
        for item in report["dca"]["items"]
    )
    assert all(item["automatic_execution"] is False for item in report["actions"])
    assert report["research"]["market_regime"] == "unknown"
    assert report["research"]["risk_budget_multiplier"] == "0"
    assert "research.snapshot" in {
        item["source_id"] for item in report["source_health"]
    }


def test_invalid_research_is_rejected_before_provider_or_ledger_mutation(
    tmp_path: Path,
) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, first, second = _state(
        tmp_path, config, clock
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    calls = (len(first.calls), len(second.calls))
    events = ledger.project("modeled").event_count

    with pytest.raises(PrivateDailyRuntimeError, match="research_snapshot_invalid"):
        _runtime(
            config, calendar, ledger, outbox, reports, registry, clock
        ).prepare(
            TARGET,
            research_input=PrivateResearchInput(
                as_of=clock.value + dt.timedelta(seconds=1)
            ),
        )

    assert (len(first.calls), len(second.calls)) == calls
    assert ledger.project("modeled").event_count == events


def test_semantically_invalid_projection_is_rejected_before_any_mutation(
    tmp_path: Path,
) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, first, second = _state(
        tmp_path, config, clock
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    valid = build_private_research_projection(
        PrivateResearchInput(as_of=clock.value),
        prepared_at=clock.value,
    )
    research = copy.deepcopy(valid.research)
    research["social_attention"] = [
        {
            "platform": "x",
            "topic": "platform_aggregate",
            "direction": "positive",
            "status": "healthy",
            "score": "0.5",
            "attention_weight": "1",
            "candidate_execution_weight": "0.1",
            "calibration_state": "active",
            "effective_execution_weight": "0.9",
            "research_only": True,
            "summary": "research only",
        }
    ]
    research["signal_calibration"] = [
        {
            "platform": "x",
            "topic": "semiconductors",
            "model_version": "social-v1",
            "market_regime": "risk_on",
            "horizon": 20,
            "state": "active",
            "sample_count": 100,
            "recent_sample_count": 20,
            "reasons": ["calibration_healthy"],
            "automatic_trading_permitted": False,
        }
    ]
    research["social_decision"] = {
        "raw_contribution": "0.01",
        "effective_contribution": "0.01",
        "effective_execution_coverage": "0.9",
        "decision_weight_cap": "0.05",
        "calibration_state": "active",
        "research_only": True,
    }
    malformed = PrivateResearchProjection(research, valid.source_health)
    calls = (len(first.calls), len(second.calls))
    events = ledger.project("modeled").event_count

    with pytest.raises(PrivateDailyRuntimeError, match="research_snapshot_invalid"):
        _runtime(
            config, calendar, ledger, outbox, reports, registry, clock
        ).prepare(TARGET, research_projection=malformed)

    assert (len(first.calls), len(second.calls)) == calls
    assert ledger.project("modeled").event_count == events


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

    replay = runtime.prepare(
        TARGET,
        research_input=PrivateResearchInput(
            as_of=clock.value + dt.timedelta(days=1)
        ),
    )

    assert replay.status == "existing"
    assert replay.delivery_id == first_result.delivery_id
    assert (len(first.calls), len(second.calls)) == calls
    assert ledger.project("modeled").event_count == events


def test_same_day_slot_must_belong_to_the_current_ledger_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, _, _ = _state(
        tmp_path,
        config,
        clock,
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    runtime = _runtime(config, calendar, ledger, outbox, reports, registry, clock)
    runtime.prepare(TARGET)
    monkeypatch.setattr(ledger, "contains_event_hash", lambda _value: False)

    with pytest.raises(
        PrivateDailyIntegrityError,
        match="same_day_report_not_in_current_ledger_chain",
    ):
        runtime.prepare(TARGET)


def test_same_day_slot_rejects_an_ancestor_that_is_not_the_current_chain_head(
    tmp_path: Path,
) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, _, _ = _state(
        tmp_path,
        config,
        clock,
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    runtime = _runtime(config, calendar, ledger, outbox, reports, registry, clock)
    prepared = runtime.prepare(TARGET)
    persisted = outbox.load_validated_content(prepared.delivery_id)
    prior_hash = persisted.report["portfolio"]["ledger_last_event_hash"]
    ledger.record_cash_flow(
        dt.date(2026, 1, 6),
        Decimal("1"),
        idempotency_key="post-report-owner-event",
    )
    assert ledger.contains_event_hash(prior_hash)
    assert ledger.last_event_hash() != prior_hash

    with pytest.raises(
        PrivateDailyIntegrityError,
        match="same_day_report_not_in_current_ledger_chain",
    ):
        runtime.prepare(TARGET)


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

    blocked = runtime.prepare(
        TARGET,
        research_input=PrivateResearchInput(
            as_of=clock.value + dt.timedelta(days=1)
        ),
    )

    assert blocked.status == "pending_prior_delivery"
    assert (len(first.calls), len(second.calls)) == calls
    assert ledger.project("modeled").event_count == events


@pytest.mark.parametrize("advance_days", (0, 1))
def test_live_replay_and_pending_paths_still_require_consumed_attestation(
    tmp_path: Path,
    advance_days: int,
) -> None:
    live = replace(_config(), classification="private", simulation=False)
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    calendar, ledger, outbox, reports, registry, _, _ = _state(
        tmp_path,
        live,
        clock,
    )
    clock.value = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    runtime = _runtime(live, calendar, ledger, outbox, reports, registry, clock)
    runtime.prepare(TARGET)
    paths = PrivateRuntimePaths(
        root=ledger.database_path.parent,
        ledger_database=ledger.database_path,
        outbox_database=outbox.database_path,
        report_directory=reports,
        lock_file=ledger.database_path.parent / "private-daily-runtime.lock",
    )
    paths.opening_receipt_file.unlink()
    clock.value += dt.timedelta(days=advance_days)

    with pytest.raises(
        PrivateDailyIntegrityError,
        match="opening_owner_attestation_not_consumed",
    ):
        runtime.prepare(TARGET)


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


def test_failed_external_gate_does_not_create_ledger_or_brick_claim_renewal(
    tmp_path: Path,
) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    root = tmp_path / "private-runtime"
    paths = PrivateRuntimePaths(
        root=root,
        ledger_database=root / "portfolio-ledger.sqlite3",
        outbox_database=root / "daily-outbox.sqlite3",
        report_directory=root / "reports",
        lock_file=root / "private-daily-runtime.lock",
    )
    ensure_private_storage(paths)
    config_digest = hashlib.sha256(
        (ROOT / "config" / PUBLIC_EXAMPLE_NAME).read_bytes()
    ).hexdigest()

    def presence():
        input_stream = io.StringIO("CONFIRM 23456789AB\n")
        output_stream = io.StringIO()
        input_stream.isatty = lambda: True
        output_stream.isatty = lambda: True
        return interactive_owner_presence(
            input_stream,
            output_stream,
            challenge_factory=lambda: "23456789AB",
        )

    create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=config_digest,
        owner_presence=presence(),
        clock=clock,
    )
    registry, _, _ = _registry(
        config,
        clock,
        disagreement_after=config.opening.session,
    )
    factory_called = False

    def ledger_factory() -> PortfolioLedger:
        nonlocal factory_called
        factory_called = True
        return PortfolioLedger(paths.ledger_database, policy=config.ledger_policy)

    with pytest.raises(PrivateDailyRuntimeError, match="opening_price_gate_blocked"):
        initialize_private_ledger(
            config,
            runtime_paths=paths,
            config_bytes_sha256=config_digest,
            ledger=None,
            ledger_factory=ledger_factory,
            close_registry=registry,
            calendar=ExchangeSessionResolver(),
            clock=clock,
        )

    assert factory_called is False
    assert not paths.ledger_database.exists()
    assert not paths.opening_intent_file.exists()

    clock.value += dt.timedelta(minutes=31)
    renewed = create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=config_digest,
        owner_presence=presence(),
        clock=clock,
    )
    assert renewed.status == "renewed"


def test_ledger_factory_failure_happens_before_durable_intent(
    tmp_path: Path,
) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    root = tmp_path / "private-runtime"
    paths = PrivateRuntimePaths(
        root=root,
        ledger_database=root / "portfolio-ledger.sqlite3",
        outbox_database=root / "daily-outbox.sqlite3",
        report_directory=root / "reports",
        lock_file=root / "private-daily-runtime.lock",
    )
    ensure_private_storage(paths)
    input_stream = io.StringIO("CONFIRM 23456789AB\n")
    output_stream = io.StringIO()
    input_stream.isatty = lambda: True
    output_stream.isatty = lambda: True
    create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=hashlib.sha256(
            (ROOT / "config" / PUBLIC_EXAMPLE_NAME).read_bytes()
        ).hexdigest(),
        owner_presence=interactive_owner_presence(
            input_stream,
            output_stream,
            challenge_factory=lambda: "23456789AB",
        ),
        clock=clock,
    )
    registry, _, _ = _registry(config, clock)

    with pytest.raises(OSError, match="synthetic_schema_failure"):
        initialize_private_ledger(
            config,
            runtime_paths=paths,
            config_bytes_sha256=hashlib.sha256(
                (ROOT / "config" / PUBLIC_EXAMPLE_NAME).read_bytes()
            ).hexdigest(),
            ledger=None,
            ledger_factory=lambda: (_ for _ in ()).throw(
                OSError("synthetic_schema_failure")
            ),
            close_registry=registry,
            calendar=ExchangeSessionResolver(),
            clock=clock,
        )

    assert not paths.opening_intent_file.exists()
    assert not paths.ledger_database.exists()


def test_ledger_mutation_after_factory_is_rejected_before_intent(
    tmp_path: Path,
) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    root = tmp_path / "private-runtime"
    paths = PrivateRuntimePaths(
        root=root,
        ledger_database=root / "portfolio-ledger.sqlite3",
        outbox_database=root / "daily-outbox.sqlite3",
        report_directory=root / "reports",
        lock_file=root / "private-daily-runtime.lock",
    )
    ensure_private_storage(paths)
    input_stream = io.StringIO("CONFIRM 23456789AB\n")
    output_stream = io.StringIO()
    input_stream.isatty = lambda: True
    output_stream.isatty = lambda: True
    config_digest = hashlib.sha256(
        (ROOT / "config" / PUBLIC_EXAMPLE_NAME).read_bytes()
    ).hexdigest()
    create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=config_digest,
        owner_presence=interactive_owner_presence(
            input_stream,
            output_stream,
            challenge_factory=lambda: "23456789AB",
        ),
        clock=clock,
    )
    registry, _, _ = _registry(config, clock)

    def mutated_factory() -> PortfolioLedger:
        ledger = PortfolioLedger(paths.ledger_database, policy=config.ledger_policy)
        Path(str(paths.ledger_database) + "-wal").write_bytes(b"raced")
        return ledger

    with pytest.raises(
        PrivateDailyIntegrityError,
        match="opening_owner_attestation_commit_failed",
    ):
        initialize_private_ledger(
            config,
            runtime_paths=paths,
            config_bytes_sha256=config_digest,
            ledger=None,
            ledger_factory=mutated_factory,
            close_registry=registry,
            calendar=ExchangeSessionResolver(),
            clock=clock,
        )

    assert not paths.opening_intent_file.exists()


def test_fresh_durable_intent_resumes_with_actual_commit_time(
    tmp_path: Path,
) -> None:
    config = _config()
    clock = MutableClock(dt.datetime(2026, 1, 3, 5, tzinfo=dt.timezone.utc))
    root = tmp_path / "private-runtime"
    paths = PrivateRuntimePaths(
        root=root,
        ledger_database=root / "portfolio-ledger.sqlite3",
        outbox_database=root / "daily-outbox.sqlite3",
        report_directory=root / "reports",
        lock_file=root / "private-daily-runtime.lock",
    )
    ensure_private_storage(paths)
    input_stream = io.StringIO("CONFIRM 23456789AB\n")
    output_stream = io.StringIO()
    input_stream.isatty = lambda: True
    output_stream.isatty = lambda: True
    config_digest = hashlib.sha256(
        (ROOT / "config" / PUBLIC_EXAMPLE_NAME).read_bytes()
    ).hexdigest()
    create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=config_digest,
        owner_presence=interactive_owner_presence(
            input_stream,
            output_stream,
            challenge_factory=lambda: "23456789AB",
        ),
        clock=clock,
    )
    audit = opening_attestation.audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=config_digest,
        now=clock(),
        ledger_binding=None,
    )
    assert audit.claim is not None
    intent = opening_attestation.publish_opening_intent(
        audit.claim,
        paths,
        clock=clock,
    )
    clock.value += dt.timedelta(minutes=5)
    registry, _, _ = _registry(config, clock)

    result = initialize_private_ledger(
        config,
        runtime_paths=paths,
        config_bytes_sha256=config_digest,
        ledger=None,
        ledger_factory=lambda: PortfolioLedger(
            paths.ledger_database,
            policy=config.ledger_policy,
        ),
        close_registry=registry,
        calendar=ExchangeSessionResolver(),
        clock=clock,
    )
    ledger = PortfolioLedger(paths.ledger_database, policy=config.ledger_policy)

    assert result.status == "initialized"
    assert ledger.opening_checkpoint().created_at == clock.value
    assert ledger.opening_checkpoint().created_at > intent.created_at


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
