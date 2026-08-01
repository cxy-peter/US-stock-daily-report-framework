"""Strict configuration contract for the owner-only daily runtime.

The live configuration itself contains private opening positions, so it must
live outside both Git worktrees and common cloud-sync folders.  Secrets and
delivery targets are referenced by environment-variable *name* only; their
values never enter this object or a public configuration file.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .portfolio_ledger import DcaPlan, LedgerPolicy, OpeningPosition
from .provider_registry import CloseAcceptancePolicy, InstrumentRef
from .trading_calendar import ExchangeSessionError, ExchangeSessionResolver


CONFIG_SCHEMA_VERSION = "private_daily_runtime/v1.0.0"
PUBLIC_EXAMPLE_NAME = "private_daily_runtime.example.yaml"

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_SUPPORTED_PROVIDERS = ("twelve_data", "alpha_vantage")
_FIXED_STORAGE_ROOT_ENV = "SERENITY_PRIVATE_ROOT"
_FIXED_DELIVERY_TARGET_ENV = "CODEX_DAILY_TARGET_KEY"
_FIXED_DELIVERY_CHANNEL = "codex"
_CLEAR_CORPORATE_ACTION_STATUSES = frozenset({"clear_none", "reconciled"})
_MAX_CONFIG_BYTES = 1_000_000
_FORBIDDEN_PRIVATE_KEYS = frozenset(
    {
        "account_id",
        "api_key",
        "apikey",
        "broker",
        "broker_credential",
        "chat_id",
        "ibkr",
        "order",
        "password",
        "secret",
        "target",
        "target_id",
        "target_key",
        "thread_id",
        "token",
    }
)


class PrivateRuntimeConfigError(ValueError):
    """Raised when a private-runtime configuration is incomplete or ambiguous."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().lower()
        if not _SAFE_ID.fullmatch(normalized):
            normalized = "invalid_private_runtime_config"
        self.code = normalized
        super().__init__(normalized)


def _fail(code: str) -> None:
    raise PrivateRuntimeConfigError(code)


def _mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    return value


