"""Owner-confirmed private events with no prose or unattended ingestion.

The owner writes one JSON request at a fixed owner-only runtime path.  The
request is approved only after an interactive random TTY challenge.  Approved
documents and their ledger receipts are canonical, self-hashed and immutable.

This module intentionally has no broker, order or natural-language interface.
Silence is represented by an empty approved queue and never becomes an
economic ledger event.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, TextIO

from .private_runtime_config import CONFIG_SCHEMA_VERSION, PrivateDailyRuntimeConfig
from .private_runtime_paths import (
    PrivateRuntimePaths,
    tighten_private_file,
    validate_existing_private_storage_root,
)
from .private_windows_security import (
    PrivateWindowsSecurityError,
    read_owner_only_file,
    secure_create_owner_only_directory,
    verify_owner_only_dacl,
)


REQUEST_CONTRACT_VERSION = "manual_owner_event_request/v1.0.0"
APPROVAL_CONTRACT_VERSION = "manual_owner_event_approval/v1.0.0"
RECEIPT_CONTRACT_VERSION = "manual_owner_event_receipt/v1.0.0"
CONFIRMATION_METHOD = "interactive_tty_random_challenge/v1"

_EVENT_KINDS = frozenset(
    {"confirmed_fill", "cash_flow", "fee", "income", "split", "skip_dca"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_CHALLENGE = re.compile(r"^[23456789A-HJ-NP-Z]{10}$")
_CHALLENGE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_MAX_REQUEST_BYTES = 65_536
_MAX_CONTROL_BYTES = 131_072
_MAX_TEXT = 1_024
_PRESENCE_SENTINEL = object()


class ManualOwnerEventError(RuntimeError):
    """A fixed-code failure that never contains private request values."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", normalized):
            normalized = "manual_event_invalid"
        self.code = normalized
        super().__init__(normalized)


def _fail(code: str) -> None:
    raise ManualOwnerEventError(code)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (_canonical_json(value) + "\n").encode("ascii")


def _utc_text(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _aware_utc(value: Any, code: str) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail(code)
    return value.astimezone(dt.timezone.utc).replace(microsecond=0)


def _parse_time(value: Any, code: str, *, optional: bool = False) -> dt.datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        _fail(code)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail(code)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(code)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0)


def _parse_date(value: Any, code: str) -> dt.date:
    if not isinstance(value, str):
        _fail(code)
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError:
        _fail(code)
    if parsed.isoformat() != value:
        _fail(code)
    return parsed


def _closed(value: Mapping[str, Any], allowed: set[str], code: str) -> None:
    if any(not isinstance(key, str) for key in value) or set(value) != allowed:
        _fail(code)


def _mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    return value


