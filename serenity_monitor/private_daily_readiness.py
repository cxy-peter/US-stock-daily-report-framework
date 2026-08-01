"""Read-only, redacted activation audit for the private daily runtime.

The audit performs no network request, creates no directory or database and
does not initialize, settle, enqueue, claim or deliver anything.  Its JSON
contract contains only fixed check identifiers, fixed reason codes and boolean
readiness decisions.  Private paths, symbols, positions, amounts, receiver
targets and credential values never enter the result.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Mapping, TextIO
from zoneinfo import ZoneInfo

from .daily_outbox import (
    DailyReportOutbox,
    DeliveryAdapterCapabilities,
)
from .opening_owner_attestation import (
    OpeningLedgerBinding,
    audit_opening_owner_attestation,
)
from .manual_owner_event import ManualOwnerEventError, load_manual_event_queue
from .portfolio_ledger import (
    LedgerEventCheckpoint,
    LedgerNotInitializedError,
    OpeningPosition,
    PortfolioLedger,
)
from .private_daily_report import compute_target_key_sha256
from .private_runtime_config import (
    PrivateDailyRuntimeConfig,
    load_private_daily_runtime_config,
)
from .private_runtime_paths import (
    PrivateRuntimePaths,
    missing_provider_environment,
    read_validated_live_private_config,
    require_delivery_target,
    resolve_private_runtime_paths,
    validate_existing_private_runtime_file,
    validate_existing_private_storage_root,
)
from .trading_calendar import ExchangeSessionResolver


READINESS_CONTRACT_VERSION = "private_daily_activation_readiness/v1.0.0"
PRIVATE_CONFIG_ENV = "SERENITY_PRIVATE_CONFIG"
EXIT_READY = 0
EXIT_BLOCKED = 2
_ALPHA_VANTAGE_FREE_DAILY_BUDGET = 25
_ZERO = Decimal("0")
_CHECK_ORDER = (
    "accepted_close_live_probe",
    "automation_state",
    "config_acl",
    "corporate_action_coverage",
    "delivery_target",
    "ledger_integrity",
    "live_end_to_end_trial",
    "manual_event_ingestion",
    "opening_owner_attestation",
    "outbox_integrity",
    "provider_call_budget",
    "provider_credentials",
    "receiver_idempotency",
    "storage_acl",
    "unresolved_delivery",
)
_CHECK_STATUSES = frozenset(
    {"passed", "blocked", "not_run", "not_implemented", "unverified"}
)
_OPERATIONAL_STATES = frozenset(
    {
        "already_complete",
        "blocked",
        "needs_initialization",
        "pending_delivery",
        "ready_for_prepare",
        "reconciliation_required",
    }
)
_NEXT_SAFE_ACTIONS = frozenset(
    {"deliver", "initialize", "none", "operator_review", "prepare", "reconcile"}
)
_OUTBOX_STATES = frozenset(
    {
        "already_complete",
        "blocked",
        "conflict",
        "empty",
        "pending_delivery",
        "reconciliation_required",
    }
)
_LEDGER_STATES = frozenset(
    {"blocked", "missing", "not_initialized", "opening_only", "ready"}
)
_LEDGER_SCHEMA_SHA256 = {
    ("index", "idx_ledger_events_session"):
        "18446653abc76b62466f80016aacfcee570527753b1ac704b8f306c91233ed7b",
    ("index", "idx_ledger_events_type_session"):
        "51a19eb4e5d37f5d5f419f825fa398125b54ee5f8b7e0a59b6451e665906724d",
    ("table", "ledger_events"):
        "72d7e4d8b97e68539f35168439b0eb04f743e9193412a7ac804757d9ee953fe7",
    ("trigger", "ledger_events_no_delete"):
        "b644445a8780fd9b9536345348159e4706adb70e437c21c56ebf3a9777845486",
    ("trigger", "ledger_events_no_update"):
        "9cb5b4d148a40c5252cd8f11743304956fb5a1f9118e8946abcb3621b17d325f",
}
_OUTBOX_SCHEMA_SHA256 = {
    ("table", "daily_delivery_attempts"):
        "9b6c4529d0862b514ac09899d953e259bc9d6fdcfd80d38fb928d12cfb88d6ad",
    ("table", "daily_report_outbox"):
        "8f6bb58835614082a73ab5e37bf0906fb39e1834e29c105f33ae7e66322924be",
    ("trigger", "daily_delivery_attempts_immutable"):
        "6e56572e04998e7d5c6c9b2165a482aef84ff03d3022e82e983bf1221967cb6b",
    ("trigger", "daily_delivery_attempts_no_delete"):
        "a80dc956a45a67d0ec51646bc88e3c4ac1e6c37074b4bc02dd06054d38b563ae",
    ("trigger", "daily_delivery_attempts_status_transition"):
        "9af1a182533af408359f0e9170149f605dc0c3b6e7254eda28fff4ecd12c09b4",
    ("trigger", "daily_report_outbox_immutable"):
        "f832ea645fe526545b59bfd54983f52ea42cd2652dde3cfdcce4f39a60f7151f",
    ("trigger", "daily_report_outbox_no_delete"):
        "eff5144631ed6addb292421b9c9e0b3cd878bdd3ace18e9b0520583d5d77b8c4",
    ("trigger", "daily_report_outbox_status_transition"):
        "2672119780c6bd250e184ceede20007c4eeffae98e093adbf816ed559603b035",
}


class PrivateDailyReadinessError(RuntimeError):
    """Internal fixed-code audit failure; exception text is never rendered."""


@dataclass(frozen=True, repr=False)
class ReadinessCheck:
    check_id: str
    status: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.check_id not in _CHECK_ORDER:
            raise PrivateDailyReadinessError("readiness_check_id_invalid")
        if self.status not in _CHECK_STATUSES:
            raise PrivateDailyReadinessError("readiness_check_status_invalid")
        if not self.reason_code or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
            for character in self.reason_code
        ):
            raise PrivateDailyReadinessError("readiness_reason_code_invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "check_id": self.check_id,
            "reason_code": self.reason_code,
            "status": self.status,
        }


@dataclass(frozen=True, repr=False)
class PrivateDailyActivationReadiness:
    checked_at: dt.datetime
    checks: tuple[ReadinessCheck, ...]
    operational_state: str
    next_safe_action: str
    outbox_state: str
    ready_for_initialize: bool
    ready_for_prepare: bool
    ready_for_delivery: bool
    workflow_activation_allowed: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.checked_at, dt.datetime)
            or self.checked_at.tzinfo is None
            or self.checked_at.utcoffset() is None
        ):
            raise PrivateDailyReadinessError("readiness_checked_at_invalid")
        if tuple(item.check_id for item in self.checks) != _CHECK_ORDER:
            raise PrivateDailyReadinessError("readiness_checks_not_canonical")
        if self.operational_state not in _OPERATIONAL_STATES:
            raise PrivateDailyReadinessError("readiness_operational_state_invalid")
        if self.next_safe_action not in _NEXT_SAFE_ACTIONS:
            raise PrivateDailyReadinessError("readiness_next_safe_action_invalid")
        if self.outbox_state not in _OUTBOX_STATES:
            raise PrivateDailyReadinessError("readiness_outbox_state_invalid")
        for name in (
            "ready_for_initialize",
            "ready_for_prepare",
            "ready_for_delivery",
            "workflow_activation_allowed",
        ):
            if type(getattr(self, name)) is not bool:
                raise PrivateDailyReadinessError("readiness_boolean_invalid")
        if self.ready_for_delivery and self.outbox_state != "pending_delivery":
            raise PrivateDailyReadinessError("delivery_readiness_requires_pending")
        if (
            self.operational_state == "pending_delivery"
            and self.outbox_state != "pending_delivery"
        ):
            raise PrivateDailyReadinessError("pending_operational_state_mismatch")
        if (
            self.operational_state == "reconciliation_required"
            and self.outbox_state != "reconciliation_required"
        ):
            raise PrivateDailyReadinessError("reconciliation_operational_state_mismatch")
        if (
            self.operational_state == "already_complete"
            and self.outbox_state != "already_complete"
        ):
            raise PrivateDailyReadinessError("complete_operational_state_mismatch")
        expected_action = {
            "already_complete": "none",
            "needs_initialization": "initialize",
            "ready_for_prepare": "prepare",
            "reconciliation_required": "reconcile",
        }.get(self.operational_state)
        if expected_action is not None and self.next_safe_action != expected_action:
            raise PrivateDailyReadinessError("readiness_action_state_mismatch")
        if self.workflow_activation_allowed and self.operational_state not in {
            "already_complete",
            "ready_for_prepare",
        }:
            raise PrivateDailyReadinessError("workflow_activation_state_invalid")

    @property
    def overall(self) -> str:
        return "ready" if self.workflow_activation_allowed else "blocked"

    def to_dict(self) -> dict[str, object]:
        return {
            "checked_at": self.checked_at.astimezone(dt.timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "checks": [item.to_dict() for item in self.checks],
            "contract_version": READINESS_CONTRACT_VERSION,
            "next_safe_action": self.next_safe_action,
            "operational_state": self.operational_state,
            "overall": self.overall,
            "outbox_state": self.outbox_state,
            "ready_for_delivery": self.ready_for_delivery,
            "ready_for_initialize": self.ready_for_initialize,
            "ready_for_prepare": self.ready_for_prepare,
            "workflow_activation_allowed": self.workflow_activation_allowed,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


@dataclass(frozen=True, repr=False)
class _LedgerAudit:
    state: str
    latest_common_session: dt.date | None
    active_symbols: tuple[str, ...]
    opening_binding: OpeningLedgerBinding | None = None
    event_checkpoints: tuple[LedgerEventCheckpoint, ...] = ()
    chain_head: str | None = None
    valuation_watermark: dt.date | None = None

    def __post_init__(self) -> None:
        if self.state not in _LEDGER_STATES:
            raise PrivateDailyReadinessError("ledger_audit_state_invalid")
        if self.state in {"opening_only", "ready"} and self.opening_binding is None:
            raise PrivateDailyReadinessError("ledger_opening_binding_missing")
        if self.state not in {"opening_only", "ready"} and self.opening_binding is not None:
            raise PrivateDailyReadinessError("ledger_opening_binding_unexpected")
        if any(
            not isinstance(item, LedgerEventCheckpoint)
            for item in self.event_checkpoints
        ):
            raise PrivateDailyReadinessError("ledger_event_checkpoint_invalid")
        if self.state not in {"opening_only", "ready"} and self.event_checkpoints:
            raise PrivateDailyReadinessError("ledger_event_checkpoint_unexpected")
        if self.state in {"opening_only", "ready"}:
            if not self.event_checkpoints or self.chain_head != self.event_checkpoints[-1].event_hash:
                raise PrivateDailyReadinessError("ledger_chain_head_invalid")
        elif self.chain_head is not None:
            raise PrivateDailyReadinessError("ledger_chain_head_unexpected")
        if self.state not in {"opening_only", "ready"} and self.valuation_watermark is not None:
            raise PrivateDailyReadinessError("ledger_valuation_watermark_unexpected")


@dataclass(frozen=True, repr=False)
class _OutboxAudit:
    state: str
    pending_status: str | None = None
    required_idempotency_scope_sha256: str | None = None
    pending_ledger_last_event_hash: str | None = None

    def __post_init__(self) -> None:
        if self.state not in _OUTBOX_STATES - {"blocked"}:
            raise PrivateDailyReadinessError("outbox_audit_state_invalid")
        if self.pending_status not in {None, "prepared", "retryable"}:
            raise PrivateDailyReadinessError("outbox_pending_status_invalid")
        if self.state == "pending_delivery" and self.pending_status is None:
            raise PrivateDailyReadinessError("outbox_pending_status_required")
        if self.state != "pending_delivery" and self.pending_status is not None:
            raise PrivateDailyReadinessError("outbox_pending_status_unexpected")
        if self.state != "pending_delivery" and self.pending_ledger_last_event_hash is not None:
            raise PrivateDailyReadinessError("outbox_pending_ledger_hash_unexpected")


def _utc_now(clock) -> dt.datetime:
    value = clock()
    if (
        not isinstance(value, dt.datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise PrivateDailyReadinessError("readiness_clock_invalid")
    return value.astimezone(dt.timezone.utc)


def _parse_readonly_utc(value: object) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrivateDailyReadinessError("ledger_opening_time_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PrivateDailyReadinessError("ledger_opening_time_invalid")
    return parsed.astimezone(dt.timezone.utc)


def _check(check_id: str, status: str, reason_code: str) -> ReadinessCheck:
    return ReadinessCheck(check_id, status, reason_code)


def _file_fingerprint(path: Path) -> tuple[int, int, int, int, str]:
    metadata = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1_048_576):
            digest.update(chunk)
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        digest.hexdigest(),
    )


def _sqlite_snapshot(path: Path) -> tuple[object, ...]:
    sidecars: list[tuple[str, object]] = []
    for suffix in ("-journal", "-shm", "-wal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            sidecars.append((suffix, _file_fingerprint(candidate)))
    return (_file_fingerprint(path), tuple(sidecars))


def _reject_uncheckpointed_sqlite(path: Path) -> None:
    for suffix in ("-journal", "-wal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists() and candidate.stat().st_size > 0:
            raise PrivateDailyReadinessError(
                "runtime_database_wal_requires_checkpoint"
                if suffix == "-wal"
                else "runtime_database_journal_requires_recovery"
            )


@contextmanager
def _immutable_sqlite(path: Path) -> Iterator[sqlite3.Connection]:
    _reject_uncheckpointed_sqlite(path)
    before = _sqlite_snapshot(path)
    uri = path.absolute().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    finally:
        connection.close()
        _reject_uncheckpointed_sqlite(path)
        if _sqlite_snapshot(path) != before:
            raise PrivateDailyReadinessError(
                "runtime_database_changed_during_readonly_audit"
            )


def _verify_sqlite_health(connection: sqlite3.Connection) -> None:
    quick_check = tuple(
        str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()
    )
    if quick_check != ("ok",):
        raise PrivateDailyReadinessError("runtime_database_quick_check_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise PrivateDailyReadinessError("runtime_database_foreign_key_check_failed")


def _schema_sql_fingerprints(
    connection: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    fingerprints: dict[tuple[str, str], str] = {}
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for row in rows:
        canonical_sql = str(row[2]).strip()
        fingerprints[(str(row[0]), str(row[1]))] = hashlib.sha256(
            canonical_sql.encode("utf-8")
        ).hexdigest()
    return fingerprints


def _unique_index_columns(
    connection: sqlite3.Connection,
    table: str,
) -> frozenset[tuple[str, ...]]:
    groups: set[tuple[str, ...]] = set()
    for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
        if int(row[2]) != 1:
            continue
        name = str(row[1]).replace('"', '""')
        columns = tuple(
            str(item[2])
            for item in connection.execute(
                f'PRAGMA index_info("{name}")'
            ).fetchall()
        )
        groups.add(columns)
    return frozenset(groups)


def _verify_ledger_schema(connection: sqlite3.Connection) -> None:
    _verify_sqlite_health(connection)
    if _schema_sql_fingerprints(connection) != _LEDGER_SCHEMA_SHA256:
        raise PrivateDailyReadinessError("ledger_schema_definition_mismatch")
    unique_groups = _unique_index_columns(connection, "ledger_events")
    if not {
        ("event_hash",),
        ("event_id",),
        ("idempotency_key",),
    } <= unique_groups:
        raise PrivateDailyReadinessError("ledger_schema_unique_constraint_missing")


def _verify_outbox_schema(connection: sqlite3.Connection) -> None:
    _verify_sqlite_health(connection)
    if _schema_sql_fingerprints(connection) != _OUTBOX_SCHEMA_SHA256:
        raise PrivateDailyReadinessError("outbox_schema_definition_mismatch")
    outbox_unique = _unique_index_columns(connection, "daily_report_outbox")
    if not {
        ("delivery_id",),
        ("report_id",),
        ("channel", "target_key_sha256", "delivery_date"),
    } <= outbox_unique:
        raise PrivateDailyReadinessError("outbox_schema_unique_constraint_missing")
    attempt_unique = _unique_index_columns(connection, "daily_delivery_attempts")
    if not {
        ("attempt_id",),
        ("lease_token_sha256",),
        ("outbox_id", "attempt_number"),
    } <= attempt_unique:
        raise PrivateDailyReadinessError("outbox_attempt_unique_constraint_missing")
    outbox_foreign_keys = {
        (str(row[2]), str(row[3]), str(row[4]))
        for row in connection.execute(
            'PRAGMA foreign_key_list("daily_report_outbox")'
        ).fetchall()
    }
    attempt_foreign_keys = {
        (str(row[2]), str(row[3]), str(row[4]))
        for row in connection.execute(
            'PRAGMA foreign_key_list("daily_delivery_attempts")'
        ).fetchall()
    }
    if (
        ("daily_delivery_attempts", "current_attempt_id", "attempt_id")
        not in outbox_foreign_keys
        or ("daily_report_outbox", "outbox_id", "outbox_id")
        not in attempt_foreign_keys
    ):
        raise PrivateDailyReadinessError("outbox_schema_foreign_key_missing")


def _opening_from_row(
    row: sqlite3.Row,
) -> tuple[dt.date, str, Decimal, tuple[OpeningPosition, ...]]:
    try:
        payload = json.loads(row["payload_json"])
        raw_positions = payload["positions"]
        if not isinstance(raw_positions, list):
            raise ValueError
        positions = tuple(
            sorted(
                (
                    OpeningPosition(
                        symbol=item["symbol"],
                        quantity=item["quantity"],
                        average_economic_cost=item["average_economic_cost"],
                    )
                    for item in raw_positions
                    if isinstance(item, Mapping)
                ),
                key=lambda item: item.symbol,
            )
        )
        if len(positions) != len(raw_positions):
            raise ValueError
        return (
            dt.date.fromisoformat(str(row["session_date"])),
            str(payload["currency"]).strip().upper(),
            Decimal(str(payload["cash"])),
            positions,
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise PrivateDailyReadinessError("ledger_opening_snapshot_invalid") from exc


def _audit_ledger_readonly(
    config: PrivateDailyRuntimeConfig,
    database_path: Path,
) -> _LedgerAudit:
    ledger = object.__new__(PortfolioLedger)
    ledger.database_path = database_path
    ledger.policy = config.ledger_policy
    ledger.calendar_resolver = ExchangeSessionResolver()
    try:
        with _immutable_sqlite(database_path) as connection:
            _verify_ledger_schema(connection)
            ledger._verify_hash_chain_connection(connection)
            try:
                opening_row = ledger._require_initialized(connection)
            except LedgerNotInitializedError:
                return _LedgerAudit("not_initialized", None, (), None)
            ledger._validated_valuation_chains_connection(connection)
            session, currency, cash, positions = _opening_from_row(opening_row)
            expected_positions = tuple(
                sorted(config.opening.positions, key=lambda item: item.symbol)
            )
            if (
                session != config.opening.session
                or currency != config.ledger_policy.currency
                or cash != config.opening.cash
                or positions != expected_positions
            ):
                raise PrivateDailyReadinessError(
                    "ledger_opening_configuration_mismatch"
                )
            latest = ledger._latest_common_valuation_session_connection(connection)
            event_checkpoints = tuple(
                LedgerEventCheckpoint(
                    event_id=str(row["event_id"]),
                    event_hash=str(row["event_hash"]),
                    idempotency_key=str(row["idempotency_key"]),
                    session=dt.date.fromisoformat(str(row["session_date"])),
                    event_type=str(row["event_type"]),
                    source_class=str(row["source_class"]),
                )
                for row in connection.execute(
                    "SELECT event_id, event_hash, idempotency_key, session_date, "
                    "event_type, source_class FROM ledger_events ORDER BY sequence_no"
                ).fetchall()
            )
            watermark_row = connection.execute(
                "SELECT MAX(session_date) AS session_date FROM ledger_events "
                "WHERE event_type = 'valuation'"
            ).fetchone()
            valuation_watermark = (
                None
                if watermark_row is None or watermark_row["session_date"] is None
                else dt.date.fromisoformat(str(watermark_row["session_date"]))
            )
            symbols = set(config.dca_plan.base_amounts)
            if latest is not None:
                for book_kind in ("confirmed", "modeled"):
                    projection = ledger._project_connection(
                        connection,
                        book_kind,
                        latest,
                    )
                    symbols.update(
                        item.symbol
                        for item in projection.positions
                        if item.quantity != _ZERO
                    )
            return _LedgerAudit(
                "ready" if latest is not None else "opening_only",
                latest,
                tuple(sorted(symbols)),
                OpeningLedgerBinding(
                    opening_event_id=str(opening_row["event_id"]),
                    opening_event_hash=str(opening_row["event_hash"]),
                    idempotency_key=str(opening_row["idempotency_key"]),
                    created_at=_parse_readonly_utc(opening_row["created_at"]),
                ),
                event_checkpoints,
                event_checkpoints[-1].event_hash,
                valuation_watermark,
            )
    except PrivateDailyReadinessError:
        raise
    except (OSError, sqlite3.Error, ValueError, ArithmeticError) as exc:
        raise PrivateDailyReadinessError("ledger_readonly_integrity_failed") from exc


def _audit_outbox_readonly(
    database_path: Path,
    *,
    target_key: str,
    channel: str,
    delivery_date: dt.date,
) -> _OutboxAudit:
    """Verify every row, then derive only the current receiver's safe state."""

    target_key_sha256 = compute_target_key_sha256(target_key)
    outbox = object.__new__(DailyReportOutbox)
    outbox.database_path = database_path
    outbox.busy_timeout_ms = 5_000
    outbox.default_lease = dt.timedelta(minutes=5)
    try:
        with _immutable_sqlite(database_path) as connection:
            _verify_outbox_schema(connection)
            rows = outbox._verified_rows(connection)
            foreign_unresolved = [
                record
                for record, _report in rows
                if record.status
                in {"prepared", "sending", "delivery_unknown", "retryable"}
                and not (
                    hmac.compare_digest(
                        record.target_key_sha256,
                        target_key_sha256,
                    )
                    and record.channel == channel
                )
            ]
            if foreign_unresolved:
                return _OutboxAudit("conflict")
            matches = [
                record
                for record, _report in rows
                if hmac.compare_digest(
                    record.target_key_sha256,
                    target_key_sha256,
                )
                and record.channel == channel
            ]
            unresolved = [
                record
                for record in matches
                if record.status
                in {"prepared", "sending", "delivery_unknown", "retryable"}
            ]
            delivered = [record for record in matches if record.status == "delivered"]

            if len({record.delivery_date for record in matches}) != len(matches):
                return _OutboxAudit("conflict")
            if any(record.delivery_date > delivery_date for record in matches):
                return _OutboxAudit("conflict")
            if len(unresolved) > 1:
                return _OutboxAudit("conflict")
            if unresolved:
                pending = unresolved[0]
                if any(
                    record.delivery_date >= pending.delivery_date
                    for record in delivered
                ):
                    return _OutboxAudit("conflict")
                if pending.status in {"sending", "delivery_unknown"}:
                    return _OutboxAudit("reconciliation_required")
                required_scope: str | None = None
                if pending.status == "retryable":
                    if pending.current_attempt_id is None:  # pragma: no cover - verified guard
                        raise PrivateDailyReadinessError(
                            "retryable_delivery_attempt_missing"
                        )
                    attempt_row = outbox._attempt_row(
                        connection,
                        pending.current_attempt_id,
                    )
                    attempt = outbox._attempt_from_row(attempt_row)
                    if attempt.idempotent_retry_authorized_at is not None:
                        required_scope = attempt.idempotency_scope_sha256
                return _OutboxAudit(
                    "pending_delivery",
                    pending_status=pending.status,
                    required_idempotency_scope_sha256=required_scope,
                    pending_ledger_last_event_hash=pending.ledger_last_event_hash,
                )
            if any(record.delivery_date == delivery_date for record in delivered):
                return _OutboxAudit("already_complete")
            return _OutboxAudit("empty")
    except PrivateDailyReadinessError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise PrivateDailyReadinessError("outbox_readonly_integrity_failed") from exc


