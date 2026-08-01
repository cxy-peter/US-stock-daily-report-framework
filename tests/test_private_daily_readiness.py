from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

import scripts.check_private_daily_readiness as readiness_script
import serenity_monitor.opening_owner_attestation as opening_attestation
import serenity_monitor.manual_owner_event as manual_owner_event
import serenity_monitor.private_daily_readiness as readiness
from serenity_monitor.daily_outbox import (
    DailyReportOutbox,
    DeliveryAdapterCapabilities,
)
from serenity_monitor.portfolio_ledger import PortfolioLedger
from serenity_monitor.opening_owner_attestation import (
    OpeningLedgerBinding,
    create_opening_owner_claim,
    interactive_owner_presence,
    opening_ledger_idempotency_key,
    publish_opening_intent,
    publish_opening_receipt,
)
from serenity_monitor.private_runtime_config import (
    PUBLIC_EXAMPLE_NAME,
    load_private_daily_runtime_config,
)
from serenity_monitor.private_daily_report import (
    compute_target_key_sha256,
    finalize_private_daily_report,
)
from serenity_monitor.private_runtime_paths import (
    PrivateRuntimePathError,
    PrivateRuntimePaths,
    ensure_private_storage,
    validate_existing_private_storage_root,
)
from test_private_daily_report_semantics import blocked_first_run_draft


ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)
TARGET = "codex-private-thread-readiness"
OTHER_TARGET = "codex-private-thread-unrelated"
EXACTLY_ONCE = DeliveryAdapterCapabilities(
    True,
    False,
    "codex-thread-message/v1",
)
OTHER_EXACTLY_ONCE = DeliveryAdapterCapabilities(
    True,
    False,
    "different-codex-thread-message/v1",
)
CONFIG_DIGEST = hashlib.sha256(
    (ROOT / "config" / PUBLIC_EXAMPLE_NAME).read_bytes()
).hexdigest()


class _TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _allow_attestation_temp_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        opening_attestation,
        "validate_existing_private_storage_root",
        lambda paths: paths.root,
    )
    monkeypatch.setattr(
        manual_owner_event,
        "validate_existing_private_storage_root",
        lambda paths: paths.root,
    )


def _config():
    return load_private_daily_runtime_config(
        ROOT / "config" / PUBLIC_EXAMPLE_NAME,
        allow_synthetic=True,
    )


def _paths(root: Path) -> PrivateRuntimePaths:
    return PrivateRuntimePaths(
        root=root,
        ledger_database=root / "portfolio-ledger.sqlite3",
        outbox_database=root / "daily-outbox.sqlite3",
        report_directory=root / "reports",
        lock_file=root / "private-daily-runtime.lock",
    )