def _decimal_text(
    value: Any,
    code: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
    nonzero: bool = False,
) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        _fail(code)
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(code)
    if not number.is_finite():
        _fail(code)
    if positive and number <= 0:
        _fail(code)
    if nonnegative and number < 0:
        _fail(code)
    if nonzero and number == 0:
        _fail(code)
    # A single plain-text representation prevents exponent and signed-zero
    # aliases from producing multiple approved identities.
    canonical = format(number, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if canonical in {"", "-0"}:
        canonical = "0"
    if value != canonical:
        _fail(code)
    return canonical


def _symbol(value: Any, config: PrivateDailyRuntimeConfig, code: str) -> str:
    if not isinstance(value, str) or value != value.strip().upper() or not value:
        _fail(code)
    if value not in config.by_symbol:
        _fail(code)
    return value


def _safe_reason(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_TEXT:
        _fail(code)
    if any(ord(char) < 32 and char not in "\t" for char in value):
        _fail(code)
    return value.strip()


def _frozen_symbol(value: Any, code: str) -> str:
    """Validate a v1 symbol without consulting a later mutable universe."""

    if (
        not isinstance(value, str)
        or value != value.strip().upper()
        or not re.fullmatch(r"[A-Z0-9][A-Z0-9._/-]{0,31}", value)
    ):
        _fail(code)
    return value


def _frozen_plan_identity(value: Any, code: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}", value)
    ):
        _fail(code)
    return value


def _normalize_frozen_payload(
    event_kind: str,
    raw: Mapping[str, Any],
    occurred_at: dt.datetime | None,
) -> Mapping[str, Any]:
    """Validate the immutable v1 vocabulary independently of current config."""

    if event_kind == "confirmed_fill":
        _closed(
            raw,
            {
                "fees",
                "modeled_dca_replacement",
                "plan_id",
                "plan_version",
                "price",
                "quantity",
                "side",
                "symbol",
            },
            "manual_event_fill_schema_invalid",
        )
        side = raw["side"]
        if side not in {"buy", "sell"}:
            _fail("manual_event_fill_side_invalid")
        replacement = raw["modeled_dca_replacement"]
        if not isinstance(replacement, bool):
            _fail("manual_event_replacement_flag_invalid")
        plan_id = raw["plan_id"]
        plan_version = raw["plan_version"]
        if replacement:
            if side != "buy":
                _fail("manual_event_replacement_not_permitted")
            plan_id = _frozen_plan_identity(plan_id, "manual_event_replacement_plan_invalid")
            plan_version = _frozen_plan_identity(
                plan_version,
                "manual_event_replacement_plan_invalid",
            )
        elif plan_id is not None or plan_version is not None:
            _fail("manual_event_unexpected_plan_identity")
        return MappingProxyType(
            {
                "fees": _decimal_text(raw["fees"], "manual_event_fees_invalid", nonnegative=True),
                "modeled_dca_replacement": replacement,
                "plan_id": plan_id,
                "plan_version": plan_version,
                "price": _decimal_text(raw["price"], "manual_event_price_invalid", positive=True),
                "quantity": _decimal_text(raw["quantity"], "manual_event_quantity_invalid", positive=True),
                "side": side,
                "symbol": _frozen_symbol(raw["symbol"], "manual_event_symbol_invalid"),
            }
        )
    if event_kind == "cash_flow":
        _closed(raw, {"amount", "description", "valuation_weight"}, "manual_event_cash_schema_invalid")
        weight = raw["valuation_weight"]
        if (occurred_at is None) != (weight is None):
            _fail("manual_event_cash_time_weight_mismatch")
        normalized_weight = None
        if weight is not None:
            normalized_weight = _decimal_text(weight, "manual_event_valuation_weight_invalid", nonnegative=True)
            if Decimal(normalized_weight) > 1:
                _fail("manual_event_valuation_weight_invalid")
        return MappingProxyType(
            {
                "amount": _decimal_text(raw["amount"], "manual_event_cash_amount_invalid", nonzero=True),
                "description": _safe_reason(raw["description"], "manual_event_description_invalid"),
                "valuation_weight": normalized_weight,
            }
        )
    if event_kind == "fee":
        _closed(raw, {"amount", "description"}, "manual_event_fee_schema_invalid")
        return MappingProxyType(
            {
                "amount": _decimal_text(raw["amount"], "manual_event_fee_amount_invalid", positive=True),
                "description": _safe_reason(raw["description"], "manual_event_description_invalid"),
            }
        )
    if event_kind == "income":
        _closed(raw, {"amount", "description", "symbol"}, "manual_event_income_schema_invalid")
        symbol = None if raw["symbol"] is None else _frozen_symbol(
            raw["symbol"], "manual_event_symbol_invalid"
        )
        return MappingProxyType(
            {
                "amount": _decimal_text(raw["amount"], "manual_event_income_amount_invalid", positive=True),
                "description": _safe_reason(raw["description"], "manual_event_description_invalid"),
                "symbol": symbol,
            }
        )
    if event_kind == "split":
        _closed(raw, {"ratio", "symbol"}, "manual_event_split_schema_invalid")
        return MappingProxyType(
            {
                "ratio": _decimal_text(raw["ratio"], "manual_event_split_ratio_invalid", positive=True),
                "symbol": _frozen_symbol(raw["symbol"], "manual_event_symbol_invalid"),
            }
        )
    if event_kind == "skip_dca":
        if occurred_at is not None:
            _fail("manual_event_skip_time_forbidden")
        _closed(raw, {"plan_id", "plan_version", "reason"}, "manual_event_skip_schema_invalid")
        return MappingProxyType(
            {
                "plan_id": _frozen_plan_identity(raw["plan_id"], "manual_event_skip_plan_invalid"),
                "plan_version": _frozen_plan_identity(
                    raw["plan_version"], "manual_event_skip_plan_invalid"
                ),
                "reason": _safe_reason(raw["reason"], "manual_event_reason_invalid"),
            }
        )
    _fail("manual_event_kind_invalid")


def _json_document(payload: bytes, code: str) -> Mapping[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        _fail(code)

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in values:
            if not isinstance(key, str) or key in result:
                _fail("manual_event_duplicate_or_invalid_key")
            result[key] = item
        return result

    try:
        document = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: _fail("manual_event_nonfinite_number"),
        )
    except (json.JSONDecodeError, UnicodeError):
        _fail(code)
    return _mapping(document, code)


def _config_identity(config: PrivateDailyRuntimeConfig) -> str:
    instruments = []
    for item in sorted(config.instruments, key=lambda candidate: candidate.canonical_symbol):
        instruments.append(
            {
                "asset_type": item.asset_type,
                "canonical_symbol": item.canonical_symbol,
                "currency": item.currency,
                "exchange_mic": item.exchange_mic,
            }
        )
    identity = {
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "dca": {
            "base_amounts": {
                key: format(value, "f")
                for key, value in sorted(config.dca_plan.base_amounts.items())
            },
            "currency": config.dca_plan.currency,
            "funding_mode": config.dca_plan.funding_mode,
            "plan_id": config.dca_plan.plan_id,
            "share_scale": config.dca_plan.share_scale,
            "version": config.dca_plan.version,
        },
        "instruments": instruments,
        "ledger_currency": config.ledger_policy.currency,
    }
    return _sha256(_canonical_bytes(identity))


def _normalize_payload(
    event_kind: str,
    raw: Mapping[str, Any],
    config: PrivateDailyRuntimeConfig,
    occurred_at: dt.datetime | None,
) -> Mapping[str, Any]:
    if event_kind == "confirmed_fill":
        _closed(
            raw,
            {
                "fees",
                "modeled_dca_replacement",
                "plan_id",
                "plan_version",
                "price",
                "quantity",
                "side",
                "symbol",
            },
            "manual_event_fill_schema_invalid",
        )
        symbol = _symbol(raw["symbol"], config, "manual_event_symbol_invalid")
        side = raw["side"]
        if side not in {"buy", "sell"}:
            _fail("manual_event_fill_side_invalid")
        replacement = raw["modeled_dca_replacement"]
        if not isinstance(replacement, bool):
            _fail("manual_event_replacement_flag_invalid")
        plan_id = raw["plan_id"]
        plan_version = raw["plan_version"]
        if replacement:
            if side != "buy" or symbol not in config.dca_plan.base_amounts:
                _fail("manual_event_replacement_not_permitted")
            if plan_id != config.dca_plan.plan_id or plan_version != config.dca_plan.version:
                _fail("manual_event_replacement_plan_mismatch")
        elif plan_id is not None or plan_version is not None:
            _fail("manual_event_unexpected_plan_identity")
        return MappingProxyType(
            {
                "fees": _decimal_text(raw["fees"], "manual_event_fees_invalid", nonnegative=True),
                "modeled_dca_replacement": replacement,
                "plan_id": plan_id,
                "plan_version": plan_version,
                "price": _decimal_text(raw["price"], "manual_event_price_invalid", positive=True),
                "quantity": _decimal_text(raw["quantity"], "manual_event_quantity_invalid", positive=True),
                "side": side,
                "symbol": symbol,
            }
        )
    if event_kind == "cash_flow":
        _closed(raw, {"amount", "description", "valuation_weight"}, "manual_event_cash_schema_invalid")
        weight = raw["valuation_weight"]
        if (occurred_at is None) != (weight is None):
            _fail("manual_event_cash_time_weight_mismatch")
        normalized_weight = None
        if weight is not None:
            normalized_weight = _decimal_text(weight, "manual_event_valuation_weight_invalid", nonnegative=True)
            if Decimal(normalized_weight) > 1:
                _fail("manual_event_valuation_weight_invalid")
        return MappingProxyType(
            {
                "amount": _decimal_text(raw["amount"], "manual_event_cash_amount_invalid", nonzero=True),
                "description": _safe_reason(raw["description"], "manual_event_description_invalid"),
                "valuation_weight": normalized_weight,
            }
        )
    if event_kind == "fee":
        _closed(raw, {"amount", "description"}, "manual_event_fee_schema_invalid")
        return MappingProxyType(
            {
                "amount": _decimal_text(raw["amount"], "manual_event_fee_amount_invalid", positive=True),
                "description": _safe_reason(raw["description"], "manual_event_description_invalid"),
            }
        )
    if event_kind == "income":
        _closed(raw, {"amount", "description", "symbol"}, "manual_event_income_schema_invalid")
        symbol = None if raw["symbol"] is None else _symbol(
            raw["symbol"], config, "manual_event_symbol_invalid"
        )
        return MappingProxyType(
            {
                "amount": _decimal_text(raw["amount"], "manual_event_income_amount_invalid", positive=True),
                "description": _safe_reason(raw["description"], "manual_event_description_invalid"),
                "symbol": symbol,
            }
        )
    if event_kind == "split":
        _closed(raw, {"ratio", "symbol"}, "manual_event_split_schema_invalid")
        return MappingProxyType(
            {
                "ratio": _decimal_text(raw["ratio"], "manual_event_split_ratio_invalid", positive=True),
                "symbol": _symbol(raw["symbol"], config, "manual_event_symbol_invalid"),
            }
        )
    if event_kind == "skip_dca":
        if occurred_at is not None:
            _fail("manual_event_skip_time_forbidden")
        _closed(raw, {"plan_id", "plan_version", "reason"}, "manual_event_skip_schema_invalid")
        if raw["plan_id"] != config.dca_plan.plan_id or raw["plan_version"] != config.dca_plan.version:
            _fail("manual_event_skip_plan_mismatch")
        return MappingProxyType(
            {
                "plan_id": config.dca_plan.plan_id,
                "plan_version": config.dca_plan.version,
                "reason": _safe_reason(raw["reason"], "manual_event_reason_invalid"),
            }
        )
    _fail("manual_event_kind_invalid")


@dataclass(frozen=True, repr=False)
class ManualEventRequest:
    event_nonce: str
    event_kind: str
    session: dt.date
    occurred_at: dt.datetime | None
    payload: Mapping[str, Any]
    request_bytes_sha256: str
    raw_bytes: bytes | None

    def __post_init__(self) -> None:
        if not _NONCE.fullmatch(self.event_nonce):
            _fail("manual_event_nonce_invalid")
        if self.event_kind not in _EVENT_KINDS:
            _fail("manual_event_kind_invalid")
        if not isinstance(self.session, dt.date) or isinstance(self.session, dt.datetime):
            _fail("manual_event_session_invalid")
        if self.occurred_at is not None:
            object.__setattr__(self, "occurred_at", _aware_utc(self.occurred_at, "manual_event_time_invalid"))
        if not _SHA256.fullmatch(self.request_bytes_sha256):
            _fail("manual_event_request_digest_invalid")
        if self.raw_bytes is not None and (
            not isinstance(self.raw_bytes, bytes)
            or _sha256(self.raw_bytes) != self.request_bytes_sha256
        ):
            _fail("manual_event_request_bytes_mismatch")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def modeled_dca_replacement(self) -> bool:
        return self.event_kind == "confirmed_fill" and bool(
            self.payload.get("modeled_dca_replacement")
        )

    @property
    def phase(self) -> str:
        return "post_dca_replacement" if self.modeled_dca_replacement else "pre_dca"

    def body(self) -> dict[str, Any]:
        return {
            "contract_version": REQUEST_CONTRACT_VERSION,
            "event_kind": self.event_kind,
            "event_nonce": self.event_nonce,
            "occurred_at": None if self.occurred_at is None else _utc_text(self.occurred_at),
            "payload": dict(self.payload),
            "session": self.session.isoformat(),
        }


@dataclass(frozen=True, repr=False)
class ManualEventPresenceProof:
    request_bytes_sha256: str
    event_nonce: str
    _sentinel: object


@dataclass(frozen=True, repr=False)
class ManualEventApproval:
    request: ManualEventRequest
    approved_at: dt.datetime
    config_identity_sha256: str
    approval_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "approved_at", _aware_utc(self.approved_at, "manual_event_approval_time_invalid"))
        if not _SHA256.fullmatch(self.config_identity_sha256):
            _fail("manual_event_config_digest_invalid")
        expected = _sha256(_canonical_bytes(self.body()))
        if self.approval_sha256 and self.approval_sha256 != expected:
            _fail("manual_event_approval_self_hash_mismatch")
        object.__setattr__(self, "approval_sha256", expected)

    @property
    def event_nonce(self) -> str:
        return self.request.event_nonce

    @property
    def event_kind(self) -> str:
        return self.request.event_kind

    @property
    def session(self) -> dt.date:
        return self.request.session

    @property
    def phase(self) -> str:
        return self.request.phase

    def body(self) -> dict[str, Any]:
        return {
            "approved_at": _utc_text(self.approved_at),
            "config_identity_sha256": self.config_identity_sha256,
            "confirmation_method": CONFIRMATION_METHOD,
            "contract_version": APPROVAL_CONTRACT_VERSION,
            "request": self.request.body(),
            "request_bytes_sha256": self.request.request_bytes_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "approval_sha256": self.approval_sha256}


