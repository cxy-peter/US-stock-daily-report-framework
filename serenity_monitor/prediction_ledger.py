"""Private, point-in-time prediction research ledger.

The ledger records sanitized signal metadata and later research outcomes.  It is
deliberately disconnected from brokers, orders, and portfolio execution.  All
events are immutable, canonically encoded, and linked by a SHA-256 hash chain.
Corrections are represented by explicit reversal events.

Raw posts, account names, URLs, credentials, and binary floating-point numbers
are outside this module's persistence contract.  Callers must provide only an
irreversible author digest and content/evidence digests for which they have a
recorded right-to-use basis.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Sequence

from .provider_registry import AcceptedClose, CloseAcceptancePolicy
from .trading_calendar import ExchangeSessionError, ExchangeSessionResolver


ZERO = Decimal("0")
ONE = Decimal("1")
_GENESIS_HASH = "0" * 64
_REQUIRED_HORIZONS = (1, 5, 20, 60)
_DIRECTIONS = {"bullish": 1, "bearish": -1}
_WEIGHT_STATES = frozenset({"active", "decayed", "quarantined", "research_only"})
_SAFE_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
_SAFE_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,31}")
_TOPIC_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_DEFAULT_ALLOWED_TOPICS = (
    "broad_market",
    "crypto_assets",
    "dividend_equity",
    "nasdaq_100",
    "semiconductors",
    "sp_500",
)
_SUPPORTED_US_EQUITY_MICS = frozenset({"XNYS", "XNAS", "ARCX", "BATS"})
_SUPPORTED_ASSET_TYPES = frozenset({"equity", "etf", "fund", "index"})
_SETTLEMENT_SOURCE_TIERS = frozenset({"primary", "secondary", "contracted_api"})
_TRUSTED_CALENDAR_NAME = "XNYS"
_TRUSTED_EXCHANGE_TIMEZONE = "America/New_York"
_CALENDAR_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_PLATFORMS = frozenset({"reddit", "x", "xiaohongshu", "cross_platform", "other_authorized"})
_SOURCE_CATEGORIES = frozenset(
    {"authorized_export", "licensed_dataset", "manual_derived", "platform_archive", "public_api"}
)
_MARKET_REGIMES = frozenset({"neutral", "risk_off", "risk_on", "stress", "transition"})
_RIGHTS_BASES = frozenset(
    {"authorized_export", "licensed_dataset", "public_api", "public_web_derived", "user_provided"}
)
PREDICTION_WEIGHT_TOPICS = frozenset(_DEFAULT_ALLOWED_TOPICS)
PREDICTION_WEIGHT_MARKET_REGIMES = frozenset(_MARKET_REGIMES)
PREDICTION_WEIGHT_MODEL_VERSIONS = frozenset({"social-v1", "social-v2"})
PREDICTION_WEIGHT_REASON_CODES = frozenset(
    {
        "minimum_samples_not_met",
        "minimum_recent_samples_not_met",
        "recent_hit_rate_failed",
        "recent_brier_failed",
        "hit_rate_distribution_drift",
        "recent_hit_rate_weak",
        "recent_brier_weak",
        "hit_rate_drift",
        "calibration_healthy",
    }
)
_MODEL_VERSION = re.compile(
    r"(?:cross-platform|prediction|research|social|social-heat|xhs)-v[0-9]+(?:[._-][a-z0-9]+)*"
)
_FACTOR_MODEL_VERSION = re.compile(
    r"(?:barra|factor|ff3|ff5|q-factor|risk)-v[0-9]+(?:[._-][a-z0-9]+)*"
)
_REVERSAL_REASON_CODES = frozenset(
    {
        "backdated_reversal",
        "corporate_action_revision",
        "different_reason",
        "duplicate_signal",
        "factor_model_revision",
        "later_correction",
        "lineage_recheck",
        "provider_correction",
        "rights_revoked",
        "signal_metadata_error",
        "wrong_close_lineage",
    }
)
_EVENT_COLUMNS = (
    "sequence_no",
    "event_id",
    "idempotency_hash",
    "session_date",
    "occurred_at",
    "event_type",
    "payload_json",
    "previous_hash",
    "event_hash",
    "created_at",
)
_PRIVATE_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_CHECKPOINT_SCHEMA = "prediction-ledger-checkpoint/v2"
_TRIGGER_UPDATE_SQL = """CREATE TRIGGER prediction_events_no_update
                BEFORE UPDATE ON prediction_events
                BEGIN
                    SELECT RAISE(ABORT, 'prediction_events are append-only');
                END"""
_TRIGGER_DELETE_SQL = """CREATE TRIGGER prediction_events_no_delete
                BEFORE DELETE ON prediction_events
                BEGIN
                    SELECT RAISE(ABORT, 'prediction_events are append-only');
                END"""
_FORBIDDEN_KEYS = frozenset(
    {
        "raw",
        "raw_content",
        "content",
        "body",
        "text",
        "post",
        "username",
        "user_name",
        "handle",
        "author_name",
        "url",
        "uri",
        "query",
        "query_string",
        "secret",
        "token",
        "api_key",
        "password",
        "cookie",
    }
)
_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


class PredictionLedgerError(Exception):
    """Base class for private prediction-ledger failures."""


class PredictionValidationError(PredictionLedgerError, ValueError):
    """Raised when an input violates the point-in-time or privacy contract."""


class PredictionIntegrityError(PredictionLedgerError):
    """Raised when persisted events or their semantic chain do not verify."""


class PredictionIdempotencyConflict(PredictionLedgerError):
    """Raised when an immutable identity is reused for different content."""


class PredictionSettlementBlocked(PredictionLedgerError):
    """Raised when accepted-close or future-information gates do not pass."""


class PredictionCommitUnknown(PredictionLedgerError):
    """SQLite may have committed; an idempotent retry must recover the checkpoint."""


@dataclass(frozen=True)
class PredictionLedgerPolicy:
    """Numerical and research-weight policy; no field permits trading."""

    busy_timeout_ms: int = 5_000
    minimum_samples: int = 20
    recent_window: int = 10
    minimum_recent_samples: int = 5
    decay_hit_rate: Decimal = Decimal("0.50")
    quarantine_hit_rate: Decimal = Decimal("0.35")
    decay_brier: Decimal = Decimal("0.27")
    quarantine_brier: Decimal = Decimal("0.36")
    decay_hit_rate_drop: Decimal = Decimal("0.15")
    quarantine_hit_rate_drop: Decimal = Decimal("0.30")
    allowed_topics: tuple[str, ...] = _DEFAULT_ALLOWED_TOPICS

    def __post_init__(self) -> None:
        for field_name in (
            "busy_timeout_ms",
            "minimum_samples",
            "recent_window",
            "minimum_recent_samples",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise PredictionValidationError(f"{field_name} must be a positive integer")
        if self.minimum_recent_samples > self.recent_window:
            raise PredictionValidationError("minimum_recent_samples may not exceed recent_window")
        for field_name in (
            "decay_hit_rate",
            "quarantine_hit_rate",
            "decay_brier",
            "quarantine_brier",
            "decay_hit_rate_drop",
            "quarantine_hit_rate_drop",
        ):
            value = _decimal(getattr(self, field_name), field_name, minimum=ZERO, maximum=ONE)
            object.__setattr__(self, field_name, value)
        if self.quarantine_hit_rate > self.decay_hit_rate:
            raise PredictionValidationError("quarantine hit threshold must not exceed decay threshold")
        if self.quarantine_brier < self.decay_brier:
            raise PredictionValidationError("quarantine Brier threshold must not be below decay threshold")
        if self.quarantine_hit_rate_drop < self.decay_hit_rate_drop:
            raise PredictionValidationError("quarantine drift threshold must not be below decay threshold")
        if isinstance(self.allowed_topics, (str, bytes)):
            raise PredictionValidationError("allowed_topics must be a closed topic sequence")
        try:
            normalized_topics = tuple(_topic(item) for item in self.allowed_topics)
        except TypeError as exc:
            raise PredictionValidationError("allowed_topics must be a closed topic sequence") from exc
        if not normalized_topics:
            raise PredictionValidationError("allowed_topics may not be empty")
        if len(normalized_topics) != len(set(normalized_topics)):
            raise PredictionValidationError("allowed_topics may not contain duplicates")
        if not set(normalized_topics).issubset(_DEFAULT_ALLOWED_TOPICS):
            raise PredictionValidationError(
                "allowed_topics may only narrow the built-in closed taxonomy"
            )
        object.__setattr__(self, "allowed_topics", tuple(sorted(normalized_topics)))


@dataclass(frozen=True)
class RightsLineage:
    """Sanitized proof that the derived evidence may be used."""

    basis: str
    attestation_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "basis", _closed_identifier(self.basis, "rights basis", _RIGHTS_BASES))
        object.__setattr__(
            self,
            "attestation_sha256",
            _digest(self.attestation_sha256, "rights attestation"),
        )


@dataclass(frozen=True)
class PredictionSignal:
    """One first-observed signal stripped of raw social-media content."""

    first_observed_at: dt.datetime | str
    observation_session: dt.date | str
    platform: str
    source_category: str
    author_id_sha256: str
    topic: str
    valuation_symbol: str
    valuation_exchange_mic: str
    valuation_currency: str
    direction: str
    strength: Decimal
    probability: Decimal
    horizon_sessions: Mapping[int, dt.date | str]
    market_regime: str
    model_version: str
    evidence_sha256: Sequence[str]
    rights: RightsLineage
    ticker: str | None = None
    calendar_name: str = field(init=False)
    calendar_version: str = field(init=False)
    exchange_timezone: str = field(init=False)
    observation_session_close: str = field(init=False)
    session_path: Mapping[int, dt.date] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        first_observed_at = _aware_text(self.first_observed_at, "first_observed_at")
        observation_session = _session(self.observation_session, "observation_session")
        resolver = ExchangeSessionResolver()
        try:
            provenance = resolver.provenance(self.valuation_exchange_mic)
            valuation_exchange_mic = provenance.instrument_mic
            session_close = resolver.session_close(observation_session, valuation_exchange_mic)
            session_path = resolver.future_session_offsets(
                observation_session,
                list(range(1, max(_REQUIRED_HORIZONS) + 1)),
                valuation_exchange_mic,
            )
        except ExchangeSessionError as exc:
            raise PredictionValidationError("valuation exchange session contract is invalid") from exc
        if session_close >= _parse_aware(first_observed_at, "first_observed_at"):
            raise PredictionValidationError(
                "observation_session must have officially closed before first_observed_at"
            )
        valuation_currency = _currency(self.valuation_currency)
        if valuation_currency != "USD":
            raise PredictionValidationError(
                "valuation_currency must be USD for the supported U.S. equity calendars"
            )
        direction = str(self.direction).strip().lower()
        if direction not in _DIRECTIONS:
            raise PredictionValidationError("direction must be bullish or bearish")
        strength = _decimal(self.strength, "strength", positive=True, maximum=ONE)
        probability = _decimal(self.probability, "probability", minimum=ZERO, maximum=ONE)

        if not isinstance(self.horizon_sessions, Mapping):
            raise PredictionValidationError("horizon_sessions must be a mapping")
        normalized_horizons: dict[int, dt.date] = {}
        for raw_horizon, raw_session in self.horizon_sessions.items():
            if isinstance(raw_horizon, bool) or not isinstance(raw_horizon, int):
                raise PredictionValidationError("horizon keys must be integer trading-session counts")
            if raw_horizon in normalized_horizons:
                raise PredictionValidationError("duplicate horizon")
            normalized_horizons[raw_horizon] = _session(
                raw_session, f"horizon_sessions[{raw_horizon}]"
            )
        if tuple(sorted(normalized_horizons)) != _REQUIRED_HORIZONS:
            raise PredictionValidationError("horizons must be exactly 1, 5, 20, and 60 trading sessions")
        for horizon in _REQUIRED_HORIZONS:
            target = normalized_horizons[horizon]
            if target != session_path[horizon]:
                raise PredictionValidationError(
                    "horizon sessions must match trusted exchange-session offsets"
                )

        evidence = tuple(sorted({_digest(item, "evidence lineage") for item in self.evidence_sha256}))
        if not evidence:
            raise PredictionValidationError("at least one evidence lineage digest is required")
        if not isinstance(self.rights, RightsLineage):
            raise PredictionValidationError("rights must be RightsLineage")

        ticker = None if self.ticker is None else _symbol(self.ticker, "ticker")
        valuation_symbol = _symbol(self.valuation_symbol, "valuation_symbol")
        object.__setattr__(self, "first_observed_at", first_observed_at)
        object.__setattr__(self, "observation_session", observation_session)
        object.__setattr__(self, "platform", _closed_identifier(self.platform, "platform", _PLATFORMS))
        object.__setattr__(
            self,
            "source_category",
            _closed_identifier(self.source_category, "source_category", _SOURCE_CATEGORIES),
        )
        object.__setattr__(self, "author_id_sha256", _digest(self.author_id_sha256, "author id"))
        object.__setattr__(self, "topic", _topic(self.topic))
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "valuation_symbol", valuation_symbol)
        object.__setattr__(self, "valuation_exchange_mic", valuation_exchange_mic)
        object.__setattr__(self, "valuation_currency", valuation_currency)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "strength", strength)
        object.__setattr__(self, "probability", probability)
        object.__setattr__(
            self,
            "horizon_sessions",
            MappingProxyType(dict(sorted(normalized_horizons.items()))),
        )
        object.__setattr__(
            self,
            "market_regime",
            _closed_identifier(self.market_regime, "market_regime", _MARKET_REGIMES),
        )
        object.__setattr__(self, "model_version", _prediction_model_version(self.model_version))
        object.__setattr__(self, "evidence_sha256", evidence)
        object.__setattr__(self, "calendar_name", provenance.calendar_name)
        object.__setattr__(self, "calendar_version", provenance.calendar_version)
        object.__setattr__(self, "exchange_timezone", provenance.exchange_timezone)
        object.__setattr__(self, "observation_session_close", _aware_text(session_close, "session close"))
        object.__setattr__(
            self,
            "session_path",
            MappingProxyType(dict(sorted(session_path.items()))),
        )


@dataclass(frozen=True)
class _RecordedSignal:
    """Validated signal draft plus its immutable accepted-close price anchor."""

    draft: PredictionSignal
    reference_close: Mapping[str, Any]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.draft, name)

    @property
    def reference_price(self) -> Decimal:
        return _decimal(self.reference_close["price"], "reference close price", positive=True)


@dataclass(frozen=True)
class FactorResidualEvidence:
    """Optional point-in-time residual-return output from a versioned factor model."""

    residual_return: Decimal
    as_of: dt.datetime | str
    model_version: str
    lineage_sha256: Sequence[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "residual_return", _decimal(self.residual_return, "residual_return"))
        object.__setattr__(self, "as_of", _aware_text(self.as_of, "factor residual as_of"))
        object.__setattr__(self, "model_version", _factor_model_version(self.model_version))
        lineage = tuple(sorted({_digest(item, "factor lineage") for item in self.lineage_sha256}))
        if not lineage:
            raise PredictionValidationError("factor residual evidence requires lineage")
        object.__setattr__(self, "lineage_sha256", lineage)


@dataclass(frozen=True)
class PredictionEventReceipt:
    event_id: str
    event_hash: str
    idempotent_replay: bool


@dataclass(frozen=True)
class PredictionOutcome:
    settlement_id: str
    settlement_event_hash: str
    signal_id: str
    platform: str
    topic: str
    model_version: str
    market_regime: str
    horizon: int
    target_session: dt.date
    direction: str
    strength: Decimal
    probability: Decimal
    raw_return: Decimal
    residual_return: Decimal | None
    direction_hit: bool
    mfe: Decimal
    mae: Decimal
    brier: Decimal
    settled_at: str
    recording_mode: str
    calibration_eligible: bool


@dataclass(frozen=True)
class CalibrationSummary:
    platform: str
    topic: str
    model_version: str
    market_regime: str
    horizon: int
    sample_scope: str
    sample_count: int
    hit_rate: Decimal | None
    mean_residual_return: Decimal | None
    residual_sample_count: int
    brier: Decimal | None
    rank_ic: Decimal | None


@dataclass(frozen=True)
class PredictionWeightState:
    platform: str
    topic: str
    model_version: str
    market_regime: str
    horizon: int
    state: str
    sample_count: int
    recent_sample_count: int
    reasons: tuple[str, ...]
    automatic_trading_permitted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.state not in _WEIGHT_STATES:
            raise PredictionValidationError("unknown weight state")


Clock = Callable[[], dt.datetime]


class PredictionLedger:
    """Append-only SQLite store for settlement and calibration research only."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        integrity_key: bytes,
        policy: PredictionLedgerPolicy | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(integrity_key, bytes) or len(integrity_key) < 32:
            raise PredictionValidationError("integrity_key must contain at least 32 bytes")
        self.database_path = Path(database_path)
        if str(self.database_path) == ":memory:":
            raise PredictionValidationError("prediction ledger requires a local file path")
        self.checkpoint_path = Path(str(self.database_path) + ".integrity.json")
        self.lock_path = Path(str(self.database_path) + ".lock")
        self._integrity_key = bytes(integrity_key)
        self.policy = policy or PredictionLedgerPolicy()
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._ensure_schema()

    def record_signal(
        self,
        signal: PredictionSignal,
        *,
        reference_close: AcceptedClose,
        idempotency_key: str,
        recorded_at: dt.datetime | str | None = None,
        is_backfill: bool = False,
        supersedes_signal_id: str | None = None,
    ) -> PredictionEventReceipt:
        if not isinstance(signal, PredictionSignal):
            raise PredictionValidationError("signal must be PredictionSignal")
        if signal.topic not in self.policy.allowed_topics:
            raise PredictionValidationError("topic is not in the configured closed taxonomy")
        now = self._now()
        event_time = now if recorded_at is None else _parse_aware(recorded_at, "recorded_at")
        if event_time > now:
            raise PredictionValidationError("recorded_at may not be in the future")
        if _parse_aware(signal.first_observed_at, "first_observed_at") > event_time:
            raise PredictionValidationError("first observation may not be later than recorded_at")
        if not isinstance(is_backfill, bool):
            raise PredictionValidationError("is_backfill must be boolean")
        supersedes = (
            None
            if supersedes_signal_id is None
            else _digest(supersedes_signal_id, "supersedes_signal_id")
        )
        reference_point = _validated_close_point(
            reference_close,
            signal,
            role="reference",
            available_by=_parse_aware(signal.first_observed_at, "first_observed_at"),
        )
        if _session(reference_point["session"], "reference close session") != signal.observation_session:
            raise PredictionValidationError(
                "reference accepted close must match the observation session"
            )
        try:
            latest_session = ExchangeSessionResolver().last_completed_session(
                now,
                signal.valuation_exchange_mic,
            )
        except ExchangeSessionError as exc:
            raise PredictionValidationError("could not classify signal recording mode") from exc
        payload = _signal_payload(
            signal,
            reference_close=reference_point,
            supersedes_signal_id=supersedes,
            recording_latest_completed_session=latest_session,
            recording_mode="historical_backfill" if is_backfill else "live_observation",
            calibration_eligible=not is_backfill,
        )
        with self._transaction() as connection:
            rows = self._validated_rows(connection)
            reversed_ids = {
                row["payload"]["target_event_id"]
                for row in rows
                if row["event_type"] == "reversal"
            }
            existing = _row_by_idempotency(rows, _idempotency_hash(idempotency_key))
            if existing is not None:
                if (
                    existing["event_type"] == "signal"
                    and existing["payload"]["logical_signal_fingerprint"]
                    == payload["logical_signal_fingerprint"]
                    and _canonical_json(_logical_signal_body(existing["payload"]))
                    == _canonical_json(_logical_signal_body(payload))
                ):
                    if existing["event_id"] in reversed_ids:
                        raise PredictionIdempotencyConflict(
                            "a reversed signal cannot be reported as an idempotent success"
                        )
                    return PredictionEventReceipt(
                        existing["event_id"], existing["event_hash"], True
                    )
                if existing["event_type"] == "idempotency_alias":
                    target = _alias_target(rows, existing)
                    if (
                        target is not None
                        and target["event_type"] == "signal"
                        and _event_content_fingerprint("signal", target["payload"])
                        == payload["logical_signal_fingerprint"]
                        and _canonical_json(_logical_signal_body(target["payload"]))
                        == _canonical_json(_logical_signal_body(payload))
                    ):
                        if target["event_id"] in reversed_ids:
                            raise PredictionIdempotencyConflict(
                                "a reversed signal cannot be reported as an idempotent success"
                            )
                        return PredictionEventReceipt(
                            target["event_id"], target["event_hash"], True
                        )
                raise PredictionIdempotencyConflict(
                    "idempotency identity was reused for different signal content"
                )
            logical_body = _logical_signal_body(payload)
            logical_fingerprint = payload["logical_signal_fingerprint"]
            matching_signals = [
                prior
                for prior in rows
                if prior["event_type"] == "signal"
                and prior["payload"]["logical_signal_fingerprint"] == logical_fingerprint
            ]
            active_matching = [
                prior for prior in matching_signals if prior["event_id"] not in reversed_ids
            ]
            if active_matching:
                for prior in active_matching:
                    if _canonical_json(_logical_signal_body(prior["payload"])) != _canonical_json(
                        logical_body
                    ):
                        raise PredictionIdempotencyConflict(
                            "logical signal fingerprint was reused for different content"
                        )
                    self._append_event(
                        connection,
                        event_type="idempotency_alias",
                        session=signal.observation_session,
                        occurred_at=event_time,
                        idempotency_key=idempotency_key,
                        payload=_idempotency_alias_payload(prior),
                    )
                    return PredictionEventReceipt(
                        prior["event_id"], prior["event_hash"], True
                    )
            if matching_signals:
                if supersedes is None:
                    raise PredictionIdempotencyConflict(
                        "a reversed signal requires an explicit supersedes_signal_id replacement"
                    )
                target = next(
                    (prior for prior in matching_signals if prior["event_id"] == supersedes),
                    None,
                )
                if target is None or target["event_id"] not in reversed_ids:
                    raise PredictionIdempotencyConflict(
                        "supersedes_signal_id must identify a reversed signal with the same source identity"
                    )
                if any(
                    prior["payload"].get("supersedes_signal_id") == supersedes
                    for prior in matching_signals
                ):
                    raise PredictionIdempotencyConflict(
                        "superseded signal already has a replacement"
                    )
                reversal_row = next(
                    (
                        row
                        for row in rows
                        if row["event_type"] == "reversal"
                        and row["payload"]["target_event_id"] == supersedes
                    ),
                    None,
                )
                if reversal_row is None:
                    raise PredictionIntegrityError(
                        "reversed signal lacks its reversal event"
                    )
                reversal_available = max(
                    _parse_aware(reversal_row["occurred_at"], "reversal occurred_at"),
                    _parse_aware(reversal_row["created_at"], "reversal created_at"),
                )
                if event_time < reversal_available:
                    raise PredictionValidationError(
                        "replacement signal may not precede its reversal"
                    )
            elif supersedes is not None:
                raise PredictionIdempotencyConflict(
                    "supersedes_signal_id has no matching source signal"
                )
            if signal.observation_session > latest_session:
                raise PredictionValidationError(
                    "observation session is not completed at recording time"
                )
            if signal.observation_session < latest_session and not is_backfill:
                raise PredictionValidationError(
                    "historical signal recording requires explicit is_backfill=True"
                )
            return self._append_event(
                connection,
                event_type="signal",
                session=signal.observation_session,
                occurred_at=event_time,
                idempotency_key=idempotency_key,
                payload=payload,
            )

    def settle_signal(
        self,
        signal_id: str,
        horizon: int,
        accepted_close: AcceptedClose,
        *,
        path_closes: Sequence[AcceptedClose] = (),
        factor_residual: FactorResidualEvidence | None = None,
        settled_at: dt.datetime | str | None = None,
        idempotency_key: str | None = None,
    ) -> PredictionEventReceipt:
        normalized_signal_id = _digest(signal_id, "signal_id")
        normalized_horizon = _horizon(horizon)
        if isinstance(path_closes, (str, bytes)) or not isinstance(path_closes, Sequence):
            raise PredictionValidationError("path_closes must be a sequence of accepted closes")
        if factor_residual is not None and not isinstance(factor_residual, FactorResidualEvidence):
            raise PredictionValidationError("factor_residual must be FactorResidualEvidence")

        now = self._now()
        event_time = now if settled_at is None else _parse_aware(settled_at, "settled_at")
        if event_time > now:
            raise PredictionValidationError("settled_at may not be in the future")

        with self._transaction() as connection:
            rows = self._validated_rows(connection)
            signals, active_settlements, reversed_ids = _active_state(rows, None)
            signal_row = signals.get(normalized_signal_id)
            if signal_row is None or normalized_signal_id in reversed_ids:
                raise PredictionSettlementBlocked("signal is absent or reversed")
            signal = _signal_from_payload(signal_row["payload"])
            target_session = signal.horizon_sessions[normalized_horizon]
            if event_time <= _parse_aware(signal_row["created_at"], "signal created_at"):
                raise PredictionSettlementBlocked("settlement must follow the signal recording time")

            final_point = _validated_close_point(
                accepted_close,
                signal,
                role="outcome",
                available_by=event_time,
            )
            if _session(final_point["session"], "final close session") != target_session:
                raise PredictionSettlementBlocked("accepted close session does not match the horizon target")

            points_by_session: dict[dt.date, dict[str, Any]] = {}
            for close in tuple(path_closes) + (accepted_close,):
                point = _validated_close_point(
                    close,
                    signal,
                    role="outcome",
                    available_by=event_time,
                )
                point_session = _session(point["session"], "accepted close session")
                if point_session <= signal.observation_session or point_session > target_session:
                    raise PredictionSettlementBlocked("path close is outside the signal horizon")
                prior = points_by_session.get(point_session)
                if prior is not None and prior != point:
                    raise PredictionSettlementBlocked("a path session contains conflicting accepted closes")
                points_by_session[point_session] = point
            points = [points_by_session[item] for item in sorted(points_by_session)]
            required_path = {
                offset: signal.session_path[offset]
                for offset in range(1, normalized_horizon + 1)
            }
            if tuple(sorted(points_by_session)) != tuple(required_path.values()):
                raise PredictionSettlementBlocked(
                    "close path must exactly cover every trading session in the horizon"
                )

            if factor_residual is not None:
                factor_as_of = _parse_aware(factor_residual.as_of, "factor residual as_of")
                final_accepted_at = _parse_aware(
                    final_point["accepted_at"], "accepted close accepted_at"
                )
                if factor_as_of < final_accepted_at or factor_as_of > event_time:
                    raise PredictionSettlementBlocked("factor residual violates point-in-time ordering")

            metrics = _outcome_metrics(signal, points, factor_residual)
            payload = {
                "signal_id": normalized_signal_id,
                "horizon": normalized_horizon,
                "target_session": target_session,
                "close_path": points,
                "factor_residual": None
                if factor_residual is None
                else {
                    "residual_return": factor_residual.residual_return,
                    "as_of": factor_residual.as_of,
                    "model_version": factor_residual.model_version,
                    "lineage_sha256": list(factor_residual.lineage_sha256),
                },
                "recording_mode": signal_row["payload"]["recording_mode"],
                "calibration_eligible": signal_row["payload"]["calibration_eligible"],
                **metrics,
            }
            input_hash = _sha256_text(_canonical_json(payload))
            payload["input_hash"] = input_hash

            key = idempotency_key or (
                f"settlement:{normalized_signal_id}:{normalized_horizon}:{input_hash}"
            )
            existing_key_row = _row_by_idempotency(rows, _idempotency_hash(key))
            if existing_key_row is not None:
                key_target = (
                    _alias_target(rows, existing_key_row)
                    if existing_key_row["event_type"] == "idempotency_alias"
                    else existing_key_row
                )
                if key_target is not None and key_target["event_id"] in reversed_ids:
                    if idempotency_key is None:
                        raise PredictionIdempotencyConflict(
                            "a reversed settlement requires an explicit new idempotency key"
                        )
                    raise PredictionIdempotencyConflict(
                        "a reversed settlement identity cannot be reused"
                    )
                if (
                    key_target is not None
                    and key_target["event_type"] == "settlement"
                    and key_target["payload"].get("input_hash") == input_hash
                    and key_target["payload_json"] == _canonical_json(payload)
                ):
                    return PredictionEventReceipt(
                        key_target["event_id"], key_target["event_hash"], True
                    )
                raise PredictionIdempotencyConflict(
                    "idempotency identity was reused for different settlement content"
                )

            existing = active_settlements.get((normalized_signal_id, normalized_horizon))
            if existing is not None:
                if existing["payload"].get("input_hash") != input_hash:
                    raise PredictionIdempotencyConflict(
                        "signal horizon already has a different active settlement"
                    )
                self._append_event(
                    connection,
                    event_type="idempotency_alias",
                    session=target_session,
                    occurred_at=event_time,
                    idempotency_key=key,
                    payload=_idempotency_alias_payload(existing),
                )
                return PredictionEventReceipt(
                    event_id=existing["event_id"],
                    event_hash=existing["event_hash"],
                    idempotent_replay=True,
                )
            if idempotency_key is None:
                prior_same_default = any(
                    row["event_type"] == "settlement"
                    and row["payload"]["signal_id"] == normalized_signal_id
                    and int(row["payload"]["horizon"]) == normalized_horizon
                    and row["payload"].get("input_hash") == input_hash
                    for row in rows
                )
                if prior_same_default:
                    raise PredictionIdempotencyConflict(
                        "a reversed settlement requires an explicit new idempotency key"
                    )
            return self._append_event(
                connection,
                event_type="settlement",
                session=target_session,
                occurred_at=event_time,
                idempotency_key=key,
                payload=payload,
            )

    def reverse_event(
        self,
        target_event_id: str,
        *,
        reason_code: str,
        reversed_at: dt.datetime | str | None = None,
        idempotency_key: str | None = None,
    ) -> PredictionEventReceipt:
        target = _digest(target_event_id, "target_event_id")
        reason = _closed_identifier(
            reason_code,
            "reason_code",
            _REVERSAL_REASON_CODES,
        )
        now = self._now()
        event_time = now if reversed_at is None else _parse_aware(reversed_at, "reversed_at")
        if event_time > now:
            raise PredictionValidationError("reversed_at may not be in the future")
        payload = {
            "target_event_id": target,
            "reason_code": reason,
            "recording_mode": "correction",
            "calibration_eligible": False,
        }
        key = idempotency_key or f"reversal:{target}"
        with self._transaction() as connection:
            rows = self._validated_rows(connection)
            by_id = {row["event_id"]: row for row in rows}
            existing_by_key = _row_by_idempotency(rows, _idempotency_hash(key))
            if existing_by_key is not None:
                key_target = (
                    _alias_target(rows, existing_by_key)
                    if existing_by_key["event_type"] == "idempotency_alias"
                    else existing_by_key
                )
                if (
                    key_target is None
                    or key_target["event_type"] != "reversal"
                    or key_target["payload"] != payload
                ):
                    raise PredictionIdempotencyConflict(
                        "idempotency identity was reused for different reversal content"
                    )
                return PredictionEventReceipt(
                    key_target["event_id"], key_target["event_hash"], True
                )
            target_row = by_id.get(target)
            if target_row is None or target_row["event_type"] not in {"signal", "settlement"}:
                raise PredictionValidationError("reversal target must be an existing signal or settlement")
            target_available = max(
                _parse_aware(target_row["occurred_at"], "target occurred_at"),
                _parse_aware(target_row["created_at"], "target created_at"),
            )
            if event_time < target_available:
                raise PredictionValidationError("reversal may not precede its target")
            reversals = {
                row["payload"]["target_event_id"]: row
                for row in rows
                if row["event_type"] == "reversal"
            }
            if target in reversals:
                prior = reversals[target]
                if prior["payload"] == payload:
                    prior_available = max(
                        _parse_aware(prior["occurred_at"], "prior reversal occurred_at"),
                        _parse_aware(prior["created_at"], "prior reversal created_at"),
                    )
                    if event_time < prior_available:
                        raise PredictionValidationError(
                            "idempotency alias may not precede the prior reversal"
                        )
                    self._append_event(
                        connection,
                        event_type="idempotency_alias",
                        session=prior["session_date"],
                        occurred_at=event_time,
                        idempotency_key=key,
                        payload=_idempotency_alias_payload(prior),
                    )
                    return PredictionEventReceipt(prior["event_id"], prior["event_hash"], True)
                raise PredictionIdempotencyConflict("target already has a different reversal")
            return self._append_event(
                connection,
                event_type="reversal",
                session=event_time.date(),
                occurred_at=event_time,
                idempotency_key=key,
                payload=payload,
            )

    def outcomes(
        self,
        *,
        platform: str | None = None,
        topic: str | None = None,
        model_version: str | None = None,
        market_regime: str | None = None,
        horizon: int | None = None,
        as_of: dt.datetime | str | None = None,
        include_backfill: bool = False,
    ) -> tuple[PredictionOutcome, ...]:
        if not isinstance(include_backfill, bool):
            raise PredictionValidationError("include_backfill must be boolean")
        cutoff = self._query_cutoff(as_of)
        filters = _normalized_filters(
            platform,
            topic,
            model_version,
            market_regime,
            horizon,
        )
        with self._ledger_lock():
            connection = self._connect()
            try:
                rows = self._validated_rows(connection)
                signals, settlements, _ = _active_state(rows, cutoff)
                result: list[PredictionOutcome] = []
                for (signal_id, item_horizon), settlement_row in settlements.items():
                    signal_row = signals.get(signal_id)
                    if signal_row is None:
                        continue
                    if (
                        not include_backfill
                        and signal_row["payload"]["calibration_eligible"] is not True
                    ):
                        continue
                    signal = _signal_from_payload(signal_row["payload"])
                    if not _matches(signal, item_horizon, filters):
                        continue
                    result.append(
                        _outcome_from_rows(
                            signal_id,
                            signal,
                            settlement_row,
                            recording_mode=signal_row["payload"]["recording_mode"],
                            calibration_eligible=signal_row["payload"][
                                "calibration_eligible"
                            ],
                        )
                    )
                return tuple(
                    sorted(result, key=lambda item: (item.target_session, item.settlement_id))
                )
            finally:
                connection.close()

    def calibration_summaries(
        self,
        *,
        platform: str | None = None,
        topic: str | None = None,
        model_version: str | None = None,
        market_regime: str | None = None,
        horizon: int | None = None,
        as_of: dt.datetime | str | None = None,
        include_backfill: bool = False,
    ) -> tuple[CalibrationSummary, ...]:
        outcomes = self.outcomes(
            platform=platform,
            topic=topic,
            model_version=model_version,
            market_regime=market_regime,
            horizon=horizon,
            as_of=as_of,
            include_backfill=include_backfill,
        )
        groups: dict[tuple[str, str, str, str, int], list[PredictionOutcome]] = {}
        for outcome in outcomes:
            key = (
                outcome.platform,
                outcome.topic,
                outcome.model_version,
                outcome.market_regime,
                outcome.horizon,
            )
            groups.setdefault(key, []).append(outcome)
        return tuple(
            _calibration(
                key,
                groups[key],
                sample_scope="includes_backfill" if include_backfill else "live_only",
            )
            for key in sorted(groups)
        )

    def weight_state(
        self,
        *,
        platform: str,
        topic: str,
        model_version: str,
        market_regime: str,
        horizon: int,
        as_of: dt.datetime | str | None = None,
    ) -> PredictionWeightState:
        normalized_platform = _closed_identifier(platform, "platform", _PLATFORMS)
        normalized_topic = _topic(topic)
        if normalized_topic not in self.policy.allowed_topics:
            raise PredictionValidationError("topic is not in the configured closed taxonomy")
        normalized_model_version = _prediction_model_version(model_version)
        normalized_regime = _closed_identifier(
            market_regime, "market_regime", _MARKET_REGIMES
        )
        normalized_horizon = _horizon(horizon)
        outcomes = list(
            self.outcomes(
                platform=normalized_platform,
                topic=normalized_topic,
                model_version=normalized_model_version,
                market_regime=normalized_regime,
                horizon=normalized_horizon,
                as_of=as_of,
            )
        )
        outcomes.sort(
            key=lambda item: (
                _parse_aware(item.settled_at, "settled_at"),
                item.target_session,
                item.settlement_id,
            )
        )
        sample_count = len(outcomes)
        if sample_count < self.policy.minimum_samples:
            return PredictionWeightState(
                normalized_platform,
                normalized_topic,
                normalized_model_version,
                normalized_regime,
                normalized_horizon,
                "research_only",
                sample_count,
                min(sample_count, self.policy.recent_window),
                ("minimum_samples_not_met",),
            )

        recent = outcomes[-self.policy.recent_window :]
        recent_count = len(recent)
        if recent_count < self.policy.minimum_recent_samples:
            return PredictionWeightState(
                normalized_platform,
                normalized_topic,
                normalized_model_version,
                normalized_regime,
                normalized_horizon,
                "research_only",
                sample_count,
                recent_count,
                ("minimum_recent_samples_not_met",),
            )
        recent_hit = _mean([ONE if item.direction_hit else ZERO for item in recent])
        recent_brier = _mean([item.brier for item in recent])
        older = outcomes[: -len(recent)]
        older_hit = (
            _mean([ONE if item.direction_hit else ZERO for item in older]) if older else None
        )
        hit_drop = ZERO if older_hit is None else older_hit - recent_hit

        quarantine_reasons: list[str] = []
        if recent_hit <= self.policy.quarantine_hit_rate:
            quarantine_reasons.append("recent_hit_rate_failed")
        if recent_brier >= self.policy.quarantine_brier:
            quarantine_reasons.append("recent_brier_failed")
        if older_hit is not None and hit_drop >= self.policy.quarantine_hit_rate_drop:
            quarantine_reasons.append("hit_rate_distribution_drift")
        if quarantine_reasons:
            state = "quarantined"
            reasons = tuple(sorted(set(quarantine_reasons)))
        else:
            decay_reasons: list[str] = []
            if recent_hit < self.policy.decay_hit_rate:
                decay_reasons.append("recent_hit_rate_weak")
            if recent_brier > self.policy.decay_brier:
                decay_reasons.append("recent_brier_weak")
            if older_hit is not None and hit_drop >= self.policy.decay_hit_rate_drop:
                decay_reasons.append("hit_rate_drift")
            state = "decayed" if decay_reasons else "active"
            reasons = tuple(sorted(set(decay_reasons))) or ("calibration_healthy",)
        return PredictionWeightState(
            normalized_platform,
            normalized_topic,
            normalized_model_version,
            normalized_regime,
            normalized_horizon,
            state,
            sample_count,
            recent_count,
            reasons,
        )

    def verify_hash_chain(self) -> bool:
        with self._ledger_lock():
            connection = self._connect()
            try:
                self._validated_rows(connection)
                return True
            finally:
                connection.close()

    def _now(self) -> dt.datetime:
        return _parse_aware(self._clock(), "clock")

    def _query_cutoff(self, value: dt.datetime | str | None) -> dt.datetime | None:
        if value is None:
            return None
        cutoff = _parse_aware(value, "as_of")
        if cutoff > self._now():
            raise PredictionValidationError("as_of may not be in the future")
        return cutoff

    def _ensure_schema(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._ledger_lock():
            self._ensure_schema_locked()

    def _ensure_schema_locked(self) -> None:
        database_existed = self.database_path.exists()
        checkpoint_existed = self.checkpoint_path.exists()
        checkpoint_state: dict[str, Any] | None = None
        if database_existed and not checkpoint_existed:
            raise PredictionIntegrityError("integrity checkpoint is missing")
        if not database_existed and not checkpoint_existed:
            # Sidecar-first bootstrap: a crash after this write can safely
            # recreate only an empty database; an existing DB without this
            # authenticated genesis is never trusted.
            self._write_checkpoint_state(
                committed=(0, _GENESIS_HASH),
                pending=None,
                generation=0,
            )
            checkpoint_state = self._read_checkpoint_state()
        else:
            checkpoint_state = self._read_checkpoint_state()
        if not database_existed and checkpoint_existed:
            if (
                _checkpoint_anchor(checkpoint_state["committed"], "committed")
                != (0, _GENESIS_HASH)
                or checkpoint_state["pending"] is not None
            ):
                raise PredictionIntegrityError("checkpoint cannot bootstrap an empty database")
        genesis_bootstrap = bool(
            checkpoint_state is not None
            and _checkpoint_anchor(checkpoint_state["committed"], "committed")
            == (0, _GENESIS_HASH)
            and checkpoint_state["pending"] is None
        )
        connection = self._connect()
        try:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'prediction_events'"
            ).fetchone() is not None
            if database_existed and not table_exists and not genesis_bootstrap:
                raise PredictionIntegrityError("existing database lacks the prediction ledger schema")
            if not table_exists:
                unexpected_objects = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if unexpected_objects:
                    raise PredictionIntegrityError("empty bootstrap database contains unknown objects")
                connection.executescript(
                    f"""
                CREATE TABLE prediction_events (
                    sequence_no INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_hash TEXT NOT NULL UNIQUE,
                    session_date TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX idx_prediction_events_type_session
                    ON prediction_events(event_type, session_date, sequence_no);
                {_TRIGGER_UPDATE_SQL};
                {_TRIGGER_DELETE_SQL};
                """
                )
            elif genesis_bootstrap:
                columns = tuple(
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(prediction_events)")
                )
                if columns != _EVENT_COLUMNS:
                    raise PredictionIntegrityError(
                        "genesis bootstrap table schema is invalid"
                    )
                event_count = int(
                    connection.execute("SELECT COUNT(*) FROM prediction_events").fetchone()[0]
                )
                if event_count == 0:
                    permitted_objects = {
                        "prediction_events",
                        "idx_prediction_events_type_session",
                        "prediction_events_no_update",
                        "prediction_events_no_delete",
                    }
                    existing_objects = {
                        str(row["name"])
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                        )
                    }
                    if not existing_objects.issubset(permitted_objects):
                        raise PredictionIntegrityError(
                            "genesis bootstrap database contains unknown objects"
                        )
                    connection.executescript(
                        f"""
                        CREATE INDEX IF NOT EXISTS idx_prediction_events_type_session
                            ON prediction_events(event_type, session_date, sequence_no);
                        CREATE TRIGGER IF NOT EXISTS {_TRIGGER_UPDATE_SQL.removeprefix('CREATE TRIGGER ')};
                        CREATE TRIGGER IF NOT EXISTS {_TRIGGER_DELETE_SQL.removeprefix('CREATE TRIGGER ')};
                        """
                    )
            self._validate_schema(connection)
            self._validated_rows(connection)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=self.policy.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.policy.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _ledger_lock(self) -> Iterable[None]:
        """Serialize DB/checkpoint recovery across threads and processes."""

        key = str(self.lock_path.resolve(strict=False))
        if os.name == "nt":
            key = key.casefold()
        with _THREAD_LOCKS_GUARD:
            thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
        with thread_lock:
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor: int | None = None
            try:
                descriptor = os.open(self.lock_path, flags, 0o600)
                stat = os.fstat(descriptor)
                if stat.st_nlink != 1:
                    raise PredictionIntegrityError("prediction ledger lock is unsafe")
                if stat.st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            except PredictionLedgerError:
                raise
            except Exception as exc:
                raise PredictionIntegrityError("prediction ledger lock failed") from exc
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    @contextmanager
    def _transaction(self) -> Iterable[sqlite3.Connection]:
        with self._ledger_lock():
            connection = self._connect()
            committed = False
            try:
                connection.execute("BEGIN IMMEDIATE")
                before_rows = self._validated_rows(connection)
                before_sequence, before_head = _head(before_rows)
                yield connection
                after_rows = self._validated_rows(connection, verify_checkpoint=False)
                after_sequence, after_head = _head(after_rows)
                changed = (after_sequence, after_head) != (before_sequence, before_head)
                if changed:
                    try:
                        current = self._read_checkpoint_state()
                        self._write_checkpoint_state(
                            committed=(before_sequence, before_head),
                            pending={
                                "tx_id": secrets.token_hex(16),
                                "from_sequence": before_sequence,
                                "from_head": before_head,
                                "to_sequence": after_sequence,
                                "to_head": after_head,
                            },
                            generation=int(current["generation"]) + 1,
                        )
                    except Exception as exc:
                        raise PredictionIntegrityError("integrity checkpoint prepare failed") from exc
                try:
                    connection.commit()
                except Exception as exc:
                    raise PredictionCommitUnknown("prediction_ledger_commit_unknown") from exc
                committed = True
                if changed:
                    try:
                        prepared = self._read_checkpoint_state()
                        self._write_checkpoint_state(
                            committed=(after_sequence, after_head),
                            pending=None,
                            generation=int(prepared["generation"]) + 1,
                        )
                    except Exception as exc:
                        raise PredictionCommitUnknown("prediction_ledger_commit_unknown") from exc
            except Exception:
                if not committed:
                    try:
                        connection.rollback()
                    except Exception:
                        pass
                raise
            finally:
                connection.close()

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        session: dt.date,
        occurred_at: dt.datetime,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> PredictionEventReceipt:
        normalized_type = _event_type(event_type)
        normalized_session = _session(session, "event session")
        normalized_occurred_at = _aware_text(occurred_at, "occurred_at")
        key_hash = _idempotency_hash(idempotency_key)
        payload_json = _canonical_json(payload)
        rows = self._validated_rows(connection)
        existing = _row_by_idempotency(rows, key_hash)
        if existing is not None:
            if existing["event_type"] != normalized_type or existing["payload_json"] != payload_json:
                raise PredictionIdempotencyConflict(
                    "idempotency identity was reused for different event content"
                )
            return PredictionEventReceipt(existing["event_id"], existing["event_hash"], True)

        identity = {
            "idempotency_hash": key_hash,
            "session_date": normalized_session.isoformat(),
            "occurred_at": normalized_occurred_at,
            "event_type": normalized_type,
            "payload": json.loads(payload_json),
        }
        event_id = _sha256_text(_canonical_json(identity))
        previous_hash = _GENESIS_HASH if not rows else rows[-1]["event_hash"]
        created_at = _aware_text(self._now(), "created_at")
        hash_body = {"event_id": event_id, **identity, "created_at": created_at}
        event_hash = _chain_hash(previous_hash, hash_body)
        connection.execute(
            """
            INSERT INTO prediction_events (
                event_id, idempotency_hash, session_date, occurred_at, event_type,
                payload_json, previous_hash, event_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                key_hash,
                normalized_session.isoformat(),
                normalized_occurred_at,
                normalized_type,
                payload_json,
                previous_hash,
                event_hash,
                created_at,
            ),
        )
        return PredictionEventReceipt(event_id, event_hash, False)

    def _validated_rows(
        self,
        connection: sqlite3.Connection,
        *,
        verify_checkpoint: bool = True,
    ) -> list[dict[str, Any]]:
        self._validate_schema(connection)
        raw_rows = connection.execute(
            "SELECT * FROM prediction_events ORDER BY sequence_no"
        ).fetchall()
        rows: list[dict[str, Any]] = []
        expected_previous = _GENESIS_HASH
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        for expected_sequence, raw in enumerate(raw_rows, start=1):
            if int(raw["sequence_no"]) != expected_sequence:
                raise PredictionIntegrityError("prediction event sequence is not contiguous")
            try:
                payload = json.loads(raw["payload_json"])
            except (json.JSONDecodeError, TypeError) as exc:
                raise PredictionIntegrityError("event payload is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise PredictionIntegrityError("event payload must be an object")
            try:
                canonical_payload = _canonical_json(payload)
            except PredictionValidationError as exc:
                raise PredictionIntegrityError("event payload violates the privacy contract") from exc
            if canonical_payload != raw["payload_json"]:
                raise PredictionIntegrityError("event payload is not canonical JSON")
            try:
                event_type = _event_type(raw["event_type"])
                session = _session(raw["session_date"], "persisted session")
                occurred_at = _aware_text(raw["occurred_at"], "persisted occurred_at")
                created_at = _aware_text(raw["created_at"], "persisted created_at")
                key_hash = _digest(raw["idempotency_hash"], "persisted idempotency hash")
            except PredictionValidationError as exc:
                raise PredictionIntegrityError("event envelope is malformed") from exc
            if _parse_aware(created_at, "created_at") < _parse_aware(
                occurred_at, "occurred_at"
            ):
                raise PredictionIntegrityError("event creation precedes its declared occurrence")
            if str(raw["previous_hash"]) != expected_previous:
                raise PredictionIntegrityError(
                    f"event hash-chain predecessor mismatch at sequence {raw['sequence_no']}"
                )
            identity = {
                "idempotency_hash": key_hash,
                "session_date": session.isoformat(),
                "occurred_at": occurred_at,
                "event_type": event_type,
                "payload": payload,
            }
            expected_id = _sha256_text(_canonical_json(identity))
            if str(raw["event_id"]) != expected_id:
                raise PredictionIntegrityError(
                    f"event identity mismatch at sequence {raw['sequence_no']}"
                )
            expected_hash = _chain_hash(
                expected_previous,
                {"event_id": expected_id, **identity, "created_at": created_at},
            )
            if str(raw["event_hash"]) != expected_hash:
                raise PredictionIntegrityError(
                    f"event hash mismatch at sequence {raw['sequence_no']}"
                )
            if expected_id in seen_ids or key_hash in seen_keys:
                raise PredictionIntegrityError("duplicate event identity")
            row = {
                "sequence_no": int(raw["sequence_no"]),
                "event_id": expected_id,
                "idempotency_hash": key_hash,
                "session_date": session,
                "occurred_at": occurred_at,
                "event_type": event_type,
                "payload": payload,
                "payload_json": canonical_payload,
                "previous_hash": expected_previous,
                "event_hash": expected_hash,
                "created_at": created_at,
            }
            _validate_event_semantics(
                row,
                rows,
                allowed_topics=frozenset(_DEFAULT_ALLOWED_TOPICS),
            )
            rows.append(row)
            seen_ids.add(expected_id)
            seen_keys.add(key_hash)
            expected_previous = expected_hash
        if verify_checkpoint:
            self._recover_checkpoint(rows)
        return rows

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        columns = tuple(
            str(row[1]) for row in connection.execute("PRAGMA table_info(prediction_events)")
        )
        if columns != _EVENT_COLUMNS:
            raise PredictionIntegrityError("prediction ledger table schema is invalid")
        expected_triggers = {
            "prediction_events_no_update": _normalized_sql(_TRIGGER_UPDATE_SQL),
            "prediction_events_no_delete": _normalized_sql(_TRIGGER_DELETE_SQL),
        }
        triggers = {
            str(row["name"]): _normalized_sql(str(row["sql"]))
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'prediction_events'"
            )
        }
        if triggers != expected_triggers:
            raise PredictionIntegrityError("prediction ledger append-only triggers are invalid")

    def _read_checkpoint_state(self) -> dict[str, Any]:
        if not self.checkpoint_path.is_file():
            raise PredictionIntegrityError("integrity checkpoint is missing")
        try:
            raw = self.checkpoint_path.read_bytes()
            if len(raw) > 4096 or not raw.endswith(b"\n"):
                raise ValueError
            parsed = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise PredictionIntegrityError("integrity checkpoint is malformed") from exc
        if not isinstance(parsed, dict) or set(parsed) != {
            "schema", "generation", "committed", "pending", "hmac_sha256"
        }:
            raise PredictionIntegrityError("integrity checkpoint is malformed")
        body = {key: parsed[key] for key in ("schema", "generation", "committed", "pending")}
        try:
            supplied_mac = _digest(parsed["hmac_sha256"], "checkpoint HMAC")
            _checkpoint_anchor(parsed["committed"], "committed")
            if parsed["pending"] is not None:
                _checkpoint_pending(parsed["pending"])
        except PredictionValidationError as exc:
            raise PredictionIntegrityError("integrity checkpoint is malformed") from exc
        if (
            parsed["schema"] != _CHECKPOINT_SCHEMA
            or isinstance(parsed["generation"], bool)
            or not isinstance(parsed["generation"], int)
            or parsed["generation"] < 0
        ):
            raise PredictionIntegrityError("integrity checkpoint is malformed")
        expected_mac = hmac.new(
            self._integrity_key,
            _canonical_json(body).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        expected_bytes = (_canonical_json({**body, "hmac_sha256": supplied_mac}) + "\n").encode(
            "utf-8"
        )
        if not hmac.compare_digest(supplied_mac, expected_mac) or raw != expected_bytes:
            raise PredictionIntegrityError("integrity checkpoint authentication failed")
        return parsed

    def _recover_checkpoint(self, rows: Sequence[Mapping[str, Any]]) -> None:
        state = self._read_checkpoint_state()
        committed = _checkpoint_anchor(state["committed"], "committed")
        pending = None if state["pending"] is None else _checkpoint_pending(state["pending"])
        database = _head(rows)
        if pending is None:
            if database != committed:
                raise PredictionIntegrityError("stable database head differs from checkpoint")
            return
        from_anchor = (pending["from_sequence"], pending["from_head"])
        to_anchor = (pending["to_sequence"], pending["to_head"])
        if from_anchor != committed or pending["to_sequence"] != pending["from_sequence"] + 1:
            raise PredictionIntegrityError("pending checkpoint transition is invalid")
        if committed[0] > len(rows):
            raise PredictionIntegrityError("database is behind the committed checkpoint")
        prefix_head = _GENESIS_HASH if committed[0] == 0 else str(rows[committed[0] - 1]["event_hash"])
        if prefix_head != committed[1]:
            raise PredictionIntegrityError("database checkpoint prefix is divergent")
        if database == from_anchor:
            self._write_checkpoint_state(
                committed=committed,
                pending=None,
                generation=int(state["generation"]) + 1,
            )
            return
        if database == to_anchor:
            self._write_checkpoint_state(
                committed=to_anchor,
                pending=None,
                generation=int(state["generation"]) + 1,
            )
            return
        raise PredictionIntegrityError("database does not match the pending checkpoint transition")

    def _write_checkpoint_state(
        self,
        *,
        committed: tuple[int, str],
        pending: Mapping[str, Any] | None,
        generation: int,
    ) -> None:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise PredictionIntegrityError("checkpoint generation is invalid")
        committed_body = {
            "sequence": int(committed[0]),
            "head_hash": _digest(committed[1], "checkpoint head"),
        }
        pending_body = None if pending is None else dict(_checkpoint_pending(pending))
        body = {
            "schema": _CHECKPOINT_SCHEMA,
            "generation": generation,
            "committed": committed_body,
            "pending": pending_body,
        }
        mac = hmac.new(
            self._integrity_key,
            _canonical_json(body).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        content = (_canonical_json({**body, "hmac_sha256": mac}) + "\n").encode("utf-8")
        temporary = self.checkpoint_path.with_name(
            f"{self.checkpoint_path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("checkpoint write made no progress")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self.checkpoint_path)
            if os.name != "nt":
                directory_fd = os.open(self.checkpoint_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _event_content_fingerprint(event_type: str, payload: Mapping[str, Any]) -> str:
    if event_type == "signal":
        return _digest(payload["logical_signal_fingerprint"], "logical signal fingerprint")
    if event_type == "settlement":
        return _digest(payload["input_hash"], "settlement input hash")
    if event_type == "reversal":
        return _sha256_text(_canonical_json(payload))
    raise PredictionIntegrityError("idempotency alias target type is invalid")


def _idempotency_alias_payload(target_row: Mapping[str, Any]) -> dict[str, Any]:
    target_type = str(target_row["event_type"])
    return {
        "target_event_id": target_row["event_id"],
        "target_event_type": target_type,
        "content_fingerprint": _event_content_fingerprint(
            target_type, target_row["payload"]
        ),
        "recording_mode": "idempotency_alias",
        "calibration_eligible": False,
    }


def _alias_target(
    rows: Sequence[Mapping[str, Any]], alias_row: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    target_id = alias_row["payload"]["target_event_id"]
    return next((row for row in rows if row["event_id"] == target_id), None)


def _validate_event_semantics(
    row: Mapping[str, Any],
    prior_rows: Sequence[Mapping[str, Any]],
    *,
    allowed_topics: frozenset[str],
) -> None:
    event_type = row["event_type"]
    payload = row["payload"]
    by_id = {item["event_id"]: item for item in prior_rows}
    reversed_targets = {
        item["payload"]["target_event_id"]
        for item in prior_rows
        if item["event_type"] == "reversal"
    }
    if event_type == "signal":
        signal = _signal_from_payload(payload)
        if signal.topic not in allowed_topics:
            raise PredictionIntegrityError("signal topic is outside the closed taxonomy")
        fingerprint = payload["logical_signal_fingerprint"]
        matching_signals = [
            prior
            for prior in prior_rows
            if prior["event_type"] == "signal"
            and prior["payload"]["logical_signal_fingerprint"] == fingerprint
        ]
        active_matching = [
            prior for prior in matching_signals if prior["event_id"] not in reversed_targets
        ]
        supersedes_raw = payload.get("supersedes_signal_id")
        if supersedes_raw is None:
            if matching_signals:
                raise PredictionIntegrityError("duplicate logical signal identity")
        else:
            try:
                supersedes = _digest(supersedes_raw, "supersedes signal")
            except PredictionValidationError as exc:
                raise PredictionIntegrityError("signal replacement linkage is malformed") from exc
            target = by_id.get(supersedes)
            if (
                active_matching
                or target is None
                or target["event_type"] != "signal"
                or target["payload"]["logical_signal_fingerprint"] != fingerprint
                or supersedes not in reversed_targets
            ):
                raise PredictionIntegrityError("signal replacement linkage is invalid")
            if any(
                prior["payload"].get("supersedes_signal_id") == supersedes
                for prior in matching_signals
            ):
                raise PredictionIntegrityError("signal replacement branches an existing chain")
            reversal_row = next(
                (
                    prior
                    for prior in prior_rows
                    if prior["event_type"] == "reversal"
                    and prior["payload"]["target_event_id"] == supersedes
                ),
                None,
            )
            if reversal_row is None:
                raise PredictionIntegrityError("signal replacement lacks a reversal event")
            reversal_available = max(
                _parse_aware(reversal_row["occurred_at"], "reversal occurred_at"),
                _parse_aware(reversal_row["created_at"], "reversal created_at"),
            )
            if _parse_aware(row["occurred_at"], "replacement occurred_at") < reversal_available:
                raise PredictionIntegrityError("replacement signal precedes its reversal")
        if row["session_date"] != signal.observation_session:
            raise PredictionIntegrityError("signal envelope session is inconsistent")
        if _parse_aware(signal.first_observed_at, "first_observed_at") > _parse_aware(
            row["occurred_at"], "occurred_at"
        ):
            raise PredictionIntegrityError("signal uses information observed after recording")
        latest_at_creation = _session(
            payload["recording_latest_completed_session"],
            "recording latest completed session",
        )
        is_historical = signal.observation_session < latest_at_creation
        if signal.observation_session > latest_at_creation:
            raise PredictionIntegrityError("signal session was not completed when created")
        if is_historical and payload["recording_mode"] != "historical_backfill":
            raise PredictionIntegrityError("signal backfill classification is inconsistent")
        return

    if event_type == "idempotency_alias":
        _exact_keys(
            payload,
            {
                "target_event_id",
                "target_event_type",
                "content_fingerprint",
                "recording_mode",
                "calibration_eligible",
            },
            "idempotency alias",
        )
        try:
            target_id = _digest(payload["target_event_id"], "alias target")
            fingerprint = _digest(
                payload["content_fingerprint"], "alias content fingerprint"
            )
        except PredictionValidationError as exc:
            raise PredictionIntegrityError("idempotency alias is malformed") from exc
        target_row = by_id.get(target_id)
        if target_row is None or target_row["event_type"] not in {
            "signal",
            "settlement",
            "reversal",
        }:
            raise PredictionIntegrityError("idempotency alias target is absent or invalid")
        if (
            payload["target_event_type"] != target_row["event_type"]
            or fingerprint
            != _event_content_fingerprint(target_row["event_type"], target_row["payload"])
            or payload["recording_mode"] != "idempotency_alias"
            or payload["calibration_eligible"] is not False
            or row["session_date"] != target_row["session_date"]
        ):
            raise PredictionIntegrityError("idempotency alias metadata is inconsistent")
        target_available = max(
            _parse_aware(target_row["occurred_at"], "target occurred_at"),
            _parse_aware(target_row["created_at"], "target created_at"),
        )
        if _parse_aware(row["occurred_at"], "alias occurred_at") < target_available:
            raise PredictionIntegrityError("idempotency alias precedes its target")
        return
    if event_type == "reversal":
        _exact_keys(
            payload,
            {
                "target_event_id",
                "reason_code",
                "recording_mode",
                "calibration_eligible",
            },
            "reversal",
        )
        if payload.get("recording_mode") != "correction" or payload.get(
            "calibration_eligible"
        ) is not False:
            raise PredictionIntegrityError("reversal calibration metadata is inconsistent")
        try:
            target = _digest(payload["target_event_id"], "reversal target")
            _closed_identifier(
                payload["reason_code"],
                "reversal reason",
                _REVERSAL_REASON_CODES,
            )
        except PredictionValidationError as exc:
            raise PredictionIntegrityError("reversal payload is malformed") from exc
        target_row = by_id.get(target)
        if target_row is None or target_row["event_type"] not in {"signal", "settlement"}:
            raise PredictionIntegrityError("reversal target is absent or invalid")
        if target in reversed_targets:
            raise PredictionIntegrityError("event has more than one reversal")
        target_available = max(
            _parse_aware(target_row["occurred_at"], "target occurred_at"),
            _parse_aware(target_row["created_at"], "target created_at"),
        )
        if _parse_aware(row["occurred_at"], "reversal occurred_at") < target_available:
            raise PredictionIntegrityError("reversal precedes its target")
        return

    if event_type != "settlement":
        raise PredictionIntegrityError("unknown event type")
    try:
        _validate_settlement_payload(payload, row, by_id, reversed_targets, prior_rows)
    except PredictionLedgerError:
        raise
    except Exception as exc:
        raise PredictionIntegrityError("settlement payload is malformed") from exc


def _validate_settlement_payload(
    payload: Mapping[str, Any],
    row: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
    reversed_targets: set[str],
    prior_rows: Sequence[Mapping[str, Any]],
) -> None:
    required = {
        "signal_id",
        "horizon",
        "target_session",
        "close_path",
        "factor_residual",
        "raw_return",
        "residual_return",
        "direction_hit",
        "mfe",
        "mae",
        "brier",
        "input_hash",
        "recording_mode",
        "calibration_eligible",
    }
    _exact_keys(payload, required, "settlement")
    signal_id = _digest(payload["signal_id"], "settlement signal_id")
    horizon = _horizon(payload["horizon"])
    target = _session(payload["target_session"], "settlement target_session")
    signal_row = by_id.get(signal_id)
    if signal_row is None or signal_row["event_type"] != "signal" or signal_id in reversed_targets:
        raise PredictionIntegrityError("settlement references an absent or reversed signal")
    signal = _signal_from_payload(signal_row["payload"])
    if payload["recording_mode"] != signal_row["payload"]["recording_mode"] or payload[
        "calibration_eligible"
    ] is not signal_row["payload"]["calibration_eligible"]:
        raise PredictionIntegrityError("settlement calibration metadata is inconsistent")
    if target != signal.horizon_sessions[horizon] or row["session_date"] != target:
        raise PredictionIntegrityError("settlement horizon session is inconsistent")
    if _parse_aware(row["occurred_at"], "settlement occurred_at") <= _parse_aware(
        signal_row["created_at"], "signal created_at"
    ):
        raise PredictionIntegrityError("settlement does not follow signal recording")
    if not isinstance(payload["close_path"], list) or not payload["close_path"]:
        raise PredictionIntegrityError("settlement close path is empty")
    path: list[dict[str, Any]] = []
    prior_session = signal.observation_session
    first_observed = _parse_aware(signal.first_observed_at, "first_observed_at")
    for raw_point in payload["close_path"]:
        point = _stored_close_point(raw_point)
        point_session = _session(point["session"], "close path session")
        if point_session <= prior_session or point_session > target:
            raise PredictionIntegrityError("close path sessions are not strictly ordered")
        if point["symbol"] != signal.valuation_symbol:
            raise PredictionIntegrityError("close path symbol differs from signal")
        if (
            point["exchange_mic"] != signal.valuation_exchange_mic
            or point["currency"] != signal.valuation_currency
            or point["asset_type"] != signal.reference_close["asset_type"]
            or point["calendar_id"] != signal.reference_close["calendar_id"]
        ):
            raise PredictionIntegrityError("close path identity differs from signal")
        if _parse_aware(point["retrieved_at"], "close retrieved_at") > _parse_aware(
            row["occurred_at"], "settlement occurred_at"
        ):
            raise PredictionIntegrityError("settlement uses a future close observation")
        for source in point["sources"]:
            retrieved = _parse_aware(source["retrieved_at"], "source retrieved_at")
            if retrieved <= first_observed or retrieved > _parse_aware(
                row["occurred_at"], "settlement occurred_at"
            ):
                raise PredictionIntegrityError("close source violates point-in-time ordering")
        path.append(point)
        prior_session = point_session
    if prior_session != target:
        raise PredictionIntegrityError("close path does not end at the horizon target")
    required_path = {
        offset: signal.session_path[offset]
        for offset in range(1, horizon + 1)
    }
    if tuple(_session(item["session"], "path session") for item in path) != tuple(
        required_path.values()
    ):
        raise PredictionIntegrityError("stored close path is incomplete")

    factor = _stored_factor_residual(payload["factor_residual"])
    if factor is not None:
        if _parse_aware(factor.as_of, "factor residual as_of") < _parse_aware(
            path[-1]["accepted_at"], "final close accepted_at"
        ) or _parse_aware(factor.as_of, "factor residual as_of") > _parse_aware(
            row["occurred_at"], "settlement occurred_at"
        ):
            raise PredictionIntegrityError("factor residual violates point-in-time ordering")
    metrics = _outcome_metrics(signal, path, factor)
    for key, expected in metrics.items():
        actual = payload.get(key)
        if isinstance(expected, Decimal):
            if _decimal(actual, key) != expected:
                raise PredictionIntegrityError(f"settlement {key} is inconsistent")
        elif actual != expected:
            raise PredictionIntegrityError(f"settlement {key} is inconsistent")
    input_body = {key: payload[key] for key in payload if key != "input_hash"}
    if _digest(payload["input_hash"], "settlement input_hash") != _sha256_text(
        _canonical_json(input_body)
    ):
        raise PredictionIntegrityError("settlement input hash is inconsistent")
    for prior in prior_rows:
        if prior["event_type"] != "settlement" or prior["event_id"] in reversed_targets:
            continue
        prior_payload = prior["payload"]
        if (
            prior_payload["signal_id"] == signal_id
            and int(prior_payload["horizon"]) == horizon
        ):
            raise PredictionIntegrityError("signal horizon has multiple active settlements")


def _signal_payload(
    signal: PredictionSignal,
    *,
    reference_close: Mapping[str, Any],
    supersedes_signal_id: str | None,
    recording_latest_completed_session: dt.date,
    recording_mode: str,
    calibration_eligible: bool,
) -> dict[str, Any]:
    logical_body = {
        "first_observed_at": signal.first_observed_at,
        "observation_session": signal.observation_session,
        "platform": signal.platform,
        "source_category": signal.source_category,
        "author_id_sha256": signal.author_id_sha256,
        "ticker": signal.ticker,
        "topic": signal.topic,
        "valuation_symbol": signal.valuation_symbol,
        "valuation_exchange_mic": signal.valuation_exchange_mic,
        "valuation_currency": signal.valuation_currency,
        "direction": signal.direction,
        "strength": signal.strength,
        "probability": signal.probability,
        "horizon_sessions": {str(key): value for key, value in signal.horizon_sessions.items()},
        "calendar_contract": _calendar_contract_from_signal(signal),
        "reference_close": reference_close,
        "supersedes_signal_id": supersedes_signal_id,
        "market_regime": signal.market_regime,
        "model_version": signal.model_version,
        "evidence_sha256": list(signal.evidence_sha256),
        "rights": {
            "basis": signal.rights.basis,
            "attestation_sha256": signal.rights.attestation_sha256,
        },
    }
    return {
        **logical_body,
        "logical_signal_fingerprint": _sha256_text(
            _canonical_json(_logical_signal_identity_body(logical_body))
        ),
        "recording_latest_completed_session": recording_latest_completed_session,
        "recording_mode": recording_mode,
        "calibration_eligible": calibration_eligible,
    }


def _logical_signal_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in payload
        if key
        not in {
            "logical_signal_fingerprint",
            "recording_latest_completed_session",
            "recording_mode",
            "calibration_eligible",
        }
    }


def _logical_signal_identity_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Stable source identity; mutable interpretations remain in the full body.

    Re-observing the same sanitized evidence may not create another calibration
    sample merely by changing the timestamp, score, regime, or price lineage.
    Such differences require an explicit reversal/correction.
    """

    identity_fields = {
        "platform",
        "source_category",
        "author_id_sha256",
        "ticker",
        "topic",
        "valuation_symbol",
        "valuation_exchange_mic",
        "valuation_currency",
        "model_version",
        "evidence_sha256",
    }
    if not identity_fields.issubset(payload) or not isinstance(payload.get("rights"), Mapping):
        raise PredictionIntegrityError("logical signal identity body is incomplete")
    identity = {key: payload[key] for key in sorted(identity_fields)}
    identity["rights_basis"] = payload["rights"].get("basis")
    return identity


def _calendar_contract_from_signal(signal: PredictionSignal) -> dict[str, Any]:
    body = {
        "instrument_mic": signal.valuation_exchange_mic,
        "calendar_name": signal.calendar_name,
        "calendar_version": signal.calendar_version,
        "exchange_timezone": signal.exchange_timezone,
        "observation_session_close": signal.observation_session_close,
        "session_path": {str(key): value for key, value in signal.session_path.items()},
    }
    return {
        **body,
        "contract_sha256": _sha256_text(_canonical_json(body)),
    }


def _stored_calendar_contract(
    value: Any,
    *,
    observation_session: dt.date,
    valuation_exchange_mic: str,
    horizon_sessions: Mapping[int, dt.date],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PredictionIntegrityError("stored calendar contract is malformed")
    required = {
        "instrument_mic",
        "calendar_name",
        "calendar_version",
        "exchange_timezone",
        "observation_session_close",
        "session_path",
        "contract_sha256",
    }
    _exact_keys(value, required, "calendar contract")
    body = {key: value[key] for key in required if key != "contract_sha256"}
    try:
        if _digest(value["contract_sha256"], "calendar contract digest") != _sha256_text(
            _canonical_json(body)
        ):
            raise PredictionIntegrityError("stored calendar contract digest is inconsistent")
        mic = _identifier(value["instrument_mic"], "calendar instrument MIC").upper()
        if mic != valuation_exchange_mic or mic not in _SUPPORTED_US_EQUITY_MICS:
            raise PredictionIntegrityError("stored calendar instrument identity is invalid")
        calendar_name = str(value["calendar_name"]).strip().upper()
        if calendar_name != _TRUSTED_CALENDAR_NAME:
            raise PredictionIntegrityError("stored calendar name is invalid")
        calendar_version = str(value["calendar_version"]).strip()
        if not _CALENDAR_VERSION.fullmatch(calendar_version):
            raise PredictionIntegrityError("stored calendar version is malformed")
        exchange_timezone = str(value["exchange_timezone"]).strip()
        if exchange_timezone != _TRUSTED_EXCHANGE_TIMEZONE:
            raise PredictionIntegrityError("stored exchange timezone is invalid")
        observation_close = _aware_text(
            value["observation_session_close"],
            "stored observation session close",
        )
        raw_path = value["session_path"]
        if not isinstance(raw_path, Mapping):
            raise PredictionIntegrityError("stored calendar path is malformed")
        expected_path_keys = {
            str(offset) for offset in range(1, max(_REQUIRED_HORIZONS) + 1)
        }
        if set(raw_path) != expected_path_keys:
            raise PredictionIntegrityError("stored calendar path keys are malformed")
        session_path = {
            int(key): _session(item, f"stored session path {key}")
            for key, item in raw_path.items()
        }
        expected_offsets = tuple(range(1, max(_REQUIRED_HORIZONS) + 1))
        if tuple(sorted(session_path)) != expected_offsets:
            raise PredictionIntegrityError("stored calendar path is incomplete")
        ordered_sessions = tuple(session_path[offset] for offset in expected_offsets)
        if (
            ordered_sessions[0] <= observation_session
            or any(left >= right for left, right in zip(ordered_sessions, ordered_sessions[1:]))
        ):
            raise PredictionIntegrityError("stored calendar path is not strictly ordered")
        if any(horizon_sessions[horizon] != session_path[horizon] for horizon in _REQUIRED_HORIZONS):
            raise PredictionIntegrityError("stored horizon dates differ from the calendar contract")
        return {
            "instrument_mic": mic,
            "calendar_name": calendar_name,
            "calendar_version": calendar_version,
            "exchange_timezone": exchange_timezone,
            "observation_session_close": observation_close,
            "session_path": session_path,
            "contract_sha256": _digest(value["contract_sha256"], "calendar contract digest"),
        }
    except PredictionIntegrityError:
        raise
    except (PredictionValidationError, TypeError, ValueError) as exc:
        raise PredictionIntegrityError("stored calendar contract is malformed") from exc


def _persisted_signal_draft(
    payload: Mapping[str, Any],
    *,
    rights: RightsLineage,
    horizons_raw: Mapping[str, Any],
) -> PredictionSignal:
    first_observed_at = _aware_text(payload["first_observed_at"], "first_observed_at")
    observation_session = _session(payload["observation_session"], "observation_session")
    horizons = {
        int(key): _session(item, f"horizon_sessions[{key}]")
        for key, item in horizons_raw.items()
    }
    if tuple(sorted(horizons)) != _REQUIRED_HORIZONS:
        raise PredictionIntegrityError("stored signal horizons are incomplete")
    valuation_exchange_mic = _identifier(
        payload["valuation_exchange_mic"], "valuation exchange MIC"
    ).upper()
    if valuation_exchange_mic not in _SUPPORTED_US_EQUITY_MICS:
        raise PredictionIntegrityError("stored signal exchange MIC is unsupported")
    valuation_currency = _currency(payload["valuation_currency"])
    if valuation_currency != "USD":
        raise PredictionIntegrityError("stored signal valuation currency is unsupported")
    direction = str(payload["direction"]).strip().lower()
    if direction not in _DIRECTIONS:
        raise PredictionIntegrityError("stored signal direction is invalid")
    evidence_raw = payload["evidence_sha256"]
    if isinstance(evidence_raw, (str, bytes)) or not isinstance(evidence_raw, Sequence):
        raise PredictionIntegrityError("stored signal evidence is malformed")
    evidence = tuple(sorted({_digest(item, "evidence lineage") for item in evidence_raw}))
    if not evidence:
        raise PredictionIntegrityError("stored signal evidence is empty")
    calendar = _stored_calendar_contract(
        payload["calendar_contract"],
        observation_session=observation_session,
        valuation_exchange_mic=valuation_exchange_mic,
        horizon_sessions=horizons,
    )
    if _parse_aware(calendar["observation_session_close"], "stored session close") >= _parse_aware(
        first_observed_at, "first_observed_at"
    ):
        raise PredictionIntegrityError("stored signal precedes its official session close")

    draft = object.__new__(PredictionSignal)
    values: dict[str, Any] = {
        "first_observed_at": first_observed_at,
        "observation_session": observation_session,
        "platform": _closed_identifier(payload["platform"], "platform", _PLATFORMS),
        "source_category": _closed_identifier(
            payload["source_category"], "source_category", _SOURCE_CATEGORIES
        ),
        "author_id_sha256": _digest(payload["author_id_sha256"], "author id"),
        "ticker": None if payload["ticker"] is None else _symbol(payload["ticker"], "ticker"),
        "topic": _topic(payload["topic"]),
        "valuation_symbol": _symbol(payload["valuation_symbol"], "valuation_symbol"),
        "valuation_exchange_mic": valuation_exchange_mic,
        "valuation_currency": valuation_currency,
        "direction": direction,
        "strength": _decimal(payload["strength"], "strength", positive=True, maximum=ONE),
        "probability": _decimal(payload["probability"], "probability", minimum=ZERO, maximum=ONE),
        "horizon_sessions": MappingProxyType(dict(sorted(horizons.items()))),
        "market_regime": _closed_identifier(
            payload["market_regime"], "market_regime", _MARKET_REGIMES
        ),
        "model_version": _prediction_model_version(payload["model_version"]),
        "evidence_sha256": evidence,
        "rights": rights,
        "calendar_name": calendar["calendar_name"],
        "calendar_version": calendar["calendar_version"],
        "exchange_timezone": calendar["exchange_timezone"],
        "observation_session_close": calendar["observation_session_close"],
        "session_path": MappingProxyType(dict(sorted(calendar["session_path"].items()))),
    }
    for field_name, field_value in values.items():
        object.__setattr__(draft, field_name, field_value)
    return draft


def _signal_from_payload(payload: Mapping[str, Any]) -> _RecordedSignal:
    required = {
        "first_observed_at",
        "observation_session",
        "platform",
        "source_category",
        "author_id_sha256",
        "ticker",
        "topic",
        "valuation_symbol",
        "valuation_exchange_mic",
        "valuation_currency",
        "direction",
        "strength",
        "probability",
        "horizon_sessions",
        "calendar_contract",
        "reference_close",
        "supersedes_signal_id",
        "market_regime",
        "model_version",
        "evidence_sha256",
        "rights",
        "logical_signal_fingerprint",
        "recording_latest_completed_session",
        "recording_mode",
        "calibration_eligible",
    }
    _exact_keys(payload, required, "signal")
    rights_raw = payload.get("rights")
    if not isinstance(rights_raw, Mapping):
        raise PredictionIntegrityError("signal rights lineage is malformed")
    _exact_keys(rights_raw, {"basis", "attestation_sha256"}, "rights")
    horizons_raw = payload.get("horizon_sessions")
    if not isinstance(horizons_raw, Mapping):
        raise PredictionIntegrityError("signal horizons are malformed")
    if payload.get("recording_mode") not in {"live_observation", "historical_backfill"}:
        raise PredictionIntegrityError("signal recording mode is malformed")
    expected_eligibility = payload["recording_mode"] == "live_observation"
    if payload.get("calibration_eligible") is not expected_eligibility:
        raise PredictionIntegrityError("signal calibration eligibility is inconsistent")
    try:
        if payload.get("supersedes_signal_id") is not None:
            _digest(payload["supersedes_signal_id"], "supersedes signal")
    except PredictionValidationError as exc:
        raise PredictionIntegrityError("signal replacement linkage is malformed") from exc
    try:
        latest_completed = _session(
            payload.get("recording_latest_completed_session"),
            "recording latest completed session",
        )
    except PredictionValidationError as exc:
        raise PredictionIntegrityError("signal recording classification is malformed") from exc
    try:
        fingerprint = _digest(
            payload.get("logical_signal_fingerprint"), "logical signal fingerprint"
        )
    except PredictionValidationError as exc:
        raise PredictionIntegrityError("logical signal fingerprint is malformed") from exc
    if fingerprint != _sha256_text(
        _canonical_json(_logical_signal_identity_body(payload))
    ):
        raise PredictionIntegrityError("logical signal fingerprint is inconsistent")
    try:
        draft = _persisted_signal_draft(
            payload,
            rights=RightsLineage(**rights_raw),
            horizons_raw=horizons_raw,
        )
        if latest_completed < draft.observation_session:
            raise PredictionIntegrityError("signal recording classification predates its session")
        if (
            latest_completed > draft.observation_session
            and payload["recording_mode"] != "historical_backfill"
        ):
            raise PredictionIntegrityError("signal backfill classification is inconsistent")
        reference = _stored_close_point(payload["reference_close"])
        if _session(reference["session"], "reference close session") != draft.observation_session:
            raise PredictionIntegrityError("reference close session differs from signal")
        if (
            reference["symbol"] != draft.valuation_symbol
            or reference["exchange_mic"] != draft.valuation_exchange_mic
            or reference["currency"] != draft.valuation_currency
        ):
            raise PredictionIntegrityError("reference close identity differs from signal")
        first_observed = _parse_aware(draft.first_observed_at, "first_observed_at")
        official_close = _parse_aware(
            draft.observation_session_close,
            "stored observation session close",
        )
        if reference["calendar_id"] != draft.calendar_name:
            raise PredictionIntegrityError("reference close calendar identity differs from signal")
        for source in reference["sources"]:
            retrieved = _parse_aware(source["retrieved_at"], "reference retrieved_at")
            if retrieved < official_close or retrieved > first_observed:
                raise PredictionIntegrityError("reference close violates causal availability")
        return _RecordedSignal(draft=draft, reference_close=MappingProxyType(reference))
    except (PredictionLedgerError, TypeError, ValueError) as exc:
        raise PredictionIntegrityError("signal payload is malformed") from exc


def _validated_close_point(
    close: AcceptedClose,
    signal: PredictionSignal | _RecordedSignal,
    *,
    role: str,
    available_by: dt.datetime,
) -> dict[str, Any]:
    if role not in {"reference", "outcome"}:
        raise PredictionValidationError("unknown accepted-close role")
    if not isinstance(close, AcceptedClose):
        raise PredictionSettlementBlocked("price anchor must be an AcceptedClose")
    if (
        str(close.status).strip().lower() != "accepted"
        or not close.valuation_permitted
        or not close.price_gate_permitted
        or not close.eligible_for_ledger_input
        or str(close.finality).strip().lower() != "confirmed"
        or close.selected_price is None
        or close.selected_observation_id is None
        or close.corporate_action_reconciliation_required
    ):
        raise PredictionSettlementBlocked("accepted-close price gate did not pass")
    instrument = close.instrument
    if (
        instrument.canonical_symbol != signal.valuation_symbol
        or instrument.exchange_mic != signal.valuation_exchange_mic
        or instrument.currency != signal.valuation_currency
        or close.currency != signal.valuation_currency
        or instrument.price_unit_multiplier != ONE
        or instrument.asset_type not in _SUPPORTED_ASSET_TYPES
        or not instrument.calendar_id
    ):
        raise PredictionSettlementBlocked("accepted-close instrument identity differs from signal")
    if role == "reference" and close.expected_session != signal.observation_session:
        raise PredictionSettlementBlocked("reference close session differs from observation session")
    try:
        resolver = ExchangeSessionResolver()
        provenance = resolver.provenance(signal.valuation_exchange_mic)
        official_close = resolver.session_close(
            close.expected_session,
            signal.valuation_exchange_mic,
        )
    except ExchangeSessionError as exc:
        raise PredictionSettlementBlocked("accepted close is not a real exchange session") from exc
    if instrument.calendar_id != provenance.calendar_name:
        raise PredictionSettlementBlocked(
            "accepted-close calendar identity differs from the trusted exchange calendar"
        )
    if role == "outcome" and isinstance(signal, _RecordedSignal):
        if (
            instrument.asset_type != signal.reference_close["asset_type"]
            or instrument.calendar_id != signal.reference_close["calendar_id"]
        ):
            raise PredictionSettlementBlocked(
                "outcome close identity differs from the immutable reference close"
            )

    policy = CloseAcceptancePolicy(required_currency=signal.valuation_currency)
    observations = tuple(close.observations)
    if not observations:
        raise PredictionSettlementBlocked("accepted close has no observations")
    observation_ids = [item.observation_id for item in observations]
    if len(observation_ids) != len(set(observation_ids)):
        raise PredictionSettlementBlocked("accepted close has duplicate observations")
    rejection_map = {
        item.observation_id: policy.rejection_reasons(item, instrument, close.expected_session)
        for item in observations
    }
    structurally_valid = [item for item in observations if not rejection_map[item.observation_id]]
    independent = []
    seen_groups: set[str] = set()
    for observation in structurally_valid:
        if observation.independence_group not in seen_groups:
            independent.append(observation)
            seen_groups.add(observation.independence_group)
    settlement_sources = [
        item
        for item in independent
        if item.settlement_eligible and item.source_tier in policy.settlement_source_tiers
    ]
    agreement = _pairwise_max_bps(settlement_sources)
    recomputed_reasons: list[str] = []
    for observation_id, reasons in sorted(rejection_map.items()):
        provider_id = next(
            item.provider_id for item in observations if item.observation_id == observation_id
        )
        recomputed_reasons.extend(f"{provider_id}:{reason}" for reason in reasons)
    selected = settlement_sources[0] if settlement_sources else None
    if selected is None or len(settlement_sources) < policy.min_independent_sources:
        raise PredictionSettlementBlocked("accepted close lacks required independent sources")
    if agreement is None or agreement > policy.warning_bps:
        raise PredictionSettlementBlocked("accepted close price agreement exceeds accepted threshold")
    expected_identity = {
        "instrument": instrument.canonical_symbol,
        "exchange_mic": instrument.exchange_mic,
        "expected_session": close.expected_session.isoformat(),
        "status": "accepted",
        "selected_observation_id": selected.observation_id,
        "selected_price": str(selected.raw_close),
        "agreement_bps": str(agreement),
        "independent_groups": [item.independence_group for item in settlement_sources],
        "observation_ids": observation_ids,
        "reasons": recomputed_reasons,
        "price_gate_permitted": True,
    }
    if (
        close.selected_observation_id != selected.observation_id
        or close.independent_source_count != len(settlement_sources)
        or close.agreement_bps != agreement
        or tuple(close.reasons) != tuple(recomputed_reasons)
        or _digest(close.accepted_close_id, "accepted_close_id")
        != _provider_identity_digest(expected_identity)
    ):
        raise PredictionSettlementBlocked("accepted close aggregate was not reproduced")

    source_lineage: list[dict[str, Any]] = []
    first_observed = _parse_aware(signal.first_observed_at, "first_observed_at")
    for observation in settlement_sources:
        retrieved = _parse_aware(observation.retrieved_at, "accepted close retrieved_at")
        if (
            observation.is_mock
            or observation.is_snapshot
            or not observation.settlement_eligible
            or observation.finality != "final"
            or observation.provider_drift_status != "healthy"
            or observation.source_tier in {"mock", "snapshot"}
            or observation.session_date != close.expected_session
            or observation.canonical_symbol != instrument.canonical_symbol
            or observation.exchange_mic != instrument.exchange_mic
            or observation.currency != instrument.currency
            or observation.asset_type != instrument.asset_type
            or observation.calendar_id != instrument.calendar_id
            or observation.bar_kind != "regular_session_close"
            or observation.adjustment_mode != "none"
            or observation.price_unit_multiplier != ONE
            or observation.corporate_action_status != "clear_none"
            or retrieved < official_close
            or retrieved > available_by
            or (role == "reference" and retrieved > first_observed)
            or (role == "outcome" and retrieved <= first_observed)
        ):
            raise PredictionSettlementBlocked("accepted close has ineligible source lineage")
        source_lineage.append(
            {
                "observation_id": _digest(observation.observation_id, "observation id"),
                "provider_id_sha256": _sha256_text(
                    f"provider-id:{_identifier(observation.provider_id, 'provider_id')}"
                ),
                "provider_version_sha256": _sha256_text(
                    "provider-version:"
                    f"{_identifier(observation.provider_version, 'provider_version')}"
                ),
                "independence_group_sha256": _sha256_text(
                    "independence-group:"
                    f"{_identifier(observation.independence_group, 'independence_group')}"
                ),
                "source_tier": _identifier(observation.source_tier, "source_tier"),
                "provider_payload_sha256": _digest(
                    observation.payload_sha256, "provider payload"
                ),
                "raw_close": _decimal(observation.raw_close, "raw close", positive=True),
                "retrieved_at": _aware_text(observation.retrieved_at, "retrieved_at"),
                "corporate_action_status": observation.corporate_action_status,
            }
        )
    groups = {item["independence_group_sha256"] for item in source_lineage}
    providers = {item["provider_id_sha256"] for item in source_lineage}
    if (
        len(groups) < 2
        or len(providers) < 2
        or close.independent_source_count != len(groups)
    ):
        raise PredictionSettlementBlocked("accepted close requires two independent healthy sources")
    if _decimal(close.selected_price, "accepted close price") != _decimal(
        selected.raw_close, "selected raw close"
    ):
        raise PredictionSettlementBlocked("selected price is inconsistent with selected raw close")
    source_lineage.sort(key=lambda item: item["observation_id"])
    accepted_at = max(
        _parse_aware(item["retrieved_at"], "accepted close source retrieved_at")
        for item in source_lineage
    )
    return {
        "session": close.expected_session,
        "symbol": signal.valuation_symbol,
        "exchange_mic": signal.valuation_exchange_mic,
        "currency": signal.valuation_currency,
        "asset_type": instrument.asset_type,
        "calendar_id": instrument.calendar_id,
        "price": _decimal(close.selected_price, "accepted close price", positive=True),
        "accepted_close_id": _digest(close.accepted_close_id, "accepted_close_id"),
        "selected_observation_id": _digest(
            selected.observation_id, "selected observation id"
        ),
        "retrieved_at": _aware_text(selected.retrieved_at, "accepted close retrieved_at"),
        "accepted_at": _aware_text(accepted_at, "accepted close accepted_at"),
        "agreement_bps": agreement,
        "acceptance_reason_codes": sorted(
            {
                reason.split(":", 1)[1] if ":" in reason else reason
                for reason in recomputed_reasons
            }
        ),
        "sources": source_lineage,
    }


def _stored_close_point(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PredictionIntegrityError("stored close point is malformed")
    required = {
        "session",
        "symbol",
        "exchange_mic",
        "currency",
        "asset_type",
        "calendar_id",
        "price",
        "accepted_close_id",
        "selected_observation_id",
        "retrieved_at",
        "accepted_at",
        "agreement_bps",
        "acceptance_reason_codes",
        "sources",
    }
    _exact_keys(value, required, "close point")
    try:
        raw_sources = value["sources"]
        if not isinstance(raw_sources, list) or len(raw_sources) < 2:
            raise PredictionIntegrityError("stored close sources are malformed")
        sources: list[dict[str, Any]] = []
        source_fields = {
            "observation_id",
            "provider_id_sha256",
            "provider_version_sha256",
            "independence_group_sha256",
            "source_tier",
            "provider_payload_sha256",
            "raw_close",
            "retrieved_at",
            "corporate_action_status",
        }
        for raw_source in raw_sources:
            if not isinstance(raw_source, Mapping):
                raise PredictionIntegrityError("stored close source is malformed")
            _exact_keys(raw_source, source_fields, "close source")
            corporate_status = str(raw_source["corporate_action_status"]).strip().lower()
            if corporate_status != "clear_none":
                raise PredictionIntegrityError("stored close corporate-action status is unsafe")
            sources.append(
                {
                    "observation_id": _digest(raw_source["observation_id"], "observation id"),
                    "provider_id_sha256": _digest(
                        raw_source["provider_id_sha256"], "provider id digest"
                    ),
                    "provider_version_sha256": _digest(
                        raw_source["provider_version_sha256"], "provider version digest"
                    ),
                    "independence_group_sha256": _digest(
                        raw_source["independence_group_sha256"],
                        "independence group digest",
                    ),
                    "source_tier": _closed_identifier(
                        raw_source["source_tier"],
                        "source tier",
                        _SETTLEMENT_SOURCE_TIERS,
                    ),
                    "provider_payload_sha256": _digest(
                        raw_source["provider_payload_sha256"], "provider payload"
                    ),
                    "raw_close": _decimal_text(
                        _decimal(raw_source["raw_close"], "raw close", positive=True)
                    ),
                    "retrieved_at": _aware_text(raw_source["retrieved_at"], "retrieved_at"),
                    "corporate_action_status": corporate_status,
                }
            )
        sources.sort(key=lambda item: item["observation_id"])
        groups = {item["independence_group_sha256"] for item in sources}
        providers = {item["provider_id_sha256"] for item in sources}
        selected_id = _digest(value["selected_observation_id"], "selected observation id")
        selected = next((item for item in sources if item["observation_id"] == selected_id), None)
        price = _decimal(value["price"], "close price", positive=True)
        agreement = _decimal(value["agreement_bps"], "agreement_bps", minimum=ZERO)
        reasons = value["acceptance_reason_codes"]
        if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
            raise PredictionIntegrityError("stored acceptance reasons are malformed")
        normalized_reasons = [_identifier(item, "acceptance reason code") for item in reasons]
        if normalized_reasons != sorted(set(normalized_reasons)):
            raise PredictionIntegrityError("stored acceptance reasons are not canonical")
        asset_type = _identifier(value["asset_type"], "asset type")
        if asset_type not in _SUPPORTED_ASSET_TYPES:
            raise PredictionIntegrityError("stored close asset type is unsupported")
        if len(groups) < 2 or len(providers) < 2 or selected is None:
            raise PredictionIntegrityError("stored close lacks independent source lineage")
        if price != _decimal(selected["raw_close"], "selected raw close"):
            raise PredictionIntegrityError("stored selected price is inconsistent")
        accepted_at = _aware_text(value["accepted_at"], "accepted close accepted_at")
        expected_accepted_at = max(
            _parse_aware(item["retrieved_at"], "close source retrieved_at")
            for item in sources
        )
        if _parse_aware(accepted_at, "accepted close accepted_at") != expected_accepted_at:
            raise PredictionIntegrityError("stored accepted-close confirmation time is inconsistent")
        return {
            "session": _session(value["session"], "close session").isoformat(),
            "symbol": _symbol(value["symbol"], "close symbol"),
            "exchange_mic": _identifier(value["exchange_mic"], "exchange mic").upper(),
            "currency": _currency(value["currency"]),
            "asset_type": asset_type,
            "calendar_id": _identifier(value["calendar_id"], "calendar id").upper(),
            "price": _decimal_text(price),
            "accepted_close_id": _digest(value["accepted_close_id"], "accepted close id"),
            "selected_observation_id": selected_id,
            "retrieved_at": _aware_text(value["retrieved_at"], "retrieved_at"),
            "accepted_at": accepted_at,
            "agreement_bps": _decimal_text(agreement),
            "acceptance_reason_codes": normalized_reasons,
            "sources": sources,
        }
    except PredictionValidationError as exc:
        raise PredictionIntegrityError("stored close point is malformed") from exc


def _stored_factor_residual(value: Any) -> FactorResidualEvidence | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise PredictionIntegrityError("factor residual payload is malformed")
    _exact_keys(value, {"residual_return", "as_of", "model_version", "lineage_sha256"}, "factor")
    try:
        return FactorResidualEvidence(
            residual_return=value["residual_return"],
            as_of=value["as_of"],
            model_version=value["model_version"],
            lineage_sha256=value["lineage_sha256"],
        )
    except PredictionValidationError as exc:
        raise PredictionIntegrityError("factor residual payload is malformed") from exc


def _outcome_metrics(
    signal: PredictionSignal,
    points: Sequence[Mapping[str, Any]],
    factor_residual: FactorResidualEvidence | None,
) -> dict[str, Any]:
    with localcontext(_PRIVATE_CONTEXT):
        prices = [_decimal(item["price"], "path price", positive=True) for item in points]
        returns = [price / signal.reference_price - ONE for price in prices]
        direction_sign = Decimal(_DIRECTIONS[signal.direction])
        directional = [direction_sign * value for value in returns]
        raw_return = returns[-1]
        directional_final = directional[-1]
        direction_hit = directional_final > ZERO
        mfe = max([ZERO, *directional])
        mae = min([ZERO, *directional])
        outcome = ONE if direction_hit else ZERO
        brier = (signal.probability - outcome) ** 2
        residual = None if factor_residual is None else factor_residual.residual_return
        return {
            "raw_return": raw_return,
            "residual_return": residual,
            "direction_hit": direction_hit,
            "mfe": mfe,
            "mae": mae,
            "brier": brier,
        }


def _active_state(
    rows: Sequence[Mapping[str, Any]],
    cutoff: dt.datetime | None,
) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[tuple[str, int], Mapping[str, Any]],
    set[str],
]:
    eligible = [
        row
        for row in rows
        if cutoff is None
        or (
            _parse_aware(row["occurred_at"], "occurred_at") <= cutoff
            and _parse_aware(row["created_at"], "created_at") <= cutoff
        )
    ]
    reversed_ids = {
        row["payload"]["target_event_id"]
        for row in eligible
        if row["event_type"] == "reversal"
    }
    signals = {
        row["event_id"]: row
        for row in eligible
        if row["event_type"] == "signal" and row["event_id"] not in reversed_ids
    }
    settlements: dict[tuple[str, int], Mapping[str, Any]] = {}
    for row in eligible:
        if row["event_type"] != "settlement" or row["event_id"] in reversed_ids:
            continue
        signal_id = row["payload"]["signal_id"]
        if signal_id not in signals:
            continue
        key = (signal_id, int(row["payload"]["horizon"]))
        if key in settlements:
            raise PredictionIntegrityError("signal horizon has multiple active settlements")
        settlements[key] = row
    return signals, settlements, reversed_ids


def _outcome_from_rows(
    signal_id: str,
    signal: PredictionSignal,
    settlement_row: Mapping[str, Any],
    *,
    recording_mode: str,
    calibration_eligible: bool,
) -> PredictionOutcome:
    payload = settlement_row["payload"]
    return PredictionOutcome(
        settlement_id=settlement_row["event_id"],
        settlement_event_hash=settlement_row["event_hash"],
        signal_id=signal_id,
        platform=signal.platform,
        topic=signal.topic,
        model_version=signal.model_version,
        market_regime=signal.market_regime,
        horizon=int(payload["horizon"]),
        target_session=_session(payload["target_session"], "target_session"),
        direction=signal.direction,
        strength=signal.strength,
        probability=signal.probability,
        raw_return=_decimal(payload["raw_return"], "raw_return"),
        residual_return=None
        if payload["residual_return"] is None
        else _decimal(payload["residual_return"], "residual_return"),
        direction_hit=bool(payload["direction_hit"]),
        mfe=_decimal(payload["mfe"], "mfe"),
        mae=_decimal(payload["mae"], "mae"),
        brier=_decimal(payload["brier"], "brier", minimum=ZERO, maximum=ONE),
        settled_at=settlement_row["occurred_at"],
        recording_mode=recording_mode,
        calibration_eligible=calibration_eligible,
    )


def _calibration(
    key: tuple[str, str, str, str, int],
    outcomes: Sequence[PredictionOutcome],
    *,
    sample_scope: str,
) -> CalibrationSummary:
    if sample_scope not in {"live_only", "includes_backfill"}:
        raise PredictionValidationError("unknown calibration sample scope")
    hits = [ONE if item.direction_hit else ZERO for item in outcomes]
    residual_pairs = [
        (
            Decimal(_DIRECTIONS[item.direction]) * item.strength * item.probability,
            item.residual_return,
        )
        for item in outcomes
        if item.residual_return is not None
    ]
    residuals = [item[1] for item in residual_pairs]
    return CalibrationSummary(
        platform=key[0],
        topic=key[1],
        model_version=key[2],
        market_regime=key[3],
        horizon=key[4],
        sample_scope=sample_scope,
        sample_count=len(outcomes),
        hit_rate=_mean(hits) if hits else None,
        mean_residual_return=_mean(residuals) if residuals else None,
        residual_sample_count=len(residuals),
        brier=_mean([item.brier for item in outcomes]) if outcomes else None,
        rank_ic=_spearman(residual_pairs),
    )


def _spearman(pairs: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    if len(pairs) < 2:
        return None
    left = _average_ranks([item[0] for item in pairs])
    right = _average_ranks([item[1] for item in pairs])
    if len(set(left)) < 2 or len(set(right)) < 2:
        return None
    left_mean = _mean(left)
    right_mean = _mean(right)
    numerator = sum(
        ((x - left_mean) * (y - right_mean) for x, y in zip(left, right)),
        ZERO,
    )
    left_ss = sum(((x - left_mean) ** 2 for x in left), ZERO)
    right_ss = sum(((y - right_mean) ** 2 for y in right), ZERO)
    if left_ss == ZERO or right_ss == ZERO:
        return None
    with localcontext(_PRIVATE_CONTEXT):
        return numerator / (left_ss * right_ss).sqrt()


def _average_ranks(values: Sequence[Decimal]) -> list[Decimal]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [ZERO] * len(values)
    index = 0
    while index < len(indexed):
        end = index + 1
        while end < len(indexed) and indexed[end][1] == indexed[index][1]:
            end += 1
        average = (Decimal(index + 1) + Decimal(end)) / Decimal("2")
        for position in range(index, end):
            ranks[indexed[position][0]] = average
        index = end
    return ranks


def _normalized_filters(
    platform: str | None,
    topic: str | None,
    model_version: str | None,
    market_regime: str | None,
    horizon: int | None,
) -> tuple[str | None, str | None, str | None, str | None, int | None]:
    return (
        None if platform is None else _closed_identifier(platform, "platform", _PLATFORMS),
        None if topic is None else _topic(topic),
        None if model_version is None else _prediction_model_version(model_version),
        None
        if market_regime is None
        else _closed_identifier(market_regime, "market_regime", _MARKET_REGIMES),
        None if horizon is None else _horizon(horizon),
    )


def _matches(
    signal: PredictionSignal,
    horizon: int,
    filters: tuple[str | None, str | None, str | None, str | None, int | None],
) -> bool:
    platform, topic, model_version, regime, target_horizon = filters
    return all(
        (
            platform is None or signal.platform == platform,
            topic is None or signal.topic == topic,
            model_version is None or signal.model_version == model_version,
            regime is None or signal.market_regime == regime,
            target_horizon is None or horizon == target_horizon,
        )
    )


def _row_by_idempotency(
    rows: Sequence[Mapping[str, Any]], key_hash: str
) -> Mapping[str, Any] | None:
    return next((row for row in rows if row["idempotency_hash"] == key_hash), None)


def _head(rows: Sequence[Mapping[str, Any]]) -> tuple[int, str]:
    if not rows:
        return 0, _GENESIS_HASH
    return int(rows[-1]["sequence_no"]), str(rows[-1]["event_hash"])


def _checkpoint_anchor(value: Any, label: str) -> tuple[int, str]:
    if not isinstance(value, Mapping) or set(value) != {"sequence", "head_hash"}:
        raise PredictionValidationError(f"{label} checkpoint anchor is malformed")
    sequence = value["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise PredictionValidationError(f"{label} checkpoint sequence is malformed")
    head_hash = _digest(value["head_hash"], f"{label} checkpoint head")
    if sequence == 0 and head_hash != _GENESIS_HASH:
        raise PredictionValidationError(f"{label} genesis checkpoint is malformed")
    return sequence, head_hash


def _checkpoint_pending(value: Any) -> dict[str, Any]:
    required = {"tx_id", "from_sequence", "from_head", "to_sequence", "to_head"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise PredictionValidationError("pending checkpoint is malformed")
    tx_id = str(value["tx_id"]).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", tx_id):
        raise PredictionValidationError("pending checkpoint transaction id is malformed")
    from_sequence = value["from_sequence"]
    to_sequence = value["to_sequence"]
    if (
        isinstance(from_sequence, bool)
        or not isinstance(from_sequence, int)
        or from_sequence < 0
        or isinstance(to_sequence, bool)
        or not isinstance(to_sequence, int)
        or to_sequence < 1
    ):
        raise PredictionValidationError("pending checkpoint sequence is malformed")
    return {
        "tx_id": tx_id,
        "from_sequence": from_sequence,
        "from_head": _digest(value["from_head"], "pending from head"),
        "to_sequence": to_sequence,
        "to_head": _digest(value["to_head"], "pending to head"),
    }


def _normalized_sql(value: str) -> str:
    return "".join(value.casefold().split()).rstrip(";")


def _event_type(value: Any) -> str:
    normalized = str(value).strip().lower()
    if normalized not in {"signal", "settlement", "reversal", "idempotency_alias"}:
        raise PredictionValidationError("unknown prediction event type")
    return normalized


def _idempotency_hash(value: Any) -> str:
    normalized = str(value).strip()
    lowered = normalized.casefold()
    if (
        not normalized
        or len(normalized) > 512
        or any(ord(character) < 32 for character in normalized)
        or "://" in normalized
        or "?" in normalized
        or any(marker in lowered for marker in ("secret=", "token=", "api_key="))
    ):
        raise PredictionValidationError("idempotency key is empty, too long, or unsafe")
    return _sha256_text(normalized)


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise PredictionValidationError(f"{field_name} must be text")
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    if not _SAFE_IDENTIFIER.fullmatch(normalized):
        raise PredictionValidationError(f"{field_name} must be a safe identifier")
    return normalized


def _closed_identifier(value: Any, field_name: str, allowed: frozenset[str]) -> str:
    normalized = _identifier(value, field_name)
    if normalized not in allowed:
        raise PredictionValidationError(f"{field_name} is outside the closed taxonomy")
    return normalized


def _prediction_model_version(value: Any) -> str:
    normalized = _identifier(value, "model_version")
    if not _MODEL_VERSION.fullmatch(normalized):
        raise PredictionValidationError("model_version is outside the controlled version namespace")
    return normalized


def _factor_model_version(value: Any) -> str:
    normalized = _identifier(value, "factor model version")
    if not _FACTOR_MODEL_VERSION.fullmatch(normalized):
        raise PredictionValidationError(
            "factor model version is outside the controlled version namespace"
        )
    return normalized


def _topic(value: Any) -> str:
    if not isinstance(value, str):
        raise PredictionValidationError("topic must be text")
    normalized = value.strip()
    if normalized != value or not _TOPIC_SLUG.fullmatch(normalized):
        raise PredictionValidationError("topic must be a lowercase ASCII taxonomy slug")
    return normalized


def _currency(value: Any) -> str:
    if not isinstance(value, str):
        raise PredictionValidationError("currency must be text")
    normalized = value.strip().upper()
    if not _CURRENCY.fullmatch(normalized):
        raise PredictionValidationError("currency must be a three-letter code")
    return normalized


def _symbol(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise PredictionValidationError(f"{field_name} must be text")
    normalized = value.strip().upper()
    if not _SAFE_SYMBOL.fullmatch(normalized):
        raise PredictionValidationError(f"{field_name} is not a safe canonical symbol")
    return normalized


def _digest(value: Any, field_name: str) -> str:
    normalized = str(value).strip().lower()
    if not _HEX_64.fullmatch(normalized):
        raise PredictionValidationError(f"{field_name} must be a SHA-256 hex digest")
    return normalized


def _horizon(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in _REQUIRED_HORIZONS:
        raise PredictionValidationError("horizon must be one of 1, 5, 20, or 60")
    return value


def _session(value: dt.date | str, field_name: str) -> dt.date:
    if isinstance(value, dt.datetime):
        raise PredictionValidationError(f"{field_name} must be a date, not datetime")
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError as exc:
            raise PredictionValidationError(f"{field_name} must be ISO YYYY-MM-DD") from exc
    raise PredictionValidationError(f"{field_name} must be a date")


def _parse_aware(value: dt.datetime | str, field_name: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise PredictionValidationError(f"{field_name} may not be empty")
        try:
            moment = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError as exc:
            raise PredictionValidationError(f"{field_name} must be an ISO datetime") from exc
    else:
        raise PredictionValidationError(f"{field_name} must be a timezone-aware datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise PredictionValidationError(f"{field_name} must include a timezone")
    return moment.astimezone(dt.timezone.utc)


def _aware_text(value: dt.datetime | str, field_name: str) -> str:
    return _parse_aware(value, field_name).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _decimal(
    value: Any,
    field_name: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, (float, bool)):
        raise PredictionValidationError(f"{field_name} must not use binary floating point")
    if not isinstance(value, (Decimal, int, str)):
        raise PredictionValidationError(f"{field_name} must be Decimal-compatible text or integer")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise PredictionValidationError(f"{field_name} is not a valid decimal") from exc
    if not result.is_finite():
        raise PredictionValidationError(f"{field_name} must be finite")
    if positive and result <= ZERO:
        raise PredictionValidationError(f"{field_name} must be positive")
    if minimum is not None and result < minimum:
        raise PredictionValidationError(f"{field_name} is below its minimum")
    if maximum is not None and result > maximum:
        raise PredictionValidationError(f"{field_name} exceeds its maximum")
    return result


def _decimal_text(value: Decimal) -> str:
    if value == ZERO:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _canonical_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.casefold() in _FORBIDDEN_KEYS:
        raise PredictionValidationError("raw content, identity, URL, and secret fields are forbidden")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise PredictionValidationError("payload decimals must be finite")
        return _decimal_text(value)
    if isinstance(value, dt.datetime):
        return _aware_text(value, key or "datetime")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            text_key = str(raw_key)
            if text_key in normalized:
                raise PredictionValidationError("payload has duplicate normalized keys")
            normalized[text_key] = _canonical_value(item, key=text_key)
        return {item_key: normalized[item_key] for item_key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        raise PredictionValidationError("binary floating point is forbidden")
    if value is None or isinstance(value, (str, int, bool)):
        if isinstance(value, str) and ("://" in value or "?" in value):
            raise PredictionValidationError("URLs and query parameters are forbidden")
        return value
    raise PredictionValidationError(f"unsupported payload type: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise PredictionIntegrityError(f"{label} fields do not match the schema")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _chain_hash(previous_hash: str, body: Mapping[str, Any]) -> str:
    return _sha256_text(previous_hash + "\n" + _canonical_json(body))


def _provider_identity_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pairwise_max_bps(observations: Sequence[Any]) -> Decimal | None:
    if len(observations) < 2:
        return None
    maximum = ZERO
    with localcontext(_PRIVATE_CONTEXT):
        for index, left in enumerate(observations):
            for right in observations[index + 1 :]:
                midpoint = (left.raw_close + right.raw_close) / Decimal("2")
                difference = abs(left.raw_close - right.raw_close) / midpoint * Decimal("10000")
                maximum = max(maximum, difference)
    return maximum


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise PredictionValidationError("mean requires at least one value")
    with localcontext(_PRIVATE_CONTEXT):
        return sum(values, ZERO) / Decimal(len(values))


__all__ = [
    "CalibrationSummary",
    "FactorResidualEvidence",
    "PredictionCommitUnknown",
    "PredictionEventReceipt",
    "PredictionIdempotencyConflict",
    "PredictionIntegrityError",
    "PredictionLedger",
    "PredictionLedgerError",
    "PredictionLedgerPolicy",
    "PredictionOutcome",
    "PredictionSettlementBlocked",
    "PredictionSignal",
    "PredictionValidationError",
    "PredictionWeightState",
    "PREDICTION_WEIGHT_MARKET_REGIMES",
    "PREDICTION_WEIGHT_MODEL_VERSIONS",
    "PREDICTION_WEIGHT_REASON_CODES",
    "PREDICTION_WEIGHT_TOPICS",
    "RightsLineage",
]