def _all_passed(checks: Mapping[str, ReadinessCheck], names: set[str]) -> bool:
    return all(checks[name].status == "passed" for name in names)


def evaluate_private_daily_readiness(
    config: PrivateDailyRuntimeConfig,
    paths: PrivateRuntimePaths,
    *,
    environ: Mapping[str, str],
    clock,
    config_acl_passed: bool,
    config_bytes_sha256: str | None = None,
    receiver_capabilities: DeliveryAdapterCapabilities | None = None,
) -> PrivateDailyActivationReadiness:
    """Evaluate current activation gates without network or filesystem writes."""

    now = _utc_now(clock)
    checks: dict[str, ReadinessCheck] = {
        "accepted_close_live_probe": _check(
            "accepted_close_live_probe",
            "not_run",
            "accepted_close_live_probe_not_run",
        ),
        "automation_state": _check(
            "automation_state",
            "unverified",
            "automation_state_requires_product_check",
        ),
        "config_acl": _check(
            "config_acl",
            "passed" if config_acl_passed else "blocked",
            "private_config_acl_passed"
            if config_acl_passed
            else "private_config_acl_blocked",
        ),
        "corporate_action_coverage": _check(
            "corporate_action_coverage",
            "not_run",
            "corporate_action_coverage_not_run",
        ),
        "delivery_target": _check(
            "delivery_target",
            "not_run",
            "delivery_target_not_checked",
        ),
        "ledger_integrity": _check(
            "ledger_integrity",
            "not_run",
            "ledger_not_checked",
        ),
        "live_end_to_end_trial": _check(
            "live_end_to_end_trial",
            "not_run",
            "live_end_to_end_trial_not_run",
        ),
        "manual_event_ingestion": _check(
            "manual_event_ingestion",
            "not_run",
            "manual_event_ingestion_not_checked",
        ),
        "opening_owner_attestation": _check(
            "opening_owner_attestation",
            "not_implemented",
            "opening_owner_attestation_not_implemented",
        ),
        "outbox_integrity": _check(
            "outbox_integrity",
            "not_run",
            "outbox_not_checked",
        ),
        "provider_call_budget": _check(
            "provider_call_budget",
            "not_run",
            "provider_call_budget_not_checked",
        ),
        "provider_credentials": _check(
            "provider_credentials",
            "not_run",
            "provider_credentials_not_checked",
        ),
        "receiver_idempotency": _check(
            "receiver_idempotency",
            "unverified",
            "receiver_idempotency_unverified",
        ),
        "storage_acl": _check(
            "storage_acl",
            "not_run",
            "private_storage_not_checked",
        ),
        "unresolved_delivery": _check(
            "unresolved_delivery",
            "not_run",
            "unresolved_delivery_not_checked",
        ),
    }

    target_key: str | None = None
    try:
        target_key = require_delivery_target(config, environ)
        checks["delivery_target"] = _check(
            "delivery_target", "passed", "delivery_target_present"
        )
    except Exception:
        checks["delivery_target"] = _check(
            "delivery_target", "blocked", "delivery_target_missing_or_invalid"
        )

    missing_credentials = missing_provider_environment(environ)
    checks["provider_credentials"] = _check(
        "provider_credentials",
        "passed" if not missing_credentials else "blocked",
        "provider_credentials_present"
        if not missing_credentials
        else "provider_credentials_missing",
    )

    storage_ok = False
    try:
        validate_existing_private_storage_root(paths)
        storage_ok = True
        checks["storage_acl"] = _check(
            "storage_acl", "passed", "private_storage_acl_passed"
        )
    except Exception:
        checks["storage_acl"] = _check(
            "storage_acl", "blocked", "private_storage_missing_or_unsafe"
        )

    ledger_audit = _LedgerAudit("blocked", None, ())
    if storage_ok and os.path.lexists(str(paths.ledger_database)):
        try:
            ledger_path = validate_existing_private_runtime_file(
                paths,
                paths.ledger_database,
            )
            ledger_audit = _audit_ledger_readonly(config, ledger_path)
            if ledger_audit.state == "ready":
                checks["ledger_integrity"] = _check(
                    "ledger_integrity", "passed", "ledger_initialized_and_verified"
                )
            elif ledger_audit.state == "opening_only":
                checks["ledger_integrity"] = _check(
                    "ledger_integrity",
                    "passed",
                    "ledger_opening_verified_valuations_pending",
                )
            else:
                checks["ledger_integrity"] = _check(
                    "ledger_integrity", "not_run", "ledger_not_initialized"
                )
        except Exception:
            ledger_audit = _LedgerAudit("blocked", None, ())
            checks["ledger_integrity"] = _check(
                "ledger_integrity", "blocked", "ledger_readonly_integrity_blocked"
            )
    elif storage_ok:
        ledger_audit = _LedgerAudit("missing", None, ())
        checks["ledger_integrity"] = _check(
            "ledger_integrity", "not_run", "ledger_not_created"
        )
    else:
        checks["ledger_integrity"] = _check(
            "ledger_integrity", "blocked", "ledger_storage_unavailable"
        )

    if storage_ok and ledger_audit.state in {"opening_only", "ready"}:
        checkpoints = {
            item.event_id: item for item in ledger_audit.event_checkpoints
        }
        try:
            pending_manual = load_manual_event_queue(
                config,
                paths,
                checkpoints.get,
                ledger_audit.valuation_watermark,
            )
            checks["manual_event_ingestion"] = _check(
                "manual_event_ingestion",
                "passed",
                (
                    "owner_event_pending_consumption"
                    if pending_manual
                    else "manual_event_ingestion_available"
                ),
            )
        except ManualOwnerEventError as exc:
            reason = {
                "manual_event_request_requires_confirmation": (
                    "owner_event_confirmation_required"
                ),
                "manual_event_after_valuation_finality": (
                    "owner_event_after_valuation_finality"
                ),
            }.get(exc.code, "owner_event_integrity_failed")
            checks["manual_event_ingestion"] = _check(
                "manual_event_ingestion",
                "blocked",
                reason,
            )
        except Exception:
            checks["manual_event_ingestion"] = _check(
                "manual_event_ingestion",
                "blocked",
                "owner_event_integrity_failed",
            )
    elif ledger_audit.state in {"missing", "not_initialized"}:
        checks["manual_event_ingestion"] = _check(
            "manual_event_ingestion",
            "not_run",
            "manual_event_ingestion_requires_initialized_ledger",
        )
    else:
        checks["manual_event_ingestion"] = _check(
            "manual_event_ingestion",
            "blocked",
            "manual_event_ingestion_storage_or_ledger_unavailable",
        )

    opening_attestation_state = "unsafe"
    if not storage_ok or ledger_audit.state == "blocked":
        checks["opening_owner_attestation"] = _check(
            "opening_owner_attestation",
            "blocked",
            "opening_owner_attestation_storage_or_ledger_unavailable",
        )
    else:
        try:
            opening_audit = audit_opening_owner_attestation(
                config,
                paths,
                config_bytes_sha256=config_bytes_sha256 or "",
                now=now,
                ledger_binding=ledger_audit.opening_binding,
            )
            opening_attestation_state = opening_audit.state
            passed = opening_audit.state in {
                "consumed_verified",
                "pending_verified",
                "recovery_available",
                "resume_available",
            }
            checks["opening_owner_attestation"] = _check(
                "opening_owner_attestation",
                "passed" if passed else "blocked",
                opening_audit.reason_code,
            )
        except Exception:
            checks["opening_owner_attestation"] = _check(
                "opening_owner_attestation",
                "blocked",
                "opening_owner_attestation_audit_failed",
            )

    outbox_state = "blocked"
    outbox_audit: _OutboxAudit | None = None
    if storage_ok and os.path.lexists(str(paths.outbox_database)):
        try:
            outbox_path = validate_existing_private_runtime_file(
                paths,
                paths.outbox_database,
            )
            delivery_date = now.astimezone(ZoneInfo(config.report_timezone)).date()
            outbox_audit = _audit_outbox_readonly(
                outbox_path,
                target_key=(target_key or "readiness-invalid-target"),
                channel=config.delivery_channel,
                delivery_date=delivery_date,
            )
            outbox_state = outbox_audit.state
            if outbox_state == "conflict":
                checks["outbox_integrity"] = _check(
                    "outbox_integrity", "blocked", "outbox_sequence_conflict"
                )
                checks["unresolved_delivery"] = _check(
                    "unresolved_delivery", "blocked", "outbox_sequence_conflict"
                )
            else:
                checks["outbox_integrity"] = _check(
                    "outbox_integrity", "passed", "outbox_integrity_verified"
                )
                unresolved_reason = {
                    "already_complete": ("passed", "delivery_already_complete"),
                    "empty": ("passed", "no_unresolved_delivery"),
                    "pending_delivery": (
                        "blocked",
                        "retryable_delivery_pending"
                        if outbox_audit.pending_status == "retryable"
                        else "prepared_delivery_pending",
                    ),
                    "reconciliation_required": (
                        "blocked",
                        "delivery_reconciliation_required",
                    ),
                }[outbox_state]
                checks["unresolved_delivery"] = _check(
                    "unresolved_delivery",
                    unresolved_reason[0],
                    unresolved_reason[1],
                )
        except Exception:
            outbox_state = "blocked"
            checks["outbox_integrity"] = _check(
                "outbox_integrity", "blocked", "outbox_readonly_integrity_blocked"
            )
            checks["unresolved_delivery"] = _check(
                "unresolved_delivery", "blocked", "outbox_integrity_required"
            )
    elif storage_ok:
        outbox_audit = _OutboxAudit("empty")
        outbox_state = "empty"
        checks["outbox_integrity"] = _check(
            "outbox_integrity", "passed", "outbox_not_created"
        )
        checks["unresolved_delivery"] = _check(
            "unresolved_delivery", "passed", "no_outbox_no_unresolved_delivery"
        )
    else:
        checks["outbox_integrity"] = _check(
            "outbox_integrity", "blocked", "outbox_storage_unavailable"
        )
        checks["unresolved_delivery"] = _check(
            "unresolved_delivery", "blocked", "outbox_integrity_required"
        )

    if target_key is None:
        outbox_audit = None
        outbox_state = "blocked"
        checks["unresolved_delivery"] = _check(
            "unresolved_delivery",
            "blocked",
            "delivery_target_required_for_outbox_scope",
        )

    pending_checkpoint_stale = (
        outbox_audit is not None
        and outbox_audit.state == "pending_delivery"
        and (
            outbox_audit.pending_ledger_last_event_hash is None
            or ledger_audit.chain_head is None
            or not hmac.compare_digest(
                outbox_audit.pending_ledger_last_event_hash,
                ledger_audit.chain_head,
            )
        )
    )
    if pending_checkpoint_stale:
        checks["receiver_idempotency"] = _check(
            "receiver_idempotency",
            "blocked",
            "prepared_report_ledger_head_stale",
        )
    elif receiver_capabilities is None:
        checks["receiver_idempotency"] = _check(
            "receiver_idempotency",
            "unverified",
            "receiver_idempotency_unverified",
        )
    elif (
        not isinstance(receiver_capabilities, DeliveryAdapterCapabilities)
        or not receiver_capabilities.supports_exactly_once
    ):
        checks["receiver_idempotency"] = _check(
            "receiver_idempotency",
            "blocked",
            "receiver_exactly_once_capability_missing",
        )
    elif (
        outbox_audit is not None
        and outbox_audit.required_idempotency_scope_sha256 is not None
        and (
            not receiver_capabilities.supports_idempotency_key
            or receiver_capabilities.idempotency_scope_sha256 is None
            or not hmac.compare_digest(
                outbox_audit.required_idempotency_scope_sha256,
                receiver_capabilities.idempotency_scope_sha256,
            )
        )
    ):
        checks["receiver_idempotency"] = _check(
            "receiver_idempotency",
            "blocked",
            "receiver_retry_scope_mismatch",
        )
    else:
        checks["receiver_idempotency"] = _check(
            "receiver_idempotency",
            "passed",
            "receiver_exactly_once_capability_verified",
        )

    calendar = ExchangeSessionResolver()
    try:
        if ledger_audit.state == "ready" and ledger_audit.latest_common_session is not None:
            sessions = calendar.unsettled_sessions(
                ledger_audit.latest_common_session,
                now,
                config.primary_mic,
            )
            symbols = ledger_audit.active_symbols
        else:
            sessions = (config.opening.session,)
            symbols = tuple(
                sorted(
                    {
                        item.symbol
                        for item in config.opening.positions
                        if item.quantity != _ZERO
                    }
                    | set(config.dca_plan.base_amounts)
                )
            )
        if len(sessions) > config.max_backfill_sessions:
            checks["corporate_action_coverage"] = _check(
                "corporate_action_coverage",
                "blocked",
                "corporate_action_backfill_limit_exceeded",
            )
        elif all(
            config.corporate_action_statuses(
                session,
                as_of=now,
                symbols=symbols,
            )
            is not None
            for session in sessions
        ):
            checks["corporate_action_coverage"] = _check(
                "corporate_action_coverage",
                "passed",
                "corporate_action_coverage_complete",
            )
        else:
            checks["corporate_action_coverage"] = _check(
                "corporate_action_coverage",
                "blocked",
                "corporate_action_attestation_missing",
            )
        estimated_calls = len(symbols) * len(sessions)
        checks["provider_call_budget"] = _check(
            "provider_call_budget",
            "passed"
            if estimated_calls <= _ALPHA_VANTAGE_FREE_DAILY_BUDGET
            else "blocked",
            "provider_call_budget_within_limit"
            if estimated_calls <= _ALPHA_VANTAGE_FREE_DAILY_BUDGET
            else "provider_call_budget_exceeded",
        )
    except Exception:
        checks["corporate_action_coverage"] = _check(
            "corporate_action_coverage",
            "blocked",
            "corporate_action_coverage_unavailable",
        )
        checks["provider_call_budget"] = _check(
            "provider_call_budget", "blocked", "provider_call_budget_unavailable"
        )

    initialize_requirements = {
        "config_acl",
        "corporate_action_coverage",
        "delivery_target",
        "outbox_integrity",
        "provider_call_budget",
        "provider_credentials",
        "storage_acl",
        "unresolved_delivery",
    }
    prepare_requirements = {
        "config_acl",
        "corporate_action_coverage",
        "delivery_target",
        "ledger_integrity",
        "manual_event_ingestion",
        "opening_owner_attestation",
        "provider_call_budget",
        "provider_credentials",
        "storage_acl",
        "unresolved_delivery",
    }
    delivery_requirements = {
        "config_acl",
        "delivery_target",
        "outbox_integrity",
        "receiver_idempotency",
        "storage_acl",
    }
    activation_requirements = {
        "accepted_close_live_probe",
        "automation_state",
        "config_acl",
        "corporate_action_coverage",
        "delivery_target",
        "ledger_integrity",
        "live_end_to_end_trial",
        "manual_event_ingestion",
        "opening_owner_attestation",
        "outbox_integrity",
        "provider_call_budget",
        "provider_credentials",
        "receiver_idempotency",
        "storage_acl",
        "unresolved_delivery",
    }
    ready_for_initialize = (
        (
            (
                opening_attestation_state == "pending_verified"
                and ledger_audit.state in {"missing", "not_initialized"}
            )
            or (
                opening_attestation_state == "recovery_available"
                and ledger_audit.state in {"opening_only", "ready"}
            )
            or (
                opening_attestation_state == "resume_available"
                and ledger_audit.state in {"missing", "not_initialized"}
            )
            or (
                opening_attestation_state == "consumed_verified"
                and ledger_audit.state == "opening_only"
            )
        )
        and outbox_state == "empty"
        and _all_passed(checks, initialize_requirements)
    )
    ready_for_prepare = (
        ledger_audit.state == "ready"
        and opening_attestation_state == "consumed_verified"
        and outbox_state == "empty"
        and _all_passed(checks, prepare_requirements)
    )
    ready_for_delivery = (
        outbox_state == "pending_delivery"
        and _all_passed(checks, delivery_requirements)
    )
    workflow_activation_allowed = (
        ledger_audit.state == "ready"
        and opening_attestation_state == "consumed_verified"
        and outbox_state in {"already_complete", "empty"}
        and _all_passed(checks, activation_requirements)
    )

    trusted_outbox_boundary = _all_passed(
        checks,
        {"config_acl", "delivery_target", "outbox_integrity", "storage_acl"},
    )
    if not trusted_outbox_boundary or outbox_state in {"blocked", "conflict"}:
        operational_state = "blocked"
        next_safe_action = "operator_review"
    elif outbox_state == "reconciliation_required":
        operational_state = "reconciliation_required"
        next_safe_action = "reconcile"
    elif outbox_state == "pending_delivery":
        operational_state = "pending_delivery"
        next_safe_action = "deliver" if ready_for_delivery else "operator_review"
    elif outbox_state == "already_complete":
        operational_state = "already_complete"
        next_safe_action = "none"
    elif ready_for_initialize:
        operational_state = "needs_initialization"
        next_safe_action = "initialize"
    elif ready_for_prepare:
        operational_state = "ready_for_prepare"
        next_safe_action = "prepare"
    else:
        operational_state = "blocked"
        next_safe_action = "operator_review"

    ordered = tuple(checks[name] for name in _CHECK_ORDER)
    return PrivateDailyActivationReadiness(
        checked_at=now,
        checks=ordered,
        operational_state=operational_state,
        next_safe_action=next_safe_action,
        outbox_state=outbox_state,
        ready_for_initialize=ready_for_initialize,
        ready_for_prepare=ready_for_prepare,
        ready_for_delivery=ready_for_delivery,
        workflow_activation_allowed=workflow_activation_allowed,
    )