def _create_opening_claim(config, paths: PrivateRuntimePaths) -> object:
    ensure_private_storage(paths)
    presence = interactive_owner_presence(
        _TTYBuffer("CONFIRM 23456789AB\n"),
        _TTYBuffer(),
        challenge_factory=lambda: "23456789AB",
    )
    create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        owner_presence=presence,
        clock=lambda: NOW,
    )
    audit = opening_attestation.audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=NOW,
        ledger_binding=None,
    )
    assert audit.claim is not None
    return audit.claim


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checkpoint(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _report(
    target: str,
    delivery_date: dt.date,
    *,
    prepared_at: dt.datetime | None = None,
) -> dict:
    prepared = prepared_at or dt.datetime.combine(
        delivery_date,
        dt.time(5, 15),
        tzinfo=dt.timezone.utc,
    )
    prepared_text = prepared.astimezone(dt.timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )
    draft = blocked_first_run_draft()
    draft["prepared_at"] = prepared_text
    draft["delivery"]["delivery_date"] = delivery_date.isoformat()
    draft["calendar"]["as_of"] = prepared_text
    return finalize_private_daily_report(
        draft,
        target_key_sha256=compute_target_key_sha256(target),
    )


def _enqueue(
    outbox: DailyReportOutbox,
    *,
    target: str = TARGET,
    delivery_date: dt.date = NOW.date(),
    prepared_at: dt.datetime | None = None,
):
    prepared = prepared_at or dt.datetime.combine(
        delivery_date,
        dt.time(5, 15),
        tzinfo=dt.timezone.utc,
    )
    return outbox.enqueue(
        _report(target, delivery_date, prepared_at=prepared),
        target,
        None,
        now=prepared + dt.timedelta(minutes=1),
    )


def test_readiness_contract_is_fixed_redacted_and_separates_activation_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    paths = _paths(root)
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_storage_root",
        lambda supplied: supplied.root,
    )
    environment = {
        "CODEX_DAILY_TARGET_KEY": "synthetic-target",
        "TWELVE_DATA_API_KEY": "present-but-never-rendered",
        "ALPHA_VANTAGE_API_KEY": "present-but-never-rendered",
    }
    config = replace(_config(), corporate_action_attestations=())

    result = readiness.evaluate_private_daily_readiness(
        config,
        paths,
        environ=environment,
        clock=lambda: NOW,
        config_acl_passed=True,
    )
    document = result.to_dict()
    serialized = result.to_json()

    assert document["contract_version"] == readiness.READINESS_CONTRACT_VERSION
    assert document["overall"] == "blocked"
    assert document["operational_state"] == "blocked"
    assert document["next_safe_action"] == "operator_review"
    assert document["outbox_state"] == "empty"
    assert document["ready_for_initialize"] is False
    assert document["ready_for_prepare"] is False
    assert document["ready_for_delivery"] is False
    assert document["workflow_activation_allowed"] is False
    assert [item["check_id"] for item in document["checks"]] == sorted(
        item["check_id"] for item in document["checks"]
    )
    status = {item["check_id"]: item["status"] for item in document["checks"]}
    assert status["provider_credentials"] == "passed"
    assert status["corporate_action_coverage"] == "blocked"
    assert status["opening_owner_attestation"] == "blocked"
    assert status["manual_event_ingestion"] == "not_run"
    assert status["receiver_idempotency"] == "unverified"
    assert "synthetic-target" not in serialized
    assert "present-but-never-rendered" not in serialized
    assert str(root) not in serialized
    assert all(item.canonical_symbol not in serialized for item in config.instruments)


def test_ledger_readonly_audit_does_not_change_database_or_create_sidecars(
    tmp_path: Path,
) -> None:
    config = _config()
    database = tmp_path / "ledger.sqlite3"
    ledger = PortfolioLedger(database, policy=config.ledger_policy)
    ledger.initialize(
        config.opening.session,
        config.opening.cash,
        config.opening.positions,
    )
    _checkpoint(database)
    before_hash = _sha256(database)
    before_files = {item.name for item in tmp_path.iterdir()}
    before_mtime = database.stat().st_mtime_ns

    audit = readiness._audit_ledger_readonly(config, database)

    assert audit.state == "opening_only"
    assert audit.latest_common_session is None
    assert _sha256(database) == before_hash
    assert database.stat().st_mtime_ns == before_mtime
    assert {item.name for item in tmp_path.iterdir()} == before_files


def test_verified_claim_authorizes_initialization_without_enabling_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    paths = _paths(tmp_path / "runtime")
    _create_opening_claim(config, paths)
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_storage_root",
        lambda supplied: supplied.root,
    )
    environment = {
        "CODEX_DAILY_TARGET_KEY": TARGET,
        "TWELVE_DATA_API_KEY": "present",
        "ALPHA_VANTAGE_API_KEY": "present",
    }

    result = readiness.evaluate_private_daily_readiness(
        config,
        paths,
        environ=environment,
        clock=lambda: NOW,
        config_acl_passed=True,
        config_bytes_sha256=CONFIG_DIGEST,
    )
    checks = {item.check_id: item for item in result.checks}

    assert result.ready_for_initialize is True
    assert result.operational_state == "needs_initialization"
    assert result.next_safe_action == "initialize"
    assert result.workflow_activation_allowed is False
    assert checks["opening_owner_attestation"].status == "passed"
    assert checks["opening_owner_attestation"].reason_code == (
        "opening_attestation_pending_verified"
    )


