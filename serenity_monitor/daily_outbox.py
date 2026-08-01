"""Private SQLite outbox for one auditable daily-report delivery.

The outbox deliberately stores only a SHA-256 digest of the delivery target.
The caller remains responsible for keeping the actual Codex thread, chat, or
other receiver key in private runtime configuration.  A report is claimed at
most once until an ambiguous delivery has been explicitly reconciled with the
receiver; an expired lease is *not* permission to resend.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .private_daily_markdown import render_private_daily_markdown
from .private_daily_report import (
    canonical_json,
    compute_delivery_id,
    compute_report_id,
    compute_target_key_sha256,
    validate_private_daily_report,
)


_OUTBOX_STATUSES = frozenset(
    {"prepared", "sending", "delivery_unknown", "retryable", "delivered"}
)
_ATTEMPT_STATUSES = frozenset(
    {"sending", "delivery_unknown", "receiver_not_found", "delivered"}
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ERROR_CODES = frozenset(
    {
        "adapter_error",
        "delivery_status_unknown",
        "lease_expired_status_unknown",
        "network_timeout",
        "other_delivery_error",
        "provider_rejected",
        "response_ambiguous",
        "transport_error",
    }
)


class DailyOutboxError(Exception):
    """Base class for private daily-report outbox failures."""


class OutboxValidationError(DailyOutboxError, ValueError):
    """Raised when report, target, time, or capability input is invalid."""


class OutboxIdempotencyConflict(DailyOutboxError):
    """Raised when a delivery slot is reused with different content."""


class OutboxStateError(DailyOutboxError):
    """Raised when a delivery-state transition is not permitted."""


class OutboxLeaseError(DailyOutboxError):
    """Raised when a lease is active, unknown, invalid, or unavailable."""


class OutboxCapabilityError(DailyOutboxError):
    """Raised when an adapter cannot support exactly-once delivery."""


class OutboxIntegrityError(DailyOutboxError):
    """Raised when persisted report or attempt state violates the contract."""


@dataclass(frozen=True)
class DeliveryAdapterCapabilities:
    """Receiver features needed to recover safely from an ambiguous send."""

    supports_idempotency_key: bool
    supports_delivery_lookup: bool
    idempotency_scope: str | None = None
    lookup_scope: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.supports_idempotency_key, bool):
            raise OutboxValidationError("supports_idempotency_key must be boolean")
        if not isinstance(self.supports_delivery_lookup, bool):
            raise OutboxValidationError("supports_delivery_lookup must be boolean")
        if self.supports_idempotency_key:
            scope = _nonempty_text(
                self.idempotency_scope, "idempotency_scope", 256
            )
            object.__setattr__(self, "idempotency_scope", scope)
        elif self.idempotency_scope is not None:
            raise OutboxValidationError(
                "idempotency_scope requires idempotency-key support"
            )
        if self.supports_delivery_lookup:
            scope = _nonempty_text(self.lookup_scope, "lookup_scope", 256)
            object.__setattr__(self, "lookup_scope", scope)
        elif self.lookup_scope is not None:
            raise OutboxValidationError(
                "lookup_scope requires delivery-lookup support"
            )

    @property
    def supports_exactly_once(self) -> bool:
        """Whether the adapter exposes at least one safe deduplication path."""

        return self.supports_idempotency_key or self.supports_delivery_lookup

    @property
    def idempotency_scope_sha256(self) -> str | None:
        """Opaque identity of the receiver's deduplication namespace."""

        if self.idempotency_scope is None:
            return None
        return _sha256_text(self.idempotency_scope)

    @property
    def lookup_scope_sha256(self) -> str | None:
        """Opaque identity of the receiver lookup namespace."""

        if self.lookup_scope is None:
            return None
        return _sha256_text(self.lookup_scope)


@dataclass(frozen=True)
class EnqueueResult:
    """Receipt for an inserted or idempotently replayed report."""

    outbox_id: int
    report_id: str
    delivery_id: str
    delivery_date: dt.date
    channel: str
    target_key_sha256: str
    status: str
    idempotent_replay: bool = False


@dataclass(frozen=True)
class DeliveryClaim:
    """One leased send attempt; the raw target remains outside this value."""

    outbox_id: int
    attempt_id: str
    attempt_number: int
    delivery_id: str
    report_id: str
    delivery_date: dt.date
    channel: str
    target_key_sha256: str
    report: Mapping[str, Any]
    markdown: str
    idempotency_key: str
    lease_token: str
    claimed_at: dt.datetime
    lease_expires_at: dt.datetime


@dataclass(frozen=True)
class OutboxRecord:
    """Read-only public projection that never contains the raw target."""

    outbox_id: int
    report_id: str
    delivery_id: str
    delivery_date: dt.date
    timezone: str
    channel: str
    target_key_sha256: str
    ledger_last_event_hash: str | None
    status: str
    attempt_count: int
    created_at: dt.datetime
    updated_at: dt.datetime
    delivered_at: dt.datetime | None
    current_attempt_id: str | None
    lease_expires_at: dt.datetime | None


@dataclass(frozen=True)
class DeliveryAttempt:
    """Sanitized audit view of a delivery attempt."""

    attempt_id: str
    outbox_id: int
    attempt_number: int
    status: str
    supports_idempotency_key: bool
    supports_delivery_lookup: bool
    idempotency_scope_sha256: str | None
    lookup_scope_sha256: str | None
    claimed_at: dt.datetime
    lease_expires_at: dt.datetime
    completed_at: dt.datetime | None
    reconciled_at: dt.datetime | None
    idempotent_retry_authorized_at: dt.datetime | None
    error_code: str | None
    receiver_receipt_sha256: str | None


