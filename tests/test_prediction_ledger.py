from __future__ import annotations

import datetime as dt
import hashlib
import json
import multiprocessing
import socket
import sqlite3
import threading
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from serenity_monitor.prediction_ledger import (
    FactorResidualEvidence,
    PredictionIdempotencyConflict,
    PredictionIntegrityError,
    PredictionCommitUnknown,
    PredictionLedger,
    PredictionLedgerPolicy,
    PredictionSettlementBlocked,
    PredictionSignal,
    PredictionValidationError,
    RightsLineage,
)
from serenity_monitor.provider_registry import AcceptedClose, CloseObservation, InstrumentRef
from serenity_monitor.trading_calendar import ExchangeSessionResolver


OBSERVATION_SESSION = dt.date(2026, 1, 2)
HORIZONS = {
    1: dt.date(2026, 1, 5),
    5: dt.date(2026, 1, 9),
    20: dt.date(2026, 2, 2),
    60: dt.date(2026, 3, 31),
}
NOW = dt.datetime(2026, 12, 31, 12, tzinfo=dt.timezone.utc)
INTEGRITY_KEY = b"prediction-ledger-test-integrity-key-v1"


class _MutableClock:
    def __init__(self, value: dt.datetime) -> None:
        self.value = value

    def __call__(self) -> dt.datetime:
        return self.value


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _signal(
    *,
    probability: Decimal = Decimal("0.8"),
    strength: Decimal = Decimal("0.7"),
    direction: str = "bullish",
    platform: str = "reddit",
    topic: str = "semiconductors",
    regime: str = "risk_on",
    model_version: str = "social-v1",
    author_salt: str = "one",
) -> PredictionSignal:
    return PredictionSignal(
        first_observed_at=dt.datetime(2026, 1, 2, 23, 30, tzinfo=dt.timezone.utc),
        observation_session=OBSERVATION_SESSION,
        platform=platform,
        source_category="authorized_export",
        author_id_sha256=_digest({"author": author_salt}),
        ticker="AAA",
        topic=topic,
        valuation_symbol="AAA",
        valuation_exchange_mic="XNAS",
        valuation_currency="USD",
        direction=direction,
        strength=strength,
        probability=probability,
        horizon_sessions=HORIZONS,
        market_regime=regime,
        model_version=model_version,
        evidence_sha256=(_digest({"derived": author_salt}),),
        rights=RightsLineage("authorized_export", _digest("rights-attestation")),
    )


def _close(
    session: dt.date,
    price: str,
    *,
    salt: str = "",
    retrieved_at: dt.datetime | None = None,
    asset_type: str = "etf",
    calendar_id: str = "XNYS",
) -> AcceptedClose:
    instrument = InstrumentRef(
        canonical_symbol="AAA",
        asset_type=asset_type,
        exchange_mic="XNAS",
        currency="USD",
        calendar_id=calendar_id,
    )
    observations = tuple(
        CloseObservation(
            provider_id=f"test_{provider}",
            provider_version="fixture_v1",
            independence_group=f"fixture_{provider}",
            source_tier=provider,
            settlement_eligible=True,
            canonical_symbol="AAA",
            provider_symbol="AAA",
            asset_type=asset_type,
            exchange_mic="XNAS",
            session_date=session,
            raw_close=Decimal(price),
            currency="USD",
            exchange_timezone="America/New_York",
            bar_kind="regular_session_close",
            adjustment_mode="none",
            price_unit_multiplier=Decimal("1"),
            retrieved_at=retrieved_at
            or dt.datetime.combine(session, dt.time(23), tzinfo=dt.timezone.utc),
            payload_sha256=_digest(
                {"provider": provider, "session": session, "price": price, "salt": salt}
            ),
            finality="final",
            corporate_action_status="clear_none",
            provider_drift_status="healthy",
            calendar_id=calendar_id,
        )
        for provider in ("primary", "secondary")
    )
    accepted_identity = {
        "instrument": "AAA",
        "exchange_mic": "XNAS",
        "expected_session": session.isoformat(),
        "status": "accepted",
        "selected_observation_id": observations[0].observation_id,
        "selected_price": str(Decimal(price)),
        "agreement_bps": "0",
        "independent_groups": [item.independence_group for item in observations],
        "observation_ids": [item.observation_id for item in observations],
        "reasons": [],
        "price_gate_permitted": True,
    }
    return AcceptedClose(
        accepted_close_id=_digest(accepted_identity),
        instrument=instrument,
        expected_session=session,
        status="accepted",
        selected_observation_id=observations[0].observation_id,
        selected_price=Decimal(price),
        currency="USD",
        agreement_bps=Decimal("0"),
        independent_source_count=2,
        observations=observations,
        attempts=(),
        reasons=(),
        valuation_permitted=True,
        price_gate_permitted=True,
        finality="confirmed",
        corporate_action_reconciliation_required=False,
        atomic_batch_permitted=True,
    )


def _ledger(
    tmp_path,
    *,
    policy: PredictionLedgerPolicy | None = None,
    clock=None,
) -> PredictionLedger:
    active_clock = clock or _MutableClock(dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc))
    ledger = PredictionLedger(
        tmp_path / "prediction.private.sqlite",
        integrity_key=INTEGRITY_KEY,
        policy=policy,
        clock=active_clock,
    )
    if isinstance(active_clock, _MutableClock):
        ledger._test_clock = active_clock
    return ledger


def _process_record_worker(database_path: str, index: int, result_queue) -> None:
    """Spawn-safe worker used to exercise the OS lock, not just SQLite locking."""

    try:
        ledger = PredictionLedger(
            database_path,
            integrity_key=INTEGRITY_KEY,
            clock=lambda: dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
        )
        receipt = ledger.record_signal(
            _signal(author_salt=f"process-{index}"),
            reference_close=_close(
                OBSERVATION_SESSION,
                "100",
                salt=f"process-reference-{index}",
            ),
            idempotency_key=f"process-key-{index}",
            recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
        )
        result_queue.put(("ok", receipt.event_id))
    except Exception as exc:  # pragma: no cover - reported to the parent process
        result_queue.put(("error", type(exc).__name__, str(exc)))


def _remove_sqlite_files(database_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(database_path) + suffix).unlink(missing_ok=True)


def _record(ledger: PredictionLedger, signal: PredictionSignal, key: str):
    return ledger.record_signal(
        signal,
        reference_close=_close(OBSERVATION_SESSION, "100", salt=f"reference-{key}"),
        idempotency_key=key,
        recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )


def _advance(ledger: PredictionLedger, value: dt.datetime) -> None:
    test_clock = getattr(ledger, "_test_clock", None)
    if test_clock is not None:
        test_clock.value = value


def _settle(
    ledger: PredictionLedger,
    signal_id: str,
    price: str,
    *,
    horizon: int = 1,
    residual: str | None = None,
    salt: str = "",
):
    factor = None
    if residual is not None:
        factor = FactorResidualEvidence(
            Decimal(residual),
            dt.datetime.combine(HORIZONS[horizon], dt.time(23, 30), tzinfo=dt.timezone.utc),
            "barra-v1",
            (_digest({"factor": salt or signal_id}),),
        )
    settlement_time = dt.datetime.combine(
        HORIZONS[horizon] + dt.timedelta(days=1),
        dt.time(1),
        tzinfo=dt.timezone.utc,
    )
    if salt.isdigit():
        settlement_time += dt.timedelta(minutes=int(salt))
    test_clock = getattr(ledger, "_test_clock", None)
    if test_clock is not None and test_clock.value < settlement_time:
        test_clock.value = settlement_time
    return ledger.settle_signal(
        signal_id,
        horizon,
        _close(HORIZONS[horizon], price, salt=salt),
        factor_residual=factor,
        settled_at=settlement_time,
    )