def _sequence(value: Any, code: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _fail(code)
    return value


def _closed(value: Mapping[str, Any], allowed: set[str], code: str) -> None:
    if any(not isinstance(key, str) for key in value):
        _fail(code)
    if set(value) - allowed:
        _fail(code)


def _text(value: Any, code: str, *, safe_id: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(code)
    result = value.strip()
    if safe_id and not _SAFE_ID.fullmatch(result):
        _fail(code)
    return result


def _boolean(value: Any, code: str) -> bool:
    if not isinstance(value, bool):
        _fail(code)
    return value


def _integer(value: Any, code: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(code)
    if value < minimum or value > maximum:
        _fail(code)
    return value


def _decimal(
    value: Any,
    code: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, (float, bool)):
        _fail(code)
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        _fail(code)
    if not result.is_finite():
        _fail(code)
    if positive and result <= 0:
        _fail(code)
    if nonnegative and result < 0:
        _fail(code)
    return result


def _date(value: Any, code: str) -> dt.date:
    if isinstance(value, dt.datetime):
        _fail(code)
    try:
        return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        _fail(code)


def _utc_datetime(value: Any, code: str) -> dt.datetime:
    if not isinstance(value, (str, dt.datetime)):
        _fail(code)
    try:
        parsed = (
            value
            if isinstance(value, dt.datetime)
            else dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
    except ValueError:
        _fail(code)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(code)
    return parsed.astimezone(dt.timezone.utc)


def _env_name(value: Any, code: str) -> str:
    result = _text(value, code)
    if not _ENV_NAME.fullmatch(result):
        _fail(code)
    return result


class _ClosedSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that forbids aliases and duplicate mapping keys."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            _fail("yaml_alias_forbidden")
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            _fail("yaml_anchor_forbidden")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            _fail("yaml_mapping_invalid")
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError:
                _fail("yaml_mapping_key_invalid")
            if duplicate:
                _fail("yaml_duplicate_key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _reject_forbidden_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                isinstance(key, str)
                and key.strip().casefold() in _FORBIDDEN_PRIVATE_KEYS
            ):
                _fail("embedded_secret_or_broker_field_forbidden")
            _reject_forbidden_keys(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_forbidden_keys(item)


@dataclass(frozen=True, repr=False)
class CorporateActionAttestation:
    symbol: str
    status: str
    valid_from_session: dt.date
    valid_through_session: dt.date
    attested_at: dt.datetime

    def covers(self, session: dt.date, as_of: dt.datetime) -> bool:
        if (
            not isinstance(as_of, dt.datetime)
            or as_of.tzinfo is None
            or as_of.utcoffset() is None
        ):
            return False
        return (
            self.valid_from_session <= session <= self.valid_through_session
            and self.attested_at <= as_of.astimezone(dt.timezone.utc)
        )


@dataclass(frozen=True, repr=False)
class OpeningSnapshot:
    session: dt.date
    cash: Decimal
    positions: tuple[OpeningPosition, ...]


@dataclass(frozen=True, repr=False)
class PrivateDailyRuntimeConfig:
    classification: str
    simulation: bool
    report_timezone: str
    primary_mic: str
    storage_root_env: str
    delivery_channel: str
    delivery_target_env: str
    max_backfill_sessions: int
    providers: tuple[str, ...]
    instruments: tuple[InstrumentRef, ...]
    ledger_policy: LedgerPolicy
    opening: OpeningSnapshot
    dca_plan: DcaPlan
    close_policy: CloseAcceptancePolicy
    corporate_action_attestations: tuple[CorporateActionAttestation, ...]

    @property
    def by_symbol(self) -> Mapping[str, InstrumentRef]:
        return MappingProxyType(
            {item.canonical_symbol: item for item in self.instruments}
        )

    def corporate_action_statuses(
        self,
        session: dt.date,
        *,
        as_of: dt.datetime,
        symbols: Sequence[str] | None = None,
    ) -> Mapping[str, str] | None:
        statuses: dict[str, str] = {}
        required_symbols = (
            tuple(self.by_symbol)
            if symbols is None
            else tuple(sorted({str(item).strip().upper() for item in symbols}))
        )
        if not required_symbols or any(symbol not in self.by_symbol for symbol in required_symbols):
            return None
        for symbol in required_symbols:
            matches = [
                item
                for item in self.corporate_action_attestations
                if item.symbol == symbol and item.covers(session, as_of)
            ]
            if len(matches) != 1:
                return None
            statuses[symbol] = matches[0].status
        return MappingProxyType(dict(sorted(statuses.items())))


def _parse_instruments(value: Any) -> tuple[InstrumentRef, ...]:
    rows = _sequence(value, "instruments_must_be_a_list")
    instruments: list[InstrumentRef] = []
    seen: set[str] = set()
    for raw in rows:
        row = _mapping(raw, "instrument_must_be_an_object")
        _closed(
            row,
            {
                "symbol",
                "asset_type",
                "exchange_mic",
                "currency",
                "calendar_id",
                "price_unit_multiplier",
                "provider_symbols",
            },
            "instrument_contains_unknown_field",
        )
        provider_symbols = _mapping(
            row.get("provider_symbols", {}),
            "provider_symbols_must_be_an_object",
        )
        if set(provider_symbols) != set(_SUPPORTED_PROVIDERS):
            _fail("required_provider_symbol_mapping_missing")
        try:
            asset_type = _text(
                row.get("asset_type"),
                "instrument_asset_type_required",
            ).lower()
            if asset_type not in {"stock", "etf"}:
                _fail("instrument_asset_type_invalid")
            instrument = InstrumentRef(
                canonical_symbol=_text(row.get("symbol"), "instrument_symbol_required"),
                asset_type=asset_type,
                exchange_mic=_text(row.get("exchange_mic"), "instrument_mic_required"),
                currency=_text(row.get("currency", "USD"), "instrument_currency_required"),
                calendar_id=_text(row.get("calendar_id", "XNYS"), "instrument_calendar_required"),
                price_unit_multiplier=_decimal(
                    row.get("price_unit_multiplier", "1"),
                    "instrument_price_multiplier_invalid",
                    positive=True,
                ),
                provider_symbols={
                    str(key): _text(item, "provider_symbol_required")
                    for key, item in provider_symbols.items()
                },
            )
        except ValueError:
            _fail("instrument_identity_invalid")
        if instrument.canonical_symbol in seen:
            _fail("duplicate_instrument_symbol")
        seen.add(instrument.canonical_symbol)
        instruments.append(instrument)
    if not instruments:
        _fail("at_least_one_instrument_required")
    return tuple(sorted(instruments, key=lambda item: item.canonical_symbol))


def _parse_opening(value: Any, instruments: Mapping[str, InstrumentRef]) -> OpeningSnapshot:
    opening = _mapping(value, "opening_snapshot_required")
    _closed(opening, {"session", "cash", "positions"}, "opening_contains_unknown_field")
    positions: list[OpeningPosition] = []
    seen: set[str] = set()
    for raw in _sequence(opening.get("positions", []), "opening_positions_must_be_a_list"):
        row = _mapping(raw, "opening_position_must_be_an_object")
        _closed(
            row,
            {"symbol", "quantity", "average_economic_cost"},
            "opening_position_contains_unknown_field",
        )
        symbol = _text(row.get("symbol"), "opening_position_symbol_required").upper()
        if symbol not in instruments:
            _fail("opening_position_missing_instrument_identity")
        if symbol in seen:
            _fail("duplicate_opening_position")
        seen.add(symbol)
        try:
            position = OpeningPosition(
                symbol=symbol,
                quantity=_decimal(
                    row.get("quantity"),
                    "opening_quantity_invalid",
                    positive=True,
                ),
                average_economic_cost=_decimal(
                    row.get("average_economic_cost"),
                    "opening_economic_cost_invalid",
                    nonnegative=True,
                ),
            )
        except ValueError:
            _fail("opening_position_invalid")
        positions.append(position)
    return OpeningSnapshot(
        session=_date(opening.get("session"), "opening_session_invalid"),
        cash=_decimal(opening.get("cash"), "opening_cash_invalid", nonnegative=True),
        positions=tuple(sorted(positions, key=lambda item: item.symbol)),
    )


def _parse_dca(value: Any, instruments: Mapping[str, InstrumentRef]) -> DcaPlan:
    dca = _mapping(value, "dca_plan_required")
    _closed(
        dca,
        {"plan_id", "version", "currency", "funding_mode", "share_scale", "base_amounts"},
        "dca_plan_contains_unknown_field",
    )
    raw_amounts = _mapping(dca.get("base_amounts"), "dca_base_amounts_required")
    amounts: dict[str, Decimal] = {}
    for raw_symbol, raw_amount in raw_amounts.items():
        symbol = _text(raw_symbol, "dca_symbol_invalid").upper()
        if symbol not in instruments:
            _fail("dca_symbol_missing_instrument_identity")
        if symbol in amounts:
            _fail("duplicate_dca_symbol")
        amounts[symbol] = _decimal(
            raw_amount,
            "dca_amount_invalid",
            positive=True,
        )
    try:
        return DcaPlan(
            plan_id=_text(dca.get("plan_id"), "dca_plan_id_required", safe_id=True),
            version=_text(dca.get("version"), "dca_plan_version_required", safe_id=True),
            currency=_text(dca.get("currency", "USD"), "dca_currency_required"),
            funding_mode=_text(dca.get("funding_mode"), "dca_funding_mode_required"),
            share_scale=(
                None
                if dca.get("share_scale") is None
                else _integer(
                    dca.get("share_scale"),
                    "dca_share_scale_invalid",
                    minimum=0,
                    maximum=18,
                )
            ),
            base_amounts=amounts,
        )
    except ValueError:
        _fail("dca_plan_invalid")


def _parse_attestations(
    value: Any,
    instrument_symbols: set[str],
) -> tuple[CorporateActionAttestation, ...]:
    section = _mapping(value, "corporate_action_section_required")
    _closed(section, {"mode", "attestations"}, "corporate_action_contains_unknown_field")
    if _text(section.get("mode"), "corporate_action_mode_required") != "manual_attestation":
        _fail("unsupported_corporate_action_mode")
    result: list[CorporateActionAttestation] = []
    for raw in _sequence(section.get("attestations", []), "attestations_must_be_a_list"):
        row = _mapping(raw, "attestation_must_be_an_object")
        _closed(
            row,
            {
                "symbol",
                "status",
                "valid_from_session",
                "valid_through_session",
                "attested_at",
            },
            "attestation_contains_unknown_field",
        )
        symbol = _text(row.get("symbol"), "attestation_symbol_required").upper()
        if symbol not in instrument_symbols:
            _fail("attestation_symbol_missing_instrument_identity")
        status = _text(row.get("status"), "attestation_status_required").lower()
        if status not in _CLEAR_CORPORATE_ACTION_STATUSES:
            _fail("attestation_status_not_clear")
        start = _date(row.get("valid_from_session"), "attestation_start_invalid")
        end = _date(row.get("valid_through_session"), "attestation_end_invalid")
        if end < start:
            _fail("attestation_range_invalid")
        result.append(
            CorporateActionAttestation(
                symbol=symbol,
                status=status,
                valid_from_session=start,
                valid_through_session=end,
                attested_at=_utc_datetime(
                    row.get("attested_at"),
                    "attestation_time_invalid",
                ),
            )
        )
    ordered = tuple(
        sorted(result, key=lambda item: (item.symbol, item.valid_from_session, item.valid_through_session))
    )
    by_symbol: dict[str, list[CorporateActionAttestation]] = {}
    for item in ordered:
        prior = by_symbol.setdefault(item.symbol, [])
        if prior and item.valid_from_session <= prior[-1].valid_through_session:
            _fail("overlapping_corporate_action_attestations")
        prior.append(item)
    return ordered


def _parse_close_policy(value: Any, currency: str) -> CloseAcceptancePolicy:
    policy = _mapping(value, "close_policy_required")
    _closed(
        policy,
        {
            "min_independent_sources",
            "warning_bps",
            "block_bps",
            "allow_warning_settlement",
        },
        "close_policy_contains_unknown_field",
    )
    allow_warning = _boolean(
        policy.get("allow_warning_settlement", False),
        "allow_warning_settlement_invalid",
    )
    if allow_warning:
        _fail("warning_settlement_forbidden")
    try:
        return CloseAcceptancePolicy(
            required_currency=currency,
            min_independent_sources=_integer(
                policy.get("min_independent_sources", 2),
                "min_independent_sources_invalid",
                minimum=2,
                maximum=8,
            ),
            warning_bps=_decimal(
                policy.get("warning_bps", "30"),
                "warning_bps_invalid",
                nonnegative=True,
            ),
            block_bps=_decimal(
                policy.get("block_bps", "75"),
                "block_bps_invalid",
                nonnegative=True,
            ),
            allow_warning_settlement=False,
        )
    except ValueError:
        _fail("close_policy_invalid")


def load_private_daily_runtime_config(
    path: str | Path,
    *,
    allow_synthetic: bool = False,
    _validated_bytes: bytes | None = None,
) -> PrivateDailyRuntimeConfig:
    """Read and strictly validate a private daily-runtime YAML document."""

    config_path = Path(path)
    try:
        if _validated_bytes is None:
            if not config_path.is_file() or config_path.stat().st_size > _MAX_CONFIG_BYTES:
                _fail("configuration_file_invalid")
            raw_text = config_path.read_text(encoding="utf-8")
        else:
            if not isinstance(_validated_bytes, bytes) or len(_validated_bytes) > _MAX_CONFIG_BYTES:
                _fail("configuration_file_invalid")
            raw_text = _validated_bytes.decode("utf-8", errors="strict")
        document = yaml.load(
            raw_text,
            Loader=_ClosedSafeLoader,
        ) or {}
    except PrivateRuntimeConfigError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError):
        _fail("configuration_read_failed")
    root = _mapping(document, "configuration_root_must_be_an_object")
    _reject_forbidden_keys(root)
    _closed(
        root,
        {"schema_version", "runtime", "private_daily_runtime"},
        "configuration_contains_unknown_field",
    )
    if root.get("schema_version") != CONFIG_SCHEMA_VERSION:
        _fail("unsupported_configuration_schema")

    runtime = _mapping(root.get("runtime"), "runtime_section_required")
    _closed(
        runtime,
        {
            "data_classification",
            "allow_live_report",
            "example_only",
            "execution_mode",
        },
        "runtime_contains_unknown_field",
    )
    classification = _text(
        runtime.get("data_classification"),
        "data_classification_required",
    ).lower()
    if runtime.get("execution_mode") != "modeled_manual_only":
        _fail("execution_mode_must_be_modeled_manual_only")
    if classification == "synthetic_example":
        if not allow_synthetic:
            _fail("synthetic_runtime_not_live")
        if config_path.name != PUBLIC_EXAMPLE_NAME:
            _fail("synthetic_example_name_invalid")
        if _boolean(runtime.get("allow_live_report"), "allow_live_report_invalid"):
            _fail("synthetic_runtime_cannot_be_live")
        if not _boolean(runtime.get("example_only"), "example_only_required"):
            _fail("synthetic_example_only_required")
        simulation = True
    elif classification == "private":
        if not config_path.name.endswith((".private.yaml", ".private.yml")):
            _fail("private_configuration_name_invalid")
        if not _boolean(runtime.get("allow_live_report"), "allow_live_report_required"):
            _fail("private_live_reporting_not_enabled")
        if runtime.get("example_only") not in (None, False):
            _fail("private_runtime_cannot_be_example")
        simulation = False
    else:
        _fail("data_classification_invalid")

    private = _mapping(root.get("private_daily_runtime"), "private_daily_runtime_required")
    _closed(
        private,
        {
            "report_timezone",
            "primary_mic",
            "storage_root_env",
            "delivery",
            "max_backfill_sessions",
            "providers",
            "instruments",
            "ledger",
            "dca_plan",
            "close_policy",
            "corporate_actions",
        },
        "private_daily_runtime_contains_unknown_field",
    )
    delivery = _mapping(private.get("delivery"), "delivery_section_required")
    _closed(delivery, {"channel", "target_env"}, "delivery_contains_unknown_field")
    configured_channel = _text(
        delivery.get("channel"),
        "delivery_channel_invalid",
        safe_id=True,
    )
    configured_target_env = _env_name(
        delivery.get("target_env"),
        "delivery_target_env_invalid",
    )
    configured_storage_env = _env_name(
        private.get("storage_root_env"),
        "storage_root_env_invalid",
    )
    if configured_channel != _FIXED_DELIVERY_CHANNEL:
        _fail("delivery_channel_must_be_codex")
    if configured_target_env != _FIXED_DELIVERY_TARGET_ENV:
        _fail("delivery_target_environment_must_be_fixed")
    if configured_storage_env != _FIXED_STORAGE_ROOT_ENV:
        _fail("storage_root_environment_must_be_fixed")
    provider_rows = tuple(
        _text(item, "provider_name_invalid").lower()
        for item in _sequence(private.get("providers"), "providers_must_be_a_list")
    )
    if provider_rows != _SUPPORTED_PROVIDERS:
        _fail("required_provider_pair_invalid")

    instruments = _parse_instruments(private.get("instruments"))
    by_symbol = {item.canonical_symbol: item for item in instruments}
    for provider_id in _SUPPORTED_PROVIDERS:
        provider_symbols = [item.symbol_for(provider_id) for item in instruments]
        if len(provider_symbols) != len(set(provider_symbols)):
            _fail("duplicate_provider_symbol_identity")
    primary_mic = _text(private.get("primary_mic"), "primary_mic_required").upper()
    if primary_mic not in {item.exchange_mic for item in instruments}:
        _fail("primary_mic_missing_from_instruments")
    resolver = ExchangeSessionResolver()
    try:
        for item in instruments:
            if resolver.provenance(item.exchange_mic).calendar_name != item.calendar_id:
                _fail("instrument_calendar_identity_mismatch")
    except ExchangeSessionError:
        _fail("unsupported_instrument_calendar")

    ledger = _mapping(private.get("ledger"), "ledger_section_required")
    _closed(
        ledger,
        {"currency", "share_scale", "opening"},
        "ledger_contains_unknown_field",
    )
    currency = _text(ledger.get("currency", "USD"), "ledger_currency_required").upper()
    if any(item.currency != currency for item in instruments):
        _fail("instrument_currency_mismatch")
    ledger_policy = LedgerPolicy(
        currency=currency,
        share_scale=_integer(
            ledger.get("share_scale", 12),
            "ledger_share_scale_invalid",
            minimum=0,
            maximum=18,
        ),
    )
    opening = _parse_opening(ledger.get("opening"), by_symbol)
    dca_plan = _parse_dca(private.get("dca_plan"), by_symbol)
    if dca_plan.currency != currency:
        _fail("dca_currency_mismatch")
    attestations = _parse_attestations(
        private.get("corporate_actions"),
        set(by_symbol),
    )
    report_timezone = _text(
        private.get("report_timezone"),
        "report_timezone_required",
    )
    try:
        ZoneInfo(report_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        _fail("report_timezone_invalid")

    return PrivateDailyRuntimeConfig(
        classification=classification,
        simulation=simulation,
        report_timezone=report_timezone,
        primary_mic=primary_mic,
        storage_root_env=_env_name(
            private.get("storage_root_env"),
            "storage_root_env_invalid",
        ),
        delivery_channel=_text(
            delivery.get("channel"),
            "delivery_channel_invalid",
            safe_id=True,
        ),
        delivery_target_env=_env_name(
            delivery.get("target_env"),
            "delivery_target_env_invalid",
        ),
        max_backfill_sessions=_integer(
            private.get("max_backfill_sessions", 20),
            "max_backfill_sessions_invalid",
            minimum=1,
            maximum=252,
        ),
        providers=provider_rows,
        instruments=instruments,
        ledger_policy=ledger_policy,
        opening=opening,
        dca_plan=dca_plan,
        close_policy=_parse_close_policy(private.get("close_policy", {}), currency),
        corporate_action_attestations=attestations,
    )


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "PUBLIC_EXAMPLE_NAME",
    "CorporateActionAttestation",
    "OpeningSnapshot",
    "PrivateDailyRuntimeConfig",
    "PrivateRuntimeConfigError",
    "load_private_daily_runtime_config",
]