class DailyReportOutbox:
    """Durable, fail-closed outbox for a private daily-report receiver."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        default_lease: dt.timedelta = dt.timedelta(minutes=5),
    ) -> None:
        if isinstance(database_path, str) and database_path == ":memory:":
            raise OutboxValidationError("a durable filesystem database is required")
        self.database_path = Path(database_path)
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int):
            raise OutboxValidationError("busy_timeout_ms must be an integer")
        if busy_timeout_ms < 1:
            raise OutboxValidationError("busy_timeout_ms must be positive")
        if not isinstance(default_lease, dt.timedelta) or default_lease <= dt.timedelta(0):
            raise OutboxValidationError("default_lease must be a positive timedelta")
        self.busy_timeout_ms = busy_timeout_ms
        self.default_lease = default_lease
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @staticmethod
    def require_exactly_once_capability(
        capabilities: DeliveryAdapterCapabilities,
    ) -> None:
        """Reject adapters whose best possible semantic is only at-most-once."""

        if not isinstance(capabilities, DeliveryAdapterCapabilities):
            raise OutboxValidationError(
                "capabilities must be DeliveryAdapterCapabilities"
            )
        if not capabilities.supports_exactly_once:
            raise OutboxCapabilityError(
                "adapter has neither idempotency-key nor delivery-lookup support; "
                "exactly-once delivery is unsafe and only at-most-once is possible"
            )

    def enqueue(
        self,
        report: Mapping[str, Any],
        target_key: str,
        current_ledger_hash: str | None,
        *,
        now: dt.datetime | None = None,
    ) -> EnqueueResult:
        """Render, validate and reserve one receiver/day delivery slot.

        Markdown is derived internally from the validated JSON contract so a
        caller cannot enqueue a stale or independently edited presentation.
        """

        if not isinstance(report, Mapping):
            raise OutboxValidationError("report must be a mapping")
        if not isinstance(target_key, str) or not target_key:
            raise OutboxValidationError("target_key must be non-empty text")
        if len(target_key) > 8_192:
            raise OutboxValidationError("target_key is too long")
        ledger_hash = _optional_hash_text(
            current_ledger_hash, "current_ledger_hash"
        )
        timestamp = _now_or_aware(now, "now")

        try:
            validated = validate_private_daily_report(report)
        except Exception as exc:
            raise OutboxValidationError("private daily report schema validation failed") from exc
        if not isinstance(validated, Mapping):
            raise OutboxIntegrityError("report validator did not return a mapping")
        if _contains_forbidden_target_field(validated):
            raise OutboxValidationError("delivery target fields are forbidden in the report")

        report_json = canonical_json(validated)
        markdown = render_private_daily_markdown(validated)
        prepared_at = _parse_timestamp(validated["prepared_at"], "report.prepared_at")
        if timestamp < prepared_at:
            raise OutboxValidationError("enqueue time may not precede report.prepared_at")
        target_key_sha256 = _hash_text(
            compute_target_key_sha256(target_key), "target_key_sha256"
        )
        # The target must never become report or Markdown content persisted by this store.
        if target_key in report_json or target_key in markdown:
            raise OutboxValidationError("raw target key must not appear in persisted content")
        if target_key_sha256 in report_json or target_key_sha256 in markdown:
            raise OutboxValidationError(
                "target key digest must not appear in persisted report content"
            )

        report_id = _hash_text(validated.get("report_id"), "report.report_id")
        expected_report_id = compute_report_id(validated)
        if not hmac.compare_digest(report_id, expected_report_id):
            raise OutboxValidationError("report_id does not match canonical report content")

        delivery = validated.get("delivery")
        if not isinstance(delivery, Mapping):
            raise OutboxValidationError("report.delivery must be an object")
        delivery_id = _hash_text(delivery.get("delivery_id"), "report.delivery.delivery_id")
        delivery_date = _date(delivery.get("delivery_date"), "report.delivery.delivery_date")
        timezone = _nonempty_text(delivery.get("timezone"), "report.delivery.timezone", 128)
        channel = _nonempty_text(delivery.get("channel"), "report.delivery.channel", 128)
        schema_version = _nonempty_text(
            validated.get("schema_version"), "report.schema_version", 128
        )

        expected_delivery_id = compute_delivery_id(
            delivery_date=delivery_date.isoformat(),
            timezone=timezone,
            channel=channel,
            target_key_sha256=target_key_sha256,
            schema_version=schema_version,
        )
        if not hmac.compare_digest(delivery_id, expected_delivery_id):
            raise OutboxValidationError(
                "delivery_id does not match date, timezone, channel, and target digest"
            )

        portfolio = validated.get("portfolio")
        if not isinstance(portfolio, Mapping):
            raise OutboxValidationError("report.portfolio must be an object")
        report_ledger_hash = _optional_hash_text(
            portfolio.get("ledger_last_event_hash"),
            "report.portfolio.ledger_last_event_hash",
        )
        if not _optional_hashes_equal(report_ledger_hash, ledger_hash):
            raise OutboxValidationError("report ledger hash does not match current ledger hash")

        content_sha256 = _sha256_text(report_json + "\n---MARKDOWN---\n" + markdown)
        timestamp_text = _timestamp_text(timestamp)
        immutable: dict[str, Any] = {
            "report_id": report_id,
            "delivery_id": delivery_id,
            "delivery_date": delivery_date.isoformat(),
            "timezone": timezone,
            "channel": channel,
            "target_key_sha256": target_key_sha256,
            "ledger_last_event_hash": ledger_hash,
            "report_json": report_json,
            "markdown": markdown,
            "content_sha256": content_sha256,
        }

        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM daily_report_outbox "
                "WHERE channel = ? AND target_key_sha256 = ? AND delivery_date = ?",
                (channel, target_key_sha256, delivery_date.isoformat()),
            ).fetchone()
            if existing is not None:
                return self._compare_existing_enqueue(existing, immutable)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO daily_report_outbox (
                        report_id, delivery_id, delivery_date, timezone, channel,
                        target_key_sha256, ledger_last_event_hash, report_json,
                        markdown, content_sha256, status, attempt_count,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', 0, ?, ?)
                    """,
                    (
                        report_id,
                        delivery_id,
                        delivery_date.isoformat(),
                        timezone,
                        channel,
                        target_key_sha256,
                        ledger_hash,
                        report_json,
                        markdown,
                        content_sha256,
                        timestamp_text,
                        timestamp_text,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # A different unique identity can only be an immutable collision.
                collision = connection.execute(
                    "SELECT * FROM daily_report_outbox "
                    "WHERE delivery_id = ? OR report_id = ?",
                    (delivery_id, report_id),
                ).fetchone()
                if collision is not None:
                    return self._compare_existing_enqueue(collision, immutable)
                raise OutboxIdempotencyConflict(
                    "the daily delivery slot conflicts with persisted content"
                ) from exc
            return EnqueueResult(
                outbox_id=int(cursor.lastrowid),
                report_id=report_id,
                delivery_id=delivery_id,
                delivery_date=delivery_date,
                channel=channel,
                target_key_sha256=target_key_sha256,
                status="prepared",
            )

    def claim(
        self,
        delivery_id: str,
        capabilities: DeliveryAdapterCapabilities,
        *,
        now: dt.datetime,
        lease_duration: dt.timedelta | None = None,
    ) -> DeliveryClaim:
        """Lease a prepared/retryable report and append an attempt atomically.

        If a previous lease expired, this call records ``delivery_unknown`` and
        fails.  The caller must look up the receiver and call
        :meth:`reconcile_unknown`; it may not blindly resend.
        """

        self.require_exactly_once_capability(capabilities)
        normalized_delivery_id = _hash_text(delivery_id, "delivery_id")
        claimed_at = _aware_utc(now, "now")
        duration = self.default_lease if lease_duration is None else lease_duration
        if not isinstance(duration, dt.timedelta) or duration <= dt.timedelta(0):
            raise OutboxValidationError("lease_duration must be a positive timedelta")
        try:
            lease_expires_at = claimed_at + duration
        except OverflowError as exc:
            raise OutboxValidationError("lease_duration is outside datetime bounds") from exc
        lease_token = secrets.token_urlsafe(32)
        lease_token_sha256 = _sha256_text(lease_token)
        attempt_id = str(uuid.uuid4())
        expired_to_unknown = False
        claim: DeliveryClaim | None = None

        with self._transaction() as connection:
            row = self._delivery_row(connection, normalized_delivery_id)
            self._verify_persisted_immutable(row)
            created_at = _parse_timestamp(row["created_at"], "created_at")
            updated_at = _parse_timestamp(row["updated_at"], "updated_at")
            if claimed_at < created_at or claimed_at < updated_at:
                raise OutboxValidationError(
                    "claim time may not precede persisted outbox time"
                )
            status = str(row["status"])
            if status == "sending":
                expires_at = _parse_timestamp(row["lease_expires_at"], "lease_expires_at")
                if claimed_at < expires_at:
                    raise OutboxLeaseError("delivery already has an active lease")
                self._mark_expired_unknown_locked(connection, row, claimed_at)
                expired_to_unknown = True
            elif status not in {"prepared", "retryable"}:
                raise OutboxStateError(f"cannot claim a delivery in {status!r} state")
            else:
                if status == "retryable":
                    prior_attempt_id = row["current_attempt_id"]
                    if not prior_attempt_id:
                        raise OutboxIntegrityError(
                            "retryable delivery has no prior attempt"
                        )
                    prior_attempt = self._attempt_row(
                        connection, str(prior_attempt_id)
                    )
                    if prior_attempt["idempotent_retry_authorized_at"] is not None:
                        if not capabilities.supports_idempotency_key:
                            raise OutboxCapabilityError(
                                "idempotent retry must continue using an idempotency key"
                            )
                        original_scope = _optional_hash_text(
                            prior_attempt["idempotency_scope_sha256"],
                            "persisted idempotency_scope_sha256",
                        )
                        if not _optional_hashes_equal(
                            original_scope,
                            capabilities.idempotency_scope_sha256,
                        ):
                            raise OutboxCapabilityError(
                                "idempotent retry must use the original receiver scope"
                            )
                    elif prior_attempt["status"] != "receiver_not_found":
                        raise OutboxIntegrityError(
                            "retryable delivery has no safe retry basis"
                        )
                attempt_number = int(row["attempt_count"]) + 1
                connection.execute(
                    """
                    INSERT INTO daily_delivery_attempts (
                        attempt_id, outbox_id, attempt_number, status,
                        lease_token_sha256, supports_idempotency_key,
                        supports_delivery_lookup, idempotency_scope_sha256,
                        lookup_scope_sha256, claimed_at, lease_expires_at
                    ) VALUES (?, ?, ?, 'sending', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        int(row["outbox_id"]),
                        attempt_number,
                        lease_token_sha256,
                        int(capabilities.supports_idempotency_key),
                        int(capabilities.supports_delivery_lookup),
                        capabilities.idempotency_scope_sha256,
                        capabilities.lookup_scope_sha256,
                        _timestamp_text(claimed_at),
                        _timestamp_text(lease_expires_at),
                    ),
                )
                connection.execute(
                    """
                    UPDATE daily_report_outbox SET
                        status = 'sending', attempt_count = ?, current_attempt_id = ?,
                        lease_token_sha256 = ?, lease_expires_at = ?, updated_at = ?
                    WHERE outbox_id = ?
                    """,
                    (
                        attempt_number,
                        attempt_id,
                        lease_token_sha256,
                        _timestamp_text(lease_expires_at),
                        _timestamp_text(claimed_at),
                        int(row["outbox_id"]),
                    ),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise OutboxIntegrityError("outbox lease update did not affect one row")
                claim = DeliveryClaim(
                    outbox_id=int(row["outbox_id"]),
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    delivery_id=str(row["delivery_id"]),
                    report_id=str(row["report_id"]),
                    delivery_date=_date(row["delivery_date"], "delivery_date"),
                    channel=str(row["channel"]),
                    target_key_sha256=str(row["target_key_sha256"]),
                    report=json.loads(str(row["report_json"])),
                    markdown=str(row["markdown"]),
                    idempotency_key=str(row["delivery_id"]),
                    lease_token=lease_token,
                    claimed_at=claimed_at,
                    lease_expires_at=lease_expires_at,
                )
        if expired_to_unknown:
            raise OutboxLeaseError(
                "expired lease has unknown receiver state; reconcile before any retry"
            )
        if claim is None:  # pragma: no cover - defensive invariant
            raise OutboxIntegrityError("claim transaction produced no claim")
        return claim

    def mark_delivered(
        self,
        delivery_id: str,
        lease_token: str,
        *,
        delivered_at: dt.datetime,
        receiver_receipt: str | None = None,
    ) -> OutboxRecord:
        """Commit a positive response from the active send attempt."""

        delivery_id = _hash_text(delivery_id, "delivery_id")
        delivered = _aware_utc(delivered_at, "delivered_at")
        token_hash = _token_hash(lease_token)
        receipt_hash = _optional_secret_hash(receiver_receipt, "receiver_receipt")
        with self._transaction() as connection:
            row = self._delivery_row(connection, delivery_id)
            self._require_active_lease(row, token_hash)
            attempt_id = str(row["current_attempt_id"])
            attempt = self._attempt_row(connection, attempt_id)
            claimed_at = _parse_timestamp(attempt["claimed_at"], "claimed_at")
            if delivered < claimed_at:
                raise OutboxValidationError(
                    "delivered_at may not precede the attempt claim"
                )
            connection.execute(
                """
                UPDATE daily_delivery_attempts SET
                    status = 'delivered', completed_at = ?,
                    receiver_receipt_sha256 = ?
                WHERE attempt_id = ? AND status = 'sending'
                """,
                (_timestamp_text(delivered), receipt_hash, attempt_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise OutboxIntegrityError("active delivery attempt is unavailable")
            connection.execute(
                """
                UPDATE daily_report_outbox SET
                    status = 'delivered', delivered_at = ?, lease_token_sha256 = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE outbox_id = ? AND status = 'sending'
                """,
                (_timestamp_text(delivered), _timestamp_text(delivered), int(row["outbox_id"])),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise OutboxIntegrityError("outbox delivery update did not affect one row")
            return self._record_from_row(
                self._delivery_row(connection, delivery_id)
            )

    def mark_unknown(
        self,
        delivery_id: str,
        lease_token: str,
        *,
        observed_at: dt.datetime,
        error_code: str = "delivery_status_unknown",
    ) -> OutboxRecord:
        """Record an ambiguous send result without making it retryable."""

        delivery_id = _hash_text(delivery_id, "delivery_id")
        observed = _aware_utc(observed_at, "observed_at")
        token_hash = _token_hash(lease_token)
        safe_error = _safe_error_code(error_code)
        with self._transaction() as connection:
            row = self._delivery_row(connection, delivery_id)
            self._require_active_lease(row, token_hash)
            attempt_id = str(row["current_attempt_id"])
            attempt = self._attempt_row(connection, attempt_id)
            claimed_at = _parse_timestamp(attempt["claimed_at"], "claimed_at")
            if observed < claimed_at:
                raise OutboxValidationError(
                    "observed_at may not precede the attempt claim"
                )
            connection.execute(
                """
                UPDATE daily_delivery_attempts SET
                    status = 'delivery_unknown', completed_at = ?, error_code = ?
                WHERE attempt_id = ? AND status = 'sending'
                """,
                (_timestamp_text(observed), safe_error, attempt_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise OutboxIntegrityError("active delivery attempt is unavailable")
            connection.execute(
                """
                UPDATE daily_report_outbox SET
                    status = 'delivery_unknown', lease_token_sha256 = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE outbox_id = ? AND status = 'sending'
                """,
                (_timestamp_text(observed), int(row["outbox_id"])),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise OutboxIntegrityError("outbox unknown update did not affect one row")
            return self._record_from_row(
                self._delivery_row(connection, delivery_id)
            )

    def reconcile_unknown(
        self,
        delivery_id: str,
        *,
        receiver_status: str,
        capabilities: DeliveryAdapterCapabilities,
        reconciled_at: dt.datetime,
        receiver_receipt: str | None = None,
    ) -> OutboxRecord:
        """Resolve ambiguity using an explicit receiver lookup result.

        ``not_found`` is the only result that permits another claim.  Any other
        non-confirming result leaves the delivery unknown and raises.
        """

        if not isinstance(capabilities, DeliveryAdapterCapabilities):
            raise OutboxValidationError(
                "capabilities must be DeliveryAdapterCapabilities"
            )
        if not capabilities.supports_delivery_lookup:
            raise OutboxCapabilityError(
                "unknown delivery can be reconciled only by receiver lookup"
            )
        delivery_id = _hash_text(delivery_id, "delivery_id")
        reconciled = _aware_utc(reconciled_at, "reconciled_at")
        normalized_status = str(receiver_status).strip().lower()
        if normalized_status not in {"delivered", "not_found"}:
            raise OutboxValidationError(
                "receiver_status must be explicit 'delivered' or 'not_found'"
            )
        receipt_hash = _optional_secret_hash(receiver_receipt, "receiver_receipt")
        if normalized_status == "delivered" and receipt_hash is None:
            raise OutboxValidationError(
                "a delivered lookup requires a receiver receipt or lookup token"
            )

        with self._transaction() as connection:
            row = self._delivery_row(connection, delivery_id)
            if row["status"] != "delivery_unknown":
                raise OutboxStateError(
                    f"cannot reconcile a delivery in {row['status']!r} state"
                )
            attempt_id = str(row["current_attempt_id"])
            attempt = self._attempt_row(connection, attempt_id)
            if not bool(attempt["supports_delivery_lookup"]):
                raise OutboxCapabilityError(
                    "the ambiguous send attempt did not support receiver lookup"
                )
            original_lookup_scope = _optional_hash_text(
                attempt["lookup_scope_sha256"],
                "persisted lookup_scope_sha256",
            )
            if not _optional_hashes_equal(
                original_lookup_scope,
                capabilities.lookup_scope_sha256,
            ):
                raise OutboxCapabilityError(
                    "receiver lookup must use the original receiver scope"
                )
            completed_at = _parse_timestamp(attempt["completed_at"], "completed_at")
            if reconciled < completed_at:
                raise OutboxValidationError(
                    "reconciled_at may not precede attempt completion"
                )
            attempt_status = "delivered" if normalized_status == "delivered" else "receiver_not_found"
            connection.execute(
                """
                UPDATE daily_delivery_attempts SET
                    status = ?, reconciled_at = ?, receiver_receipt_sha256 = ?
                WHERE attempt_id = ? AND status = 'delivery_unknown'
                """,
                (attempt_status, _timestamp_text(reconciled), receipt_hash, attempt_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise OutboxIntegrityError("unknown attempt is unavailable for reconciliation")
            if normalized_status == "delivered":
                connection.execute(
                    """
                    UPDATE daily_report_outbox SET
                        status = 'delivered', delivered_at = ?, updated_at = ?
                    WHERE outbox_id = ? AND status = 'delivery_unknown'
                    """,
                    (_timestamp_text(reconciled), _timestamp_text(reconciled), int(row["outbox_id"])),
                )
            else:
                connection.execute(
                    """
                    UPDATE daily_report_outbox SET
                        status = 'retryable', updated_at = ?
                    WHERE outbox_id = ? AND status = 'delivery_unknown'
                    """,
                    (_timestamp_text(reconciled), int(row["outbox_id"])),
                )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise OutboxIntegrityError("outbox reconciliation did not affect one row")
            return self._record_from_row(
                self._delivery_row(connection, delivery_id)
            )

    def authorize_idempotent_retry(
        self,
        delivery_id: str,
        *,
        capabilities: DeliveryAdapterCapabilities,
        authorized_at: dt.datetime,
    ) -> OutboxRecord:
        """Permit retry of an unknown send under the same idempotency key.

        This is deliberately distinct from receiver ``not_found``.  The old
        attempt remains ``delivery_unknown`` and records only that a retry was
        authorized because both the original and current adapters support the
        stable ``delivery_id`` idempotency key.
        """

        if not isinstance(capabilities, DeliveryAdapterCapabilities):
            raise OutboxValidationError(
                "capabilities must be DeliveryAdapterCapabilities"
            )
        if not capabilities.supports_idempotency_key:
            raise OutboxCapabilityError(
                "idempotent retry requires current adapter idempotency-key support"
            )
        normalized_delivery_id = _hash_text(delivery_id, "delivery_id")
        authorized = _aware_utc(authorized_at, "authorized_at")
        with self._transaction() as connection:
            row = self._delivery_row(connection, normalized_delivery_id)
            if row["status"] != "delivery_unknown":
                raise OutboxStateError(
                    f"cannot authorize retry from {row['status']!r} state"
                )
            attempt_id = str(row["current_attempt_id"])
            attempt = self._attempt_row(connection, attempt_id)
            if attempt["status"] != "delivery_unknown":
                raise OutboxIntegrityError(
                    "unknown outbox does not reference an unknown attempt"
                )
            if not bool(attempt["supports_idempotency_key"]):
                raise OutboxCapabilityError(
                    "the ambiguous send attempt did not use an idempotency key"
                )
            original_scope = _optional_hash_text(
                attempt["idempotency_scope_sha256"],
                "persisted idempotency_scope_sha256",
            )
            if not _optional_hashes_equal(
                original_scope,
                capabilities.idempotency_scope_sha256,
            ):
                raise OutboxCapabilityError(
                    "idempotent retry requires the original receiver scope"
                )
            completed_at = _parse_timestamp(attempt["completed_at"], "completed_at")
            if authorized < completed_at:
                raise OutboxValidationError(
                    "authorized_at may not precede attempt completion"
                )
            connection.execute(
                """
                UPDATE daily_delivery_attempts SET
                    idempotent_retry_authorized_at = ?
                WHERE attempt_id = ? AND status = 'delivery_unknown'
                  AND idempotent_retry_authorized_at IS NULL
                """,
                (_timestamp_text(authorized), attempt_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise OutboxIntegrityError(
                    "unknown attempt could not record idempotent retry authorization"
                )
            connection.execute(
                """
                UPDATE daily_report_outbox SET
                    status = 'retryable', updated_at = ?
                WHERE outbox_id = ? AND status = 'delivery_unknown'
                """,
                (_timestamp_text(authorized), int(row["outbox_id"])),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise OutboxIntegrityError(
                    "outbox idempotent retry authorization did not affect one row"
                )
            return self._record_from_row(
                self._delivery_row(connection, normalized_delivery_id)
            )

    def get(self, delivery_id: str) -> OutboxRecord:
        """Return delivery state without report content or raw target data."""

        normalized = _hash_text(delivery_id, "delivery_id")
        with self._connect() as connection:
            return self._record_from_row(self._delivery_row(connection, normalized))

    def attempts(self, delivery_id: str) -> tuple[DeliveryAttempt, ...]:
        """Return sanitized attempt history in creation order."""

        normalized = _hash_text(delivery_id, "delivery_id")
        with self._connect() as connection:
            row = self._delivery_row(connection, normalized)
            rows = connection.execute(
                "SELECT * FROM daily_delivery_attempts WHERE outbox_id = ? "
                "ORDER BY attempt_number",
                (int(row["outbox_id"]),),
            ).fetchall()
        return tuple(self._attempt_from_row(item) for item in rows)

    def _compare_existing_enqueue(
        self,
        row: sqlite3.Row,
        immutable: Mapping[str, Any],
    ) -> EnqueueResult:
        mismatches = [
            key for key, value in immutable.items() if row[key] != value
        ]
        if mismatches:
            raise OutboxIdempotencyConflict(
                "daily delivery slot already contains different immutable content"
            )
        return EnqueueResult(
            outbox_id=int(row["outbox_id"]),
            report_id=str(row["report_id"]),
            delivery_id=str(row["delivery_id"]),
            delivery_date=_date(row["delivery_date"], "delivery_date"),
            channel=str(row["channel"]),
            target_key_sha256=str(row["target_key_sha256"]),
            status=str(row["status"]),
            idempotent_replay=True,
        )

    @staticmethod
    def _verify_persisted_immutable(row: sqlite3.Row) -> None:
        """Recompute every immutable identity immediately before a send."""

        try:
            report_json = row["report_json"]
            markdown = row["markdown"]
            if not isinstance(report_json, str) or not isinstance(markdown, str):
                raise OutboxIntegrityError("persisted report content is not text")
            if not markdown.strip() or "\x00" in markdown:
                raise OutboxIntegrityError("persisted Markdown is invalid")
            parsed = json.loads(report_json)
            if not isinstance(parsed, Mapping):
                raise OutboxIntegrityError("persisted report JSON is not an object")
            validated = validate_private_daily_report(parsed)
            if canonical_json(validated) != report_json:
                raise OutboxIntegrityError("persisted report JSON is not canonical")
            if render_private_daily_markdown(validated) != markdown:
                raise OutboxIntegrityError(
                    "persisted Markdown is not the deterministic report rendering"
                )

            report_id = _hash_text(row["report_id"], "persisted report_id")
            json_report_id = _hash_text(
                validated.get("report_id"), "persisted report.report_id"
            )
            expected_report_id = compute_report_id(validated)
            if not (
                hmac.compare_digest(report_id, json_report_id)
                and hmac.compare_digest(report_id, expected_report_id)
            ):
                raise OutboxIntegrityError("persisted report identity does not verify")

            delivery = validated.get("delivery")
            if not isinstance(delivery, Mapping):
                raise OutboxIntegrityError("persisted report delivery is invalid")
            delivery_id = _hash_text(row["delivery_id"], "persisted delivery_id")
            json_delivery_id = _hash_text(
                delivery.get("delivery_id"), "persisted report.delivery.delivery_id"
            )
            delivery_date = _date(
                delivery.get("delivery_date"), "persisted report.delivery.delivery_date"
            ).isoformat()
            timezone = _nonempty_text(
                delivery.get("timezone"), "persisted report.delivery.timezone", 128
            )
            channel = _nonempty_text(
                delivery.get("channel"), "persisted report.delivery.channel", 128
            )
            target_hash = _hash_text(
                row["target_key_sha256"], "persisted target_key_sha256"
            )
            expected_delivery_id = compute_delivery_id(
                delivery_date=delivery_date,
                timezone=timezone,
                channel=channel,
                target_key_sha256=target_hash,
                schema_version=validated.get("schema_version"),
            )
            if not (
                hmac.compare_digest(delivery_id, json_delivery_id)
                and hmac.compare_digest(delivery_id, expected_delivery_id)
                and str(row["delivery_date"]) == delivery_date
                and str(row["timezone"]) == timezone
                and str(row["channel"]) == channel
            ):
                raise OutboxIntegrityError("persisted delivery identity does not verify")

            portfolio = validated.get("portfolio")
            if not isinstance(portfolio, Mapping):
                raise OutboxIntegrityError("persisted report portfolio is invalid")
            ledger_hash = _optional_hash_text(
                row["ledger_last_event_hash"], "persisted ledger_last_event_hash"
            )
            json_ledger_hash = _optional_hash_text(
                portfolio.get("ledger_last_event_hash"),
                "persisted report.portfolio.ledger_last_event_hash",
            )
            if not _optional_hashes_equal(ledger_hash, json_ledger_hash):
                raise OutboxIntegrityError("persisted ledger identity does not verify")

            content_hash = _hash_text(
                row["content_sha256"], "persisted content_sha256"
            )
            expected_content_hash = _sha256_text(
                report_json + "\n---MARKDOWN---\n" + markdown
            )
            if not hmac.compare_digest(content_hash, expected_content_hash):
                raise OutboxIntegrityError("persisted report content hash does not verify")
        except OutboxIntegrityError:
            raise
        except Exception as exc:
            raise OutboxIntegrityError(
                "persisted immutable daily report failed verification"
            ) from exc

    @staticmethod
    def _attempt_row(
        connection: sqlite3.Connection,
        attempt_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM daily_delivery_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise OutboxIntegrityError("outbox current attempt is unavailable")
        return row

    def _mark_expired_unknown_locked(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        observed_at: dt.datetime,
    ) -> None:
        attempt_id = row["current_attempt_id"]
        if not attempt_id:
            raise OutboxIntegrityError("sending delivery has no current attempt")
        connection.execute(
            """
            UPDATE daily_delivery_attempts SET
                status = 'delivery_unknown', completed_at = ?,
                error_code = 'lease_expired_status_unknown'
            WHERE attempt_id = ? AND status = 'sending'
            """,
            (_timestamp_text(observed_at), str(attempt_id)),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise OutboxIntegrityError("expired sending attempt is unavailable")
        connection.execute(
            """
            UPDATE daily_report_outbox SET
                status = 'delivery_unknown', lease_token_sha256 = NULL,
                lease_expires_at = NULL, updated_at = ?
            WHERE outbox_id = ? AND status = 'sending'
            """,
            (_timestamp_text(observed_at), int(row["outbox_id"])),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise OutboxIntegrityError("expired outbox lease is unavailable")

    @staticmethod
    def _require_active_lease(row: sqlite3.Row, token_hash: str) -> None:
        if row["status"] != "sending":
            raise OutboxStateError(
                f"delivery is in {row['status']!r} state, not 'sending'"
            )
        stored = row["lease_token_sha256"]
        if not isinstance(stored, str) or not hmac.compare_digest(stored, token_hash):
            raise OutboxLeaseError("lease token does not match the active attempt")
        if not row["current_attempt_id"] or not row["lease_expires_at"]:
            raise OutboxIntegrityError("sending delivery has incomplete lease state")

    @staticmethod
    def _delivery_row(
        connection: sqlite3.Connection,
        delivery_id: str,
    ) -> sqlite3.Row:
        rows = connection.execute(
            "SELECT * FROM daily_report_outbox WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchall()
        if not rows:
            # A dropped trigger must not turn a tampered delivery_id column into
            # an ordinary "not found" result.  The canonical report still owns
            # the expected identity, so detect that mismatch before returning.
            candidates = connection.execute(
                "SELECT delivery_id, report_json FROM daily_report_outbox"
            ).fetchall()
            for candidate in candidates:
                try:
                    parsed = json.loads(str(candidate["report_json"]))
                    report_delivery_id = parsed["delivery"]["delivery_id"]
                except (KeyError, TypeError, json.JSONDecodeError):
                    continue
                if report_delivery_id == delivery_id:
                    raise OutboxIntegrityError(
                        "persisted delivery_id column does not match report identity"
                    )
            raise OutboxValidationError("delivery_id is not present in the outbox")
        if len(rows) != 1:
            raise OutboxIntegrityError("delivery_id is not unique in the outbox")
        return rows[0]

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> OutboxRecord:
        status = str(row["status"])
        if status not in _OUTBOX_STATUSES:
            raise OutboxIntegrityError("persisted outbox status is invalid")
        return OutboxRecord(
            outbox_id=int(row["outbox_id"]),
            report_id=str(row["report_id"]),
            delivery_id=str(row["delivery_id"]),
            delivery_date=_date(row["delivery_date"], "delivery_date"),
            timezone=str(row["timezone"]),
            channel=str(row["channel"]),
            target_key_sha256=str(row["target_key_sha256"]),
            ledger_last_event_hash=(
                None
                if row["ledger_last_event_hash"] is None
                else str(row["ledger_last_event_hash"])
            ),
            status=status,
            attempt_count=int(row["attempt_count"]),
            created_at=_parse_timestamp(row["created_at"], "created_at"),
            updated_at=_parse_timestamp(row["updated_at"], "updated_at"),
            delivered_at=(
                None
                if row["delivered_at"] is None
                else _parse_timestamp(row["delivered_at"], "delivered_at")
            ),
            current_attempt_id=(
                None if row["current_attempt_id"] is None else str(row["current_attempt_id"])
            ),
            lease_expires_at=(
                None
                if row["lease_expires_at"] is None
                else _parse_timestamp(row["lease_expires_at"], "lease_expires_at")
            ),
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> DeliveryAttempt:
        status = str(row["status"])
        if status not in _ATTEMPT_STATUSES:
            raise OutboxIntegrityError("persisted attempt status is invalid")
        try:
            scope_hash = _optional_hash_text(
                row["idempotency_scope_sha256"],
                "persisted idempotency_scope_sha256",
            )
        except OutboxValidationError as exc:
            raise OutboxIntegrityError(
                "persisted idempotency scope identity is invalid"
            ) from exc
        if bool(row["supports_idempotency_key"]) != (scope_hash is not None):
            raise OutboxIntegrityError(
                "persisted idempotency capability and scope disagree"
            )
        try:
            lookup_scope_hash = _optional_hash_text(
                row["lookup_scope_sha256"],
                "persisted lookup_scope_sha256",
            )
        except OutboxValidationError as exc:
            raise OutboxIntegrityError(
                "persisted lookup scope identity is invalid"
            ) from exc
        if bool(row["supports_delivery_lookup"]) != (
            lookup_scope_hash is not None
        ):
            raise OutboxIntegrityError(
                "persisted lookup capability and scope disagree"
            )
        return DeliveryAttempt(
            attempt_id=str(row["attempt_id"]),
            outbox_id=int(row["outbox_id"]),
            attempt_number=int(row["attempt_number"]),
            status=status,
            supports_idempotency_key=bool(row["supports_idempotency_key"]),
            supports_delivery_lookup=bool(row["supports_delivery_lookup"]),
            idempotency_scope_sha256=scope_hash,
            lookup_scope_sha256=lookup_scope_hash,
            claimed_at=_parse_timestamp(row["claimed_at"], "claimed_at"),
            lease_expires_at=_parse_timestamp(row["lease_expires_at"], "lease_expires_at"),
            completed_at=(
                None
                if row["completed_at"] is None
                else _parse_timestamp(row["completed_at"], "completed_at")
            ),
            reconciled_at=(
                None
                if row["reconciled_at"] is None
                else _parse_timestamp(row["reconciled_at"], "reconciled_at")
            ),
            idempotent_retry_authorized_at=(
                None
                if row["idempotent_retry_authorized_at"] is None
                else _parse_timestamp(
                    row["idempotent_retry_authorized_at"],
                    "idempotent_retry_authorized_at",
                )
            ),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            receiver_receipt_sha256=(
                None
                if row["receiver_receipt_sha256"] is None
                else str(row["receiver_receipt_sha256"])
            ),
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            self._enable_wal(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_report_outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id TEXT NOT NULL UNIQUE,
                    delivery_id TEXT NOT NULL UNIQUE,
                    delivery_date TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    target_key_sha256 TEXT NOT NULL,
                    ledger_last_event_hash TEXT,
                    report_json TEXT NOT NULL,
                    markdown TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN (
                        'prepared', 'sending', 'delivery_unknown', 'retryable', 'delivered'
                    )),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
                    current_attempt_id TEXT,
                    lease_token_sha256 TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE (channel, target_key_sha256, delivery_date),
                    FOREIGN KEY (current_attempt_id)
                        REFERENCES daily_delivery_attempts(attempt_id)
                );

                CREATE TABLE IF NOT EXISTS daily_delivery_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    outbox_id INTEGER NOT NULL REFERENCES daily_report_outbox(outbox_id),
                    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                    status TEXT NOT NULL CHECK (status IN (
                        'sending', 'delivery_unknown', 'receiver_not_found', 'delivered'
                    )),
                    lease_token_sha256 TEXT NOT NULL UNIQUE,
                    supports_idempotency_key INTEGER NOT NULL CHECK (
                        supports_idempotency_key IN (0, 1)
                    ),
                    supports_delivery_lookup INTEGER NOT NULL CHECK (
                        supports_delivery_lookup IN (0, 1)
                    ),
                    idempotency_scope_sha256 TEXT CHECK (
                        (supports_idempotency_key = 1 AND idempotency_scope_sha256 IS NOT NULL) OR
                        (supports_idempotency_key = 0 AND idempotency_scope_sha256 IS NULL)
                    ),
                    lookup_scope_sha256 TEXT CHECK (
                        (supports_delivery_lookup = 1 AND lookup_scope_sha256 IS NOT NULL) OR
                        (supports_delivery_lookup = 0 AND lookup_scope_sha256 IS NULL)
                    ),
                    claimed_at TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    completed_at TEXT,
                    reconciled_at TEXT,
                    idempotent_retry_authorized_at TEXT,
                    error_code TEXT,
                    receiver_receipt_sha256 TEXT,
                    UNIQUE (outbox_id, attempt_number)
                );

                CREATE TRIGGER IF NOT EXISTS daily_report_outbox_immutable
                BEFORE UPDATE ON daily_report_outbox
                WHEN
                    NEW.report_id IS NOT OLD.report_id OR
                    NEW.delivery_id IS NOT OLD.delivery_id OR
                    NEW.delivery_date IS NOT OLD.delivery_date OR
                    NEW.timezone IS NOT OLD.timezone OR
                    NEW.channel IS NOT OLD.channel OR
                    NEW.target_key_sha256 IS NOT OLD.target_key_sha256 OR
                    NEW.ledger_last_event_hash IS NOT OLD.ledger_last_event_hash OR
                    NEW.report_json IS NOT OLD.report_json OR
                    NEW.markdown IS NOT OLD.markdown OR
                    NEW.content_sha256 IS NOT OLD.content_sha256 OR
                    NEW.created_at IS NOT OLD.created_at
                BEGIN
                    SELECT RAISE(ABORT, 'daily report immutable fields cannot be updated');
                END;

                CREATE TRIGGER IF NOT EXISTS daily_report_outbox_status_transition
                BEFORE UPDATE OF status ON daily_report_outbox
                WHEN NEW.status IS NOT OLD.status AND NOT (
                    (OLD.status = 'prepared' AND NEW.status = 'sending') OR
                    (OLD.status = 'retryable' AND NEW.status = 'sending') OR
                    (OLD.status = 'sending' AND NEW.status IN ('delivered', 'delivery_unknown')) OR
                    (OLD.status = 'delivery_unknown' AND NEW.status IN ('delivered', 'retryable'))
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid daily outbox status transition');
                END;

                CREATE TRIGGER IF NOT EXISTS daily_report_outbox_no_delete
                BEFORE DELETE ON daily_report_outbox
                BEGIN
                    SELECT RAISE(ABORT, 'daily outbox rows are append-preserved');
                END;

                CREATE TRIGGER IF NOT EXISTS daily_delivery_attempts_immutable
                BEFORE UPDATE ON daily_delivery_attempts
                WHEN
                    NEW.attempt_id IS NOT OLD.attempt_id OR
                    NEW.outbox_id IS NOT OLD.outbox_id OR
                    NEW.attempt_number IS NOT OLD.attempt_number OR
                    NEW.lease_token_sha256 IS NOT OLD.lease_token_sha256 OR
                    NEW.supports_idempotency_key IS NOT OLD.supports_idempotency_key OR
                    NEW.supports_delivery_lookup IS NOT OLD.supports_delivery_lookup OR
                    NEW.idempotency_scope_sha256 IS NOT OLD.idempotency_scope_sha256 OR
                    NEW.lookup_scope_sha256 IS NOT OLD.lookup_scope_sha256 OR
                    NEW.claimed_at IS NOT OLD.claimed_at OR
                    NEW.lease_expires_at IS NOT OLD.lease_expires_at
                BEGIN
                    SELECT RAISE(ABORT, 'delivery attempt identity cannot be updated');
                END;

                CREATE TRIGGER IF NOT EXISTS daily_delivery_attempts_status_transition
                BEFORE UPDATE OF status ON daily_delivery_attempts
                WHEN NEW.status IS NOT OLD.status AND NOT (
                    (OLD.status = 'sending' AND NEW.status IN ('delivered', 'delivery_unknown')) OR
                    (OLD.status = 'delivery_unknown' AND NEW.status IN ('delivered', 'receiver_not_found'))
                )
                BEGIN
                    SELECT RAISE(ABORT, 'invalid delivery attempt status transition');
                END;

                CREATE TRIGGER IF NOT EXISTS daily_delivery_attempts_no_delete
                BEFORE DELETE ON daily_delivery_attempts
                BEGIN
                    SELECT RAISE(ABORT, 'delivery attempts are append-preserved');
                END;
                """
            )

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        """Enable WAL with a short bounded retry for first-open races."""

        if str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
            return
        for attempt in range(8):
            try:
                mode = str(
                    connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                ).lower()
                if mode != "wal":
                    raise OutboxIntegrityError("SQLite refused WAL journal mode")
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(0.01 * (2**attempt))


def _hash_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise OutboxValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _optional_hash_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _hash_text(value, field_name)


def _optional_hashes_equal(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is right
    return hmac.compare_digest(left, right)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _nonempty_text(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OutboxValidationError(f"{field_name} must be non-empty text")
    if value != value.strip():
        raise OutboxValidationError(f"{field_name} may not have surrounding whitespace")
    if len(value) > maximum:
        raise OutboxValidationError(f"{field_name} is too long")
    return value


def _date(value: Any, field_name: str) -> dt.date:
    if isinstance(value, dt.datetime):
        raise OutboxValidationError(f"{field_name} must be an ISO date")
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise OutboxValidationError(f"{field_name} must be an ISO date") from exc
    raise OutboxValidationError(f"{field_name} must be an ISO date")


def _aware_utc(value: Any, field_name: str) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        raise OutboxValidationError(f"{field_name} must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise OutboxValidationError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(dt.timezone.utc)


def _now_or_aware(value: dt.datetime | None, field_name: str) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) if value is None else _aware_utc(value, field_name)


def _timestamp_text(value: dt.datetime) -> str:
    aware = _aware_utc(value, "timestamp")
    return aware.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any, field_name: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise OutboxIntegrityError(f"persisted {field_name} is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise OutboxIntegrityError(f"persisted {field_name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OutboxIntegrityError(f"persisted {field_name} is not timezone-aware")
    return parsed.astimezone(dt.timezone.utc)


def _token_hash(token: Any) -> str:
    if not isinstance(token, str) or len(token) < 32 or len(token) > 512:
        raise OutboxLeaseError("lease token is invalid")
    return _sha256_text(token)


def _optional_secret_hash(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise OutboxValidationError(f"{field_name} must be non-empty bounded text")
    return _sha256_text(value)


def _safe_error_code(value: Any) -> str:
    if not isinstance(value, str):
        return "other_delivery_error"
    normalized = value.strip().lower()
    if normalized not in _SAFE_ERROR_CODES:
        return "other_delivery_error"
    return normalized


def _contains_forbidden_target_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in {"target_key", "target_key_sha256"}:
                return True
            if _contains_forbidden_target_field(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_target_field(item) for item in value)
    return False


__all__ = [
    "DailyOutboxError",
    "DailyReportOutbox",
    "DeliveryAdapterCapabilities",
    "DeliveryAttempt",
    "DeliveryClaim",
    "EnqueueResult",
    "OutboxCapabilityError",
    "OutboxIdempotencyConflict",
    "OutboxIntegrityError",
    "OutboxLeaseError",
    "OutboxRecord",
    "OutboxStateError",
    "OutboxValidationError",
]
