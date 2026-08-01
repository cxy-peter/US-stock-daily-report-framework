"""Auditable, fail-closed acceptance of official U.S. daily closes.

This module intentionally does not replace :mod:`serenity_monitor.data`.
The legacy providers remain useful for research and display.  The classes here
form a separate boundary for prices that may later be offered to a private
ledger.  No class in this module writes a ledger or executes a trade.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol, Sequence

import requests


Clock = Callable[[], dt.datetime]
Sleeper = Callable[[float], None]

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_PROVIDER_FAILURE_DETAILS = {
    "authentication_error": "authentication rejected",
    "http_error": "non-retriable HTTP failure",
    "identity_mismatch": "provider identity mismatch",
    "lineage_mismatch": "registered provider lineage mismatch",
    "missing_credentials": "API credentials are not configured",
    "not_found": "requested close was not found",
    "provider_error": "provider request failed",
    "rate_limited": "provider request was rate limited",
    "schema_error": "provider response failed schema validation",
    "session_mismatch": "expected session is absent",
    "transient_error": "temporary provider failure",
}


def _default_clock() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _rfc3339(value: dt.datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    utc = value.astimezone(dt.timezone.utc)
    return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalized_rfc3339(value: str | dt.datetime) -> str:
    if isinstance(value, dt.datetime):
        return _rfc3339(value)
    text = str(value).strip()
    if not text:
        raise ValueError("timestamp may not be empty")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be RFC3339") from exc
    return _rfc3339(parsed)


def _canonical_json(value: Any) -> bytes:
    def default(item: Any) -> str:
        if isinstance(item, (dt.date, dt.datetime, Decimal)):
            return str(item)
        raise TypeError(f"unsupported canonical JSON value: {type(item).__name__}")

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=default,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} is not a valid decimal") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field_name} must be finite and positive")
    return result


def _normalized_identifier(value: Any, field_name: str) -> str:
    result = str(value).strip().lower()
    if not _IDENTIFIER.fullmatch(result):
        raise ValueError(f"{field_name} is not a safe identifier")
    return result


def _safe_provider_failure(provider_id: str, status: Any) -> tuple[str, str]:
    normalized = str(status).strip().lower()
    if normalized not in _PROVIDER_FAILURE_DETAILS:
        normalized = "provider_error"
    return normalized, f"{provider_id}: {_PROVIDER_FAILURE_DETAILS[normalized]}"


def _normalized_date(value: dt.date | str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError("session date must be ISO YYYY-MM-DD") from exc


@dataclass(frozen=True)
class InstrumentRef:
    """Canonical identity supplied by the private runtime configuration."""

    canonical_symbol: str
    asset_type: str
    exchange_mic: str
    currency: str = "USD"
    calendar_id: str = "XNYS"
    price_unit_multiplier: Decimal = Decimal("1")
    provider_symbols: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        symbol = str(self.canonical_symbol).strip().upper()
        mic = str(self.exchange_mic).strip().upper()
        currency = str(self.currency).strip().upper()
        if not symbol or not self.asset_type or not mic or not currency or not self.calendar_id:
            raise ValueError("instrument identity fields may not be empty")
        object.__setattr__(self, "canonical_symbol", symbol)
        object.__setattr__(self, "asset_type", str(self.asset_type).strip().lower())
        object.__setattr__(self, "exchange_mic", mic)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "calendar_id", str(self.calendar_id).strip().upper())
        object.__setattr__(
            self,
            "price_unit_multiplier",
            _positive_decimal(self.price_unit_multiplier, "price_unit_multiplier"),
        )
        provider_symbols = {
            str(key).strip().lower(): str(value).strip().upper()
            for key, value in self.provider_symbols.items()
        }
        if any(not key or not value for key, value in provider_symbols.items()):
            raise ValueError("provider symbol mappings may not be empty")
        object.__setattr__(self, "provider_symbols", provider_symbols)

    def symbol_for(self, provider_id: str) -> str:
        return self.provider_symbols.get(provider_id.lower(), self.canonical_symbol)


@dataclass(frozen=True)
class CloseObservation:
    """One provider's immutable statement about one regular-session close."""

    provider_id: str
    provider_version: str
    independence_group: str
    source_tier: str
    settlement_eligible: bool
    canonical_symbol: str
    provider_symbol: str
    exchange_mic: str
    session_date: dt.date
    raw_close: Decimal
    currency: str
    exchange_timezone: str
    bar_kind: str
    adjustment_mode: str
    price_unit_multiplier: Decimal
    retrieved_at: str | dt.datetime
    payload_sha256: str
    finality: str = "final"
    corporate_action_status: str = "not_checked"
    provider_drift_status: str = "healthy"
    is_mock: bool = False
    is_snapshot: bool = False
    asset_type: str = ""
    exchange_mic_provenance: str = "provider_meta"
    calendar_id: str = "XNYS"
    currency_provenance: str = "provider_meta"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _normalized_identifier(self.provider_id, "provider_id"),
        )
        object.__setattr__(
            self,
            "provider_version",
            _normalized_identifier(self.provider_version, "provider_version"),
        )
        object.__setattr__(
            self,
            "independence_group",
            _normalized_identifier(self.independence_group, "independence_group"),
        )
        object.__setattr__(
            self,
            "source_tier",
            _normalized_identifier(self.source_tier, "source_tier"),
        )
        object.__setattr__(self, "canonical_symbol", str(self.canonical_symbol).strip().upper())
        object.__setattr__(self, "provider_symbol", str(self.provider_symbol).strip().upper())
        object.__setattr__(self, "asset_type", str(self.asset_type).strip().lower())
        object.__setattr__(self, "exchange_mic", str(self.exchange_mic).strip().upper())
        object.__setattr__(self, "currency", str(self.currency).strip().upper())
        object.__setattr__(self, "exchange_timezone", str(self.exchange_timezone).strip())
        object.__setattr__(self, "bar_kind", str(self.bar_kind).strip().lower())
        object.__setattr__(self, "adjustment_mode", str(self.adjustment_mode).strip().lower())
        object.__setattr__(self, "finality", str(self.finality).strip().lower())
        object.__setattr__(
            self,
            "corporate_action_status",
            str(self.corporate_action_status).strip().lower(),
        )
        object.__setattr__(
            self,
            "provider_drift_status",
            str(self.provider_drift_status).strip().lower(),
        )
        object.__setattr__(self, "calendar_id", str(self.calendar_id).strip().upper())
        if not isinstance(self.settlement_eligible, bool):
            raise ValueError("settlement_eligible must be boolean")
        if not isinstance(self.is_mock, bool) or not isinstance(self.is_snapshot, bool):
            raise ValueError("mock and snapshot lineage flags must be boolean")
        object.__setattr__(self, "session_date", _normalized_date(self.session_date))
        object.__setattr__(self, "retrieved_at", _normalized_rfc3339(self.retrieved_at))
        object.__setattr__(self, "raw_close", _positive_decimal(self.raw_close, "raw_close"))
        object.__setattr__(
            self,
            "price_unit_multiplier",
            _positive_decimal(self.price_unit_multiplier, "price_unit_multiplier"),
        )
        if not all(
            (
                self.provider_id,
                self.provider_version,
                self.independence_group,
                self.source_tier,
                self.canonical_symbol,
                self.provider_symbol,
            )
        ):
            raise ValueError("provider and instrument lineage fields may not be empty")
        if len(self.payload_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.payload_sha256.lower()):
            raise ValueError("payload_sha256 must be a SHA-256 hex digest")

    @property
    def observation_id(self) -> str:
        return _sha256(
            {
                "provider_id": self.provider_id,
                "provider_version": self.provider_version,
                "independence_group": self.independence_group,
                "source_tier": self.source_tier,
                "settlement_eligible": self.settlement_eligible,
                "canonical_symbol": self.canonical_symbol,
                "provider_symbol": self.provider_symbol,
                "asset_type": self.asset_type,
                "session_date": self.session_date.isoformat(),
                "raw_close": str(self.raw_close),
                "currency": self.currency,
                "exchange_mic": self.exchange_mic,
                "calendar_id": self.calendar_id,
                "bar_kind": self.bar_kind,
                "adjustment_mode": self.adjustment_mode,
                "finality": self.finality,
                "price_unit_multiplier": str(self.price_unit_multiplier),
            }
        )