@dataclass(frozen=True, repr=False)
class ManualEventReceipt:
    event_nonce: str
    approval_sha256: str
    ledger_event_id: str
    ledger_event_hash: str
    ledger_idempotency_key: str
    ledger_event_type: str
    session: dt.date
    recorded_at: dt.datetime
    receipt_sha256: str = ""

    def __post_init__(self) -> None:
        if not _NONCE.fullmatch(self.event_nonce):
            _fail("manual_event_nonce_invalid")
        for digest in (self.approval_sha256, self.ledger_event_id, self.ledger_event_hash):
            if not _SHA256.fullmatch(digest):
                _fail("manual_event_receipt_digest_invalid")
        if not isinstance(self.ledger_idempotency_key, str) or not self.ledger_idempotency_key:
            _fail("manual_event_receipt_key_invalid")
        if not isinstance(self.ledger_event_type, str) or not self.ledger_event_type:
            _fail("manual_event_receipt_type_invalid")
        if not isinstance(self.session, dt.date) or isinstance(self.session, dt.datetime):
            _fail("manual_event_receipt_session_invalid")
        object.__setattr__(self, "recorded_at", _aware_utc(self.recorded_at, "manual_event_receipt_time_invalid"))
        expected = _sha256(_canonical_bytes(self.body()))
        if self.receipt_sha256 and self.receipt_sha256 != expected:
            _fail("manual_event_receipt_self_hash_mismatch")
        object.__setattr__(self, "receipt_sha256", expected)

    def body(self) -> dict[str, Any]:
        return {
            "approval_sha256": self.approval_sha256,
            "contract_version": RECEIPT_CONTRACT_VERSION,
            "event_nonce": self.event_nonce,
            "ledger_event_hash": self.ledger_event_hash,
            "ledger_event_id": self.ledger_event_id,
            "ledger_event_type": self.ledger_event_type,
            "ledger_idempotency_key": self.ledger_idempotency_key,
            "recorded_at": _utc_text(self.recorded_at),
            "session": self.session.isoformat(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "receipt_sha256": self.receipt_sha256}


def _request_path(paths: PrivateRuntimePaths) -> Path:
    path = getattr(paths, "manual_event_request_file", None)
    if not isinstance(path, Path):
        _fail("manual_event_request_path_unavailable")
    if path.absolute().parent != paths.root.absolute():
        _fail("manual_event_request_path_invalid")
    return path.absolute()


def _approved_directory(paths: PrivateRuntimePaths) -> Path:
    path = getattr(paths, "manual_event_approved_directory", None)
    if not isinstance(path, Path):
        _fail("manual_event_approved_path_unavailable")
    return path.absolute()


def _receipt_directory(paths: PrivateRuntimePaths) -> Path:
    path = getattr(paths, "manual_event_receipt_directory", None)
    if not isinstance(path, Path):
        _fail("manual_event_receipt_path_unavailable")
    return path.absolute()


def _manual_directory(paths: PrivateRuntimePaths) -> Path:
    path = getattr(paths, "manual_event_directory", None)
    if not isinstance(path, Path):
        _fail("manual_event_directory_path_unavailable")
    return path.absolute()


def _create_owner_directory(directory: Path) -> None:
    try:
        if os.name == "nt":
            secure_create_owner_only_directory(directory, parents=False)
        else:
            directory.mkdir(mode=0o700)
    except (OSError, PrivateWindowsSecurityError) as exc:
        raise ManualOwnerEventError("manual_event_directory_create_failed") from exc


def _validate_control_directory(paths: PrivateRuntimePaths, directory: Path, *, create: bool) -> Path:
    root = validate_existing_private_storage_root(paths)
    manual = _manual_directory(paths)
    if manual.parent != root or manual == root:
        _fail("manual_event_directory_path_invalid")
    if directory not in {manual, _approved_directory(paths), _receipt_directory(paths)}:
        _fail("manual_event_directory_path_invalid")
    if directory != manual and directory.parent != manual:
        _fail("manual_event_directory_path_invalid")
    if create and directory != manual and not os.path.lexists(str(manual)):
        _create_owner_directory(manual)
        _validate_control_directory(paths, manual, create=False)
    if create and not os.path.lexists(str(directory)):
        _create_owner_directory(directory)
    if not os.path.lexists(str(directory)):
        _fail("manual_event_directory_missing")
    metadata = directory.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _fail("manual_event_directory_unsafe")
    if os.name == "nt":
        try:
            verify_owner_only_dacl(directory, require_protected=True)
        except PrivateWindowsSecurityError as exc:
            raise ManualOwnerEventError("manual_event_directory_unsafe") from exc
    elif metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        _fail("manual_event_directory_unsafe")
    return directory


def _read_posix(path: Path, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ManualOwnerEventError("manual_event_control_open_failed") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > max_bytes
        ):
            _fail("manual_event_control_unsafe")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8_192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            _fail("manual_event_control_too_large")
        after = os.stat(path, follow_symlinks=False)
        if not os.path.samestat(before, after):
            _fail("manual_event_control_identity_changed")
        return payload
    finally:
        os.close(descriptor)


def _read_owner_file(path: Path, max_bytes: int) -> bytes:
    if not os.path.lexists(str(path)):
        raise FileNotFoundError
    try:
        return (
            read_owner_only_file(path, max_bytes)
            if os.name == "nt"
            else _read_posix(path, max_bytes)
        )
    except PrivateWindowsSecurityError as exc:
        raise ManualOwnerEventError("manual_event_control_unsafe") from exc


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)))
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
                raise OSError("manual event write made no progress")
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


