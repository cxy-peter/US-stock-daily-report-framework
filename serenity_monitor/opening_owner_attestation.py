"""One-time owner attestation bound to the private ledger opening event.

The three owner-only control files contain hashes and timestamps only.  They
never contain symbols, quantities, costs, cash, private paths or delivery
targets.  A claim is created only after a random interactive TTY challenge;
an intent is durably published immediately before the opening transaction;
and a receipt binds that intent to the committed opening event.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import string
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, TextIO

from .private_runtime_config import CONFIG_SCHEMA_VERSION, PrivateDailyRuntimeConfig
from .private_runtime_paths import (
    PrivateRuntimePaths,
    tighten_private_file,
    validate_existing_private_runtime_file,
    validate_existing_private_storage_root,
)
from .private_windows_security import PrivateWindowsSecurityError, read_owner_only_file


_CLAIM_CONTRACT_V1 = "opening_owner_attestation_claim/v1.0.0"
_INTENT_CONTRACT_V1 = "opening_owner_attestation_intent/v1.0.0"
_RECEIPT_CONTRACT_V1 = "opening_owner_attestation_receipt/v1.0.0"
_CLAIM_CONFIG_SCHEMA_V1 = "private_daily_runtime/v1.0.0"
_OPENING_IDENTITY_V1 = "opening_snapshot_identity/v1.0.0"
_CONFIRMATION_METHOD_V1 = "interactive_tty_random_challenge/v1"
CLAIM_CONTRACT_VERSION = _CLAIM_CONTRACT_V1
INTENT_CONTRACT_VERSION = _INTENT_CONTRACT_V1
RECEIPT_CONTRACT_VERSION = _RECEIPT_CONTRACT_V1
OPENING_IDENTITY_VERSION = _OPENING_IDENTITY_V1
CONFIRMATION_METHOD = _CONFIRMATION_METHOD_V1
CLAIM_TTL = dt.timedelta(minutes=30)
MAX_FUTURE_SKEW = dt.timedelta(seconds=30)
_MAX_CONTROL_BYTES = 8_192
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RANDOM_ID = re.compile(r"^[0-9a-f]{32}$")
_CHALLENGE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_PRESENCE_SENTINEL = object()
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


class OpeningOwnerAttestationError(RuntimeError):
    """A fixed-code attestation error whose text contains no private value."""


@dataclass(frozen=True, repr=False)
class OwnerPresenceProof:
    """Opaque in-process result of the interactive random challenge."""

    _sentinel: object


@dataclass(frozen=True, repr=False)
class OpeningLedgerBinding:
    opening_event_id: str
    opening_event_hash: str
    idempotency_key: str
    created_at: dt.datetime

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.opening_event_id):
            raise OpeningOwnerAttestationError("opening_binding_event_id_invalid")
        if not _SHA256.fullmatch(self.opening_event_hash):
            raise OpeningOwnerAttestationError("opening_binding_event_hash_invalid")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise OpeningOwnerAttestationError("opening_binding_key_invalid")
        object.__setattr__(
            self,
            "created_at",
            _aware_utc(self.created_at, "opening_binding_time_invalid").replace(
                microsecond=0
            ),
        )


@dataclass(frozen=True, repr=False)
class OpeningOwnerClaim:
    attestation_id: str
    attested_at: dt.datetime
    expires_at: dt.datetime
    config_bytes_sha256: str
    opening_snapshot_sha256: str
    config_schema_version: str = _CLAIM_CONFIG_SCHEMA_V1
    opening_identity_version: str = _OPENING_IDENTITY_V1

    def __post_init__(self) -> None:
        if not _RANDOM_ID.fullmatch(self.attestation_id):
            raise OpeningOwnerAttestationError("opening_claim_id_invalid")
        attested = _aware_utc(self.attested_at, "opening_claim_time_invalid").replace(
            microsecond=0
        )
        expires = _aware_utc(self.expires_at, "opening_claim_expiry_invalid").replace(
            microsecond=0
        )
        if expires - attested != CLAIM_TTL:
            raise OpeningOwnerAttestationError("opening_claim_ttl_invalid")
        _digest(self.config_bytes_sha256, "opening_claim_config_digest_invalid")
        _digest(self.opening_snapshot_sha256, "opening_claim_snapshot_digest_invalid")
        if self.config_schema_version != _CLAIM_CONFIG_SCHEMA_V1:
            raise OpeningOwnerAttestationError(
                "opening_claim_config_schema_unsupported"
            )
        if self.opening_identity_version != _OPENING_IDENTITY_V1:
            raise OpeningOwnerAttestationError(
                "opening_claim_identity_version_unsupported"
            )
        object.__setattr__(self, "attested_at", attested)
        object.__setattr__(self, "expires_at", expires)

    def body(self) -> dict[str, str]:
        return {
            "attestation_id": self.attestation_id,
            "attested_at": _utc_text(self.attested_at),
            "config_bytes_sha256": self.config_bytes_sha256,
            "config_schema_version": self.config_schema_version,
            "confirmation_method": _CONFIRMATION_METHOD_V1,
            "contract_version": _CLAIM_CONTRACT_V1,
            "expires_at": _utc_text(self.expires_at),
            "opening_identity_version": self.opening_identity_version,
            "opening_snapshot_sha256": self.opening_snapshot_sha256,
        }

    @property
    def claim_sha256(self) -> str:
        return _sha256(_canonical_json(self.body()).encode("ascii"))

    def to_dict(self) -> dict[str, str]:
        return {**self.body(), "claim_sha256": self.claim_sha256}


@dataclass(frozen=True, repr=False)
class OpeningOwnerIntent:
    claim_sha256: str
    intent_id: str
    created_at: dt.datetime

    def __post_init__(self) -> None:
        _digest(self.claim_sha256, "opening_intent_claim_digest_invalid")
        if not _RANDOM_ID.fullmatch(self.intent_id):
            raise OpeningOwnerAttestationError("opening_intent_id_invalid")
        object.__setattr__(
            self,
            "created_at",
            _aware_utc(self.created_at, "opening_intent_time_invalid").replace(
                microsecond=0
            ),
        )

    def body(self) -> dict[str, str]:
        return {
            "claim_sha256": self.claim_sha256,
            "contract_version": _INTENT_CONTRACT_V1,
            "created_at": _utc_text(self.created_at),
            "intent_id": self.intent_id,
        }

    @property
    def intent_sha256(self) -> str:
        return _sha256(_canonical_json(self.body()).encode("ascii"))

    def to_dict(self) -> dict[str, str]:
        return {**self.body(), "intent_sha256": self.intent_sha256}


@dataclass(frozen=True, repr=False)
class OpeningOwnerReceipt:
    claim_sha256: str
    intent_sha256: str
    opening_event_id: str
    opening_event_hash: str
    consumed_at: dt.datetime

    def __post_init__(self) -> None:
        for value, code in (
            (self.claim_sha256, "opening_receipt_claim_digest_invalid"),
            (self.intent_sha256, "opening_receipt_intent_digest_invalid"),
            (self.opening_event_id, "opening_receipt_event_id_invalid"),
            (self.opening_event_hash, "opening_receipt_event_hash_invalid"),
        ):
            _digest(value, code)
        object.__setattr__(
            self,
            "consumed_at",
            _aware_utc(self.consumed_at, "opening_receipt_time_invalid").replace(
                microsecond=0
            ),
        )

    def body(self) -> dict[str, str]:
        return {
            "claim_sha256": self.claim_sha256,
            "consumed_at": _utc_text(self.consumed_at),
            "contract_version": _RECEIPT_CONTRACT_V1,
            "intent_sha256": self.intent_sha256,
            "opening_event_hash": self.opening_event_hash,
            "opening_event_id": self.opening_event_id,
        }

    @property
    def receipt_sha256(self) -> str:
        return _sha256(_canonical_json(self.body()).encode("ascii"))

    def to_dict(self) -> dict[str, str]:
        return {**self.body(), "receipt_sha256": self.receipt_sha256}


@dataclass(frozen=True, repr=False)
class OpeningAttestationAudit:
    state: str
    reason_code: str
    claim: OpeningOwnerClaim | None = None
    intent: OpeningOwnerIntent | None = None
    receipt: OpeningOwnerReceipt | None = None

    def __post_init__(self) -> None:
        if self.state not in {
            "commit_unknown",
            "config_mismatch",
            "consumed_verified",
            "expired",
            "missing",
            "pending_verified",
            "recovery_available",
            "replay_or_rollback",
            "resume_available",
            "resume_requires_owner_reconfirmation",
            "unsafe",
        }:
            raise OpeningOwnerAttestationError("opening_audit_state_invalid")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", self.reason_code):
            raise OpeningOwnerAttestationError("opening_audit_reason_invalid")


@dataclass(frozen=True, repr=False)
class OpeningClaimReceipt:
    status: str

    def __post_init__(self) -> None:
        if self.status not in {"created", "existing", "renewed"}:
            raise OpeningOwnerAttestationError("opening_claim_status_invalid")


def _aware_utc(value: dt.datetime, code: str) -> dt.datetime:
    if (
        not isinstance(value, dt.datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise OpeningOwnerAttestationError(code)
    return value.astimezone(dt.timezone.utc)


def _utc_text(value: dt.datetime) -> str:
    return _aware_utc(value, "opening_timestamp_invalid").isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _parse_time(value: Any, code: str) -> dt.datetime:
    if not isinstance(value, str):
        raise OpeningOwnerAttestationError(code)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OpeningOwnerAttestationError(code) from exc
    return _aware_utc(parsed, code)


def _digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise OpeningOwnerAttestationError(code)
    return value


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decimal_text(value: Decimal) -> str:
    """Context-free Decimal identity; never call normalize()."""

    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def opening_snapshot_sha256(
    config: PrivateDailyRuntimeConfig,
    *,
    identity_version: str = _OPENING_IDENTITY_V1,
) -> str:
    """Bind the opening economics and each opening security identity."""

    if identity_version != _OPENING_IDENTITY_V1:
        raise OpeningOwnerAttestationError(
            "opening_snapshot_identity_version_unsupported"
        )

    instruments: list[dict[str, Any]] = []
    for position in sorted(config.opening.positions, key=lambda item: item.symbol):
        instrument = config.by_symbol[position.symbol]
        instruments.append(
            {
                "asset_type": instrument.asset_type,
                "calendar_id": instrument.calendar_id,
                "canonical_symbol": instrument.canonical_symbol,
                "currency": instrument.currency,
                "exchange_mic": instrument.exchange_mic,
                "price_unit_multiplier": _decimal_text(
                    instrument.price_unit_multiplier
                ),
            }
        )
    identity = {
        "currency": config.ledger_policy.currency,
        "instruments": instruments,
        "opening": {
            "cash": _decimal_text(config.opening.cash),
            "positions": [
                {
                    "average_economic_cost": _decimal_text(
                        position.average_economic_cost
                    ),
                    "quantity": _decimal_text(position.quantity),
                    "symbol": position.symbol,
                }
                for position in sorted(
                    config.opening.positions,
                    key=lambda item: item.symbol,
                )
            ],
            "session": config.opening.session.isoformat(),
        },
        "share_scale": config.ledger_policy.share_scale,
        "version": identity_version,
    }
    return _sha256(_canonical_json(identity).encode("ascii"))


def opening_ledger_idempotency_key(
    claim: OpeningOwnerClaim,
    intent: OpeningOwnerIntent,
) -> str:
    if intent.claim_sha256 != claim.claim_sha256:
        raise OpeningOwnerAttestationError("opening_intent_claim_mismatch")
    return f"opening-attestation/v1:{claim.claim_sha256}:{intent.intent_sha256}"


def validate_opening_commit_time(
    claim: OpeningOwnerClaim,
    intent: OpeningOwnerIntent,
    recorded_at: dt.datetime,
) -> dt.datetime:
    """Validate the final wall-clock boundary before the SQLite commit."""

    moment = _aware_utc(recorded_at, "opening_commit_time_invalid").replace(
        microsecond=0
    )
    if not intent.created_at <= moment < claim.expires_at:
        raise OpeningOwnerAttestationError("opening_commit_time_outside_claim")
    return moment


def _pairs_no_duplicates(pairs: list[tuple[Any, Any]]) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OpeningOwnerAttestationError("opening_control_duplicate_key")
        result[key] = value
    return result


def _decode_document(payload: bytes) -> Mapping[str, Any]:
    try:
        document = json.loads(
            payload.decode("ascii", errors="strict"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_float=lambda _value: (_ for _ in ()).throw(
                OpeningOwnerAttestationError("opening_control_number_forbidden")
            ),
            parse_constant=lambda _value: (_ for _ in ()).throw(
                OpeningOwnerAttestationError("opening_control_number_forbidden")
            ),
        )
    except OpeningOwnerAttestationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OpeningOwnerAttestationError("opening_control_document_invalid") from exc
    if not isinstance(document, Mapping):
        raise OpeningOwnerAttestationError("opening_control_schema_invalid")
    return document


def _canonical_bytes(document: Mapping[str, Any]) -> bytes:
    return (_canonical_json(document) + "\n").encode("ascii")


def _parse_claim(payload: bytes) -> OpeningOwnerClaim:
    document = _decode_document(payload)
    required = {
        "attestation_id",
        "attested_at",
        "claim_sha256",
        "config_bytes_sha256",
        "config_schema_version",
        "confirmation_method",
        "contract_version",
        "expires_at",
        "opening_identity_version",
        "opening_snapshot_sha256",
    }
    if set(document) != required:
        raise OpeningOwnerAttestationError("opening_claim_schema_invalid")
    if (
        document.get("contract_version") != _CLAIM_CONTRACT_V1
        or document.get("confirmation_method") != _CONFIRMATION_METHOD_V1
    ):
        raise OpeningOwnerAttestationError("opening_claim_contract_invalid")
    claim = OpeningOwnerClaim(
        attestation_id=str(document.get("attestation_id", "")),
        attested_at=_parse_time(document.get("attested_at"), "opening_claim_time_invalid"),
        expires_at=_parse_time(document.get("expires_at"), "opening_claim_expiry_invalid"),
        config_bytes_sha256=str(document.get("config_bytes_sha256", "")),
        opening_snapshot_sha256=str(document.get("opening_snapshot_sha256", "")),
        config_schema_version=str(document.get("config_schema_version", "")),
        opening_identity_version=str(
            document.get("opening_identity_version", "")
        ),
    )
    if document.get("claim_sha256") != claim.claim_sha256:
        raise OpeningOwnerAttestationError("opening_claim_self_hash_mismatch")
    if payload != _canonical_bytes(claim.to_dict()):
        raise OpeningOwnerAttestationError("opening_claim_not_canonical")
    return claim


def _parse_intent(payload: bytes) -> OpeningOwnerIntent:
    document = _decode_document(payload)
    if set(document) != {
        "claim_sha256",
        "contract_version",
        "created_at",
        "intent_id",
        "intent_sha256",
    } or document.get("contract_version") != _INTENT_CONTRACT_V1:
        raise OpeningOwnerAttestationError("opening_intent_schema_invalid")
    intent = OpeningOwnerIntent(
        claim_sha256=str(document.get("claim_sha256", "")),
        intent_id=str(document.get("intent_id", "")),
        created_at=_parse_time(document.get("created_at"), "opening_intent_time_invalid"),
    )
    if document.get("intent_sha256") != intent.intent_sha256:
        raise OpeningOwnerAttestationError("opening_intent_self_hash_mismatch")
    if payload != _canonical_bytes(intent.to_dict()):
        raise OpeningOwnerAttestationError("opening_intent_not_canonical")
    return intent


def _parse_receipt(payload: bytes) -> OpeningOwnerReceipt:
    document = _decode_document(payload)
    if set(document) != {
        "claim_sha256",
        "consumed_at",
        "contract_version",
        "intent_sha256",
        "opening_event_hash",
        "opening_event_id",
        "receipt_sha256",
    } or document.get("contract_version") != _RECEIPT_CONTRACT_V1:
        raise OpeningOwnerAttestationError("opening_receipt_schema_invalid")
    receipt = OpeningOwnerReceipt(
        claim_sha256=str(document.get("claim_sha256", "")),
        intent_sha256=str(document.get("intent_sha256", "")),
        opening_event_id=str(document.get("opening_event_id", "")),
        opening_event_hash=str(document.get("opening_event_hash", "")),
        consumed_at=_parse_time(
            document.get("consumed_at"),
            "opening_receipt_time_invalid",
        ),
    )
    if document.get("receipt_sha256") != receipt.receipt_sha256:
        raise OpeningOwnerAttestationError("opening_receipt_self_hash_mismatch")
    if payload != _canonical_bytes(receipt.to_dict()):
        raise OpeningOwnerAttestationError("opening_receipt_not_canonical")
    return receipt


def _read_posix_owner_only(path: Path) -> bytes:
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OpeningOwnerAttestationError("opening_control_open_failed") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > _MAX_CONTROL_BYTES
        ):
            raise OpeningOwnerAttestationError("opening_control_file_unsafe")
        if not os.path.samestat(metadata, os.stat(path, follow_symlinks=False)):
            raise OpeningOwnerAttestationError("opening_control_identity_changed")
        chunks: list[bytes] = []
        remaining = _MAX_CONTROL_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 4_096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > _MAX_CONTROL_BYTES:
            raise OpeningOwnerAttestationError("opening_control_too_large")
        if not os.path.samestat(metadata, os.stat(path, follow_symlinks=False)):
            raise OpeningOwnerAttestationError("opening_control_identity_changed")
        return payload
    finally:
        os.close(descriptor)


def _allowed_control_paths(paths: PrivateRuntimePaths) -> frozenset[Path]:
    return frozenset(
        {
            paths.opening_claim_file.absolute(),
            paths.opening_intent_file.absolute(),
            paths.opening_receipt_file.absolute(),
        }
    )


def _read_control(paths: PrivateRuntimePaths, path: Path) -> bytes:
    validate_existing_private_storage_root(paths)
    candidate = path.absolute()
    if candidate not in _allowed_control_paths(paths) or candidate.parent != paths.root.absolute():
        raise OpeningOwnerAttestationError("opening_control_path_invalid")
    if not os.path.lexists(str(candidate)):
        raise FileNotFoundError
    try:
        return (
            read_owner_only_file(candidate, _MAX_CONTROL_BYTES)
            if os.name == "nt"
            else _read_posix_owner_only(candidate)
        )
    except PrivateWindowsSecurityError as exc:
        raise OpeningOwnerAttestationError("opening_control_file_unsafe") from exc


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        directory,
        os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_temp(path: Path, payload: bytes) -> Path:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("opening control write made no progress")
            offset += written
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    try:
        tighten_private_file(temporary)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _publish_new(path: Path, payload: bytes) -> None:
    temporary = _write_temp(path, payload)
    try:
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        tighten_private_file(path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _recover_one_publication(paths: PrivateRuntimePaths, path: Path) -> None:
    """Finish the sole safe hard-link transition left by an abrupt stop."""

    if not os.path.lexists(str(path)):
        return
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise OpeningOwnerAttestationError("opening_control_file_unsafe")
    if metadata.st_nlink == 1:
        return
    if metadata.st_nlink != 2:
        raise OpeningOwnerAttestationError("opening_control_file_unsafe")
    temporary_matches: list[Path] = []
    for candidate in path.parent.glob(f".{path.name}.*.tmp"):
        try:
            candidate_metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(candidate_metadata.st_mode) and os.path.samestat(
            metadata,
            candidate_metadata,
        ):
            temporary_matches.append(candidate)
    archive_matches: list[Path] = []
    archive_pattern = None
    if path == paths.opening_claim_file:
        archive_pattern = "opening-owner-attestation.claim.*.expired.json"
    elif path == paths.opening_intent_file:
        archive_pattern = "opening-owner-attestation.intent.*.aborted.json"
    if archive_pattern is not None:
        for candidate in path.parent.glob(archive_pattern):
            try:
                candidate_metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(candidate_metadata.st_mode) and os.path.samestat(
                metadata,
                candidate_metadata,
            ):
                archive_matches.append(candidate)
    if len(temporary_matches) == 1 and not archive_matches:
        temporary_matches[0].unlink()
        tighten_private_file(path)
    elif len(archive_matches) == 1 and not temporary_matches:
        path.unlink()
        tighten_private_file(archive_matches[0])
    else:
        raise OpeningOwnerAttestationError("opening_control_publication_ambiguous")
    _fsync_directory(path.parent)


def recover_opening_control_publications(paths: PrivateRuntimePaths) -> None:
    """Mutating recovery used only by explicit claim/initialization commands."""

    validate_existing_private_storage_root(paths)
    for path in (
        paths.opening_claim_file,
        paths.opening_intent_file,
        paths.opening_receipt_file,
    ):
        _recover_one_publication(paths, path)


def _archive_claim(paths: PrivateRuntimePaths, claim: OpeningOwnerClaim) -> None:
    path = paths.opening_claim_file
    archive = path.parent / f"opening-owner-attestation.claim.{claim.claim_sha256}.expired.json"
    if os.path.lexists(str(archive)):
        raise OpeningOwnerAttestationError("opening_claim_archive_conflict")
    os.link(path, archive, follow_symlinks=False)
    path.unlink()
    tighten_private_file(archive)
    _fsync_directory(path.parent)


def _archive_intent(paths: PrivateRuntimePaths, intent: OpeningOwnerIntent) -> None:
    path = paths.opening_intent_file
    archive = path.parent / (
        "opening-owner-attestation.intent."
        f"{intent.intent_sha256}.aborted.json"
    )
    if os.path.lexists(str(archive)):
        raise OpeningOwnerAttestationError("opening_intent_archive_conflict")
    os.link(path, archive, follow_symlinks=False)
    path.unlink()
    tighten_private_file(archive)
    _fsync_directory(path.parent)


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


def _directory_fingerprint(path: Path) -> tuple[int, int, int]:
    metadata = path.stat()
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mtime_ns),
    )


def _is_pristine_empty_database(
    paths: PrivateRuntimePaths,
    database_path: Path,
    *,
    expected_schema: Mapping[tuple[str, str], str],
    empty_tables: tuple[str, ...],
) -> bool:
    """Prove an absent or exact empty SQLite store without writing it."""

    directory_before = _directory_fingerprint(database_path.parent)
    if any(
        os.path.lexists(str(database_path) + suffix)
        for suffix in ("-journal", "-shm", "-wal")
    ):
        return False
    if not os.path.lexists(str(database_path)):
        return (
            not os.path.lexists(str(database_path))
            and not any(
                os.path.lexists(str(database_path) + suffix)
                for suffix in ("-journal", "-shm", "-wal")
            )
            and _directory_fingerprint(database_path.parent) == directory_before
        )
    try:
        tighten_private_file(database_path)
        database = validate_existing_private_runtime_file(
            paths,
            database_path,
        )
        before = _file_fingerprint(database)
        connection = sqlite3.connect(
            database.absolute().as_uri() + "?mode=ro&immutable=1",
            uri=True,
            isolation_level=None,
        )
        try:
            if tuple(
                str(row[0])
                for row in connection.execute("PRAGMA quick_check").fetchall()
            ) != ("ok",):
                return False
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                return False
            schema = {
                (str(row[0]), str(row[1])): _sha256(
                    str(row[2]).strip().encode("utf-8")
                )
                for row in connection.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if schema != expected_schema:
                return False
            for table in empty_tables:
                safe_table = table.replace('"', '""')
                if connection.execute(
                    f'SELECT 1 FROM "{safe_table}" LIMIT 1'
                ).fetchone():
                    return False
        finally:
            connection.close()
        return (
            _file_fingerprint(database) == before
            and not any(
                os.path.lexists(str(database) + suffix)
                for suffix in ("-journal", "-shm", "-wal")
            )
            and _directory_fingerprint(database.parent) == directory_before
        )
    except Exception:
        return False


def _is_pristine_empty_ledger(paths: PrivateRuntimePaths) -> bool:
    """Allow recovery from an old schema-only constructor side effect."""

    return _is_pristine_empty_database(
        paths,
        paths.ledger_database,
        expected_schema=_LEDGER_SCHEMA_SHA256,
        empty_tables=("ledger_events",),
    )


def require_opening_ledger_pristine(paths: PrivateRuntimePaths) -> None:
    """Require an absent or exact empty opening ledger before intent."""

    validate_existing_private_storage_root(paths)
    if not _is_pristine_empty_ledger(paths):
        raise OpeningOwnerAttestationError("opening_ledger_not_pristine")


def require_opening_outbox_pristine(paths: PrivateRuntimePaths) -> None:
    """Allow no delivery state beyond an exact, empty legacy schema."""

    validate_existing_private_storage_root(paths)
    if not _is_pristine_empty_database(
        paths,
        paths.outbox_database,
        expected_schema=_OUTBOX_SCHEMA_SHA256,
        empty_tables=("daily_delivery_attempts", "daily_report_outbox"),
    ):
        raise OpeningOwnerAttestationError("opening_outbox_not_pristine")


def interactive_owner_presence(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    challenge_factory=None,
) -> OwnerPresenceProof:
    """Require a random TTY response; pipes and unattended flags are rejected."""

    if not input_stream.isatty() or not output_stream.isatty():
        raise OpeningOwnerAttestationError("opening_owner_tty_required")
    factory = challenge_factory or (
        lambda: "".join(secrets.choice(_CHALLENGE_ALPHABET) for _ in range(10))
    )
    challenge = str(factory())
    if not re.fullmatch(r"[23456789A-HJ-NP-Z]{10}", challenge):
        raise OpeningOwnerAttestationError("opening_owner_challenge_invalid")
    output_stream.write("Review the owner-only opening snapshot before confirming.\n")
    output_stream.write(f"Type CONFIRM {challenge} to attest it: ")
    output_stream.flush()
    response = input_stream.readline(64).rstrip("\r\n")
    if response != f"CONFIRM {challenge}":
        raise OpeningOwnerAttestationError("opening_owner_challenge_rejected")
    return OwnerPresenceProof(_PRESENCE_SENTINEL)


def create_opening_owner_claim(
    config: PrivateDailyRuntimeConfig,
    paths: PrivateRuntimePaths,
    *,
    config_bytes_sha256: str,
    owner_presence: OwnerPresenceProof,
    clock,
) -> OpeningClaimReceipt:
    """Create or renew an unused claim after verified interactive presence."""

    if not isinstance(owner_presence, OwnerPresenceProof) or owner_presence._sentinel is not _PRESENCE_SENTINEL:
        raise OpeningOwnerAttestationError("opening_owner_presence_required")
    if CONFIG_SCHEMA_VERSION != _CLAIM_CONFIG_SCHEMA_V1:
        raise OpeningOwnerAttestationError(
            "opening_claim_current_config_schema_unsupported"
        )
    config_digest = _digest(
        config_bytes_sha256,
        "opening_claim_config_digest_invalid",
    )
    validate_existing_private_storage_root(paths)
    recover_opening_control_publications(paths)
    require_opening_outbox_pristine(paths)
    if (
        os.path.lexists(str(paths.opening_receipt_file))
        or not _is_pristine_empty_ledger(paths)
    ):
        raise OpeningOwnerAttestationError("opening_claim_runtime_already_started")
    now = _aware_utc(clock(), "opening_claim_clock_invalid").replace(microsecond=0)
    status = "created"
    if os.path.lexists(str(paths.opening_intent_file)):
        if not os.path.lexists(str(paths.opening_claim_file)):
            raise OpeningOwnerAttestationError("opening_intent_without_claim")
        prior_claim = _parse_claim(_read_control(paths, paths.opening_claim_file))
        prior_intent = _parse_intent(_read_control(paths, paths.opening_intent_file))
        if (
            prior_intent.claim_sha256 != prior_claim.claim_sha256
            or not (
                prior_claim.attested_at
                <= prior_intent.created_at
                < prior_claim.expires_at
            )
        ):
            raise OpeningOwnerAttestationError("opening_intent_binding_invalid")
        _archive_intent(paths, prior_intent)
    if os.path.lexists(str(paths.opening_claim_file)):
        existing = _parse_claim(_read_control(paths, paths.opening_claim_file))
        _archive_claim(paths, existing)
        status = "renewed"
    claim = OpeningOwnerClaim(
        attestation_id=secrets.token_hex(16),
        attested_at=now,
        expires_at=now + CLAIM_TTL,
        config_bytes_sha256=config_digest,
        opening_snapshot_sha256=opening_snapshot_sha256(config),
        config_schema_version=CONFIG_SCHEMA_VERSION,
        opening_identity_version=OPENING_IDENTITY_VERSION,
    )
    try:
        _publish_new(paths.opening_claim_file, _canonical_bytes(claim.to_dict()))
    except (OSError, ValueError) as exc:
        raise OpeningOwnerAttestationError("opening_claim_persistence_failed") from exc
    return OpeningClaimReceipt(status)


def _control_snapshot(paths: PrivateRuntimePaths) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for name, path in (
        ("claim", paths.opening_claim_file),
        ("intent", paths.opening_intent_file),
        ("receipt", paths.opening_receipt_file),
    ):
        snapshot[name] = (
            _read_control(paths, path) if os.path.lexists(str(path)) else None
        )
    return snapshot


def _binding_matches(
    claim: OpeningOwnerClaim,
    intent: OpeningOwnerIntent,
    binding: OpeningLedgerBinding,
) -> bool:
    return (
        intent.claim_sha256 == claim.claim_sha256
        and binding.idempotency_key == opening_ledger_idempotency_key(claim, intent)
        and claim.attested_at <= intent.created_at < claim.expires_at
        and intent.created_at <= binding.created_at < claim.expires_at
    )


def audit_opening_owner_attestation(
    config: PrivateDailyRuntimeConfig,
    paths: PrivateRuntimePaths,
    *,
    config_bytes_sha256: str,
    now: dt.datetime,
    ledger_binding: OpeningLedgerBinding | None,
) -> OpeningAttestationAudit:
    """Read-only state-machine audit used by readiness and runtime gates."""

    observed_at = _aware_utc(now, "opening_audit_clock_invalid")
    try:
        validate_existing_private_storage_root(paths)
        before = _control_snapshot(paths)
        after = _control_snapshot(paths)
        if after != before:
            return OpeningAttestationAudit(
                "unsafe",
                "opening_attestation_changed_during_audit",
            )
        claim = None if before["claim"] is None else _parse_claim(before["claim"])
        intent = None if before["intent"] is None else _parse_intent(before["intent"])
        receipt = (
            None if before["receipt"] is None else _parse_receipt(before["receipt"])
        )
    except FileNotFoundError:
        return OpeningAttestationAudit("unsafe", "opening_attestation_race_detected")
    except Exception:
        return OpeningAttestationAudit("unsafe", "opening_attestation_file_unsafe")
    if claim is None:
        state = "missing" if intent is None and receipt is None and ledger_binding is None else "replay_or_rollback"
        reason = "opening_attestation_missing" if state == "missing" else "opening_attestation_replay_or_rollback"
        return OpeningAttestationAudit(state, reason)
    future_limit = observed_at + MAX_FUTURE_SKEW
    if (
        claim.attested_at > future_limit
        or (intent is not None and intent.created_at > future_limit)
        or (ledger_binding is not None and ledger_binding.created_at > future_limit)
        or (receipt is not None and receipt.consumed_at > future_limit)
    ):
        return OpeningAttestationAudit(
            "unsafe",
            "opening_attestation_future_control",
            claim,
            intent,
            receipt,
        )
    expected_snapshot = opening_snapshot_sha256(
        config,
        identity_version=claim.opening_identity_version,
    )
    if claim.opening_snapshot_sha256 != expected_snapshot:
        return OpeningAttestationAudit(
            "config_mismatch",
            "opening_attestation_snapshot_mismatch",
            claim,
            intent,
            receipt,
        )
    if ledger_binding is None:
        if receipt is not None:
            return OpeningAttestationAudit(
                "replay_or_rollback",
                "opening_attestation_receipt_without_ledger",
                claim,
                intent,
                receipt,
            )
        if claim.config_bytes_sha256 != _digest(
            config_bytes_sha256,
            "opening_audit_config_digest_invalid",
        ):
            return OpeningAttestationAudit(
                "config_mismatch",
                "opening_attestation_config_bytes_mismatch",
                claim,
                intent,
                receipt,
            )
        if intent is not None:
            if not (
                intent.claim_sha256 == claim.claim_sha256
                and claim.attested_at <= intent.created_at < claim.expires_at
            ):
                return OpeningAttestationAudit(
                    "replay_or_rollback",
                    "opening_attestation_intent_binding_mismatch",
                    claim,
                    intent,
                )
            if observed_at >= claim.expires_at:
                return OpeningAttestationAudit(
                    "resume_requires_owner_reconfirmation",
                    "opening_attestation_resume_requires_owner_reconfirmation",
                    claim,
                    intent,
                )
            return OpeningAttestationAudit(
                "resume_available",
                "opening_attestation_commit_resume_available",
                claim,
                intent,
            )
        if claim.attested_at > observed_at + MAX_FUTURE_SKEW:
            return OpeningAttestationAudit(
                "unsafe",
                "opening_attestation_future_claim",
                claim,
            )
        if observed_at >= claim.expires_at:
            return OpeningAttestationAudit(
                "expired",
                "opening_attestation_claim_expired",
                claim,
            )
        return OpeningAttestationAudit(
            "pending_verified",
            "opening_attestation_pending_verified",
            claim,
        )
    if intent is None or not _binding_matches(claim, intent, ledger_binding):
        return OpeningAttestationAudit(
            "replay_or_rollback",
            "opening_attestation_ledger_binding_mismatch",
            claim,
            intent,
            receipt,
        )
    if receipt is None:
        return OpeningAttestationAudit(
            "recovery_available",
            "opening_attestation_receipt_recovery_available",
            claim,
            intent,
        )
    if (
        receipt.claim_sha256 != claim.claim_sha256
        or receipt.intent_sha256 != intent.intent_sha256
        or receipt.opening_event_id != ledger_binding.opening_event_id
        or receipt.opening_event_hash != ledger_binding.opening_event_hash
        or receipt.consumed_at < ledger_binding.created_at
    ):
        return OpeningAttestationAudit(
            "replay_or_rollback",
            "opening_attestation_receipt_binding_mismatch",
            claim,
            intent,
            receipt,
        )
    return OpeningAttestationAudit(
        "consumed_verified",
        "opening_attestation_consumed_verified",
        claim,
        intent,
        receipt,
    )


def publish_opening_intent(
    claim: OpeningOwnerClaim,
    paths: PrivateRuntimePaths,
    *,
    clock,
) -> OpeningOwnerIntent:
    """Publish the commit intent after all external initialization gates pass."""

    now = _aware_utc(clock(), "opening_intent_clock_invalid").replace(microsecond=0)
    recover_opening_control_publications(paths)
    if claim.attested_at > now or now >= claim.expires_at:
        raise OpeningOwnerAttestationError("opening_claim_not_fresh_for_intent")
    if os.path.lexists(str(paths.opening_intent_file)):
        raise OpeningOwnerAttestationError("opening_intent_already_exists")
    if os.path.lexists(str(paths.opening_receipt_file)):
        raise OpeningOwnerAttestationError("opening_receipt_already_exists")
    intent = OpeningOwnerIntent(
        claim_sha256=claim.claim_sha256,
        intent_id=secrets.token_hex(16),
        created_at=now,
    )
    try:
        _publish_new(paths.opening_intent_file, _canonical_bytes(intent.to_dict()))
    except (OSError, ValueError) as exc:
        raise OpeningOwnerAttestationError("opening_intent_persistence_failed") from exc
    return intent


def publish_opening_receipt(
    claim: OpeningOwnerClaim,
    intent: OpeningOwnerIntent,
    binding: OpeningLedgerBinding,
    paths: PrivateRuntimePaths,
    *,
    clock,
) -> OpeningOwnerReceipt:
    """Bind a committed opening event; exact replays return the existing receipt."""

    if not _binding_matches(claim, intent, binding):
        raise OpeningOwnerAttestationError("opening_receipt_binding_invalid")
    recover_opening_control_publications(paths)
    if os.path.lexists(str(paths.opening_receipt_file)):
        receipt = _parse_receipt(_read_control(paths, paths.opening_receipt_file))
        if (
            receipt.claim_sha256 == claim.claim_sha256
            and receipt.intent_sha256 == intent.intent_sha256
            and receipt.opening_event_id == binding.opening_event_id
            and receipt.opening_event_hash == binding.opening_event_hash
            and receipt.consumed_at >= binding.created_at
        ):
            return receipt
        raise OpeningOwnerAttestationError("opening_receipt_conflict")
    consumed_at = _aware_utc(clock(), "opening_receipt_clock_invalid").replace(
        microsecond=0
    )
    if consumed_at < binding.created_at:
        raise OpeningOwnerAttestationError("opening_receipt_precedes_ledger")
    receipt = OpeningOwnerReceipt(
        claim_sha256=claim.claim_sha256,
        intent_sha256=intent.intent_sha256,
        opening_event_id=binding.opening_event_id,
        opening_event_hash=binding.opening_event_hash,
        consumed_at=consumed_at,
    )
    try:
        _publish_new(paths.opening_receipt_file, _canonical_bytes(receipt.to_dict()))
    except (OSError, ValueError) as exc:
        raise OpeningOwnerAttestationError("opening_receipt_persistence_failed") from exc
    return receipt


__all__ = [
    "CLAIM_CONTRACT_VERSION",
    "CLAIM_TTL",
    "CONFIRMATION_METHOD",
    "INTENT_CONTRACT_VERSION",
    "MAX_FUTURE_SKEW",
    "OPENING_IDENTITY_VERSION",
    "RECEIPT_CONTRACT_VERSION",
    "OpeningAttestationAudit",
    "OpeningClaimReceipt",
    "OpeningLedgerBinding",
    "OpeningOwnerAttestationError",
    "OpeningOwnerClaim",
    "OpeningOwnerIntent",
    "OpeningOwnerReceipt",
    "OwnerPresenceProof",
    "audit_opening_owner_attestation",
    "create_opening_owner_claim",
    "interactive_owner_presence",
    "opening_ledger_idempotency_key",
    "opening_snapshot_sha256",
    "publish_opening_intent",
    "publish_opening_receipt",
    "recover_opening_control_publications",
    "require_opening_ledger_pristine",
    "require_opening_outbox_pristine",
    "validate_opening_commit_time",
]