def test_fresh_intent_without_opening_is_resumable_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    paths = _paths(tmp_path / "runtime")
    claim = _create_opening_claim(config, paths)
    publish_opening_intent(
        claim,
        paths,
        clock=lambda: NOW + dt.timedelta(seconds=1),
    )
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_storage_root",
        lambda supplied: supplied.root,
    )

    result = readiness.evaluate_private_daily_readiness(
        config,
        paths,
        environ={
            "CODEX_DAILY_TARGET_KEY": TARGET,
            "TWELVE_DATA_API_KEY": "present",
            "ALPHA_VANTAGE_API_KEY": "present",
        },
        clock=lambda: NOW + dt.timedelta(seconds=2),
        config_acl_passed=True,
        config_bytes_sha256=CONFIG_DIGEST,
    )
    check = {item.check_id: item for item in result.checks}[
        "opening_owner_attestation"
    ]

    assert result.ready_for_initialize is True
    assert result.operational_state == "needs_initialization"
    assert check.status == "passed"
    assert check.reason_code == "opening_attestation_commit_resume_available"


def test_expired_intent_requires_new_interactive_owner_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    paths = _paths(tmp_path / "runtime")
    claim = _create_opening_claim(config, paths)
    publish_opening_intent(
        claim,
        paths,
        clock=lambda: NOW + dt.timedelta(seconds=1),
    )
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_storage_root",
        lambda supplied: supplied.root,
    )

    result = readiness.evaluate_private_daily_readiness(
        config,
        paths,
        environ={
            "CODEX_DAILY_TARGET_KEY": TARGET,
            "TWELVE_DATA_API_KEY": "present",
            "ALPHA_VANTAGE_API_KEY": "present",
        },
        clock=lambda: claim.expires_at,
        config_acl_passed=True,
        config_bytes_sha256=CONFIG_DIGEST,
    )
    check = {item.check_id: item for item in result.checks}[
        "opening_owner_attestation"
    ]

    assert result.ready_for_initialize is False
    assert result.operational_state == "blocked"
    assert check.status == "blocked"
    assert check.reason_code == (
        "opening_attestation_resume_requires_owner_reconfirmation"
    )


def test_opening_only_binding_is_recoverable_then_consumed_readonly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    paths = _paths(tmp_path / "runtime")
    claim = _create_opening_claim(config, paths)
    intent = publish_opening_intent(
        claim,
        paths,
        clock=lambda: NOW + dt.timedelta(seconds=1),
    )
    ledger = PortfolioLedger(paths.ledger_database, policy=config.ledger_policy)
    ledger.initialize(
        config.opening.session,
        config.opening.cash,
        config.opening.positions,
        idempotency_key=opening_ledger_idempotency_key(claim, intent),
        recorded_at=NOW + dt.timedelta(seconds=2),
    )
    _checkpoint(paths.ledger_database)
    checkpoint = ledger.opening_checkpoint()
    binding = OpeningLedgerBinding(
        opening_event_id=checkpoint.opening_event_id,
        opening_event_hash=checkpoint.opening_event_hash,
        idempotency_key=checkpoint.idempotency_key,
        created_at=checkpoint.created_at,
    )
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_storage_root",
        lambda supplied: supplied.root,
    )
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_runtime_file",
        lambda _paths_value, supplied: Path(supplied),
    )
    environment = {
        "CODEX_DAILY_TARGET_KEY": TARGET,
        "TWELVE_DATA_API_KEY": "present",
        "ALPHA_VANTAGE_API_KEY": "present",
    }

    recovery = readiness.evaluate_private_daily_readiness(
        config,
        paths,
        environ=environment,
        clock=lambda: NOW + dt.timedelta(seconds=3),
        config_acl_passed=True,
        config_bytes_sha256=CONFIG_DIGEST,
    )
    recovery_check = {item.check_id: item for item in recovery.checks}[
        "opening_owner_attestation"
    ]
    assert recovery.ready_for_initialize is True
    assert recovery.ready_for_prepare is False
    assert recovery_check.status == "passed"
    assert recovery_check.reason_code == (
        "opening_attestation_receipt_recovery_available"
    )

    publish_opening_receipt(
        claim,
        intent,
        binding,
        paths,
        clock=lambda: NOW + dt.timedelta(seconds=4),
    )
    before = {
        path.name: (path.stat().st_mtime_ns, _sha256(path))
        for path in (
            paths.opening_claim_file,
            paths.opening_intent_file,
            paths.opening_receipt_file,
            paths.ledger_database,
        )
    }
    consumed = readiness.evaluate_private_daily_readiness(
        config,
        paths,
        environ=environment,
        clock=lambda: NOW + dt.timedelta(seconds=5),
        config_acl_passed=True,
        config_bytes_sha256="0" * 64,
    )
    after = {
        path.name: (path.stat().st_mtime_ns, _sha256(path))
        for path in (
            paths.opening_claim_file,
            paths.opening_intent_file,
            paths.opening_receipt_file,
            paths.ledger_database,
        )
    }
    consumed_check = {item.check_id: item for item in consumed.checks}[
        "opening_owner_attestation"
    ]
    assert consumed.ready_for_initialize is True
    assert consumed_check.reason_code == "opening_attestation_consumed_verified"
    assert after == before