def _recover_link_publication(path: Path) -> None:
    if not os.path.lexists(str(path)):
        return
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        _fail("manual_event_control_unsafe")
    if metadata.st_nlink == 1:
        return
    if metadata.st_nlink != 2:
        _fail("manual_event_control_unsafe")
    matches: list[Path] = []
    for candidate in path.parent.glob(f".{path.name}.*.tmp"):
        try:
            candidate_metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(candidate_metadata.st_mode) and os.path.samestat(metadata, candidate_metadata):
            matches.append(candidate)
    if len(matches) != 1:
        _fail("manual_event_publication_recovery_ambiguous")
    matches[0].unlink()
    tighten_private_file(path)
    _fsync_directory(path.parent)


def _recover_expected_unlinked_temp(path: Path, payload: bytes) -> bool:
    """Finish a crash left after temp fsync but before no-overwrite linking."""

    if os.path.lexists(str(path)):
        return False
    candidates = tuple(path.parent.glob(f".{path.name}.*.tmp"))
    if not candidates:
        return False
    matching: list[Path] = []
    for candidate in candidates:
        try:
            if _read_owner_file(candidate, _MAX_CONTROL_BYTES) == payload:
                matching.append(candidate)
        except (FileNotFoundError, ManualOwnerEventError):
            _fail("manual_event_publication_recovery_ambiguous")
    if len(candidates) != 1 or len(matching) != 1:
        _fail("manual_event_publication_recovery_ambiguous")
    temporary = matching[0]
    try:
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        tighten_private_file(path)
        _fsync_directory(path.parent)
    except BaseException as exc:
        raise ManualOwnerEventError("manual_event_publication_recovery_failed") from exc
    return True