def test_duplicate_signal_and_settlement_are_idempotent_and_chain_verifies(tmp_path):
    ledger = _ledger(tmp_path)
    first = _record(ledger, _signal(), "signal-one")
    replay = _record(ledger, _signal(), "signal-one")
    assert replay.event_id == first.event_id
    assert replay.idempotent_replay is True

    settled = _settle(ledger, first.event_id, "110")
    duplicate = _settle(ledger, first.event_id, "110")
    assert duplicate.event_id == settled.event_id
    assert duplicate.idempotent_replay is True
    assert ledger.verify_hash_chain() is True


def test_different_duplicate_settlement_is_rejected_without_mutation(tmp_path):
    ledger = _ledger(tmp_path)
    signal_id = _record(ledger, _signal(), "signal-one").event_id
    first = _settle(ledger, signal_id, "110")
    with pytest.raises(PredictionIdempotencyConflict):
        _settle(ledger, signal_id, "111")
    assert ledger.outcomes()[0].settlement_id == first.event_id
    assert ledger.verify_hash_chain() is True


def test_brier_hit_mfe_mae_and_residual_are_exact_decimals(tmp_path):
    ledger = _ledger(tmp_path)
    signal_id = _record(ledger, _signal(probability=Decimal("0.8")), "path-signal").event_id
    _advance(ledger, dt.datetime(2026, 1, 10, 1, tzinfo=dt.timezone.utc))
    ledger.settle_signal(
        signal_id,
        5,
        _close(HORIZONS[5], "105", salt="final"),
        path_closes=(
            _close(HORIZONS[1], "110", salt="up"),
            _close(dt.date(2026, 1, 6), "100", salt="flat"),
            _close(dt.date(2026, 1, 7), "80", salt="down"),
            _close(dt.date(2026, 1, 8), "90", salt="recover"),
        ),
        factor_residual=FactorResidualEvidence(
            Decimal("0.03"),
            dt.datetime(2026, 1, 9, 23, 30, tzinfo=dt.timezone.utc),
            "barra-v1",
            (_digest("factor-lineage"),),
        ),
        settled_at=dt.datetime(2026, 1, 10, 1, tzinfo=dt.timezone.utc),
    )
    outcome = ledger.outcomes()[0]
    assert outcome.raw_return == Decimal("0.05")
    assert outcome.residual_return == Decimal("0.03")
    assert outcome.direction_hit is True
    assert outcome.mfe == Decimal("0.1")
    assert outcome.mae == Decimal("-0.2")
    assert outcome.brier == Decimal("0.04")


def test_future_information_and_naive_times_are_rejected(tmp_path):
    with pytest.raises(PredictionValidationError, match="timezone"):
        replace(_signal(), first_observed_at=dt.datetime(2026, 1, 2, 22))
    ledger = _ledger(tmp_path)
    with pytest.raises(PredictionValidationError, match="future"):
        ledger.record_signal(
            _signal(),
            reference_close=_close(OBSERVATION_SESSION, "100"),
            idempotency_key="future-signal",
            recorded_at=NOW + dt.timedelta(seconds=1),
        )
    signal_id = _record(ledger, _signal(), "valid-signal").event_id
    _advance(ledger, dt.datetime(2026, 1, 10, 1, tzinfo=dt.timezone.utc))
    future_close = _close(
        HORIZONS[1],
        "101",
        retrieved_at=dt.datetime(2026, 1, 6, 2, tzinfo=dt.timezone.utc),
    )
    with pytest.raises(PredictionSettlementBlocked, match="accepted close"):
        ledger.settle_signal(
            signal_id,
            1,
            future_close,
            settled_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
        )
    with pytest.raises(PredictionSettlementBlocked, match="horizon target"):
        ledger.settle_signal(
            signal_id,
            1,
            _close(HORIZONS[5], "101"),
            settled_at=dt.datetime(2026, 1, 10, 1, tzinfo=dt.timezone.utc),
        )


def test_privacy_contract_rejects_float_username_url_and_persists_no_raw_data(tmp_path):
    with pytest.raises(PredictionValidationError, match="floating"):
        replace(_signal(), probability=0.8)
    with pytest.raises(PredictionValidationError, match="SHA-256"):
        replace(_signal(), author_id_sha256="visible_username")
    with pytest.raises(PredictionValidationError, match="taxonomy"):
        replace(_signal(), topic="https://example.test/post?secret=1")

    database = tmp_path / "prediction.private.sqlite"
    ledger = _ledger(tmp_path)
    receipt = _record(ledger, _signal(), "opaque-local-key")
    with pytest.raises(PredictionValidationError, match="closed taxonomy"):
        ledger.reverse_event(receipt.event_id, reason_code="elonmusk")
    _settle(ledger, receipt.event_id, "101")
    stored = database.read_bytes()
    for forbidden in (
        b"visible_username",
        b"raw post body",
        b"https://example.test/post?secret=1",
        b"opaque-local-key",
    ):
        assert forbidden not in stored
    public_names = {name.casefold() for name in dir(ledger) if not name.startswith("_")}
    assert not any(
        marker in name
        for name in public_names
        for marker in ("broker", "order", "execute", "trade")
    )


def test_tampering_and_sql_update_delete_are_detected_or_blocked(tmp_path):
    database = tmp_path / "prediction.private.sqlite"
    ledger = _ledger(tmp_path)
    signal_id = _record(ledger, _signal(), "signal-one").event_id
    _settle(ledger, signal_id, "110")
    connection = sqlite3.connect(database)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE prediction_events SET payload_json = '{}' WHERE sequence_no = 1")
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM prediction_events WHERE sequence_no = 1")
    connection.rollback()
    connection.executescript("DROP TRIGGER prediction_events_no_update;")
    connection.execute(
        "UPDATE prediction_events SET payload_json = ? WHERE sequence_no = 1",
        ('{"platform":"tampered"}',),
    )
    connection.commit()
    connection.close()
    with pytest.raises(PredictionIntegrityError):
        ledger.verify_hash_chain()


def test_grouped_calibration_preserves_missing_residual_and_computes_rank_ic(tmp_path):
    ledger = _ledger(tmp_path)
    first = _record(
        ledger,
        _signal(probability=Decimal("0.6"), strength=Decimal("0.4"), author_salt="a"),
        "signal-a",
    )
    second = _record(
        ledger,
        _signal(probability=Decimal("0.9"), strength=Decimal("0.9"), author_salt="b"),
        "signal-b",
    )
    third = _record(
        ledger,
        _signal(platform="x", topic="broad_market", author_salt="c"),
        "signal-c",
    )
    _settle(ledger, first.event_id, "101", residual="0.01", salt="a")
    _settle(ledger, second.event_id, "110", residual="0.10", salt="b")
    _settle(ledger, third.event_id, "90", salt="c")

    summaries = ledger.calibration_summaries()
    assert [
        (item.platform, item.topic, item.model_version, item.sample_count)
        for item in summaries
    ] == [
        ("reddit", "semiconductors", "social-v1", 2),
        ("x", "broad_market", "social-v1", 1),
    ]
    reddit = summaries[0]
    assert reddit.hit_rate == Decimal("1")
    assert reddit.mean_residual_return == Decimal("0.055")
    assert reddit.residual_sample_count == 2
    assert reddit.rank_ic == Decimal("1")
    x_summary = summaries[1]
    assert x_summary.mean_residual_return is None
    assert x_summary.rank_ic is None
    assert x_summary.residual_sample_count == 0


