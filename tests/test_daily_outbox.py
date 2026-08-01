from __future__ import annotations

import concurrent.futures
import copy
import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from serenity_monitor.daily_outbox import (
    DailyReportOutbox,
    DeliveredCheckpoint,
    DeliveryAdapterCapabilities,
    OutboxContent,
    OutboxCapabilityError,
    OutboxIdempotencyConflict,
    OutboxIntegrityError,
    OutboxLedgerMutationBlocked,
    OutboxLeaseError,
    OutboxStateError,
    OutboxValidationError,
)
from serenity_monitor.private_daily_markdown import render_private_daily_markdown
from serenity_monitor.private_daily_report import (
    LEGACY_SCHEMA_VERSION,
    compute_delivery_id,
    compute_report_id,
    compute_target_key_sha256,
    finalize_private_daily_report,
)
from test_private_daily_report_semantics import blocked_first_run_draft


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 1, 5, 15, tzinfo=UTC)
TARGET = "codex-private-thread-7c7353d4-0a58-4cdb-9f52-1f30e9126f81"
LEDGER_HASH = "0" * 64
IDEMPOTENT = DeliveryAdapterCapabilities(True, False, "codex-thread-message/v1")
LOOKUP = DeliveryAdapterCapabilities(
    False, True, lookup_scope="codex-thread-lookup/v1"
)
BOTH = DeliveryAdapterCapabilities(
    True,
    True,
    "codex-thread-message/v1",
    "codex-thread-lookup/v1",
)
OTHER_IDEMPOTENT_SCOPE = DeliveryAdapterCapabilities(
    True, False, "different-receiver/v1"
)
OTHER_LOOKUP_SCOPE = DeliveryAdapterCapabilities(
    False, True, lookup_scope="different-receiver-lookup/v1"
)


def _book() -> dict:
    return {
        "valuation_status": "carried_forward_display_only",
        "cash": "0",
        "nav": "0",
        "market_value": "0",
        "total_economic_cost": "0",
        "realized_pnl": "0",
        "fees": "0",
        "performance": {
            "valuation_session": "2026-07-31",
            "prior_nav": None,
            "prior_cumulative_twr": None,
            "net_external_flow": "0",
            "weighted_external_flow": "0",
            "daily_pnl": None,
            "daily_return": None,
            "cumulative_twr": None,
        },
        "positions": [],
    }