def test_outbox_readonly_audit_does_not_change_database_or_create_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "outbox.sqlite3"
    DailyReportOutbox(database)
    _checkpoint(database)
    before_hash = _sha256(database)
    before_files = {item.name for item in tmp_path.iterdir()}
    before_mtime = database.stat().st_mtime_ns

    audit = readiness._audit_outbox_readonly(
        database,
        target_key=TARGET,
        channel="codex",
        delivery_date=NOW.date(),
    )

    assert audit.state == "empty"
    assert _sha256(database) == before_hash
    assert database.stat().st_mtime_ns == before_mtime
    assert {item.name for item in tmp_path.iterdir()} == before_files


def test_pending_delivery_without_a_current_ledger_head_is_not_sendable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    paths = _paths(root)
    outbox = DailyReportOutbox(paths.outbox_database)
    _enqueue(outbox, target=TARGET)
    _checkpoint(paths.outbox_database)
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_storage_root",
        lambda supplied: supplied.root,
    )
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_runtime_file",
        lambda _paths_value, supplied: Path(supplied),
    )
    config = replace(_config(), corporate_action_attestations=())

    result = readiness.evaluate_private_daily_readiness(
        config,
        paths,
        environ={"CODEX_DAILY_TARGET_KEY": TARGET},
        clock=lambda: NOW,
        config_acl_passed=True,
        receiver_capabilities=EXACTLY_ONCE,
    )

    assert result.outbox_state == "pending_delivery"
    assert result.operational_state == "pending_delivery"
    assert result.next_safe_action == "operator_review"
    assert result.ready_for_delivery is False
    assert result.ready_for_initialize is False
    assert result.ready_for_prepare is False
    assert result.workflow_activation_allowed is False
    checks = {item.check_id: item for item in result.checks}
    assert checks["provider_credentials"].status == "blocked"
    assert checks["ledger_integrity"].status == "not_run"
    assert checks["corporate_action_coverage"].status == "blocked"
    assert checks["receiver_idempotency"].status == "blocked"
    assert (
        checks["receiver_idempotency"].reason_code
        == "prepared_report_ledger_head_stale"
    )


def test_unrelated_receiver_pending_is_a_cross_scope_conflict(
    tmp_path: Path,
) -> None:
    database = tmp_path / "outbox.sqlite3"
    outbox = DailyReportOutbox(database)
    _enqueue(outbox, target=OTHER_TARGET)
    _checkpoint(database)

    audit = readiness._audit_outbox_readonly(
        database,
        target_key=TARGET,
        channel="codex",
        delivery_date=NOW.date(),
    )

    assert audit.state == "conflict"


def test_sending_delivery_requires_reconciliation_not_blind_retry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "outbox.sqlite3"
    outbox = DailyReportOutbox(database)
    result = _enqueue(outbox)
    outbox.claim(
        result.delivery_id,
        EXACTLY_ONCE,
        now=NOW - dt.timedelta(hours=6, minutes=43),
    )
    _checkpoint(database)

    audit = readiness._audit_outbox_readonly(
        database,
        target_key=TARGET,
        channel="codex",
        delivery_date=NOW.date(),
    )

    assert audit.state == "reconciliation_required"