def test_model_versions_cannot_pool_samples_for_calibration_or_weight(tmp_path):
    policy = PredictionLedgerPolicy(
        minimum_samples=2,
        recent_window=2,
        minimum_recent_samples=2,
    )
    ledger = _ledger(tmp_path, policy=policy)
    v1 = _record(
        ledger,
        _signal(author_salt="shared-version-source", model_version="social-v1"),
        "version-v1-signal",
    )
    v2 = _record(
        ledger,
        _signal(author_salt="shared-version-source", model_version="social-v2"),
        "version-v2-signal",
    )
    _settle(ledger, v1.event_id, "110", salt="version-v1")
    _settle(ledger, v2.event_id, "110", salt="version-v2")

    assert len(ledger.outcomes()) == 2
    assert len(ledger.outcomes(model_version="social-v1")) == 1
    assert len(ledger.outcomes(model_version="social-v2")) == 1
    summaries = ledger.calibration_summaries()
    assert [
        (summary.model_version, summary.sample_count) for summary in summaries
    ] == [("social-v1", 1), ("social-v2", 1)]
    for model_version in ("social-v1", "social-v2"):
        state = ledger.weight_state(
            platform="reddit",
            topic="semiconductors",
            model_version=model_version,
            market_regime="risk_on",
            horizon=1,
        )
        assert state.model_version == model_version
        assert state.sample_count == 1
        assert state.state == "research_only"
    with pytest.raises(TypeError, match="model_version"):
        ledger.weight_state(  # type: ignore[call-arg]
            platform="reddit",
            topic="semiconductors",
            market_regime="risk_on",
            horizon=1,
        )


def test_recent_failure_and_drift_quarantine_weight_but_never_permit_trading(tmp_path):
    policy = PredictionLedgerPolicy(
        minimum_samples=4,
        recent_window=2,
        minimum_recent_samples=2,
        decay_hit_rate=Decimal("0.5"),
        quarantine_hit_rate=Decimal("0.25"),
        decay_brier=Decimal("0.3"),
        quarantine_brier=Decimal("0.6"),
        decay_hit_rate_drop=Decimal("0.25"),
        quarantine_hit_rate_drop=Decimal("0.75"),
    )
    ledger = _ledger(tmp_path, policy=policy)
    signal_ids = []
    prices = ("110", "105", "90", "80")
    for index, _price in enumerate(prices):
        signal = _signal(probability=Decimal("0.9"), author_salt=f"author-{index}")
        signal_ids.append(_record(ledger, signal, f"signal-{index}").event_id)
    for index, (signal_id, price) in enumerate(zip(signal_ids, prices)):
        _settle(ledger, signal_id, price, salt=str(index))
    state = ledger.weight_state(
        platform="reddit",
        topic="semiconductors",
        model_version="social-v1",
        market_regime="risk_on",
        horizon=1,
    )
    assert state.state == "quarantined"
    assert state.sample_count == 4
    assert state.recent_sample_count == 2
    assert "recent_hit_rate_failed" in state.reasons
    assert state.automatic_trading_permitted is False


def test_minimum_sample_state_is_research_only_and_missing_data_is_not_zero(tmp_path):
    ledger = _ledger(tmp_path, policy=PredictionLedgerPolicy(minimum_samples=2))
    signal_id = _record(ledger, _signal(), "one").event_id
    _settle(ledger, signal_id, "101")
    state = ledger.weight_state(
        platform="reddit",
        topic="semiconductors",
        model_version="social-v1",
        market_regime="risk_on",
        horizon=1,
    )
    assert state.state == "research_only"
    summary = ledger.calibration_summaries()[0]
    assert summary.mean_residual_return is None
    assert summary.residual_sample_count == 0


def test_explicit_reversal_removes_old_outcome_and_allows_corrected_settlement(tmp_path):
    ledger = _ledger(tmp_path)
    signal_id = _record(ledger, _signal(), "signal-one").event_id
    old = _settle(ledger, signal_id, "110", salt="wrong")
    _advance(ledger, dt.datetime(2026, 1, 7, 1, tzinfo=dt.timezone.utc))
    reversed_receipt = ledger.reverse_event(
        old.event_id,
        reason_code="wrong_close_lineage",
        reversed_at=dt.datetime(2026, 1, 7, 1, tzinfo=dt.timezone.utc),
    )
    _advance(ledger, dt.datetime(2026, 1, 8, 1, tzinfo=dt.timezone.utc))
    replay = ledger.reverse_event(
        old.event_id,
        reason_code="wrong_close_lineage",
        reversed_at=dt.datetime(2026, 1, 8, 1, tzinfo=dt.timezone.utc),
    )
    assert replay.event_id == reversed_receipt.event_id
    assert replay.idempotent_replay is True
    corrected = _settle(ledger, signal_id, "90", salt="corrected")
    outcomes = ledger.outcomes()
    assert len(outcomes) == 1
    assert outcomes[0].settlement_id == corrected.event_id
    assert outcomes[0].raw_return == Decimal("-0.1")
    assert ledger.verify_hash_chain() is True


def test_historical_as_of_does_not_apply_future_reversal(tmp_path):
    current = [dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc)]
    ledger = _ledger(tmp_path, clock=lambda: current[0])
    signal_id = _record(ledger, _signal(), "signal-one").event_id
    current[0] = dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc)
    settlement = _settle(ledger, signal_id, "110")
    current[0] = dt.datetime(2026, 1, 20, 1, tzinfo=dt.timezone.utc)
    ledger.reverse_event(
        settlement.event_id,
        reason_code="later_correction",
        # Occurred before the historical cutoff, but was only created later.
        reversed_at=dt.datetime(2026, 1, 8, 1, tzinfo=dt.timezone.utc),
    )
    assert len(
        ledger.outcomes(as_of=dt.datetime(2026, 1, 10, 1, tzinfo=dt.timezone.utc))
    ) == 1
    assert ledger.outcomes() == ()


def test_ledger_is_offline_under_network_failure(tmp_path, monkeypatch):
    def forbid_network(*_args, **_kwargs):
        raise AssertionError("prediction ledger attempted network access")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    ledger = _ledger(tmp_path)
    signal_id = _record(ledger, _signal(), "offline-signal").event_id
    _settle(ledger, signal_id, "101")
    assert ledger.calibration_summaries()[0].sample_count == 1
    assert ledger.verify_hash_chain() is True