@dataclass(frozen=True)
class ProviderAttempt:
    provider_id: str
    status: str
    detail: str
    observed_at: str
    latency_ms: int = 0
    cached: bool = False
    observation_id: str | None = None


@dataclass(frozen=True)
class _ProviderRegistration:
    provider_id: str
    provider_version: str
    independence_group: str
    source_tier: str
    settlement_eligible: bool

    @classmethod
    def from_provider(cls, provider: CloseProvider) -> "_ProviderRegistration":
        provider_id = _normalized_identifier(
            getattr(provider, "provider_id", ""),
            "provider_id",
        )
        provider_version = _normalized_identifier(
            getattr(provider, "provider_version", ""),
            "provider_version",
        )
        independence_group = _normalized_identifier(
            getattr(provider, "independence_group", ""),
            "independence_group",
        )
        source_tier = _normalized_identifier(
            getattr(provider, "source_tier", ""),
            "source_tier",
        )
        settlement_eligible = getattr(provider, "settlement_eligible", None)
        if not isinstance(settlement_eligible, bool):
            raise ValueError("registered provider settlement_eligible must be boolean")
        return cls(
            provider_id=provider_id,
            provider_version=provider_version,
            independence_group=independence_group,
            source_tier=source_tier,
            settlement_eligible=settlement_eligible,
        )

    def matches(self, observation: CloseObservation) -> bool:
        return all(
            (
                observation.provider_id == self.provider_id,
                observation.provider_version == self.provider_version,
                observation.independence_group == self.independence_group,
                observation.source_tier == self.source_tier,
                observation.settlement_eligible is self.settlement_eligible,
            )
        )


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    timeout_seconds: float = 15.0
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    retriable_status_codes: tuple[int, ...] = (408, 425, 429, 500, 502, 503, 504)

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.timeout_seconds <= 0:
            raise ValueError("retry attempts and timeout must be positive")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays may not be negative")

    def delay_after(self, failed_attempt: int) -> float:
        return min(self.max_delay_seconds, self.base_delay_seconds * (2 ** max(failed_attempt - 1, 0)))