def test_retryable_delivery_requires_the_original_idempotency_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    paths = _paths(root)
    outbox = DailyReportOutbox(paths.outbox_database)
    result = _enqueue(outbox)
    claimed_at = NOW - dt.timedelta(hours=6, minutes=43)
    claim = outbox.claim(result.delivery_id, EXACTLY_ONCE, now=claimed_at)
    outbox.mark_unknown(
        result.delivery_id,
        claim.lease_token,
        observed_at=claimed_at + dt.timedelta(seconds=1),
    )
    outbox.authorize_idempotent_retry(
        result.delivery_id,
        capabilities=EXACTLY_ONCE,
        authorized_at=claimed_at + dt.timedelta(seconds=2),
    )
    _checkpoint(paths.outbox_database)
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_storage_root",
        lambda supplied: supplied.root,
    )
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_runtime_file",
        lambda _paths_value, supplied: Path(supplied),
    )

    result = readiness.evaluate_private_daily_readiness(
        replace(_config(), corporate_action_attestations=()),
        paths,
        environ={"CODEX_DAILY_TARGET_KEY": TARGET},
        clock=lambda: NOW,
        config_acl_passed=True,
        receiver_capabilities=OTHER_EXACTLY_ONCE,
    )

    assert result.outbox_state == "pending_delivery"
    assert result.ready_for_delivery is False
    assert result.operational_state == "pending_delivery"
    assert result.next_safe_action == "operator_review"
    checks = {item.check_id: item for item in result.checks}
    assert checks["receiver_idempotency"].reason_code == "prepared_report_ledger_head_stale"


def test_multiple_unresolved_dates_are_a_conflict(tmp_path: Path) -> None:
    database = tmp_path / "outbox.sqlite3"
    outbox = DailyReportOutbox(database)
    _enqueue(outbox, delivery_date=dt.date(2026, 8, 1))
    _enqueue(outbox, delivery_date=dt.date(2026, 8, 2))
    _checkpoint(database)

    audit = readiness._audit_outbox_readonly(
        database,
        target_key=TARGET,
        channel="codex",
        delivery_date=dt.date(2026, 8, 2),
    )

    assert audit.state == "conflict"


def test_current_day_delivered_is_already_complete(tmp_path: Path) -> None:
    database = tmp_path / "outbox.sqlite3"
    outbox = DailyReportOutbox(database)
    result = _enqueue(outbox)
    claimed_at = NOW - dt.timedelta(hours=6, minutes=43)
    claim = outbox.claim(result.delivery_id, EXACTLY_ONCE, now=claimed_at)
    outbox.mark_delivered(
        result.delivery_id,
        claim.lease_token,
        delivered_at=claimed_at + dt.timedelta(seconds=1),
        receiver_receipt="synthetic-receipt",
    )
    _checkpoint(database)

    audit = readiness._audit_outbox_readonly(
        database,
        target_key=TARGET,
        channel="codex",
        delivery_date=NOW.date(),
    )

    assert audit.state == "already_complete"


def test_evaluate_uses_report_timezone_across_utc_date_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    paths = _paths(root)
    outbox = DailyReportOutbox(paths.outbox_database)
    checked_at = dt.datetime(2026, 8, 1, 16, 30, tzinfo=dt.timezone.utc)
    prepared_at = checked_at - dt.timedelta(minutes=15)
    delivery_date = dt.date(2026, 8, 2)
    result = _enqueue(
        outbox,
        delivery_date=delivery_date,
        prepared_at=prepared_at,
    )
    claim = outbox.claim(
        result.delivery_id,
        EXACTLY_ONCE,
        now=prepared_at + dt.timedelta(minutes=2),
    )
    outbox.mark_delivered(
        result.delivery_id,
        claim.lease_token,
        delivered_at=prepared_at + dt.timedelta(minutes=3),
        receiver_receipt="synthetic-receipt",
    )
    _checkpoint(paths.outbox_database)
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_storage_root",
        lambda supplied: supplied.root,
    )
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_runtime_file",
        lambda _paths_value, supplied: Path(supplied),
    )

    audit = readiness.evaluate_private_daily_readiness(
        replace(_config(), corporate_action_attestations=()),
        paths,
        environ={"CODEX_DAILY_TARGET_KEY": TARGET},
        clock=lambda: checked_at,
        config_acl_passed=True,
        receiver_capabilities=EXACTLY_ONCE,
    )

    assert audit.outbox_state == "already_complete"
    assert audit.operational_state == "already_complete"
    assert audit.next_safe_action == "none"