def _blocked_contract(now: dt.datetime, reason_code: str) -> PrivateDailyActivationReadiness:
    checks = {
        name: _check(name, "not_run", f"{name}_not_run")
        for name in _CHECK_ORDER
    }
    checks["config_acl"] = _check("config_acl", "blocked", reason_code)
    checks["automation_state"] = _check(
        "automation_state",
        "unverified",
        "automation_state_requires_product_check",
    )
    checks["manual_event_ingestion"] = _check(
        "manual_event_ingestion",
        "not_run",
        "manual_event_ingestion_not_run",
    )
    checks["live_end_to_end_trial"] = _check(
        "live_end_to_end_trial",
        "not_run",
        "live_end_to_end_trial_not_run",
    )
    checks["opening_owner_attestation"] = _check(
        "opening_owner_attestation",
        "not_run",
        "opening_owner_attestation_not_run",
    )
    checks["receiver_idempotency"] = _check(
        "receiver_idempotency",
        "unverified",
        "receiver_idempotency_unverified",
    )
    return PrivateDailyActivationReadiness(
        checked_at=now,
        checks=tuple(checks[name] for name in _CHECK_ORDER),
        operational_state="blocked",
        next_safe_action="operator_review",
        outbox_state="blocked",
        ready_for_initialize=False,
        ready_for_prepare=False,
        ready_for_delivery=False,
        workflow_activation_allowed=False,
    )