class CloseProviderError(RuntimeError):
    """A deliberately sanitized provider failure safe for audit artifacts."""

    def __init__(self, status: str, detail: str, *, retriable: bool = False) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.retriable = retriable


class CloseProvider(Protocol):
    provider_id: str
    provider_version: str
    independence_group: str
    source_tier: str
    settlement_eligible: bool

    def fetch_close(self, instrument: InstrumentRef, expected_session: dt.date) -> CloseObservation:
        ...


class _JsonCloseProvider:
    provider_id = "provider"
    provider_version = "v1"
    independence_group = "provider"
    source_tier = "primary"
    settlement_eligible = True
    api_key_env = ""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        clock: Clock = _default_clock,
        sleep: Sleeper = time.sleep,
        retry_policy: RetryPolicy | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.clock = clock
        self.sleep = sleep
        self.retry_policy = retry_policy or RetryPolicy()
        self.environ = os.environ if environ is None else environ

    def _key(self) -> str:
        key = str(self.environ.get(self.api_key_env, "")).strip()
        if not key:
            raise CloseProviderError("missing_credentials", f"{self.provider_id}: API key is not configured")
        return key

    def _request_json(self, url: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        last_status = "transient_error"
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                response = self.session.get(
                    url,
                    params=dict(params),
                    timeout=self.retry_policy.timeout_seconds,
                )
                status_code = int(getattr(response, "status_code", 200))
                if status_code in (401, 403):
                    raise CloseProviderError("authentication_error", f"{self.provider_id}: authentication rejected")
                if status_code == 404:
                    raise CloseProviderError("not_found", f"{self.provider_id}: close endpoint not found")
                if status_code in self.retry_policy.retriable_status_codes:
                    last_status = "rate_limited" if status_code == 429 else "transient_error"
                    raise CloseProviderError(
                        last_status,
                        f"{self.provider_id}: temporary HTTP failure ({status_code})",
                        retriable=True,
                    )
                if status_code >= 400:
                    raise CloseProviderError("http_error", f"{self.provider_id}: HTTP failure ({status_code})")
                payload = response.json()
                if not isinstance(payload, Mapping):
                    raise CloseProviderError("schema_error", f"{self.provider_id}: JSON root is not an object")
                return payload
            except CloseProviderError as exc:
                if not exc.retriable or attempt >= self.retry_policy.max_attempts:
                    raise
                last_status = exc.status
            except (requests.Timeout, requests.ConnectionError):
                last_status = "transient_error"
                if attempt >= self.retry_policy.max_attempts:
                    raise CloseProviderError(last_status, f"{self.provider_id}: network request failed") from None
            except (ValueError, json.JSONDecodeError):
                raise CloseProviderError("schema_error", f"{self.provider_id}: response is not valid JSON") from None
            except Exception:
                # Never copy arbitrary exception text: it can contain the full
                # request URL, including the API-key query parameter.
                raise CloseProviderError("provider_error", f"{self.provider_id}: unexpected provider failure") from None
            self.sleep(self.retry_policy.delay_after(attempt))
        raise CloseProviderError(last_status, f"{self.provider_id}: request failed")

    def _observation(
        self,
        *,
        instrument: InstrumentRef,
        provider_symbol: str,
        expected_session: dt.date,
        raw_close: Any,
        payload: Mapping[str, Any],
        currency: str,
        currency_provenance: str,
        exchange_mic: str,
        exchange_mic_provenance: str,
        exchange_timezone: str,
    ) -> CloseObservation:
        return CloseObservation(
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            independence_group=self.independence_group,
            source_tier=self.source_tier,
            settlement_eligible=self.settlement_eligible,
            canonical_symbol=instrument.canonical_symbol,
            provider_symbol=provider_symbol,
            asset_type=instrument.asset_type,
            exchange_mic=exchange_mic,
            exchange_mic_provenance=exchange_mic_provenance,
            calendar_id=instrument.calendar_id,
            session_date=expected_session,
            raw_close=_positive_decimal(raw_close, "raw_close"),
            currency=currency,
            currency_provenance=currency_provenance,
            exchange_timezone=exchange_timezone,
            bar_kind="regular_session_close",
            adjustment_mode="none",
            price_unit_multiplier=instrument.price_unit_multiplier,
            retrieved_at=_rfc3339(self.clock()),
            payload_sha256=_sha256(payload),
        )


class TwelveDataCloseProvider(_JsonCloseProvider):
    """Twelve Data daily close with adjustment and extended hours disabled."""

    provider_id = "twelve_data"
    provider_version = "time_series_v1"
    independence_group = "twelve_data"
    source_tier = "primary"
    api_key_env = "TWELVE_DATA_API_KEY"
    endpoint = "https://api.twelvedata.com/time_series"

    def fetch_close(self, instrument: InstrumentRef, expected_session: dt.date) -> CloseObservation:
        expected_session = _normalized_date(expected_session)
        provider_symbol = instrument.symbol_for(self.provider_id)
        payload = self._request_json(
            self.endpoint,
            {
                "symbol": provider_symbol,
                "interval": "1day",
                # Twelve Data exposes an exact ``date`` parameter.  A
                # date-only ``end_date`` is a midnight upper bound and can
                # exclude that session's daily bar, so same-day start/end
                # ranges are deliberately avoided for backfills.
                "date": expected_session.isoformat(),
                "order": "ASC",
                "timezone": "Exchange",
                "adjust": "none",
                "prepost": "false",
                "apikey": self._key(),
            },
        )
        if str(payload.get("status", "")).lower() == "error" or payload.get("code"):
            code = str(payload.get("code", ""))
            status = "rate_limited" if code == "429" else "provider_error"
            raise CloseProviderError(status, f"{self.provider_id}: provider returned an error response")
        meta = payload.get("meta")
        values = payload.get("values")
        if not isinstance(meta, Mapping) or not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise CloseProviderError("schema_error", f"{self.provider_id}: required meta or values are missing")
        returned_symbol = str(meta.get("symbol", "")).strip().upper()
        if returned_symbol and returned_symbol != provider_symbol.upper():
            raise CloseProviderError("identity_mismatch", f"{self.provider_id}: returned symbol does not match request")
        if str(meta.get("interval", "")).strip().lower() != "1day":
            raise CloseProviderError("schema_error", f"{self.provider_id}: returned interval is not daily")
        row: Mapping[str, Any] | None = None
        for candidate in values:
            if isinstance(candidate, Mapping) and str(candidate.get("datetime", ""))[:10] == expected_session.isoformat():
                row = candidate
                break
        if row is None:
            raise CloseProviderError("session_mismatch", f"{self.provider_id}: expected session is absent")
        currency = str(meta.get("currency", "")).strip().upper()
        mic = str(meta.get("mic_code") or meta.get("exchange_mic") or meta.get("mic") or "").strip().upper()
        timezone = str(meta.get("exchange_timezone") or meta.get("timezone") or "").strip()
        return self._observation(
            instrument=instrument,
            provider_symbol=provider_symbol,
            expected_session=expected_session,
            raw_close=row.get("close"),
            payload=payload,
            currency=currency,
            currency_provenance="provider_meta",
            exchange_mic=mic,
            exchange_mic_provenance="provider_meta",
            exchange_timezone=timezone,
        )


class AlphaVantageCloseProvider(_JsonCloseProvider):
    """Alpha Vantage's raw/as-traded TIME_SERIES_DAILY close."""

    provider_id = "alpha_vantage"
    provider_version = "time_series_daily_v1"
    independence_group = "alpha_vantage"
    source_tier = "secondary"
    api_key_env = "ALPHA_VANTAGE_API_KEY"
    endpoint = "https://www.alphavantage.co/query"

    def __init__(self, **kwargs: Any) -> None:
        # The free service has a tight daily quota; do not retry by default.
        if "retry_policy" not in kwargs:
            kwargs["retry_policy"] = RetryPolicy(max_attempts=1)
        super().__init__(**kwargs)

    def fetch_close(self, instrument: InstrumentRef, expected_session: dt.date) -> CloseObservation:
        expected_session = _normalized_date(expected_session)
        provider_symbol = instrument.symbol_for(self.provider_id)
        payload = self._request_json(
            self.endpoint,
            {
                "function": "TIME_SERIES_DAILY",
                "symbol": provider_symbol,
                "outputsize": "compact",
                "datatype": "json",
                "apikey": self._key(),
            },
        )
        if "Error Message" in payload:
            raise CloseProviderError("not_found", f"{self.provider_id}: symbol or request was rejected")
        if "Note" in payload:
            raise CloseProviderError("rate_limited", f"{self.provider_id}: request quota was reached")
        if "Information" in payload:
            raise CloseProviderError("provider_error", f"{self.provider_id}: provider returned an information response")
        meta = payload.get("Meta Data")
        series = payload.get("Time Series (Daily)")
        if not isinstance(meta, Mapping) or not isinstance(series, Mapping):
            raise CloseProviderError("schema_error", f"{self.provider_id}: daily series is missing")
        returned_symbol = str(meta.get("2. Symbol", "")).strip().upper()
        if returned_symbol and returned_symbol != provider_symbol.upper():
            raise CloseProviderError("identity_mismatch", f"{self.provider_id}: returned symbol does not match request")
        row = series.get(expected_session.isoformat())
        if not isinstance(row, Mapping):
            raise CloseProviderError("session_mismatch", f"{self.provider_id}: expected session is absent")
        return self._observation(
            instrument=instrument,
            provider_symbol=provider_symbol,
            expected_session=expected_session,
            raw_close=row.get("4. close"),
            payload=payload,
            # TIME_SERIES_DAILY does not return currency or MIC.  Their origin
            # is explicit so downstream audits do not mistake config for vendor metadata.
            currency=instrument.currency,
            currency_provenance="configured_identity",
            exchange_mic=instrument.exchange_mic,
            exchange_mic_provenance="configured_identity",
            exchange_timezone="configured_exchange",
        )


@dataclass(frozen=True)
class CloseAcceptancePolicy:
    required_currency: str = "USD"
    required_bar_kind: str = "regular_session_close"
    required_adjustment_mode: str = "none"
    required_price_unit_multiplier: Decimal = Decimal("1")
    min_independent_sources: int = 2
    warning_bps: Decimal = Decimal("30")
    block_bps: Decimal = Decimal("75")
    allow_warning_settlement: bool = False
    settlement_source_tiers: tuple[str, ...] = ("primary", "secondary", "contracted_api")

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_currency", self.required_currency.strip().upper())
        object.__setattr__(self, "required_bar_kind", self.required_bar_kind.strip().lower())
        object.__setattr__(
            self,
            "required_adjustment_mode",
            self.required_adjustment_mode.strip().lower(),
        )
        object.__setattr__(self, "required_price_unit_multiplier", Decimal(str(self.required_price_unit_multiplier)))
        object.__setattr__(self, "warning_bps", Decimal(str(self.warning_bps)))
        object.__setattr__(self, "block_bps", Decimal(str(self.block_bps)))
        object.__setattr__(
            self,
            "settlement_source_tiers",
            tuple(str(item).strip().lower() for item in self.settlement_source_tiers),
        )
        if self.min_independent_sources < 2:
            raise ValueError("settlement requires at least two independent sources")
        if self.warning_bps < 0 or self.block_bps < self.warning_bps:
            raise ValueError("basis-point thresholds are invalid")

    def rejection_reasons(
        self,
        observation: CloseObservation,
        instrument: InstrumentRef,
        expected_session: dt.date,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if observation.canonical_symbol != instrument.canonical_symbol:
            reasons.append("canonical_symbol_mismatch")
        if observation.provider_symbol != instrument.symbol_for(observation.provider_id):
            reasons.append("provider_symbol_mismatch")
        if observation.asset_type and observation.asset_type != instrument.asset_type:
            reasons.append("asset_type_mismatch")
        if observation.session_date != expected_session:
            reasons.append("session_mismatch")
        if observation.currency != self.required_currency or observation.currency != instrument.currency:
            reasons.append("currency_mismatch")
        if observation.exchange_mic != instrument.exchange_mic:
            reasons.append("exchange_mic_mismatch")
        if observation.calendar_id and observation.calendar_id != instrument.calendar_id:
            reasons.append("calendar_mismatch")
        if not observation.exchange_timezone:
            reasons.append("exchange_timezone_missing")
        if observation.bar_kind != self.required_bar_kind:
            reasons.append("bar_kind_not_regular_close")
        if observation.adjustment_mode != self.required_adjustment_mode:
            reasons.append("adjustment_mode_not_none")
        if observation.price_unit_multiplier != self.required_price_unit_multiplier:
            reasons.append("price_unit_mismatch")
        if observation.finality != "final":
            reasons.append("close_not_final")
        if observation.is_mock:
            reasons.append("mock_source_forbidden")
        if observation.is_snapshot:
            reasons.append("snapshot_source_forbidden")
        if observation.source_tier.lower() == "mock":
            reasons.append("mock_source_forbidden")
        if observation.source_tier.lower() == "snapshot":
            reasons.append("snapshot_source_forbidden")
        if observation.provider_drift_status.lower() in {"quarantined", "blocked"}:
            reasons.append("provider_quarantined")
        return tuple(reasons)


@dataclass(frozen=True)
class AcceptedClose:
    accepted_close_id: str
    instrument: InstrumentRef
    expected_session: dt.date
    status: str
    selected_observation_id: str | None
    selected_price: Decimal | None
    currency: str
    agreement_bps: Decimal | None
    independent_source_count: int
    observations: tuple[CloseObservation, ...]
    attempts: tuple[ProviderAttempt, ...]
    reasons: tuple[str, ...]
    valuation_permitted: bool
    price_gate_permitted: bool
    finality: str
    corporate_action_reconciliation_required: bool = True
    atomic_batch_permitted: bool | None = None

    @property
    def price(self) -> Decimal | None:
        """Compatibility alias; the price is always one selected source, never an average."""
        return self.selected_price

    @property
    def valuation_allowed(self) -> bool:
        return self.valuation_permitted

    @property
    def price_gate_passed(self) -> bool:
        """True only for this registry's price gate; downstream gates still apply."""
        return self.price_gate_permitted

    @property
    def eligible_for_ledger_input(self) -> bool:
        """Require an accepted atomic batch before downstream ledger validation."""
        return self.price_gate_permitted and self.atomic_batch_permitted is True

    @property
    def selected_provider_id(self) -> str | None:
        for observation in self.observations:
            if observation.observation_id == self.selected_observation_id:
                return observation.provider_id
        return None

    @property
    def eligible_observations(self) -> tuple[CloseObservation, ...]:
        rejected_ids = {
            reason.split(":", 1)[0]
            for reason in self.reasons
            if ":" in reason and not reason.startswith("atomic_batch")
        }
        return tuple(item for item in self.observations if item.provider_id not in rejected_ids)


@dataclass(frozen=True)
class AcceptedCloseBatch:
    batch_id: str
    expected_session: dt.date
    closes: tuple[AcceptedClose, ...]
    status: str
    price_gate_permitted: bool
    reasons: tuple[str, ...]

    @property
    def by_symbol(self) -> dict[str, AcceptedClose]:
        return {item.instrument.canonical_symbol: item for item in self.closes}

    @property
    def results(self) -> dict[str, AcceptedClose]:
        return self.by_symbol

    @property
    def price_gate_passed(self) -> bool:
        """True only when every close passed the atomic batch price gate."""
        return self.price_gate_permitted

    @property
    def eligible_for_ledger_input(self) -> bool:
        """Price-only permission; calendar and corporate-action gates remain required."""
        return self.price_gate_permitted

    @property
    def blocked_symbols(self) -> tuple[str, ...]:
        return tuple(
            sorted(item.instrument.canonical_symbol for item in self.closes if not item.price_gate_permitted)
        )


def _pairwise_max_bps(observations: Sequence[CloseObservation]) -> Decimal | None:
    if len(observations) < 2:
        return None
    maximum = Decimal("0")
    for index, left in enumerate(observations):
        for right in observations[index + 1 :]:
            midpoint = (left.raw_close + right.raw_close) / Decimal("2")
            difference = abs(left.raw_close - right.raw_close) / midpoint * Decimal("10000")
            maximum = max(maximum, difference)
    return maximum


class ProviderRegistry:
    """Collect providers and produce a deterministic, fail-closed close contract."""

    def __init__(
        self,
        providers: Sequence[CloseProvider],
        *,
        policy: CloseAcceptancePolicy | None = None,
        clock: Clock = _default_clock,
    ) -> None:
        if not providers:
            raise ValueError("at least one close provider is required")
        self.providers = tuple(providers)
        self._registrations = tuple(
            _ProviderRegistration.from_provider(provider) for provider in self.providers
        )
        provider_ids = [item.provider_id for item in self._registrations]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("provider_id values must be unique within a registry")
        self.policy = policy or CloseAcceptancePolicy()
        self.clock = clock
        self._cache: dict[tuple[str, ...], CloseObservation] = {}

    def clear_cache(self) -> None:
        self._cache.clear()

    def resolve(self, instrument: InstrumentRef, expected_session: dt.date | str) -> AcceptedClose:
        expected = _normalized_date(expected_session)
        observations: list[CloseObservation] = []
        attempts: list[ProviderAttempt] = []
        for provider, registration in zip(self.providers, self._registrations):
            provider_id = registration.provider_id
            key = (
                provider_id,
                registration.provider_version,
                registration.independence_group,
                registration.source_tier,
                str(registration.settlement_eligible),
                instrument.canonical_symbol,
                instrument.symbol_for(provider_id),
                instrument.asset_type,
                instrument.exchange_mic,
                instrument.calendar_id,
                instrument.currency,
                str(instrument.price_unit_multiplier),
                expected.isoformat(),
            )
            started = self.clock()
            cached = key in self._cache
            try:
                observation = self._cache[key] if cached else provider.fetch_close(instrument, expected)
                if not registration.matches(observation):
                    raise CloseProviderError(
                        "lineage_mismatch",
                        f"{provider_id}: registered provider lineage mismatch",
                    )
                if not cached and not self.policy.rejection_reasons(
                    observation,
                    instrument,
                    expected,
                ):
                    self._cache[key] = observation
                observations.append(observation)
                latency = max(0, int((self.clock() - started).total_seconds() * 1000))
                attempts.append(
                    ProviderAttempt(
                        provider_id=provider_id,
                        status="success",
                        detail="cached observation" if cached else "observation collected",
                        observed_at=_rfc3339(self.clock()),
                        latency_ms=latency,
                        cached=cached,
                        observation_id=observation.observation_id,
                    )
                )
            except CloseProviderError as exc:
                safe_status, safe_detail = _safe_provider_failure(provider_id, exc.status)
                attempts.append(
                    ProviderAttempt(
                        provider_id=provider_id,
                        status=safe_status,
                        detail=safe_detail,
                        observed_at=_rfc3339(self.clock()),
                    )
                )
            except Exception:
                safe_status, safe_detail = _safe_provider_failure(
                    provider_id,
                    "provider_error",
                )
                attempts.append(
                    ProviderAttempt(
                        provider_id=provider_id,
                        status=safe_status,
                        detail=safe_detail,
                        observed_at=_rfc3339(self.clock()),
                    )
                )
        return self._accept(instrument, expected, observations, attempts)

    get_accepted_close = resolve
    accept = resolve
    accept_close = resolve

    def _accept(
        self,
        instrument: InstrumentRef,
        expected: dt.date,
        observations: Sequence[CloseObservation],
        attempts: Sequence[ProviderAttempt],
    ) -> AcceptedClose:
        rejection_map: dict[str, tuple[str, ...]] = {}
        structurally_valid: list[CloseObservation] = []
        for observation in observations:
            rejected = self.policy.rejection_reasons(observation, instrument, expected)
            if rejected:
                rejection_map[observation.observation_id] = rejected
            else:
                structurally_valid.append(observation)

        # Fetch success is not acceptance success.  Preserve the collected
        # observation for audit, but never let a structurally rejected close
        # appear as a healthy provider row in the private daily report.
        normalized_attempts = tuple(
            replace(
                attempt,
                status="rejected",
                detail=f"{attempt.provider_id}: observation rejected by acceptance policy",
            )
            if attempt.observation_id in rejection_map
            else attempt
            for attempt in attempts
        )

        # A provider family counts once even if several endpoints or mirrors are configured.
        independent: list[CloseObservation] = []
        seen_groups: set[str] = set()
        for observation in structurally_valid:
            if observation.independence_group not in seen_groups:
                independent.append(observation)
                seen_groups.add(observation.independence_group)

        settlement_sources = [
            item
            for item in independent
            if item.settlement_eligible and item.source_tier in self.policy.settlement_source_tiers
        ]
        selected = settlement_sources[0] if settlement_sources else (independent[0] if independent else None)
        agreement = _pairwise_max_bps(settlement_sources)
        reasons: list[str] = []
        for observation_id, rejected in sorted(rejection_map.items()):
            provider_id = next(
                item.provider_id for item in observations if item.observation_id == observation_id
            )
            reasons.extend(f"{provider_id}:{reason}" for reason in rejected)

        if selected is None:
            status = "blocked"
            reasons.append("no_structurally_valid_close")
            valuation = False
            price_gate = False
            finality = "blocked"
        elif len(settlement_sources) < self.policy.min_independent_sources:
            status = "degraded"
            reasons.append("insufficient_independent_sources")
            valuation = True
            price_gate = False
            finality = "provisional"
        elif agreement is not None and agreement > self.policy.block_bps:
            status = "blocked"
            reasons.append("provider_disagreement_above_block_threshold")
            valuation = False
            price_gate = False
            finality = "blocked"
            selected = None
        elif agreement is not None and agreement > self.policy.warning_bps:
            status = "warning"
            reasons.append("provider_disagreement_above_warning_threshold")
            valuation = True
            price_gate = self.policy.allow_warning_settlement
            finality = "confirmed_with_warning"
        else:
            status = "accepted"
            valuation = True
            price_gate = True
            finality = "confirmed"

        identity = {
            "instrument": instrument.canonical_symbol,
            "exchange_mic": instrument.exchange_mic,
            "expected_session": expected.isoformat(),
            "status": status,
            "selected_observation_id": selected.observation_id if selected else None,
            "selected_price": str(selected.raw_close) if selected else None,
            "agreement_bps": str(agreement) if agreement is not None else None,
            "independent_groups": [item.independence_group for item in settlement_sources],
            "observation_ids": [item.observation_id for item in observations],
            "reasons": reasons,
            "price_gate_permitted": price_gate,
        }
        return AcceptedClose(
            accepted_close_id=_sha256(identity),
            instrument=instrument,
            expected_session=expected,
            status=status,
            selected_observation_id=selected.observation_id if selected else None,
            selected_price=selected.raw_close if selected else None,
            currency=selected.currency if selected else instrument.currency,
            agreement_bps=agreement,
            independent_source_count=len(settlement_sources),
            observations=tuple(observations),
            attempts=normalized_attempts,
            reasons=tuple(reasons),
            valuation_permitted=valuation,
            price_gate_permitted=price_gate,
            finality=finality,
        )

    def resolve_batch(
        self,
        instruments: Sequence[InstrumentRef],
        expected_session: dt.date | str,
    ) -> AcceptedCloseBatch:
        expected = _normalized_date(expected_session)
        ordered = tuple(sorted(instruments, key=lambda item: item.canonical_symbol))
        symbols = [item.canonical_symbol for item in ordered]
        if len(symbols) != len(set(symbols)):
            raise ValueError("accepted-close batches may not contain duplicate instruments")
        individual_closes = tuple(self.resolve(instrument, expected) for instrument in ordered)
        blocked_symbols = sorted(
            item.instrument.canonical_symbol
            for item in individual_closes
            if not item.price_gate_permitted
        )
        permitted = bool(individual_closes) and not blocked_symbols
        reasons = (
            ()
            if permitted
            else (
                "empty_batch"
                if not individual_closes
                else "atomic_batch_blocked:" + ",".join(blocked_symbols),
            )
        )
        batch_reason = reasons[0] if reasons else None
        closes = tuple(
            replace(
                item,
                atomic_batch_permitted=permitted,
                reasons=(
                    item.reasons
                    if batch_reason is None
                    else (*item.reasons, batch_reason)
                ),
            )
            for item in individual_closes
        )
        identity = {
            "expected_session": expected.isoformat(),
            "accepted_close_ids": [item.accepted_close_id for item in closes],
            "price_gate_permitted": permitted,
            "reasons": reasons,
        }
        return AcceptedCloseBatch(
            batch_id=_sha256(identity),
            expected_session=expected,
            closes=closes,
            status="accepted" if permitted else "blocked",
            price_gate_permitted=permitted,
            reasons=tuple(reasons),
        )

    accept_batch = resolve_batch


__all__ = [
    "AcceptedClose",
    "AcceptedCloseBatch",
    "AlphaVantageCloseProvider",
    "CloseAcceptancePolicy",
    "CloseObservation",
    "CloseProvider",
    "CloseProviderError",
    "InstrumentRef",
    "ProviderAttempt",
    "ProviderRegistry",
    "RetryPolicy",
    "TwelveDataCloseProvider",
]