def test_readonly_audits_never_call_normal_store_constructors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    ledger_path = tmp_path / "ledger.sqlite3"
    outbox_path = tmp_path / "outbox.sqlite3"
    ledger = PortfolioLedger(ledger_path, policy=config.ledger_policy)
    ledger.initialize(
        config.opening.session,
        config.opening.cash,
        config.opening.positions,
    )
    DailyReportOutbox(outbox_path)
    _checkpoint(ledger_path)
    _checkpoint(outbox_path)

    def forbidden_constructor(*_args, **_kwargs):
        raise AssertionError("normal constructor must not be called")

    monkeypatch.setattr(PortfolioLedger, "__init__", forbidden_constructor)
    monkeypatch.setattr(DailyReportOutbox, "__init__", forbidden_constructor)

    assert readiness._audit_ledger_readonly(config, ledger_path).state == "opening_only"
    assert (
        readiness._audit_outbox_readonly(
            outbox_path,
            target_key=TARGET,
            channel="codex",
            delivery_date=NOW.date(),
        ).state
        == "empty"
    )


def test_full_readiness_never_calls_provider_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    paths = _paths(root)
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_storage_root",
        lambda supplied: supplied.root,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("readiness audit must remain offline")

    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr(
        "serenity_monitor.provider_registry.TwelveDataCloseProvider.fetch_close",
        forbidden,
    )
    monkeypatch.setattr(
        "serenity_monitor.provider_registry.AlphaVantageCloseProvider.fetch_close",
        forbidden,
    )

    result = readiness.evaluate_private_daily_readiness(
        replace(_config(), corporate_action_attestations=()),
        paths,
        environ={"CODEX_DAILY_TARGET_KEY": TARGET},
        clock=lambda: NOW,
        config_acl_passed=True,
    )

    assert result.workflow_activation_allowed is False


def test_database_change_during_readonly_audit_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "outbox.sqlite3"
    DailyReportOutbox(database)
    _checkpoint(database)
    original = readiness._verify_outbox_schema

    def verify_then_mutate(connection):
        original(connection)
        with database.open("ab") as handle:
            handle.write(b"\0")

    monkeypatch.setattr(readiness, "_verify_outbox_schema", verify_then_mutate)

    with pytest.raises(
        readiness.PrivateDailyReadinessError,
        match="runtime_database_changed_during_readonly_audit",
    ):
        readiness._audit_outbox_readonly(
            database,
            target_key=TARGET,
            channel="codex",
            delivery_date=NOW.date(),
        )