def test_topic_taxonomy_and_trusted_session_horizons_fail_closed():
    for invalid_topic in ("Semiconductors", "半导体", "@creator", "two words", "a/b", "topic "):
        with pytest.raises(PredictionValidationError, match="taxonomy"):
            replace(_signal(), topic=invalid_topic)
    with pytest.raises(PredictionValidationError, match="officially closed"):
        replace(
            _signal(),
            first_observed_at=dt.datetime(2026, 1, 2, 20, tzinfo=dt.timezone.utc),
        )
    with pytest.raises(PredictionValidationError, match="exchange session"):
        replace(_signal(), observation_session=dt.date(2026, 1, 3))
    wrong_horizons = dict(HORIZONS)
    wrong_horizons[60] = dt.date(2026, 4, 1)
    with pytest.raises(PredictionValidationError, match="trusted"):
        replace(_signal(), horizon_sessions=wrong_horizons)
    with pytest.raises(PredictionValidationError, match="currency"):
        replace(_signal(), valuation_currency="US")
    with pytest.raises(PredictionValidationError, match="USD"):
        replace(_signal(), valuation_currency="EUR")
    with pytest.raises(PredictionValidationError, match="built-in closed taxonomy"):
        PredictionLedgerPolicy(allowed_topics=("elonmusk",))
    for field_name in ("platform", "source_category", "market_regime"):
        with pytest.raises(PredictionValidationError, match="closed taxonomy"):
            replace(_signal(), **{field_name: "elonmusk"})
    with pytest.raises(PredictionValidationError, match="closed taxonomy"):
        RightsLineage("elonmusk", _digest("unsafe-rights-label"))
    with pytest.raises(PredictionValidationError, match="controlled version namespace"):
        replace(_signal(), model_version="elonmusk-v1")
    with pytest.raises(PredictionValidationError, match="controlled version namespace"):
        FactorResidualEvidence(
            Decimal("0.01"),
            dt.datetime(2026, 1, 5, 23, 30, tzinfo=dt.timezone.utc),
            "elonmusk",
            (_digest("unsafe-factor-label"),),
        )


def test_policy_topic_allowlist_can_only_narrow_and_is_enforced_on_record(tmp_path):
    policy = PredictionLedgerPolicy(allowed_topics=("sp_500",))
    ledger = _ledger(tmp_path, policy=policy)
    with pytest.raises(PredictionValidationError, match="closed taxonomy"):
        _record(ledger, _signal(topic="semiconductors"), "disallowed-policy-topic")
    receipt = _record(ledger, _signal(topic="sp_500"), "allowed-policy-topic")
    assert receipt.idempotent_replay is False