def _recover_unlinked_control_documents(directory: Path, document_kind: str) -> None:
    """Recover canonical control files left before their final hardlink.

    A process death can occur after the temporary file is fsynced but before
    ``_publish_new`` creates the no-overwrite final name.  Only the exact
    temporary-name shape produced here, an owner-only single-link file, and a
    canonical self-hashed approval or receipt are eligible for recovery.
    Unknown entries remain for the closed directory audit to reject.
    """

    if document_kind == "approval":
        parser = _parse_frozen_approval
    elif document_kind == "receipt":
        parser = _parse_receipt
    else:  # pragma: no cover - internal invariant
        _fail("manual_event_publication_recovery_invalid")
    pattern = re.compile(r"^\.([0-9a-f]{32})\.json\.[0-9a-f]{32}\.tmp$")
    for candidate in tuple(directory.iterdir()):
        match = pattern.fullmatch(candidate.name)
        if match is None:
            continue
        payload = _read_owner_file(candidate, _MAX_CONTROL_BYTES)
        document = parser(payload)
        nonce = getattr(document, "event_nonce", None)
        if nonce != match.group(1):
            _fail("manual_event_publication_recovery_ambiguous")
        destination = directory / f"{nonce}.json"
        if not _recover_expected_unlinked_temp(destination, payload):
            _fail("manual_event_publication_recovery_ambiguous")


def _publish_new(path: Path, payload: bytes) -> None:
    _recover_link_publication(path)
    if os.path.lexists(str(path)):
        _fail("manual_event_publication_conflict")
    if _recover_expected_unlinked_temp(path, payload):
        return
    temporary = _write_temp(path, payload)
    try:
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        tighten_private_file(path)
        _fsync_directory(path.parent)
    except BaseException:
        if os.path.lexists(str(path)):
            try:
                _recover_link_publication(path)
            except BaseException:
                pass
        temporary.unlink(missing_ok=True)
        raise


def _parse_frozen_request_bytes(payload: bytes) -> ManualEventRequest:
    document = _json_document(payload, "manual_event_request_json_invalid")
    _closed(
        document,
        {"contract_version", "event_kind", "event_nonce", "occurred_at", "payload", "session"},
        "manual_event_request_schema_invalid",
    )
    if document["contract_version"] != REQUEST_CONTRACT_VERSION:
        _fail("manual_event_request_contract_unsupported")
    nonce = document["event_nonce"]
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        _fail("manual_event_nonce_invalid")
    kind = document["event_kind"]
    if not isinstance(kind, str) or kind not in _EVENT_KINDS:
        _fail("manual_event_kind_invalid")
    session = _parse_date(document["session"], "manual_event_session_invalid")
    occurred_at = _parse_time(document["occurred_at"], "manual_event_time_invalid", optional=True)
    normalized_payload = _normalize_frozen_payload(
        kind,
        _mapping(document["payload"], "manual_event_payload_invalid"),
        occurred_at,
    )
    return ManualEventRequest(
        event_nonce=nonce,
        event_kind=kind,
        session=session,
        occurred_at=occurred_at,
        payload=normalized_payload,
        request_bytes_sha256=_sha256(payload),
        raw_bytes=payload,
    )


def _parse_request_bytes(
    payload: bytes,
    config: PrivateDailyRuntimeConfig,
) -> ManualEventRequest:
    frozen = _parse_frozen_request_bytes(payload)
    normalized_payload = _normalize_payload(
        frozen.event_kind,
        frozen.payload,
        config,
        frozen.occurred_at,
    )
    return ManualEventRequest(
        event_nonce=frozen.event_nonce,
        event_kind=frozen.event_kind,
        session=frozen.session,
        occurred_at=frozen.occurred_at,
        payload=normalized_payload,
        request_bytes_sha256=frozen.request_bytes_sha256,
        raw_bytes=payload,
    )


def load_manual_event_request(
    config: PrivateDailyRuntimeConfig,
    paths: PrivateRuntimePaths,
) -> ManualEventRequest:
    """Load and normalize the fixed owner-only request without mutating it."""

    validate_existing_private_storage_root(paths)
    try:
        payload = _read_owner_file(_request_path(paths), _MAX_REQUEST_BYTES)
    except FileNotFoundError as exc:
        raise ManualOwnerEventError("manual_event_request_missing") from exc
    return _parse_request_bytes(payload, config)


def interactive_manual_event_presence(
    request: ManualEventRequest,
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    challenge_factory=None,
) -> ManualEventPresenceProof:
    """Bind exact request bytes to a random, interactive TTY response."""

    if not isinstance(request, ManualEventRequest):
        _fail("manual_event_request_required")
    if not input_stream.isatty() or not output_stream.isatty():
        _fail("manual_event_owner_tty_required")
    factory = challenge_factory or (
        lambda: "".join(secrets.choice(_CHALLENGE_ALPHABET) for _ in range(10))
    )
    challenge = str(factory())
    if not _CHALLENGE.fullmatch(challenge):
        _fail("manual_event_owner_challenge_invalid")
    prefix = request.request_bytes_sha256[:8]
    output_stream.write(
        f"Review {request.event_kind} for {request.session.isoformat()} [{prefix}].\n"
    )
    output_stream.write(f"Type CONFIRM {challenge} {prefix} to approve it: ")
    output_stream.flush()
    response = input_stream.readline(96).rstrip("\r\n")
    if response != f"CONFIRM {challenge} {prefix}":
        _fail("manual_event_owner_challenge_rejected")
    return ManualEventPresenceProof(
        request_bytes_sha256=request.request_bytes_sha256,
        event_nonce=request.event_nonce,
        _sentinel=_PRESENCE_SENTINEL,
    )


def _approval_path(paths: PrivateRuntimePaths, nonce: str) -> Path:
    if not _NONCE.fullmatch(nonce):
        _fail("manual_event_nonce_invalid")
    return _approved_directory(paths) / f"{nonce}.json"


def _receipt_path(paths: PrivateRuntimePaths, nonce: str) -> Path:
    if not _NONCE.fullmatch(nonce):
        _fail("manual_event_nonce_invalid")
    return _receipt_directory(paths) / f"{nonce}.json"