def run_private_daily_readiness_main(
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    clock=lambda: dt.datetime.now(dt.timezone.utc),
) -> int:
    """Print exactly one redacted JSON readiness object and return 0 or 2."""

    environment = os.environ if environ is None else environ
    output = sys.stdout if stdout is None else stdout
    try:
        now = _utc_now(clock)
    except Exception:
        now = dt.datetime.now(dt.timezone.utc)
    try:
        raw_config_path = str(environment.get(PRIVATE_CONFIG_ENV, "")).strip()
        if not raw_config_path:
            raise PrivateDailyReadinessError("private_config_environment_missing")
        config_path, payload = read_validated_live_private_config(raw_config_path)
        config = load_private_daily_runtime_config(
            config_path,
            allow_synthetic=False,
            _validated_bytes=payload,
        )
        config_bytes_sha256 = hashlib.sha256(payload).hexdigest()
        paths = resolve_private_runtime_paths(config, environment)
        result = evaluate_private_daily_readiness(
            config,
            paths,
            environ=environment,
            clock=lambda: now,
            config_acl_passed=True,
            config_bytes_sha256=config_bytes_sha256,
        )
    except Exception:
        result = _blocked_contract(now, "private_config_or_path_blocked")
    output.write(result.to_json() + "\n")
    return EXIT_READY if result.workflow_activation_allowed else EXIT_BLOCKED


__all__ = [
    "EXIT_BLOCKED",
    "EXIT_READY",
    "PRIVATE_CONFIG_ENV",
    "PrivateDailyActivationReadiness",
    "PrivateDailyReadinessError",
    "READINESS_CONTRACT_VERSION",
    "ReadinessCheck",
    "evaluate_private_daily_readiness",
    "run_private_daily_readiness_main",
]