def _unfinished_report(*, prepared_at: str = "2026-08-01T05:15:00Z") -> dict:
    return {
        "classification": "synthetic_example",
        "simulation": True,
        "report_status": "no_new_close",
        "prepared_at": prepared_at,
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
            "as_of": prepared_at,
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
            "ledger_last_event_hash": LEDGER_HASH,
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
                    "configured": {"amount": "10"},
                    "proposed": {
                        "amount": "25",
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
            "overall_view": "Synthetic.",
            "market_regime": "unknown",
            "risk_budget_multiplier": "0",
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


def _report(
    *, target: str = TARGET, prepared_at: str = "2026-08-01T05:15:00Z"
) -> dict:
    return finalize_private_daily_report(
        _unfinished_report(prepared_at=prepared_at),
        target_key_sha256=compute_target_key_sha256(target),
    )


def _legacy_report() -> dict:
    report = _report()
    target_hash = compute_target_key_sha256(TARGET)
    report["schema_version"] = LEGACY_SCHEMA_VERSION
    report["delivery"]["delivery_id"] = compute_delivery_id(
        delivery_date=report["delivery"]["delivery_date"],
        timezone=report["delivery"]["timezone"],
        channel=report["delivery"]["channel"],
        target_key_sha256=target_hash,
        schema_version=LEGACY_SCHEMA_VERSION,
    )
    report["report_id"] = compute_report_id(report)
    return report


def _dated_report(
    delivery_date: dt.date,
    *,
    target: str = TARGET,
    ledger_hash: str | None = LEDGER_HASH,
) -> dict:
    prepared_at = dt.datetime.combine(
        delivery_date,
        dt.time(5, 15),
        tzinfo=UTC,
    ).isoformat().replace("+00:00", "Z")
    draft = _unfinished_report(prepared_at=prepared_at)
    draft["delivery"]["delivery_date"] = delivery_date.isoformat()
    draft["portfolio"]["ledger_last_event_hash"] = ledger_hash
    return finalize_private_daily_report(
        draft,
        target_key_sha256=compute_target_key_sha256(target),
    )


def _outbox(tmp_path: Path) -> DailyReportOutbox:
    return DailyReportOutbox(tmp_path / "private" / "daily-outbox.sqlite3")


def test_sqlite_durability_pragmas_and_schema(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    with outbox._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"daily_report_outbox", "daily_delivery_attempts"} <= names


def test_enqueue_is_idempotent_for_exact_content(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    report = _report()
    first = outbox.enqueue(report, TARGET, LEDGER_HASH, now=NOW)
    second = outbox.enqueue(report, TARGET, LEDGER_HASH, now=NOW)

    assert first.status == "prepared"
    assert not first.idempotent_replay
    assert second.idempotent_replay
    assert second.outbox_id == first.outbox_id
    assert outbox.get(first.delivery_id).attempt_count == 0


def test_concurrent_identical_enqueue_creates_one_row(tmp_path: Path) -> None:
    path = tmp_path / "private" / "daily-outbox.sqlite3"
    report = _report()

    def insert(_: int):
        return DailyReportOutbox(path).enqueue(report, TARGET, LEDGER_HASH, now=NOW)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(insert, range(16)))

    assert len({item.outbox_id for item in results}) == 1
    assert sum(not item.idempotent_replay for item in results) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM daily_report_outbox").fetchone()[0] == 1


def test_same_target_channel_and_date_rejects_changed_content(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    first = _report()
    changed = _report(prepared_at="2026-08-01T05:16:00Z")
    outbox.enqueue(first, TARGET, LEDGER_HASH, now=NOW)

    with pytest.raises(OutboxIdempotencyConflict):
        outbox.enqueue(
            changed,
            TARGET,
            LEDGER_HASH,
            now=NOW + dt.timedelta(minutes=1),
        )


def test_enqueue_fails_closed_on_identity_and_ledger_mismatch(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    report = _report()

    with pytest.raises(OutboxValidationError, match="ledger hash"):
        outbox.enqueue(report, TARGET, "1" * 64, now=NOW)
    with pytest.raises(OutboxValidationError, match="delivery_id"):
        outbox.enqueue(report, TARGET + "-other", LEDGER_HASH, now=NOW)

    tampered = copy.deepcopy(report)
    tampered["report_id"] = "f" * 64
    with pytest.raises(OutboxValidationError):
        outbox.enqueue(tampered, TARGET, LEDGER_HASH, now=NOW)


def test_first_blocked_report_with_no_ledger_hash_can_be_delivered(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    report = finalize_private_daily_report(
        blocked_first_run_draft(),
        target_key_sha256=compute_target_key_sha256(TARGET),
    )

    result = outbox.enqueue(report, TARGET, None, now=NOW)

    assert outbox.get(result.delivery_id).ledger_last_event_hash is None
    claim = outbox.claim(result.delivery_id, IDEMPOTENT, now=NOW)
    assert claim.report["portfolio"]["ledger_last_event_hash"] is None


def test_target_and_secret_tokens_never_persist_as_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "private" / "daily-outbox.sqlite3"
    outbox = DailyReportOutbox(path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    claim = outbox.claim(result.delivery_id, IDEMPOTENT, now=NOW)
    receipt = "receiver-secret-receipt-e7972590"
    outbox.mark_delivered(
        result.delivery_id,
        claim.lease_token,
        delivered_at=NOW + dt.timedelta(seconds=3),
        receiver_receipt=receipt,
    )
    with outbox._connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    persisted = b"".join(
        candidate.read_bytes()
        for candidate in path.parent.glob(path.name + "*")
        if candidate.is_file()
    )
    assert TARGET.encode() not in persisted
    assert IDEMPOTENT.idempotency_scope.encode() not in persisted
    assert claim.lease_token.encode() not in persisted
    assert receipt.encode() not in persisted
    assert compute_target_key_sha256(TARGET).encode() in persisted


def test_find_slot_hashes_raw_target_and_returns_only_sanitized_state(
    tmp_path: Path,
) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)

    slot = outbox.find_slot(TARGET, "codex", dt.date(2026, 8, 1))

    assert slot is not None
    assert slot.delivery_id == result.delivery_id
    assert slot.target_key_sha256 == compute_target_key_sha256(TARGET)
    assert TARGET not in repr(slot)
    assert outbox.find_slot(TARGET + "-other", "codex", "2026-08-01") is None
    assert outbox.find_slot(TARGET, "other", "2026-08-01") is None
    assert outbox.find_slot(TARGET, "codex", "2026-08-02") is None


def test_latest_delivered_checkpoint_uses_newest_delivered_report(
    tmp_path: Path,
) -> None:
    outbox = _outbox(tmp_path)
    day_one = dt.date(2026, 8, 1)
    day_two = dt.date(2026, 8, 2)
    hash_two = "2" * 64

    first = outbox.enqueue(
        _dated_report(day_one), TARGET, LEDGER_HASH, now=NOW
    )
    first_claim = outbox.claim(first.delivery_id, IDEMPOTENT, now=NOW)
    outbox.mark_delivered(
        first.delivery_id,
        first_claim.lease_token,
        delivered_at=NOW + dt.timedelta(seconds=1),
    )
    day_two_now = NOW + dt.timedelta(days=1)
    second = outbox.enqueue(
        _dated_report(day_two, ledger_hash=hash_two),
        TARGET,
        hash_two,
        now=day_two_now,
    )
    second_claim = outbox.claim(second.delivery_id, IDEMPOTENT, now=day_two_now)
    outbox.mark_delivered(
        second.delivery_id,
        second_claim.lease_token,
        delivered_at=day_two_now + dt.timedelta(seconds=1),
    )

    checkpoint = outbox.latest_delivered_checkpoint(TARGET, "codex")

    assert isinstance(checkpoint, DeliveredCheckpoint)
    assert checkpoint.delivery_date == day_two
    assert checkpoint.portfolio_as_of_session == dt.date(2026, 7, 31)
    assert checkpoint.ledger_last_event_hash == hash_two
    assert TARGET not in repr(checkpoint)
    assert outbox.latest_delivered_checkpoint(TARGET + "-other", "codex") is None


@pytest.mark.parametrize(
    "pending_status",
    ["prepared", "sending", "delivery_unknown", "retryable"],
)
def test_oldest_pending_recognizes_every_blocking_state(
    tmp_path: Path,
    pending_status: str,
) -> None:
    outbox = _outbox(tmp_path)
    report = _dated_report(dt.date(2026, 8, 1))
    result = outbox.enqueue(report, TARGET, LEDGER_HASH, now=NOW)
    if pending_status != "prepared":
        claim = outbox.claim(result.delivery_id, BOTH, now=NOW)
        if pending_status in {"delivery_unknown", "retryable"}:
            outbox.mark_unknown(
                result.delivery_id,
                claim.lease_token,
                observed_at=NOW + dt.timedelta(seconds=1),
            )
        if pending_status == "retryable":
            outbox.reconcile_unknown(
                result.delivery_id,
                receiver_status="not_found",
                capabilities=BOTH,
                reconciled_at=NOW + dt.timedelta(seconds=2),
            )

    pending = outbox.oldest_pending(
        TARGET,
        "codex",
        before_delivery_date="2026-08-02",
    )

    assert pending is not None
    assert pending.delivery_id == result.delivery_id
    assert pending.status == pending_status
    assert outbox.oldest_pending(
        TARGET,
        "codex",
        before_delivery_date="2026-08-01",
    ) is None


def test_oldest_pending_is_chronological_not_insertion_order(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    later_now = NOW + dt.timedelta(days=2)
    later = outbox.enqueue(
        _dated_report(dt.date(2026, 8, 3)),
        TARGET,
        LEDGER_HASH,
        now=later_now,
    )
    earlier_now = NOW + dt.timedelta(days=1)
    earlier = outbox.enqueue(
        _dated_report(dt.date(2026, 8, 2)),
        TARGET,
        LEDGER_HASH,
        now=earlier_now,
    )

    pending = outbox.oldest_pending(TARGET, "codex")

    assert pending is not None
    assert pending.delivery_id == earlier.delivery_id
    assert pending.delivery_id != later.delivery_id


def test_ledger_mutation_barrier_spans_every_receiver_scope(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    other_target = TARGET + "-rotated"
    outbox.enqueue(_report(target=other_target), other_target, LEDGER_HASH, now=NOW)

    with pytest.raises(OutboxLedgerMutationBlocked):
        outbox.require_ledger_mutation_allowed(lambda _event_hash: True)


def test_ledger_mutation_barrier_requires_delivered_checkpoint_in_chain(
    tmp_path: Path,
) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    claim = outbox.claim(result.delivery_id, IDEMPOTENT, now=NOW)
    outbox.mark_delivered(
        result.delivery_id,
        claim.lease_token,
        delivered_at=NOW + dt.timedelta(seconds=1),
    )

    outbox.require_ledger_mutation_allowed(
        lambda event_hash: event_hash == LEDGER_HASH
    )
    with pytest.raises(OutboxIntegrityError, match="current ledger chain"):
        outbox.require_ledger_mutation_allowed(lambda _event_hash: False)


def test_load_validated_content_is_identity_only_and_repr_redacted(
    tmp_path: Path,
) -> None:
    outbox = _outbox(tmp_path)
    report = _report()
    result = outbox.enqueue(report, TARGET, LEDGER_HASH, now=NOW)

    content = outbox.load_validated_content(result.delivery_id)

    assert isinstance(content, OutboxContent)
    assert content.report == report
    assert content.markdown == render_private_daily_markdown(report)
    assert content.report_id == result.report_id
    assert content.delivery_id == result.delivery_id
    assert TARGET not in repr(content)
    assert "Synthetic." not in repr(content)


def test_legacy_v1_outbox_content_replays_with_byte_stable_renderer(
    tmp_path: Path,
) -> None:
    outbox = _outbox(tmp_path)
    report = _legacy_report()
    result = outbox.enqueue(report, TARGET, LEDGER_HASH, now=NOW)

    content = outbox.load_validated_content(result.delivery_id)
    claim = outbox.claim(result.delivery_id, IDEMPOTENT, now=NOW)

    assert content.report["schema_version"] == LEGACY_SCHEMA_VERSION
    assert "| 基金 | 状态 | 摘要 | 理由代码 |" in content.markdown
    assert "| 产品质量 |" not in content.markdown
    assert claim.markdown == content.markdown


def test_sensitive_dataclass_repr_omits_scopes_content_and_lease(
    tmp_path: Path,
) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    claim = outbox.claim(result.delivery_id, BOTH, now=NOW)

    assert BOTH.idempotency_scope not in repr(BOTH)
    assert BOTH.lookup_scope not in repr(BOTH)
    assert claim.lease_token not in repr(claim)
    assert claim.markdown not in repr(claim)
    assert "Synthetic." not in repr(claim)


@pytest.mark.parametrize(
    "query_name",
    ["find_slot", "latest_delivered_checkpoint", "oldest_pending", "content"],
)
def test_runtime_preflight_queries_fail_closed_on_tampered_content(
    tmp_path: Path,
    query_name: str,
) -> None:
    path = tmp_path / "private" / "daily-outbox.sqlite3"
    outbox = DailyReportOutbox(path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER daily_report_outbox_immutable")
        connection.execute(
            "UPDATE daily_report_outbox SET markdown = '# Tampered' WHERE outbox_id = ?",
            (result.outbox_id,),
        )
        connection.commit()

    with pytest.raises(OutboxIntegrityError):
        if query_name == "find_slot":
            outbox.find_slot(TARGET, "codex", "2026-08-01")
        elif query_name == "latest_delivered_checkpoint":
            outbox.latest_delivered_checkpoint(TARGET, "codex")
        elif query_name == "oldest_pending":
            outbox.oldest_pending(TARGET, "codex")
        else:
            outbox.load_validated_content(result.delivery_id)


def test_slot_lookup_cannot_hide_tampered_target_digest(tmp_path: Path) -> None:
    path = tmp_path / "private" / "daily-outbox.sqlite3"
    outbox = DailyReportOutbox(path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER daily_report_outbox_immutable")
        connection.execute(
            "UPDATE daily_report_outbox SET target_key_sha256 = ? WHERE outbox_id = ?",
            ("f" * 64, result.outbox_id),
        )
        connection.commit()

    with pytest.raises(OutboxIntegrityError):
        outbox.find_slot(TARGET, "codex", "2026-08-01")


def test_slot_lookup_fails_closed_on_impossible_mutable_state(tmp_path: Path) -> None:
    path = tmp_path / "private" / "daily-outbox.sqlite3"
    outbox = DailyReportOutbox(path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER daily_report_outbox_status_transition")
        connection.execute(
            "UPDATE daily_report_outbox SET status = 'delivered', delivered_at = ? "
            "WHERE outbox_id = ?",
            (NOW.isoformat(), result.outbox_id),
        )
        connection.commit()

    with pytest.raises(OutboxIntegrityError, match="delivered outbox"):
        outbox.find_slot(TARGET, "codex", "2026-08-01")


def test_preflight_queries_never_persist_raw_target(tmp_path: Path) -> None:
    path = tmp_path / "private" / "daily-outbox.sqlite3"
    outbox = DailyReportOutbox(path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)

    outbox.find_slot(TARGET, "codex", "2026-08-01")
    outbox.latest_delivered_checkpoint(TARGET, "codex")
    outbox.oldest_pending(TARGET, "codex")
    outbox.load_validated_content(result.delivery_id)
    with outbox._connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    persisted = b"".join(
        candidate.read_bytes()
        for candidate in path.parent.glob(path.name + "*")
        if candidate.is_file()
    )
    assert TARGET.encode() not in persisted


def test_report_rejects_raw_target_inside_persisted_content(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    draft = _unfinished_report()
    draft["research"]["overall_view"] = f"Accidentally leaked {TARGET}"
    report = finalize_private_daily_report(
        draft,
        target_key_sha256=compute_target_key_sha256(TARGET),
    )
    with pytest.raises(OutboxValidationError, match="raw target"):
        outbox.enqueue(report, TARGET, LEDGER_HASH, now=NOW)


def test_outbox_rejects_target_digest_in_replayed_report_content(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    target_hash = compute_target_key_sha256(TARGET)
    normal = _report()
    draft = _unfinished_report()
    draft["delivery"]["delivery_id"] = normal["delivery"]["delivery_id"]
    draft["research"]["notes"] = [target_hash]
    replay = finalize_private_daily_report(draft)

    with pytest.raises(OutboxValidationError, match="target key digest"):
        outbox.enqueue(replay, TARGET, LEDGER_HASH, now=NOW)


def test_exactly_once_capabilities_are_required(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    at_most_once_only = DeliveryAdapterCapabilities(False, False)

    with pytest.raises(OutboxCapabilityError, match="only at-most-once"):
        outbox.claim(result.delivery_id, at_most_once_only, now=NOW)
    assert outbox.get(result.delivery_id).status == "prepared"

    with pytest.raises(OutboxValidationError, match="idempotency_scope"):
        DeliveryAdapterCapabilities(True, False)
    with pytest.raises(OutboxValidationError, match="lookup_scope"):
        DeliveryAdapterCapabilities(False, True)


def test_claim_and_mark_delivered_are_atomic_and_token_guarded(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    claim = outbox.claim(result.delivery_id, IDEMPOTENT, now=NOW)

    assert claim.attempt_number == 1
    assert claim.idempotency_key == result.delivery_id
    assert claim.report["report_id"] == result.report_id
    assert claim.markdown == render_private_daily_markdown(claim.report)
    assert outbox.get(result.delivery_id).status == "sending"
    with pytest.raises(OutboxLeaseError):
        outbox.mark_delivered(
            result.delivery_id,
            "x" * 40,
            delivered_at=NOW + dt.timedelta(seconds=1),
        )

    delivered = outbox.mark_delivered(
        result.delivery_id,
        claim.lease_token,
        delivered_at=NOW + dt.timedelta(seconds=2),
    )
    assert delivered.status == "delivered"
    assert outbox.attempts(result.delivery_id)[0].status == "delivered"
    with pytest.raises(OutboxStateError):
        outbox.mark_unknown(
            result.delivery_id,
            claim.lease_token,
            observed_at=NOW + dt.timedelta(seconds=3),
        )


def test_expired_lease_becomes_unknown_and_cannot_blindly_resend(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    outbox.claim(
        result.delivery_id,
        LOOKUP,
        now=NOW,
        lease_duration=dt.timedelta(seconds=10),
    )

    with pytest.raises(OutboxLeaseError, match="reconcile"):
        outbox.claim(result.delivery_id, LOOKUP, now=NOW + dt.timedelta(seconds=11))
    record = outbox.get(result.delivery_id)
    assert record.status == "delivery_unknown"
    assert record.attempt_count == 1
    assert outbox.attempts(result.delivery_id)[0].error_code == "lease_expired_status_unknown"
    with pytest.raises(OutboxStateError):
        outbox.claim(result.delivery_id, LOOKUP, now=NOW + dt.timedelta(seconds=12))


def test_unknown_retries_only_after_explicit_receiver_not_found(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    claim = outbox.claim(result.delivery_id, LOOKUP, now=NOW)
    unknown = outbox.mark_unknown(
        result.delivery_id,
        claim.lease_token,
        observed_at=NOW + dt.timedelta(seconds=1),
        error_code="network_timeout",
    )
    assert unknown.status == "delivery_unknown"
    with pytest.raises(OutboxStateError):
        outbox.claim(result.delivery_id, LOOKUP, now=NOW + dt.timedelta(seconds=2))
    with pytest.raises(OutboxValidationError):
        outbox.reconcile_unknown(
            result.delivery_id,
            receiver_status="maybe",
            capabilities=LOOKUP,
            reconciled_at=NOW + dt.timedelta(seconds=3),
        )

    retryable = outbox.reconcile_unknown(
        result.delivery_id,
        receiver_status="not_found",
        capabilities=LOOKUP,
        reconciled_at=NOW + dt.timedelta(seconds=4),
    )
    assert retryable.status == "retryable"
    second = outbox.claim(result.delivery_id, LOOKUP, now=NOW + dt.timedelta(seconds=5))
    assert second.attempt_number == 2
    outbox.mark_delivered(
        result.delivery_id,
        second.lease_token,
        delivered_at=NOW + dt.timedelta(seconds=6),
    )
    assert [item.status for item in outbox.attempts(result.delivery_id)] == [
        "receiver_not_found",
        "delivered",
    ]


def test_unknown_lookup_confirmation_marks_delivered(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    claim = outbox.claim(result.delivery_id, LOOKUP, now=NOW)
    outbox.mark_unknown(
        result.delivery_id,
        claim.lease_token,
        observed_at=NOW + dt.timedelta(seconds=1),
    )
    delivered = outbox.reconcile_unknown(
        result.delivery_id,
        receiver_status="delivered",
        capabilities=LOOKUP,
        reconciled_at=NOW + dt.timedelta(seconds=2),
        receiver_receipt="lookup-receipt-123",
    )
    assert delivered.status == "delivered"
    assert delivered.delivered_at == NOW + dt.timedelta(seconds=2)
    assert outbox.attempts(result.delivery_id)[0].status == "delivered"


def test_unknown_reconciliation_requires_original_lookup_scope(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    claim = outbox.claim(result.delivery_id, LOOKUP, now=NOW)
    outbox.mark_unknown(
        result.delivery_id,
        claim.lease_token,
        observed_at=NOW + dt.timedelta(seconds=1),
    )

    with pytest.raises(OutboxCapabilityError, match="original receiver scope"):
        outbox.reconcile_unknown(
            result.delivery_id,
            receiver_status="not_found",
            capabilities=OTHER_LOOKUP_SCOPE,
            reconciled_at=NOW + dt.timedelta(seconds=2),
        )
    assert outbox.get(result.delivery_id).status == "delivery_unknown"


def test_unknown_without_lookup_capability_cannot_be_declared_retryable(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    claim = outbox.claim(result.delivery_id, IDEMPOTENT, now=NOW)
    outbox.mark_unknown(
        result.delivery_id,
        claim.lease_token,
        observed_at=NOW + dt.timedelta(seconds=1),
    )

    with pytest.raises(OutboxCapabilityError):
        outbox.reconcile_unknown(
            result.delivery_id,
            receiver_status="not_found",
            capabilities=IDEMPOTENT,
            reconciled_at=NOW + dt.timedelta(seconds=2),
        )
    # A caller cannot invent lookup support after the attempt was claimed.
    with pytest.raises(OutboxCapabilityError):
        outbox.reconcile_unknown(
            result.delivery_id,
            receiver_status="not_found",
            capabilities=LOOKUP,
            reconciled_at=NOW + dt.timedelta(seconds=2),
        )
    assert outbox.get(result.delivery_id).status == "delivery_unknown"


def test_idempotency_only_unknown_can_retry_under_same_delivery_key(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    first = outbox.claim(result.delivery_id, IDEMPOTENT, now=NOW)
    outbox.mark_unknown(
        result.delivery_id,
        first.lease_token,
        observed_at=NOW + dt.timedelta(seconds=1),
    )

    with pytest.raises(OutboxCapabilityError):
        outbox.authorize_idempotent_retry(
            result.delivery_id,
            capabilities=LOOKUP,
            authorized_at=NOW + dt.timedelta(seconds=2),
        )
    retryable = outbox.authorize_idempotent_retry(
        result.delivery_id,
        capabilities=IDEMPOTENT,
        authorized_at=NOW + dt.timedelta(seconds=2),
    )
    assert retryable.status == "retryable"
    old_attempt = outbox.attempts(result.delivery_id)[0]
    assert old_attempt.status == "delivery_unknown"
    assert old_attempt.idempotent_retry_authorized_at == NOW + dt.timedelta(seconds=2)

    with pytest.raises(OutboxCapabilityError, match="idempotency key"):
        outbox.claim(
            result.delivery_id,
            LOOKUP,
            now=NOW + dt.timedelta(seconds=3),
        )
    with pytest.raises(OutboxCapabilityError, match="original receiver scope"):
        outbox.claim(
            result.delivery_id,
            OTHER_IDEMPOTENT_SCOPE,
            now=NOW + dt.timedelta(seconds=3),
        )
    assert outbox.get(result.delivery_id).status == "retryable"
    assert outbox.get(result.delivery_id).attempt_count == 1

    second = outbox.claim(
        result.delivery_id,
        IDEMPOTENT,
        now=NOW + dt.timedelta(seconds=3),
    )
    assert second.attempt_number == 2
    assert second.idempotency_key == first.idempotency_key == result.delivery_id
    outbox.mark_delivered(
        result.delivery_id,
        second.lease_token,
        delivered_at=NOW + dt.timedelta(seconds=4),
    )
    assert [item.status for item in outbox.attempts(result.delivery_id)] == [
        "delivery_unknown",
        "delivered",
    ]


def test_idempotent_retry_requires_support_on_original_attempt(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    claim = outbox.claim(result.delivery_id, LOOKUP, now=NOW)
    outbox.mark_unknown(
        result.delivery_id,
        claim.lease_token,
        observed_at=NOW + dt.timedelta(seconds=1),
    )
    with pytest.raises(OutboxCapabilityError, match="did not use"):
        outbox.authorize_idempotent_retry(
            result.delivery_id,
            capabilities=IDEMPOTENT,
            authorized_at=NOW + dt.timedelta(seconds=2),
        )
    assert outbox.get(result.delivery_id).status == "delivery_unknown"


def test_idempotent_retry_requires_the_same_receiver_scope(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    claim = outbox.claim(result.delivery_id, IDEMPOTENT, now=NOW)
    outbox.mark_unknown(
        result.delivery_id,
        claim.lease_token,
        observed_at=NOW + dt.timedelta(seconds=1),
    )

    with pytest.raises(OutboxCapabilityError, match="original receiver scope"):
        outbox.authorize_idempotent_retry(
            result.delivery_id,
            capabilities=OTHER_IDEMPOTENT_SCOPE,
            authorized_at=NOW + dt.timedelta(seconds=2),
        )
    assert outbox.get(result.delivery_id).status == "delivery_unknown"


def test_naive_times_and_malformed_lease_token_are_rejected(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    with pytest.raises(OutboxValidationError, match="timezone-aware"):
        outbox.claim(result.delivery_id, IDEMPOTENT, now=NOW.replace(tzinfo=None))
    claim = outbox.claim(result.delivery_id, IDEMPOTENT, now=NOW)
    with pytest.raises(OutboxValidationError, match="timezone-aware"):
        outbox.mark_delivered(
            result.delivery_id,
            claim.lease_token,
            delivered_at=NOW.replace(tzinfo=None),
        )
    with pytest.raises(OutboxLeaseError):
        outbox.mark_unknown(
            result.delivery_id,
            "short",
            observed_at=NOW + dt.timedelta(seconds=1),
        )


def test_delivery_times_are_monotonic_and_failures_roll_back(tmp_path: Path) -> None:
    outbox = _outbox(tmp_path)
    with pytest.raises(OutboxValidationError, match="report.prepared_at"):
        outbox.enqueue(
            _report(),
            TARGET,
            LEDGER_HASH,
            now=NOW - dt.timedelta(seconds=1),
        )
    with sqlite3.connect(outbox.database_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM daily_report_outbox"
        ).fetchone()[0] == 0

    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    with pytest.raises(OutboxValidationError, match="precede persisted"):
        outbox.claim(result.delivery_id, BOTH, now=NOW - dt.timedelta(seconds=1))
    assert outbox.get(result.delivery_id).attempt_count == 0

    claim = outbox.claim(result.delivery_id, BOTH, now=NOW)
    with pytest.raises(OutboxValidationError, match="precede the attempt claim"):
        outbox.mark_delivered(
            result.delivery_id,
            claim.lease_token,
            delivered_at=NOW - dt.timedelta(seconds=1),
        )
    with pytest.raises(OutboxValidationError, match="precede the attempt claim"):
        outbox.mark_unknown(
            result.delivery_id,
            claim.lease_token,
            observed_at=NOW - dt.timedelta(seconds=1),
        )
    assert outbox.get(result.delivery_id).status == "sending"
    assert outbox.attempts(result.delivery_id)[0].status == "sending"

    outbox.mark_unknown(
        result.delivery_id,
        claim.lease_token,
        observed_at=NOW + dt.timedelta(seconds=5),
    )
    with pytest.raises(OutboxValidationError, match="precede attempt completion"):
        outbox.reconcile_unknown(
            result.delivery_id,
            receiver_status="not_found",
            capabilities=BOTH,
            reconciled_at=NOW + dt.timedelta(seconds=4),
        )
    with pytest.raises(OutboxValidationError, match="precede attempt completion"):
        outbox.authorize_idempotent_retry(
            result.delivery_id,
            capabilities=BOTH,
            authorized_at=NOW + dt.timedelta(seconds=4),
        )
    assert outbox.get(result.delivery_id).status == "delivery_unknown"
    assert outbox.attempts(result.delivery_id)[0].status == "delivery_unknown"


def test_error_code_is_allowlisted_not_raw_exception_text(tmp_path: Path) -> None:
    path = tmp_path / "private" / "daily-outbox.sqlite3"
    outbox = DailyReportOutbox(path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    claim = outbox.claim(result.delivery_id, LOOKUP, now=NOW)
    raw_error = "Timeout: Authorization Bearer top-secret-value"
    outbox.mark_unknown(
        result.delivery_id,
        claim.lease_token,
        observed_at=NOW + dt.timedelta(seconds=1),
        error_code=raw_error,
    )
    attempt = outbox.attempts(result.delivery_id)[0]
    assert attempt.error_code == "other_delivery_error"
    assert raw_error.encode() not in path.read_bytes()


def test_sql_triggers_block_content_update_invalid_transition_and_delete(tmp_path: Path) -> None:
    path = tmp_path / "private" / "daily-outbox.sqlite3"
    outbox = DailyReportOutbox(path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE daily_report_outbox SET markdown = '# Tampered' WHERE outbox_id = ?",
                (result.outbox_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE daily_report_outbox SET report_id = ? WHERE outbox_id = ?",
                ("f" * 64, result.outbox_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE daily_report_outbox SET target_key_sha256 = ? WHERE outbox_id = ?",
                ("e" * 64, result.outbox_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="invalid"):
            connection.execute(
                "UPDATE daily_report_outbox SET status = 'delivered' WHERE outbox_id = ?",
                (result.outbox_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-preserved"):
            connection.execute(
                "DELETE FROM daily_report_outbox WHERE outbox_id = ?",
                (result.outbox_id,),
            )


@pytest.mark.parametrize(
    ("column", "tampered_value"),
    [
        ("report_json", "{}"),
        ("markdown", "# Tampered after trigger removal"),
        ("content_sha256", "f" * 64),
        ("report_id", "f" * 64),
        ("delivery_id", "f" * 64),
        ("delivery_date", "2026-08-02"),
        ("timezone", "UTC"),
        ("channel", "other"),
        ("target_key_sha256", "f" * 64),
        ("ledger_last_event_hash", "f" * 64),
    ],
)
def test_claim_recomputes_immutable_content_after_trigger_bypass(
    tmp_path: Path,
    column: str,
    tampered_value: str,
) -> None:
    path = tmp_path / "private" / "daily-outbox.sqlite3"
    outbox = DailyReportOutbox(path)
    result = outbox.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER daily_report_outbox_immutable")
        connection.execute(
            f"UPDATE daily_report_outbox SET {column} = ? WHERE outbox_id = ?",
            (tampered_value, result.outbox_id),
        )
        connection.commit()

    with pytest.raises(OutboxIntegrityError):
        outbox.claim(result.delivery_id, IDEMPOTENT, now=NOW)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM daily_delivery_attempts"
        ).fetchone()[0] == 0


def test_concurrent_claim_has_one_active_sender(tmp_path: Path) -> None:
    path = tmp_path / "private" / "daily-outbox.sqlite3"
    seed = DailyReportOutbox(path)
    result = seed.enqueue(_report(), TARGET, LEDGER_HASH, now=NOW)

    def attempt_claim(_: int) -> str:
        try:
            DailyReportOutbox(path).claim(result.delivery_id, IDEMPOTENT, now=NOW)
            return "claimed"
        except OutboxLeaseError:
            return "leased"

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        outcomes = list(executor.map(attempt_claim, range(6)))
    assert outcomes.count("claimed") == 1
    assert outcomes.count("leased") == 5
    assert seed.get(result.delivery_id).attempt_count == 1