def test_same_name_noop_outbox_trigger_blocks_integrity(tmp_path: Path) -> None:
    database = tmp_path / "outbox.sqlite3"
    DailyReportOutbox(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER daily_report_outbox_no_delete")
        connection.execute(
            "CREATE TRIGGER daily_report_outbox_no_delete "
            "BEFORE DELETE ON daily_report_outbox BEGIN SELECT 1; END"
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with pytest.raises(
        readiness.PrivateDailyReadinessError,
        match="outbox_schema_definition_mismatch",
    ):
        readiness._audit_outbox_readonly(
            database,
            target_key=TARGET,
            channel="codex",
            delivery_date=NOW.date(),
        )


def test_schema_fingerprint_preserves_case_inside_sql_literals(tmp_path: Path) -> None:
    database = tmp_path / "outbox.sqlite3"
    DailyReportOutbox(database)
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name = ?",
            ("daily_report_outbox_status_transition",),
        ).fetchone()
        assert row is not None
        tampered = str(row[0]).replace("'sending'", "'SENDING'", 1)
        connection.execute("DROP TRIGGER daily_report_outbox_status_transition")
        connection.execute(tampered)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with pytest.raises(
        readiness.PrivateDailyReadinessError,
        match="outbox_schema_definition_mismatch",
    ):
        readiness._audit_outbox_readonly(
            database,
            target_key=TARGET,
            channel="codex",
            delivery_date=NOW.date(),
        )


def test_same_name_noop_ledger_trigger_blocks_integrity(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    config = _config()
    PortfolioLedger(database, policy=config.ledger_policy)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER ledger_events_no_delete")
        connection.execute(
            "CREATE TRIGGER ledger_events_no_delete "
            "BEFORE DELETE ON ledger_events BEGIN SELECT 1; END"
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with pytest.raises(
        readiness.PrivateDailyReadinessError,
        match="ledger_schema_definition_mismatch",
    ):
        readiness._audit_ledger_readonly(config, database)


def test_existing_non_database_objects_are_blocked_not_treated_as_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    paths = _paths(root)
    paths.ledger_database.mkdir()
    paths.outbox_database.mkdir()
    validated: list[Path] = []
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_storage_root",
        lambda supplied: supplied.root,
    )

    def reject_non_file(_paths_value, supplied):
        validated.append(Path(supplied))
        raise PrivateRuntimePathError("private_runtime_file_missing")

    monkeypatch.setattr(
        readiness,
        "validate_existing_private_runtime_file",
        reject_non_file,
    )

    result = readiness.evaluate_private_daily_readiness(
        replace(_config(), corporate_action_attestations=()),
        paths,
        environ={"CODEX_DAILY_TARGET_KEY": TARGET},
        clock=lambda: NOW,
        config_acl_passed=True,
    )

    assert set(validated) == {paths.ledger_database, paths.outbox_database}
    assert result.outbox_state == "blocked"
    checks = {item.check_id: item for item in result.checks}
    assert checks["ledger_integrity"].status == "blocked"
    assert checks["outbox_integrity"].status == "blocked"


def test_nonempty_wal_is_blocked_instead_of_ignored(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    database.write_bytes(b"not-opened")
    Path(str(database) + "-wal").write_bytes(b"uncheckpointed")

    with pytest.raises(
        readiness.PrivateDailyReadinessError,
        match="runtime_database_wal_requires_checkpoint",
    ):
        with readiness._immutable_sqlite(database):
            raise AssertionError("connection must not open")


def test_cli_without_private_environment_emits_only_fixed_blocked_json() -> None:
    output = io.StringIO()

    exit_code = readiness.run_private_daily_readiness_main(
        environ={},
        stdout=output,
        clock=lambda: NOW,
    )

    document = json.loads(output.getvalue())
    assert exit_code == readiness.EXIT_BLOCKED
    assert document["overall"] == "blocked"
    assert document["ready_for_delivery"] is False
    assert output.getvalue().count("\n") == 1
    assert "traceback" not in output.getvalue().casefold()
    assert "private_config_or_path_blocked" in output.getvalue()


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_line"),
    [
        (
            KeyboardInterrupt,
            130,
            "PRIVATE_DAILY_READINESS:INTERRUPTED\n",
        ),
        (
            RuntimeError,
            70,
            "PRIVATE_DAILY_READINESS:INTERNAL_FAILURE\n",
        ),
    ],
)
def test_script_execution_boundary_never_renders_an_exception(
    failure,
    expected_code: int,
    expected_line: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail():
        raise failure("private detail must not render")

    monkeypatch.setattr(
        readiness_script,
        "_load_main",
        lambda: (fail, 0, ""),
    )

    assert readiness_script._run() == expected_code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == expected_line
    assert "private detail" not in captured.err


def test_missing_target_cannot_claim_an_empty_receiver_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    paths = _paths(root)
    monkeypatch.setattr(
        readiness,
        "validate_existing_private_storage_root",
        lambda supplied: supplied.root,
    )

    result = readiness.evaluate_private_daily_readiness(
        replace(_config(), corporate_action_attestations=()),
        paths,
        environ={},
        clock=lambda: NOW,
        config_acl_passed=True,
    )

    assert result.outbox_state == "blocked"
    assert result.operational_state == "blocked"
    checks = {item.check_id: item for item in result.checks}
    assert checks["unresolved_delivery"].reason_code == (
        "delivery_target_required_for_outbox_scope"
    )


def test_direct_readiness_script_is_redacted_from_any_working_directory(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment.pop("SERENITY_PRIVATE_CONFIG", None)
    environment.pop("SERENITY_PRIVATE_ROOT", None)
    environment.pop("CODEX_DAILY_TARGET_KEY", None)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_private_daily_readiness.py")],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    document = json.loads(result.stdout)
    assert result.returncode == readiness.EXIT_BLOCKED
    assert result.stderr == ""
    assert document["overall"] == "blocked"
    assert str(ROOT) not in result.stdout
    assert "Traceback" not in result.stdout


def test_existing_storage_validator_does_not_create_missing_root(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-runtime"

    with pytest.raises(PrivateRuntimePathError):
        validate_existing_private_storage_root(_paths(missing))

    assert not missing.exists()