def test_narrower_topic_policy_does_not_reinterpret_valid_history(tmp_path):
    database = tmp_path / "topic-policy-history.private.sqlite"
    original = PredictionLedger(
        database,
        integrity_key=INTEGRITY_KEY,
        clock=lambda: dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    _record(original, _signal(topic="semiconductors"), "topic-policy-history")
    narrowed = PredictionLedger(
        database,
        integrity_key=INTEGRITY_KEY,
        policy=PredictionLedgerPolicy(allowed_topics=("broad_market",)),
        clock=lambda: dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    assert narrowed.verify_hash_chain() is True
    with pytest.raises(PredictionValidationError, match="closed taxonomy"):
        _record(
            narrowed,
            _signal(topic="semiconductors", author_salt="new-disallowed-topic"),
            "new-disallowed-topic",
        )


def test_record_signal_requires_strict_accepted_reference_close(tmp_path):
    ledger = _ledger(tmp_path)
    signal = _signal()
    with pytest.raises(TypeError, match="reference_close"):
        ledger.record_signal(  # type: ignore[call-arg]
            signal,
            idempotency_key="missing-reference",
            recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
        )

    valid = _close(OBSERVATION_SESSION, "100", salt="strict-reference")
    invalid_references = (
        _close(HORIZONS[1], "100", salt="wrong-reference-session"),
        replace(valid, selected_price=Decimal("101")),
        replace(
            valid,
            observations=(valid.observations[0],),
            independent_source_count=1,
        ),
        replace(valid, corporate_action_reconciliation_required=True),
        _close(
            OBSERVATION_SESSION,
            "100",
            salt="before-official-close",
            retrieved_at=dt.datetime(2026, 1, 2, 20, tzinfo=dt.timezone.utc),
        ),
        _close(
            OBSERVATION_SESSION,
            "100",
            salt="after-first-observed",
            retrieved_at=dt.datetime(2026, 1, 2, 23, 45, tzinfo=dt.timezone.utc),
        ),
        _close(
            OBSERVATION_SESSION,
            "100",
            salt="fake-calendar",
            calendar_id="FAKE",
        ),
        _close(
            OBSERVATION_SESSION,
            "100",
            salt="unsafe-asset-type",
            asset_type="elonmusk",
        ),
    )
    adjusted_observation = replace(valid.observations[0], adjustment_mode="split")
    invalid_references += (
        replace(
            valid,
            observations=(adjusted_observation, valid.observations[1]),
            selected_observation_id=adjusted_observation.observation_id,
        ),
    )
    for index, reference in enumerate(invalid_references):
        with pytest.raises((PredictionSettlementBlocked, PredictionValidationError)):
            ledger.record_signal(
                signal,
                reference_close=reference,
                idempotency_key=f"invalid-reference-{index}",
                recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
            )


def test_historical_verification_uses_signed_calendar_contract_not_current_library(
    tmp_path,
    monkeypatch,
):
    ledger = _ledger(tmp_path)
    signal_id = _record(
        ledger,
        _signal(author_salt="calendar-contract"),
        "calendar-contract",
    ).event_id
    _settle(ledger, signal_id, "101", salt="calendar-contract")

    def changed_calendar(*_args, **_kwargs):
        raise AssertionError("historical verification consulted the current calendar")

    monkeypatch.setattr(ExchangeSessionResolver, "provenance", changed_calendar)
    monkeypatch.setattr(ExchangeSessionResolver, "session_close", changed_calendar)
    monkeypatch.setattr(ExchangeSessionResolver, "future_session_offsets", changed_calendar)
    monkeypatch.setattr(ExchangeSessionResolver, "last_completed_session", changed_calendar)
    reopened = PredictionLedger(
        ledger.database_path,
        integrity_key=INTEGRITY_KEY,
        clock=lambda: NOW,
    )
    assert reopened.verify_hash_chain() is True
    assert reopened.outcomes()[0].raw_return == Decimal("0.01")


def test_reference_close_is_immutable_private_lineage_and_retry_conflicts(tmp_path):
    ledger = _ledger(tmp_path)
    signal = _signal(author_salt="reference-private")
    first_close = _close(OBSERVATION_SESSION, "100", salt="reference-private-a")
    receipt = ledger.record_signal(
        signal,
        reference_close=first_close,
        idempotency_key="reference-private-key",
        recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    with pytest.raises(PredictionIdempotencyConflict):
        ledger.record_signal(
            signal,
            reference_close=_close(
                OBSERVATION_SESSION,
                "101",
                salt="reference-private-b",
            ),
            idempotency_key="reference-private-key",
            recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
        )
    connection = sqlite3.connect(ledger.database_path)
    try:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM prediction_events WHERE event_id = ?",
                (receipt.event_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()
    reference = payload["reference_close"]
    assert reference["price"] == "100"
    assert len(reference["sources"]) == 2
    serialized = json.dumps(reference, sort_keys=True)
    for forbidden in (
        "url",
        "username",
        "token",
        "attempts",
        "raw_content",
        "test_primary",
        "test_secondary",
        "fixture_primary",
        "fixture_secondary",
    ):
        assert forbidden not in serialized.lower()


def test_same_logical_signal_and_reference_dedupes_across_retry_keys(tmp_path):
    ledger = _ledger(tmp_path)
    signal = _signal(author_salt="logical-dedupe")
    reference = _close(OBSERVATION_SESSION, "100", salt="logical-dedupe-reference")
    first = ledger.record_signal(
        signal,
        reference_close=reference,
        idempotency_key="logical-attempt-one",
        recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    replay = ledger.record_signal(
        signal,
        reference_close=reference,
        idempotency_key="logical-attempt-two",
        recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    assert replay.idempotent_replay is True
    assert replay.event_id == first.event_id
    connection = sqlite3.connect(ledger.database_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM prediction_events WHERE event_type = 'signal'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM prediction_events WHERE event_type = 'idempotency_alias'"
        ).fetchone()[0] == 1
    finally:
        connection.close()
    with pytest.raises(PredictionIdempotencyConflict):
        ledger.record_signal(
            _signal(author_salt="different-logical-content"),
            reference_close=_close(
                OBSERVATION_SESSION,
                "100",
                salt="different-logical-reference",
            ),
            idempotency_key="logical-attempt-two",
            recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
        )
    assert ledger.outcomes() == ()


def test_same_source_signal_cannot_duplicate_via_reference_or_timestamp_changes(tmp_path):
    ledger = _ledger(tmp_path)
    signal = _signal(author_salt="source-identity")
    first = ledger.record_signal(
        signal,
        reference_close=_close(
            OBSERVATION_SESSION,
            "100",
            salt="source-identity-reference-a",
        ),
        idempotency_key="source-identity-a",
        recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    with pytest.raises(PredictionIdempotencyConflict, match="fingerprint"):
        ledger.record_signal(
            signal,
            reference_close=_close(
                OBSERVATION_SESSION,
                "100",
                salt="source-identity-reference-b",
            ),
            idempotency_key="source-identity-b",
            recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
        )
    with pytest.raises(PredictionIdempotencyConflict, match="fingerprint"):
        ledger.record_signal(
            replace(
                signal,
                first_observed_at=dt.datetime(
                    2026, 1, 2, 23, 40, tzinfo=dt.timezone.utc
                ),
            ),
            reference_close=_close(
                OBSERVATION_SESSION,
                "100",
                salt="source-identity-reference-a",
            ),
            idempotency_key="source-identity-c",
            recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
        )
    connection = sqlite3.connect(ledger.database_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM prediction_events WHERE event_type='signal'"
        ).fetchone()[0] == 1
    finally:
        connection.close()
    assert first.event_id


def test_reversed_source_signal_requires_explicit_linked_replacement(tmp_path):
    ledger = _ledger(tmp_path)
    signal = _signal(author_salt="explicit-replacement")
    reference = _close(
        OBSERVATION_SESSION,
        "100",
        salt="explicit-replacement-reference",
    )
    original = ledger.record_signal(
        signal,
        reference_close=reference,
        idempotency_key="explicit-replacement-original",
        recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    _advance(ledger, dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc))
    ledger.reverse_event(
        original.event_id,
        reason_code="lineage_recheck",
        reversed_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
    )
    with pytest.raises(PredictionIdempotencyConflict, match="reversed signal"):
        ledger.record_signal(
            signal,
            reference_close=reference,
            idempotency_key="reversed-same-content-new-key",
            recorded_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
            is_backfill=True,
        )

    corrected_signal = replace(
        signal,
        direction="bearish",
        probability=Decimal("0.7"),
    )
    replacement = ledger.record_signal(
        corrected_signal,
        reference_close=reference,
        idempotency_key="explicit-replacement-linked",
        recorded_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
        is_backfill=True,
        supersedes_signal_id=original.event_id,
    )
    assert replacement.idempotent_replay is False
    retry = ledger.record_signal(
        corrected_signal,
        reference_close=reference,
        idempotency_key="explicit-replacement-linked",
        recorded_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
        is_backfill=True,
        supersedes_signal_id=original.event_id,
    )
    assert retry.event_id == replacement.event_id
    assert retry.idempotent_replay is True
    with pytest.raises(PredictionIdempotencyConflict):
        ledger.record_signal(
            replace(corrected_signal, probability=Decimal("0.6")),
            reference_close=reference,
            idempotency_key="illegal-replacement-branch",
            recorded_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
            is_backfill=True,
            supersedes_signal_id=original.event_id,
        )
    assert ledger.verify_hash_chain() is True


def test_late_signal_retries_preserve_original_classification_and_bind_alias(tmp_path):
    ledger = _ledger(tmp_path)
    signal = _signal(author_salt="late-retry")
    reference = _close(OBSERVATION_SESSION, "100", salt="late-retry-reference")
    first = ledger.record_signal(
        signal,
        reference_close=reference,
        idempotency_key="late-retry-original",
        recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    _advance(ledger, dt.datetime(2026, 1, 10, 2, tzinfo=dt.timezone.utc))
    same_key = ledger.record_signal(
        signal,
        reference_close=reference,
        idempotency_key="late-retry-original",
        recorded_at=dt.datetime(2026, 1, 10, 2, tzinfo=dt.timezone.utc),
    )
    alias = ledger.record_signal(
        signal,
        reference_close=reference,
        idempotency_key="late-retry-alias",
        recorded_at=dt.datetime(2026, 1, 10, 2, tzinfo=dt.timezone.utc),
    )
    assert same_key.event_id == first.event_id
    assert alias.event_id == first.event_id
    assert same_key.idempotent_replay is True
    assert alias.idempotent_replay is True


def test_incomplete_horizon_path_and_settlement_before_recording_are_blocked(tmp_path):
    current = [dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc)]
    ledger = _ledger(tmp_path, clock=lambda: current[0])
    signal_id = _record(ledger, _signal(), "path-gates").event_id
    with pytest.raises(PredictionSettlementBlocked, match="follow"):
        ledger.settle_signal(
            signal_id,
            1,
            _close(HORIZONS[1], "101"),
            settled_at=current[0],
        )
    current[0] = dt.datetime(2026, 1, 10, 1, tzinfo=dt.timezone.utc)
    with pytest.raises(PredictionSettlementBlocked, match="exactly cover"):
        ledger.settle_signal(
            signal_id,
            5,
            _close(HORIZONS[5], "105"),
            path_closes=(_close(HORIZONS[1], "101"),),
            settled_at=current[0],
        )


def test_accepted_close_requires_corporate_action_clear_and_two_consistent_sources(tmp_path):
    ledger = _ledger(tmp_path)
    signal_id = _record(ledger, _signal(), "close-gates").event_id
    _advance(ledger, dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc))
    valid = _close(HORIZONS[1], "100")
    reconciled_split = replace(
        _close(HORIZONS[1], "50", salt="unadjusted-split"),
        observations=tuple(
            replace(observation, corporate_action_status="reconciled")
            for observation in _close(
                HORIZONS[1], "50", salt="unadjusted-split"
            ).observations
        ),
    )
    invalid_closes = (
        replace(valid, corporate_action_reconciliation_required=True),
        replace(
            valid,
            observations=(valid.observations[0],),
            independent_source_count=1,
        ),
        replace(valid, selected_price=Decimal("101")),
        replace(valid, currency="EUR"),
        replace(
            valid,
            instrument=replace(valid.instrument, exchange_mic="ARCX"),
        ),
        _close(HORIZONS[1], "100", salt="outcome-asset-mismatch", asset_type="equity"),
        _close(HORIZONS[1], "100", salt="outcome-calendar-mismatch", calendar_id="FAKE"),
        reconciled_split,
    )
    for index, invalid in enumerate(invalid_closes):
        with pytest.raises(PredictionSettlementBlocked):
            ledger.settle_signal(
                signal_id,
                1,
                invalid,
                settled_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
                idempotency_key=f"invalid-close-{index}",
            )
    bad_observation = replace(valid.observations[0], adjustment_mode="split")
    bad_lineage = replace(
        valid,
        observations=(bad_observation, valid.observations[1]),
        selected_observation_id=bad_observation.observation_id,
    )
    with pytest.raises(PredictionSettlementBlocked, match="accepted close"):
        ledger.settle_signal(
            signal_id,
            1,
            bad_lineage,
            settled_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
        )
    assert ledger.outcomes() == ()


def test_factor_residual_waits_for_all_independent_close_sources(tmp_path):
    ledger = _ledger(tmp_path)
    signal_id = _record(
        ledger,
        _signal(author_salt="factor-accepted-at"),
        "factor-accepted-at",
    ).event_id
    _advance(ledger, dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc))
    close = _close(HORIZONS[1], "101", salt="factor-accepted-at-close")
    split_retrieval = replace(
        close,
        observations=(
            replace(
                close.observations[0],
                retrieved_at=dt.datetime(2026, 1, 5, 21, 5, tzinfo=dt.timezone.utc),
            ),
            replace(
                close.observations[1],
                retrieved_at=dt.datetime(
                    2026,
                    1,
                    5,
                    23,
                    0,
                    0,
                    900_000,
                    tzinfo=dt.timezone.utc,
                ),
            ),
        ),
    )
    with pytest.raises(PredictionSettlementBlocked, match="factor residual"):
        ledger.settle_signal(
            signal_id,
            1,
            split_retrieval,
            factor_residual=FactorResidualEvidence(
                Decimal("0.01"),
                dt.datetime(
                    2026,
                    1,
                    5,
                    23,
                    0,
                    0,
                    500_000,
                    tzinfo=dt.timezone.utc,
                ),
                "barra-v1",
                (_digest("factor-before-confirmation"),),
            ),
            settled_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
        )
    receipt = ledger.settle_signal(
        signal_id,
        1,
        split_retrieval,
        factor_residual=FactorResidualEvidence(
            Decimal("0.01"),
            dt.datetime(
                2026,
                1,
                5,
                23,
                0,
                0,
                900_000,
                tzinfo=dt.timezone.utc,
            ),
            "barra-v1",
            (_digest("factor-at-confirmation"),),
        ),
        settled_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
    )
    assert receipt.idempotent_replay is False
    assert ledger.outcomes()[0].residual_return == Decimal("0.01")


def test_backfill_is_explicitly_recorded_and_excluded_by_default(tmp_path):
    ledger = _ledger(tmp_path)
    signal_id = ledger.record_signal(
        _signal(author_salt="backfill"),
        reference_close=_close(OBSERVATION_SESSION, "100", salt="backfill-reference"),
        idempotency_key="backfill-signal",
        recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
        is_backfill=True,
    ).event_id
    _settle(ledger, signal_id, "110", salt="backfill")
    assert ledger.outcomes() == ()
    assert ledger.calibration_summaries() == ()
    state = ledger.weight_state(
        platform="reddit",
        topic="semiconductors",
        model_version="social-v1",
        market_regime="risk_on",
        horizon=1,
    )
    assert state.sample_count == 0
    included = ledger.outcomes(include_backfill=True)
    assert len(included) == 1
    assert included[0].recording_mode == "historical_backfill"
    assert included[0].calibration_eligible is False
    included_summary = ledger.calibration_summaries(include_backfill=True)[0]
    assert included_summary.sample_count == 1
    assert included_summary.sample_scope == "includes_backfill"
    with pytest.raises(TypeError, match="include_backfill"):
        ledger.weight_state(  # type: ignore[call-arg]
            platform="reddit",
            topic="semiconductors",
            model_version="social-v1",
            market_regime="risk_on",
            horizon=1,
            include_backfill=True,
        )


def test_default_calibration_scope_is_explicitly_live_only(tmp_path):
    ledger = _ledger(tmp_path)
    signal_id = _record(ledger, _signal(author_salt="live-scope"), "live-scope").event_id
    _settle(ledger, signal_id, "101", salt="live-scope")
    assert ledger.outcomes()[0].recording_mode == "live_observation"
    assert ledger.outcomes()[0].calibration_eligible is True
    assert ledger.calibration_summaries()[0].sample_scope == "live_only"


def test_undisclosed_historical_signal_is_rejected_until_explicitly_marked(tmp_path):
    current = dt.datetime(2026, 1, 10, 2, tzinfo=dt.timezone.utc)
    ledger = _ledger(tmp_path, clock=lambda: current)
    historical = _signal(author_salt="historical")
    with pytest.raises(PredictionValidationError, match="is_backfill"):
        ledger.record_signal(
            historical,
            reference_close=_close(OBSERVATION_SESSION, "100", salt="history-reference"),
            idempotency_key="undisclosed-history",
            recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
        )
    receipt = ledger.record_signal(
        historical,
        reference_close=_close(OBSERVATION_SESSION, "100", salt="history-reference"),
        idempotency_key="declared-history",
        recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
        is_backfill=True,
    )
    assert receipt.idempotent_replay is False
    assert ledger.verify_hash_chain() is True


def test_reversed_identical_settlement_requires_explicit_new_identity(tmp_path):
    ledger = _ledger(tmp_path)
    signal_id = _record(ledger, _signal(), "reversal-same-signal").event_id
    _advance(ledger, dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc))
    old = ledger.settle_signal(
        signal_id,
        1,
        _close(HORIZONS[1], "110", salt="same"),
        settled_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
        idempotency_key="old-explicit-settlement-key",
    )
    _advance(ledger, dt.datetime(2026, 1, 7, 1, tzinfo=dt.timezone.utc))
    ledger.reverse_event(
        old.event_id,
        reason_code="lineage_recheck",
        reversed_at=dt.datetime(2026, 1, 7, 1, tzinfo=dt.timezone.utc),
    )
    with pytest.raises(PredictionIdempotencyConflict, match="explicit"):
        _settle(ledger, signal_id, "110", salt="same")
    _advance(ledger, dt.datetime(2026, 1, 8, 1, tzinfo=dt.timezone.utc))
    with pytest.raises(PredictionIdempotencyConflict, match="cannot be reused"):
        ledger.settle_signal(
            signal_id,
            1,
            _close(HORIZONS[1], "110", salt="same"),
            settled_at=dt.datetime(2026, 1, 8, 1, tzinfo=dt.timezone.utc),
            idempotency_key="old-explicit-settlement-key",
        )
    replacement = ledger.settle_signal(
        signal_id,
        1,
        _close(HORIZONS[1], "110", salt="same"),
        settled_at=dt.datetime(2026, 1, 8, 1, tzinfo=dt.timezone.utc),
        idempotency_key="explicit-replacement-after-reversal",
    )
    assert ledger.outcomes()[0].settlement_id == replacement.event_id


def test_settlement_and_reversal_aliases_bind_every_successful_retry_key(tmp_path):
    ledger = _ledger(tmp_path)
    signal_id = _record(ledger, _signal(author_salt="alias-events"), "alias-events-signal").event_id
    _advance(ledger, dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc))
    close = _close(HORIZONS[1], "110", salt="alias-events-close")
    first = ledger.settle_signal(
        signal_id,
        1,
        close,
        settled_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
        idempotency_key="settlement-alias-a",
    )
    duplicate = ledger.settle_signal(
        signal_id,
        1,
        close,
        settled_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
        idempotency_key="settlement-alias-b",
    )
    assert duplicate.event_id == first.event_id
    assert duplicate.idempotent_replay is True
    with pytest.raises(PredictionIdempotencyConflict):
        ledger.settle_signal(
            signal_id,
            1,
            _close(HORIZONS[1], "111", salt="alias-events-different"),
            settled_at=dt.datetime(2026, 1, 6, 1, tzinfo=dt.timezone.utc),
            idempotency_key="settlement-alias-b",
        )

    _advance(ledger, dt.datetime(2026, 1, 7, 1, tzinfo=dt.timezone.utc))
    reversal = ledger.reverse_event(
        first.event_id,
        reason_code="lineage_recheck",
        reversed_at=dt.datetime(2026, 1, 7, 1, tzinfo=dt.timezone.utc),
        idempotency_key="reversal-alias-a",
    )
    _advance(ledger, dt.datetime(2026, 1, 8, 1, tzinfo=dt.timezone.utc))
    replay = ledger.reverse_event(
        first.event_id,
        reason_code="lineage_recheck",
        reversed_at=dt.datetime(2026, 1, 8, 1, tzinfo=dt.timezone.utc),
        idempotency_key="reversal-alias-b",
    )
    assert replay.event_id == reversal.event_id
    with pytest.raises(PredictionIdempotencyConflict):
        ledger.reverse_event(
            first.event_id,
            reason_code="different_reason",
            reversed_at=dt.datetime(2026, 1, 8, 1, tzinfo=dt.timezone.utc),
            idempotency_key="reversal-alias-b",
        )
    assert ledger.verify_hash_chain() is True


def test_reversal_cannot_be_backdated_before_target_creation(tmp_path):
    current = dt.datetime(2026, 1, 10, 2, tzinfo=dt.timezone.utc)
    ledger = _ledger(tmp_path, clock=lambda: current)
    signal_id = ledger.record_signal(
        _signal(author_salt="late-created-backfill"),
        reference_close=_close(
            OBSERVATION_SESSION,
            "100",
            salt="late-created-backfill-reference",
        ),
        idempotency_key="late-created-backfill",
        recorded_at=dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
        is_backfill=True,
    ).event_id
    with pytest.raises(PredictionValidationError, match="precede"):
        ledger.reverse_event(
            signal_id,
            reason_code="backdated_reversal",
            reversed_at=dt.datetime(2026, 1, 5, 0, tzinfo=dt.timezone.utc),
        )


def test_hmac_checkpoint_rejects_short_wrong_or_missing_key_and_hides_secret(tmp_path):
    database = tmp_path / "hmac.private.sqlite"
    with pytest.raises(PredictionValidationError, match="32 bytes"):
        PredictionLedger(database, integrity_key=b"short")
    ledger = PredictionLedger(
        database,
        integrity_key=INTEGRITY_KEY,
        clock=lambda: dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    _record(ledger, _signal(), "hmac-signal")
    checkpoint = Path(str(database) + ".integrity.json")
    assert checkpoint.is_file()
    assert INTEGRITY_KEY not in database.read_bytes()
    assert INTEGRITY_KEY not in checkpoint.read_bytes()
    with pytest.raises(PredictionIntegrityError, match="authentication") as wrong_key:
        PredictionLedger(database, integrity_key=b"x" * 32, clock=lambda: NOW)
    assert INTEGRITY_KEY.decode() not in str(wrong_key.value)
    checkpoint.unlink()
    with pytest.raises(PredictionIntegrityError, match="missing"):
        PredictionLedger(database, integrity_key=INTEGRITY_KEY, clock=lambda: NOW)


def test_sidecar_first_bootstrap_recovers_only_authenticated_empty_databases(tmp_path):
    database = tmp_path / "bootstrap.private.sqlite"
    seed = PredictionLedger(database, integrity_key=INTEGRITY_KEY, clock=lambda: NOW)
    assert seed.verify_hash_chain() is True

    _remove_sqlite_files(database)
    checkpoint_only = PredictionLedger(database, integrity_key=INTEGRITY_KEY, clock=lambda: NOW)
    assert checkpoint_only.verify_hash_chain() is True

    _remove_sqlite_files(database)
    database.touch()
    zero_byte = PredictionLedger(database, integrity_key=INTEGRITY_KEY, clock=lambda: NOW)
    assert zero_byte.verify_hash_chain() is True

    connection = sqlite3.connect(database)
    table_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='prediction_events'"
    ).fetchone()[0]
    connection.close()
    _remove_sqlite_files(database)
    connection = sqlite3.connect(database)
    connection.execute(table_sql)
    connection.commit()
    connection.close()
    partial_schema = PredictionLedger(database, integrity_key=INTEGRITY_KEY, clock=lambda: NOW)
    assert partial_schema.verify_hash_chain() is True


def test_bootstrap_rejects_unauthenticated_or_ambiguous_partial_databases(tmp_path):
    existing_without_checkpoint = tmp_path / "existing-no-checkpoint.private.sqlite"
    sqlite3.connect(existing_without_checkpoint).close()
    with pytest.raises(PredictionIntegrityError, match="checkpoint is missing"):
        PredictionLedger(
            existing_without_checkpoint,
            integrity_key=INTEGRITY_KEY,
            clock=lambda: NOW,
        )

    wrong_schema = tmp_path / "wrong-schema.private.sqlite"
    PredictionLedger(wrong_schema, integrity_key=INTEGRITY_KEY, clock=lambda: NOW)
    _remove_sqlite_files(wrong_schema)
    connection = sqlite3.connect(wrong_schema)
    connection.execute("CREATE TABLE prediction_events (bad TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(PredictionIntegrityError, match="schema"):
        PredictionLedger(wrong_schema, integrity_key=INTEGRITY_KEY, clock=lambda: NOW)

    unknown_object = tmp_path / "unknown-object.private.sqlite"
    PredictionLedger(unknown_object, integrity_key=INTEGRITY_KEY, clock=lambda: NOW)
    _remove_sqlite_files(unknown_object)
    connection = sqlite3.connect(unknown_object)
    connection.execute("CREATE TABLE unrelated_private_data (value TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(PredictionIntegrityError, match="unknown objects"):
        PredictionLedger(unknown_object, integrity_key=INTEGRITY_KEY, clock=lambda: NOW)


def test_checkpoint_stable_db_ahead_and_pending_tampering_fail_closed(tmp_path):
    ledger = _ledger(tmp_path)
    _record(ledger, _signal(author_salt="checkpoint-ahead"), "checkpoint-ahead")
    state = ledger._read_checkpoint_state()
    ledger._write_checkpoint_state(
        committed=(0, "0" * 64),
        pending=None,
        generation=state["generation"] + 1,
    )
    with pytest.raises(PredictionIntegrityError, match="stable database head"):
        ledger.verify_hash_chain()

    database = tmp_path / "pending.private.sqlite"
    pending_ledger = PredictionLedger(database, integrity_key=INTEGRITY_KEY, clock=lambda: NOW)
    pending_state = pending_ledger._read_checkpoint_state()
    pending_ledger._write_checkpoint_state(
        committed=(0, "0" * 64),
        pending={
            "tx_id": "a" * 32,
            "from_sequence": 0,
            "from_head": "0" * 64,
            "to_sequence": 2,
            "to_head": "1" * 64,
        },
        generation=pending_state["generation"] + 1,
    )
    with pytest.raises(PredictionIntegrityError, match="pending checkpoint transition"):
        pending_ledger.verify_hash_chain()

    checkpoint = Path(str(database) + ".integrity.json")
    parsed = json.loads(checkpoint.read_text(encoding="utf-8"))
    parsed["pending"]["to_head"] = "2" * 64
    checkpoint.write_text(
        json.dumps(parsed, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PredictionIntegrityError, match="authentication"):
        pending_ledger.verify_hash_chain()


def test_checkpoint_prepare_failure_rolls_back_and_pending_old_recovers(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    original_write = ledger._write_checkpoint_state

    def fail_after_prepare(*, committed, pending, generation):
        original_write(committed=committed, pending=pending, generation=generation)
        if pending is not None:
            raise OSError("sensitive-path-and-key-must-not-escape")

    monkeypatch.setattr(ledger, "_write_checkpoint_state", fail_after_prepare)
    with pytest.raises(PredictionIntegrityError, match="checkpoint prepare failed") as failure:
        _record(ledger, _signal(author_salt="prepare-failure"), "prepare-failure")
    assert "sensitive" not in str(failure.value)
    assert ledger._read_checkpoint_state()["pending"] is not None
    connection = sqlite3.connect(ledger.database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM prediction_events").fetchone()[0] == 0
    finally:
        connection.close()

    monkeypatch.setattr(ledger, "_write_checkpoint_state", original_write)
    receipt = _record(ledger, _signal(author_salt="prepare-failure"), "prepare-failure")
    assert receipt.idempotent_replay is False
    recovered = ledger._read_checkpoint_state()
    assert recovered["pending"] is None
    assert recovered["committed"]["sequence"] == 1


def test_checkpoint_finalize_failure_is_commit_unknown_and_retry_is_idempotent(
    tmp_path,
    monkeypatch,
):
    ledger = _ledger(tmp_path)
    original_write = ledger._write_checkpoint_state

    def fail_before_finalize(*, committed, pending, generation):
        if pending is None and committed[0] == 1:
            raise OSError("sensitive-finalize-detail")
        original_write(committed=committed, pending=pending, generation=generation)

    monkeypatch.setattr(ledger, "_write_checkpoint_state", fail_before_finalize)
    with pytest.raises(PredictionCommitUnknown) as failure:
        _record(ledger, _signal(author_salt="finalize-failure"), "finalize-failure")
    assert str(failure.value) == "prediction_ledger_commit_unknown"
    assert ledger._read_checkpoint_state()["pending"] is not None

    monkeypatch.setattr(ledger, "_write_checkpoint_state", original_write)
    retry = _record(ledger, _signal(author_salt="finalize-failure"), "finalize-failure")
    assert retry.idempotent_replay is True
    assert ledger._read_checkpoint_state()["pending"] is None
    connection = sqlite3.connect(ledger.database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM prediction_events").fetchone()[0] == 1
    finally:
        connection.close()


def test_trigger_removal_and_tail_deletion_with_trigger_rebuild_fail_closed(tmp_path):
    dropped_path = tmp_path / "dropped.private.sqlite"
    dropped = PredictionLedger(
        dropped_path,
        integrity_key=INTEGRITY_KEY,
        clock=lambda: dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    _record(dropped, _signal(), "drop-trigger-signal")
    connection = sqlite3.connect(dropped_path)
    connection.execute("DROP TRIGGER prediction_events_no_delete")
    connection.commit()
    connection.close()
    with pytest.raises(PredictionIntegrityError, match="triggers"):
        dropped.verify_hash_chain()

    tail_path = tmp_path / "tail.private.sqlite"
    tail = PredictionLedger(
        tail_path,
        integrity_key=INTEGRITY_KEY,
        clock=lambda: dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    first = _record(tail, _signal(author_salt="tail-a"), "tail-a")
    _record(tail, _signal(author_salt="tail-b"), "tail-b")
    connection = sqlite3.connect(tail_path)
    trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='prediction_events_no_delete'"
    ).fetchone()[0]
    connection.execute("DROP TRIGGER prediction_events_no_delete")
    connection.execute(
        "DELETE FROM prediction_events WHERE sequence_no = (SELECT MAX(sequence_no) FROM prediction_events)"
    )
    connection.execute(trigger_sql)
    connection.commit()
    connection.close()
    with pytest.raises(PredictionIntegrityError, match="checkpoint"):
        tail.verify_hash_chain()
    assert first.event_id


def test_concurrent_unique_and_same_idempotency_writes_preserve_one_chain(tmp_path):
    ledger = _ledger(tmp_path)
    barrier = threading.Barrier(6)
    receipts = []
    errors = []

    def worker(index: int) -> None:
        try:
            barrier.wait()
            if index < 3:
                receipt = _record(ledger, _signal(author_salt="shared"), "shared-key")
            else:
                receipt = _record(
                    ledger,
                    _signal(author_salt=f"unique-{index}"),
                    f"unique-key-{index}",
                )
            receipts.append((index, receipt))
        except Exception as exc:  # test captures cross-thread failures for one assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    shared_ids = {receipt.event_id for index, receipt in receipts if index < 3}
    assert len(shared_ids) == 1
    assert ledger.verify_hash_chain() is True
    connection = sqlite3.connect(ledger.database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM prediction_events").fetchone()[0] == 4
    finally:
        connection.close()


def test_reader_waits_for_two_phase_writer_instead_of_recovering_in_flight_pending(
    tmp_path,
    monkeypatch,
):
    ledger = _ledger(tmp_path)
    original_write = ledger._write_checkpoint_state
    prepared = threading.Event()
    release_writer = threading.Event()
    reader_finished = threading.Event()
    errors: list[Exception] = []

    def pause_after_prepare(*, committed, pending, generation):
        original_write(committed=committed, pending=pending, generation=generation)
        if pending is not None:
            prepared.set()
            if not release_writer.wait(timeout=5):
                raise AssertionError("test writer was not released")

    monkeypatch.setattr(ledger, "_write_checkpoint_state", pause_after_prepare)

    def writer() -> None:
        try:
            _record(ledger, _signal(author_salt="interleaved-writer"), "interleaved-writer")
        except Exception as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            ledger.outcomes()
        except Exception as exc:
            errors.append(exc)
        finally:
            reader_finished.set()

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert prepared.wait(timeout=5)
    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    assert reader_finished.wait(timeout=0.2) is False
    assert ledger._read_checkpoint_state()["pending"] is not None
    release_writer.set()
    writer_thread.join(timeout=5)
    reader_thread.join(timeout=5)
    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert errors == []
    assert ledger.verify_hash_chain() is True


def test_cross_process_writers_share_checkpoint_and_os_lock(tmp_path):
    database = tmp_path / "multiprocess.private.sqlite"
    PredictionLedger(
        database,
        integrity_key=INTEGRITY_KEY,
        clock=lambda: dt.datetime(2026, 1, 3, 0, tzinfo=dt.timezone.utc),
    )
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_process_record_worker,
            args=(str(database), index, result_queue),
        )
        for index in range(4)
    ]
    for process in processes:
        process.start()
    results = [result_queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
    assert all(process.exitcode == 0 for process in processes)
    assert all(result[0] == "ok" for result in results), results
    assert len({result[1] for result in results}) == 4

    ledger = PredictionLedger(database, integrity_key=INTEGRITY_KEY, clock=lambda: NOW)
    assert ledger.verify_hash_chain() is True
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM prediction_events WHERE event_type='signal'"
        ).fetchone()[0] == 4
    finally:
        connection.close()