def _parse_frozen_approval(payload: bytes) -> ManualEventApproval:
    document = _json_document(payload, "manual_event_approval_json_invalid")
    _closed(
        document,
        {
            "approval_sha256",
            "approved_at",
            "config_identity_sha256",
            "confirmation_method",
            "contract_version",
            "request",
            "request_bytes_sha256",
        },
        "manual_event_approval_schema_invalid",
    )
    if document["contract_version"] != APPROVAL_CONTRACT_VERSION:
        _fail("manual_event_approval_contract_unsupported")
    if document["confirmation_method"] != CONFIRMATION_METHOD:
        _fail("manual_event_confirmation_method_invalid")
    request_digest = document["request_bytes_sha256"]
    if not isinstance(request_digest, str) or not _SHA256.fullmatch(request_digest):
        _fail("manual_event_request_digest_invalid")
    request_body = _mapping(document["request"], "manual_event_embedded_request_invalid")
    parsed = _parse_frozen_request_bytes(_canonical_bytes(request_body))
    request = ManualEventRequest(
        event_nonce=parsed.event_nonce,
        event_kind=parsed.event_kind,
        session=parsed.session,
        occurred_at=parsed.occurred_at,
        payload=parsed.payload,
        request_bytes_sha256=request_digest,
        raw_bytes=None,
    )
    approval = ManualEventApproval(
        request=request,
        approved_at=_parse_time(document["approved_at"], "manual_event_approval_time_invalid"),
        config_identity_sha256=str(document["config_identity_sha256"]),
        approval_sha256=str(document["approval_sha256"]),
    )
    if payload != _canonical_bytes(approval.to_dict()):
        _fail("manual_event_approval_not_canonical")
    return approval


def _approval_for_current_config(
    approval: ManualEventApproval,
    config: PrivateDailyRuntimeConfig,
) -> ManualEventApproval:
    if approval.config_identity_sha256 != _config_identity(config):
        _fail("manual_event_approval_config_mismatch")
    parsed = _parse_request_bytes(_canonical_bytes(approval.request.body()), config)
    request = ManualEventRequest(
        event_nonce=parsed.event_nonce,
        event_kind=parsed.event_kind,
        session=parsed.session,
        occurred_at=parsed.occurred_at,
        payload=parsed.payload,
        request_bytes_sha256=approval.request.request_bytes_sha256,
        raw_bytes=None,
    )
    return ManualEventApproval(
        request=request,
        approved_at=approval.approved_at,
        config_identity_sha256=approval.config_identity_sha256,
        approval_sha256=approval.approval_sha256,
    )


def _parse_approval(
    payload: bytes,
    config: PrivateDailyRuntimeConfig,
) -> ManualEventApproval:
    return _approval_for_current_config(_parse_frozen_approval(payload), config)


def approve_manual_event(
    config: PrivateDailyRuntimeConfig,
    paths: PrivateRuntimePaths,
    request: ManualEventRequest,
    owner_presence: ManualEventPresenceProof,
    clock,
) -> ManualEventApproval:
    """Re-read exact bytes and publish one immutable, self-hashed approval."""

    if (
        not isinstance(owner_presence, ManualEventPresenceProof)
        or owner_presence._sentinel is not _PRESENCE_SENTINEL
        or owner_presence.request_bytes_sha256 != request.request_bytes_sha256
        or owner_presence.event_nonce != request.event_nonce
    ):
        _fail("manual_event_owner_presence_required")
    current = load_manual_event_request(config, paths)
    if (
        current.request_bytes_sha256 != request.request_bytes_sha256
        or current.event_nonce != request.event_nonce
        or current.body() != request.body()
    ):
        _fail("manual_event_request_changed_after_confirmation")
    approved_dir = _validate_control_directory(paths, _approved_directory(paths), create=True)
    _validate_control_directory(paths, _receipt_directory(paths), create=True)
    _recover_directory_links(approved_dir)
    _recover_unlinked_control_documents(approved_dir, "approval")
    approval = ManualEventApproval(
        request=current,
        approved_at=_aware_utc(clock(), "manual_event_approval_clock_invalid"),
        config_identity_sha256=_config_identity(config),
    )
    destination = approved_dir / f"{current.event_nonce}.json"
    receipt_destination = _receipt_directory(paths) / f"{current.event_nonce}.json"
    encoded = _canonical_bytes(approval.to_dict())
    _recover_link_publication(destination)
    if not os.path.lexists(str(destination)) and os.path.lexists(str(receipt_destination)):
        _fail("manual_event_receipt_without_approval")
    if os.path.lexists(str(destination)):
        existing = _parse_approval(_read_owner_file(destination, _MAX_CONTROL_BYTES), config)
        if existing.approval_sha256 == approval.approval_sha256:
            return existing
        if (
            existing.request.request_bytes_sha256 == current.request_bytes_sha256
            and existing.request.body() == current.body()
        ):
            # Retry at a later clock instant retains the first durable approval.
            return existing
        _fail("manual_event_nonce_reused_with_different_content")
    try:
        _publish_new(destination, encoded)
    except ManualOwnerEventError:
        raise
    except BaseException as exc:
        raise ManualOwnerEventError("manual_event_approval_persistence_failed") from exc
    return approval


def _parse_receipt(payload: bytes) -> ManualEventReceipt:
    document = _json_document(payload, "manual_event_receipt_json_invalid")
    _closed(
        document,
        {
            "approval_sha256",
            "contract_version",
            "event_nonce",
            "ledger_event_hash",
            "ledger_event_id",
            "ledger_event_type",
            "ledger_idempotency_key",
            "receipt_sha256",
            "recorded_at",
            "session",
        },
        "manual_event_receipt_schema_invalid",
    )
    if document["contract_version"] != RECEIPT_CONTRACT_VERSION:
        _fail("manual_event_receipt_contract_unsupported")
    receipt = ManualEventReceipt(
        event_nonce=str(document["event_nonce"]),
        approval_sha256=str(document["approval_sha256"]),
        ledger_event_id=str(document["ledger_event_id"]),
        ledger_event_hash=str(document["ledger_event_hash"]),
        ledger_idempotency_key=str(document["ledger_idempotency_key"]),
        ledger_event_type=str(document["ledger_event_type"]),
        session=_parse_date(document["session"], "manual_event_receipt_session_invalid"),
        recorded_at=_parse_time(document["recorded_at"], "manual_event_receipt_time_invalid"),
        receipt_sha256=str(document["receipt_sha256"]),
    )
    if payload != _canonical_bytes(receipt.to_dict()):
        _fail("manual_event_receipt_not_canonical")
    return receipt


def _checkpoint_value(checkpoint: Any, name: str) -> Any:
    if isinstance(checkpoint, Mapping):
        return checkpoint.get(name)
    return getattr(checkpoint, name, None)


def _expected_ledger_type(kind: str) -> str:
    return {
        "confirmed_fill": "user_confirmed_fill",
        "cash_flow": "cash_flow",
        "fee": "fee",
        "income": "income",
        "split": "split",
        "skip_dca": "dca_override",
    }[kind]


def _receipt_matches_checkpoint(
    receipt: ManualEventReceipt,
    approval: ManualEventApproval,
    checkpoint: Any,
) -> bool:
    if checkpoint is None:
        return False
    session = _checkpoint_value(checkpoint, "session")
    if isinstance(session, str):
        try:
            session = dt.date.fromisoformat(session)
        except ValueError:
            return False
    return (
        receipt.event_nonce == approval.event_nonce
        and receipt.approval_sha256 == approval.approval_sha256
        and _checkpoint_value(checkpoint, "event_id") == receipt.ledger_event_id
        and _checkpoint_value(checkpoint, "event_hash") == receipt.ledger_event_hash
        and _checkpoint_value(checkpoint, "idempotency_key") == receipt.ledger_idempotency_key
        and _checkpoint_value(checkpoint, "event_type") == receipt.ledger_event_type
        and receipt.ledger_event_type == _expected_ledger_type(approval.event_kind)
        and session == receipt.session == approval.session
    )


def _directory_documents(directory: Path) -> tuple[Path, ...]:
    results: list[Path] = []
    for candidate in directory.iterdir():
        if candidate.name.startswith(".") or not re.fullmatch(r"[0-9a-f]{32}\.json", candidate.name):
            _fail("manual_event_directory_contains_unknown_entry")
        results.append(candidate)
    return tuple(sorted(results, key=lambda item: item.name))


def _recover_directory_links(directory: Path) -> None:
    # A crash after os.link but before unlink leaves both the final name and
    # its same-inode temp name.  Recover final names before the closed
    # directory-entry audit rejects any remaining hidden file.
    for candidate in directory.iterdir():
        if re.fullmatch(r"[0-9a-f]{32}\.json", candidate.name):
            _recover_link_publication(candidate)


def load_manual_event_queue(
    config: PrivateDailyRuntimeConfig,
    paths: PrivateRuntimePaths,
    ledger_event_lookup: Callable[[str], Any | None],
    latest_valuation_session: dt.date | str | None,
) -> tuple[ManualEventApproval, ...]:
    """Audit every approval/receipt and return pending items deterministically."""

    validate_existing_private_storage_root(paths)
    request_exists = os.path.lexists(str(_request_path(paths)))
    manual_directory = _manual_directory(paths)
    if not os.path.lexists(str(manual_directory)):
        if request_exists:
            _fail("manual_event_request_requires_confirmation")
        return ()
    _validate_control_directory(paths, manual_directory, create=False)
    approved_exists = os.path.lexists(str(_approved_directory(paths)))
    receipt_exists = os.path.lexists(str(_receipt_directory(paths)))
    if not approved_exists and not receipt_exists:
        if tuple(manual_directory.iterdir()):
            _fail("manual_event_directory_contains_unknown_entry")
        if request_exists:
            _fail("manual_event_request_requires_confirmation")
        return ()
    if approved_exists != receipt_exists:
        _fail("manual_event_directory_incomplete")
    approved_dir = _validate_control_directory(paths, _approved_directory(paths), create=False)
    receipt_dir = _validate_control_directory(paths, _receipt_directory(paths), create=False)
    _recover_directory_links(approved_dir)
    _recover_directory_links(receipt_dir)
    _recover_unlinked_control_documents(approved_dir, "approval")
    _recover_unlinked_control_documents(receipt_dir, "receipt")
    approvals: dict[str, ManualEventApproval] = {}
    for path in _directory_documents(approved_dir):
        # Consumed history is a frozen v1 audit contract.  Current config is
        # applied only after receipts identify the still-pending subset.
        approval = _parse_frozen_approval(
            _read_owner_file(path, _MAX_CONTROL_BYTES)
        )
        if path.name != f"{approval.event_nonce}.json" or approval.event_nonce in approvals:
            _fail("manual_event_approval_identity_conflict")
        approvals[approval.event_nonce] = approval
    receipts: dict[str, ManualEventReceipt] = {}
    for path in _directory_documents(receipt_dir):
        receipt = _parse_receipt(_read_owner_file(path, _MAX_CONTROL_BYTES))
        if path.name != f"{receipt.event_nonce}.json" or receipt.event_nonce in receipts:
            _fail("manual_event_receipt_identity_conflict")
        approval = approvals.get(receipt.event_nonce)
        if approval is None:
            _fail("manual_event_receipt_without_approval")
        checkpoint = ledger_event_lookup(receipt.ledger_event_id)
        if not _receipt_matches_checkpoint(receipt, approval, checkpoint):
            _fail("manual_event_receipt_binding_failed")
        receipts[receipt.event_nonce] = receipt

    if request_exists:
        request = _parse_frozen_request_bytes(
            _read_owner_file(_request_path(paths), _MAX_REQUEST_BYTES)
        )
        existing = approvals.get(request.event_nonce)
        if (
            existing is None
            or existing.request.request_bytes_sha256 != request.request_bytes_sha256
            or existing.request.body() != request.body()
        ):
            _fail("manual_event_request_requires_confirmation")

    latest = None
    if latest_valuation_session is not None:
        latest = (
            latest_valuation_session
            if isinstance(latest_valuation_session, dt.date)
            else _parse_date(latest_valuation_session, "manual_event_latest_valuation_invalid")
        )
        if isinstance(latest, dt.datetime):
            _fail("manual_event_latest_valuation_invalid")
    pending = [
        _approval_for_current_config(approval, config)
        for nonce, approval in approvals.items()
        if nonce not in receipts
    ]
    if latest is not None and any(item.session <= latest for item in pending):
        _fail("manual_event_after_valuation_finality")
    return tuple(
        sorted(
            pending,
            key=lambda item: (
                item.session,
                1 if item.phase == "post_dca_replacement" else 0,
                item.request.occurred_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                item.event_nonce,
            ),
        )
    )


def _ledger_lookup(ledger: Any, event_id: str) -> Any:
    for name in ("event_checkpoint", "ledger_event_checkpoint", "lookup_event"):
        method = getattr(ledger, name, None)
        if callable(method):
            return method(event_id)
    _fail("manual_event_ledger_lookup_unavailable")


def record_approved_event(
    approval: ManualEventApproval,
    ledger: Any,
    config: PrivateDailyRuntimeConfig,
    *,
    modeled_replacement_event_id: str | None = None,
    clock,
) -> ManualEventReceipt:
    """Record one approved event through existing public ledger methods."""

    if not isinstance(approval, ManualEventApproval):
        _fail("manual_event_approval_required")
    if approval.config_identity_sha256 != _config_identity(config):
        _fail("manual_event_approval_config_mismatch")
    request = approval.request
    payload = request.payload
    key = f"manual-owner-event/v1:{request.event_nonce}:{approval.approval_sha256}"
    occurred = request.occurred_at
    if request.modeled_dca_replacement:
        if not isinstance(modeled_replacement_event_id, str) or not _SHA256.fullmatch(
            modeled_replacement_event_id
        ):
            _fail("manual_event_replacement_target_required")
    elif modeled_replacement_event_id is not None:
        _fail("manual_event_replacement_target_unexpected")

    if request.event_kind == "confirmed_fill":
        event_id = ledger.record_user_confirmed_fill(
            request.session,
            payload["symbol"],
            payload["side"],
            Decimal(payload["quantity"]),
            Decimal(payload["price"]),
            fees=Decimal(payload["fees"]),
            occurred_at=occurred,
            idempotency_key=key,
            replaces_modeled_event_id=modeled_replacement_event_id,
        )
    elif request.event_kind == "cash_flow":
        event_id = ledger.record_cash_flow(
            request.session,
            Decimal(payload["amount"]),
            description=payload["description"],
            occurred_at=occurred,
            valuation_weight=(
                None
                if payload["valuation_weight"] is None
                else Decimal(payload["valuation_weight"])
            ),
            idempotency_key=key,
        )
    elif request.event_kind == "fee":
        event_id = ledger.record_fee(
            request.session,
            Decimal(payload["amount"]),
            description=payload["description"],
            occurred_at=occurred,
            idempotency_key=key,
        )
    elif request.event_kind == "income":
        event_id = ledger.record_income(
            request.session,
            Decimal(payload["amount"]),
            symbol=payload["symbol"],
            description=payload["description"],
            occurred_at=occurred,
            idempotency_key=key,
        )
    elif request.event_kind == "split":
        event_id = ledger.record_split(
            request.session,
            payload["symbol"],
            Decimal(payload["ratio"]),
            occurred_at=occurred,
            idempotency_key=key,
        )
    elif request.event_kind == "skip_dca":
        event_id = ledger.record_dca_override(
            request.session,
            payload["plan_id"],
            payload["plan_version"],
            action="skip",
            reason=payload["reason"],
            idempotency_key=key,
        )
    else:  # pragma: no cover - dataclass invariant
        _fail("manual_event_kind_invalid")

    checkpoint = _ledger_lookup(ledger, event_id)
    if checkpoint is None:
        _fail("manual_event_ledger_event_missing_after_commit")
    event_hash = _checkpoint_value(checkpoint, "event_hash")
    checkpoint_key = _checkpoint_value(checkpoint, "idempotency_key")
    event_type = _checkpoint_value(checkpoint, "event_type")
    checkpoint_session = _checkpoint_value(checkpoint, "session")
    if isinstance(checkpoint_session, str):
        checkpoint_session = _parse_date(checkpoint_session, "manual_event_ledger_session_invalid")
    if (
        _checkpoint_value(checkpoint, "event_id") != event_id
        or not isinstance(event_hash, str)
        or not _SHA256.fullmatch(event_hash)
        or checkpoint_key != key
        or event_type != _expected_ledger_type(request.event_kind)
        or checkpoint_session != request.session
    ):
        _fail("manual_event_ledger_receipt_mismatch")
    return ManualEventReceipt(
        event_nonce=request.event_nonce,
        approval_sha256=approval.approval_sha256,
        ledger_event_id=event_id,
        ledger_event_hash=event_hash,
        ledger_idempotency_key=key,
        ledger_event_type=event_type,
        session=request.session,
        recorded_at=_aware_utc(clock(), "manual_event_receipt_clock_invalid"),
    )


def publish_manual_event_receipt(
    paths: PrivateRuntimePaths,
    receipt: ManualEventReceipt,
) -> ManualEventReceipt:
    """Publish one canonical receipt, recovering identical retries safely."""

    if not isinstance(receipt, ManualEventReceipt):
        _fail("manual_event_receipt_required")
    directory = _validate_control_directory(paths, _receipt_directory(paths), create=True)
    _recover_directory_links(directory)
    _recover_unlinked_control_documents(directory, "receipt")
    destination = directory / f"{receipt.event_nonce}.json"
    encoded = _canonical_bytes(receipt.to_dict())
    _recover_link_publication(destination)
    if os.path.lexists(str(destination)):
        existing = _parse_receipt(_read_owner_file(destination, _MAX_CONTROL_BYTES))
        if existing.receipt_sha256 == receipt.receipt_sha256:
            return existing
        _fail("manual_event_receipt_conflict")
    try:
        _publish_new(destination, encoded)
    except ManualOwnerEventError:
        raise
    except BaseException as exc:
        raise ManualOwnerEventError("manual_event_receipt_persistence_failed") from exc
    return receipt


__all__ = [
    "APPROVAL_CONTRACT_VERSION",
    "CONFIRMATION_METHOD",
    "ManualEventApproval",
    "ManualEventPresenceProof",
    "ManualEventReceipt",
    "ManualEventRequest",
    "ManualOwnerEventError",
    "RECEIPT_CONTRACT_VERSION",
    "REQUEST_CONTRACT_VERSION",
    "approve_manual_event",
    "interactive_manual_event_presence",
    "load_manual_event_queue",
    "load_manual_event_request",
    "publish_manual_event_receipt",
    "record_approved_event",
]
