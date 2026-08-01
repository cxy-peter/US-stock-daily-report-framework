"""Private, event-sourced portfolio accounting without broker connectivity.

The ledger has two deliberately different projections.  ``confirmed`` contains
only owner-confirmed economic activity.  ``modeled`` additionally contains the
configured base DCA entries that have not been replaced by an owner-confirmed
fill.  It is research accounting, not a broker statement or tax-lot engine.

All monetary and quantity values are :class:`~decimal.Decimal`.  SQLite stores
their canonical decimal text; binary floating-point inputs are rejected.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Context, Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_EVEN, localcontext
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .provider_registry import AcceptedClose, AcceptedCloseBatch, CloseAcceptancePolicy
from .trading_calendar import ExchangeSessionError, ExchangeSessionResolver


ZERO = Decimal("0")
ONE = Decimal("1")
_GENESIS_HASH = "0" * 64
_BOOK_KINDS = frozenset({"confirmed", "modeled"})
_FUNDING_MODES = frozenset({"existing_cash", "modeled_external_contribution"})
_LEDGER_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_VALUATION_CONTRACT_VERSION = "ledger_valuation/v2"


def _fixed_decimal_context(function: Any) -> Any:
    """Run one accounting operation independently of the process Decimal context."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with localcontext(_LEDGER_CONTEXT):
            return function(*args, **kwargs)

    return wrapped


class PortfolioLedgerError(Exception):
    """Base class for ledger failures."""


class LedgerValidationError(PortfolioLedgerError, ValueError):
    """Raised when an input violates the accounting contract."""


class LedgerNotInitializedError(PortfolioLedgerError):
    """Raised when an economic operation precedes the opening snapshot."""


class LedgerAlreadyInitializedError(PortfolioLedgerError):
    """Raised when a different opening snapshot targets an initialized ledger."""


class LedgerIdempotencyConflict(PortfolioLedgerError):
    """Raised when an idempotency key is reused for different content."""


class LedgerIntegrityError(PortfolioLedgerError):
    """Raised when the append-only event hash chain does not verify."""


class LedgerInsufficientCash(PortfolioLedgerError):
    """Raised when an existing-cash DCA batch cannot be funded atomically."""


class LedgerSettlementBlocked(PortfolioLedgerError):
    """Raised when a DCA close, calendar or corporate-action gate fails."""


class LedgerProjectionError(PortfolioLedgerError):
    """Raised when an event stream cannot be projected consistently."""


@dataclass(frozen=True)
class LedgerPolicy:
    """Persistence and numerical policy for one ledger."""

    currency: str = "USD"
    share_scale: int = 12
    busy_timeout_ms: int = 5_000
    corporate_action_clear_statuses: tuple[str, ...] = ("clear_none", "reconciled")

    def __post_init__(self) -> None:
        currency = str(self.currency).strip().upper()
        statuses = tuple(str(item).strip().lower() for item in self.corporate_action_clear_statuses)
        if not currency:
            raise LedgerValidationError("currency may not be empty")
        if isinstance(self.share_scale, bool) or not isinstance(self.share_scale, int):
            raise LedgerValidationError("share_scale must be an integer")
        if self.share_scale < 0 or self.share_scale > 18:
            raise LedgerValidationError("share_scale must be between 0 and 18")
        if isinstance(self.busy_timeout_ms, bool) or not isinstance(self.busy_timeout_ms, int):
            raise LedgerValidationError("busy_timeout_ms must be an integer")
        if self.busy_timeout_ms < 1:
            raise LedgerValidationError("busy_timeout_ms must be positive")
        if not statuses or any(not item for item in statuses):
            raise LedgerValidationError("corporate-action clear statuses may not be empty")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "corporate_action_clear_statuses", statuses)


@dataclass(frozen=True)
class OpeningPosition:
    """One position in the owner-supplied opening snapshot.

    ``average_economic_cost`` is a research carrying cost per share.  It is not
    represented as, and must not be used as, a tax basis.
    """

    symbol: str
    quantity: Decimal
    average_economic_cost: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity", minimum=ZERO))
        object.__setattr__(
            self,
            "average_economic_cost",
            _decimal(self.average_economic_cost, "average_economic_cost", minimum=ZERO),
        )
        if self.quantity == ZERO and self.average_economic_cost != ZERO:
            raise LedgerValidationError("a zero opening quantity must have zero economic cost")


@dataclass(frozen=True)
class DcaPlan:
    """Versioned base-amount DCA plan consumed by modeled settlement only."""

    plan_id: str
    version: str
    base_amounts: Mapping[str, Decimal]
    funding_mode: str = "existing_cash"
    share_scale: int | None = None
    currency: str = "USD"

    def __post_init__(self) -> None:
        plan_id = str(self.plan_id).strip()
        version = str(self.version).strip()
        funding = str(self.funding_mode).strip().lower()
        currency = str(self.currency).strip().upper()
        if not plan_id or not version:
            raise LedgerValidationError("plan_id and version may not be empty")
        if funding not in _FUNDING_MODES:
            raise LedgerValidationError("unknown DCA funding_mode")
        if not currency:
            raise LedgerValidationError("DCA currency may not be empty")
        if self.share_scale is not None:
            if isinstance(self.share_scale, bool) or not isinstance(self.share_scale, int):
                raise LedgerValidationError("DCA share_scale must be an integer")
            if self.share_scale < 0 or self.share_scale > 18:
                raise LedgerValidationError("DCA share_scale must be between 0 and 18")
        normalized: dict[str, Decimal] = {}
        for raw_symbol, raw_amount in self.base_amounts.items():
            symbol = _symbol(raw_symbol)
            if symbol in normalized:
                raise LedgerValidationError(f"duplicate DCA symbol: {symbol}")
            amount = _decimal(raw_amount, f"base_amounts[{symbol}]", positive=True)
            if _meaningful_decimal_places(amount) > 2:
                raise LedgerValidationError("DCA base amounts may have at most two decimal places")
            normalized[symbol] = amount
        if not normalized:
            raise LedgerValidationError("a DCA plan must contain at least one symbol")
        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "funding_mode", funding)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "base_amounts", MappingProxyType(dict(sorted(normalized.items()))))

    @property
    def plan_version(self) -> str:
        """Compatibility alias used by report contracts."""
        return self.version


@dataclass(frozen=True)
class DcaFillReceipt:
    """One immutable symbol-level receipt from an atomic modeled settlement."""

    symbol: str
    quantity: Decimal
    price: Decimal
    spend: Decimal
    residual: Decimal
    accepted_close_id: str
    settlement_event_id: str

    def __post_init__(self) -> None:
        symbol = _symbol(self.symbol)
        quantity = _decimal(self.quantity, "DCA receipt quantity", positive=True)
        price = _decimal(self.price, "DCA receipt price", positive=True)
        spend = _decimal(self.spend, "DCA receipt spend", positive=True)
        residual = _decimal(self.residual, "DCA receipt residual", minimum=ZERO)
        accepted_close_id = _sha256_digest(self.accepted_close_id, "accepted_close_id")
        settlement_event_id = _sha256_digest(
            self.settlement_event_id,
            "settlement_event_id",
        )
        with localcontext(_LEDGER_CONTEXT):
            if quantity * price != spend:
                raise LedgerIntegrityError(
                    "DCA receipt spend does not equal quantity times price"
                )
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "spend", spend)
        object.__setattr__(self, "residual", residual)
        object.__setattr__(self, "accepted_close_id", accepted_close_id)
        object.__setattr__(self, "settlement_event_id", settlement_event_id)


@dataclass(frozen=True)
class DcaSettlementResult:
    """Immutable receipt for an atomic modeled DCA settlement."""

    status: str
    session: dt.date
    plan_id: str
    plan_version: str
    accepted_close_batch_id: str
    batch_event_id: str | None
    fill_event_ids: tuple[str, ...]
    fill_receipts: tuple[DcaFillReceipt, ...]
    contribution_event_id: str | None
    total_configured_amount: Decimal
    total_spend: Decimal
    total_residual: Decimal
    idempotent_replay: bool = False
    skipped: bool = False

    def __post_init__(self) -> None:
        status = str(self.status).strip().lower()
        if status not in {"settled", "skipped"}:
            raise LedgerIntegrityError("DCA settlement has an unknown status")
        session = _date(self.session)
        plan_id = str(self.plan_id).strip()
        plan_version = str(self.plan_version).strip()
        if not plan_id or not plan_version:
            raise LedgerIntegrityError("DCA settlement has no plan identity")
        accepted_close_batch_id = _sha256_digest(
            self.accepted_close_batch_id,
            "accepted_close_batch_id",
        )
        batch_event_id = (
            None
            if self.batch_event_id is None
            else _sha256_digest(self.batch_event_id, "DCA batch event id")
        )
        contribution_event_id = (
            None
            if self.contribution_event_id is None
            else _sha256_digest(self.contribution_event_id, "DCA contribution event id")
        )
        total_configured = _decimal(
            self.total_configured_amount,
            "total configured amount",
            positive=True,
        )
        total_spend = _decimal(self.total_spend, "total spend", minimum=ZERO)
        total_residual = _decimal(self.total_residual, "total residual", minimum=ZERO)
        with localcontext(_LEDGER_CONTEXT):
            if total_configured != total_spend + total_residual:
                raise LedgerIntegrityError(
                    "DCA settlement totals do not conserve configured cash"
                )
        receipts = tuple(self.fill_receipts)
        if any(not isinstance(item, DcaFillReceipt) for item in receipts):
            raise LedgerIntegrityError("fill_receipts must contain DcaFillReceipt values")
        receipts = tuple(sorted(receipts, key=lambda item: item.symbol))
        if len({item.symbol for item in receipts}) != len(receipts):
            raise LedgerIntegrityError("DCA settlement has duplicate symbol receipts")
        receipt_ids = tuple(item.settlement_event_id for item in receipts)
        if tuple(self.fill_event_ids) != receipt_ids:
            raise LedgerIntegrityError("DCA fill ids do not match symbol-level receipts")
        if status == "settled" and (batch_event_id is None or not receipts or self.skipped):
            raise LedgerIntegrityError("settled DCA receipt lacks an active batch or fills")
        if status == "skipped" and (
            batch_event_id is not None
            or receipts
            or contribution_event_id is not None
            or total_spend != ZERO
            or not self.skipped
        ):
            raise LedgerIntegrityError("skipped DCA receipt contains settlement events")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "plan_version", plan_version)
        object.__setattr__(self, "accepted_close_batch_id", accepted_close_batch_id)
        object.__setattr__(self, "batch_event_id", batch_event_id)
        object.__setattr__(self, "contribution_event_id", contribution_event_id)
        object.__setattr__(self, "total_configured_amount", total_configured)
        object.__setattr__(self, "total_spend", total_spend)
        object.__setattr__(self, "total_residual", total_residual)
        object.__setattr__(self, "fill_event_ids", receipt_ids)
        object.__setattr__(self, "fill_receipts", receipts)

    @property
    def receipts_by_symbol(self) -> Mapping[str, DcaFillReceipt]:
        return MappingProxyType({item.symbol: item for item in self.fill_receipts})


@dataclass(frozen=True)
class DcaSkipReceipt:
    """One active owner-confirmed per-session DCA skip override."""

    session: dt.date
    plan_id: str
    plan_version: str
    override_event_id: str
    reason: str

    def __post_init__(self) -> None:
        plan_id = str(self.plan_id).strip()
        plan_version = str(self.plan_version).strip()
        if not plan_id or not plan_version:
            raise LedgerIntegrityError("DCA skip receipt has no plan identity")
        object.__setattr__(self, "session", _date(self.session))
        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "plan_version", plan_version)
        object.__setattr__(
            self,
            "override_event_id",
            _sha256_digest(self.override_event_id, "DCA skip override event id"),
        )
        object.__setattr__(self, "reason", str(self.reason).strip())


@dataclass(frozen=True)
class PositionState:
    """A replayed position using weighted-average economic carrying cost."""

    symbol: str
    quantity: Decimal
    average_economic_cost: Decimal
    economic_cost: Decimal
    realized_pnl: Decimal = ZERO
    modeled_quantity: Decimal = ZERO


@dataclass(frozen=True)
class LedgerProjection:
    """Point-in-time replay of either the confirmed or modeled book."""

    book_kind: str
    as_of: dt.date | None
    currency: str
    cash: Decimal
    positions: tuple[PositionState, ...]
    realized_pnl: Decimal
    fees: Decimal
    net_external_flow: Decimal
    event_count: int
    last_event_hash: str

    @property
    def by_symbol(self) -> dict[str, PositionState]:
        return {item.symbol: item for item in self.positions}

    @property
    def total_economic_cost(self) -> Decimal:
        with localcontext(_LEDGER_CONTEXT):
            return sum((item.economic_cost for item in self.positions), ZERO)


@dataclass(frozen=True)
class ValuationCloseLineage:
    """Selected accepted-close identity used for one valued symbol."""

    accepted_close_id: str
    selected_provider_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "accepted_close_id",
            _sha256_digest(self.accepted_close_id, "accepted_close_id"),
        )
        provider_id = str(self.selected_provider_id).strip().lower()
        if not provider_id:
            raise LedgerIntegrityError("selected_provider_id may not be empty")
        object.__setattr__(self, "selected_provider_id", provider_id)


@dataclass(frozen=True)
class LedgerValuation:
    """Final close valuation and time-weighted performance observation."""

    valuation_event_id: str
    book_kind: str
    session: dt.date
    accepted_close_batch_id: str
    currency: str
    cash: Decimal
    securities_value: Decimal
    nav: Decimal
    prices: Mapping[str, Decimal]
    accepted_close_lineage: Mapping[str, ValuationCloseLineage]
    prior_nav: Decimal | None
    prior_cumulative_twr: Decimal | None
    daily_pnl: Decimal | None
    daily_return: Decimal | None
    cumulative_twr: Decimal | None
    net_external_flow: Decimal
    weighted_external_flow: Decimal
    idempotent_replay: bool = False

    def __post_init__(self) -> None:
        prices = dict(sorted(self.prices.items()))
        lineage = dict(sorted(self.accepted_close_lineage.items()))
        if set(prices) != set(lineage):
            raise LedgerIntegrityError("valuation prices and accepted-close lineage differ")
        if any(not isinstance(item, ValuationCloseLineage) for item in lineage.values()):
            raise LedgerIntegrityError("valuation lineage values have the wrong type")
        object.__setattr__(self, "prices", MappingProxyType(prices))
        object.__setattr__(self, "accepted_close_lineage", MappingProxyType(lineage))


@dataclass(frozen=True)
class OpeningCheckpoint:
    """Immutable opening snapshot checkpoint exposed without SQLite access."""

    opening_event_id: str
    opening_event_hash: str
    session: dt.date
    currency: str
    cash: Decimal
    positions: tuple[OpeningPosition, ...]
    idempotency_key: str = ""
    created_at: dt.datetime = dt.datetime(
        1970,
        1,
        1,
        tzinfo=dt.timezone.utc,
    )


@dataclass(frozen=True)
class CommonLedgerValuation:
    """The two books valued at the same completed session."""

    session: dt.date
    confirmed: LedgerValuation
    modeled: LedgerValuation


@dataclass(frozen=True)
class LedgerSessionAudit:
    """Read-only recovery view for one session's DCA and valuation state."""

    session: dt.date
    dca_settlement: DcaSettlementResult | None
    owner_skip: DcaSkipReceipt | None
    confirmed_valuation: LedgerValuation | None
    modeled_valuation: LedgerValuation | None
    last_event_hash: str

    def __post_init__(self) -> None:
        session = _date(self.session)
        if self.dca_settlement is not None and self.dca_settlement.session != session:
            raise LedgerIntegrityError("session audit DCA settlement has the wrong session")
        if self.owner_skip is not None and self.owner_skip.session != session:
            raise LedgerIntegrityError("session audit owner skip has the wrong session")
        if self.dca_settlement is not None and self.owner_skip is not None:
            raise LedgerIntegrityError("session audit cannot contain DCA settlement and owner skip")
        for valuation in (self.confirmed_valuation, self.modeled_valuation):
            if valuation is not None and valuation.session != session:
                raise LedgerIntegrityError("session audit valuation has the wrong session")
        object.__setattr__(self, "session", session)
        object.__setattr__(
            self,
            "last_event_hash",
            _sha256_digest(self.last_event_hash, "session audit event hash"),
        )

    @property
    def valuation_state(self) -> str:
        present = sum(
            value is not None
            for value in (self.confirmed_valuation, self.modeled_valuation)
        )
        return ("none", "partial", "complete")[present]

    @property
    def has_partial_valuation(self) -> bool:
        return self.valuation_state == "partial"

    @property
    def has_owner_skip(self) -> bool:
        return self.owner_skip is not None


@dataclass
class _MutablePosition:
    quantity: Decimal = ZERO
    economic_cost: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    modeled_quantity: Decimal = ZERO


@dataclass(frozen=True)
class _AppendReceipt:
    event_id: str
    event_hash: str
    idempotent_replay: bool


class PortfolioLedger:
    """SQLite event store and deterministic portfolio projector.

    The class deliberately exposes no broker authentication, scraping or order
    API.  The caller supplies owner-confirmed events and accepted close batches.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        policy: LedgerPolicy | None = None,
        calendar_resolver: ExchangeSessionResolver | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.policy = policy or LedgerPolicy()
        self.calendar_resolver = calendar_resolver or ExchangeSessionResolver()
        self._ensure_schema()

    @_fixed_decimal_context
    def initialize(
        self,
        opening_session: dt.date | str,
        cash: Decimal,
        positions: Sequence[OpeningPosition] = (),
        *,
        idempotency_key: str = "opening-snapshot",
        recorded_at: dt.datetime | str | None = None,
    ) -> str:
        """Append the opening snapshot, or return its id on an identical retry."""
        session = _date(opening_session)
        opening_cash = _decimal(cash, "cash", minimum=ZERO)
        normalized_positions: list[OpeningPosition] = []
        seen: set[str] = set()
        for item in positions:
            if not isinstance(item, OpeningPosition):
                raise LedgerValidationError("positions must contain OpeningPosition values")
            if item.symbol in seen:
                raise LedgerValidationError(f"duplicate opening symbol: {item.symbol}")
            seen.add(item.symbol)
            normalized_positions.append(item)
        payload = {
            "currency": self.policy.currency,
            "cash": opening_cash,
            "positions": [
                {
                    "symbol": item.symbol,
                    "quantity": item.quantity,
                    "average_economic_cost": item.average_economic_cost,
                }
                for item in sorted(normalized_positions, key=lambda value: value.symbol)
            ],
            "cost_basis_semantics": "economic_carrying_cost_not_tax_basis",
        }
        payload_json = _canonical_json(payload)
        with self._transaction(require_initialized=False) as connection:
            existing = connection.execute(
                "SELECT event_id, idempotency_key, session_date, payload_json FROM ledger_events "
                "WHERE event_type = 'opening_snapshot' ORDER BY sequence_no LIMIT 1"
            ).fetchone()
            if existing is not None:
                if (
                    existing["payload_json"] == payload_json
                    and existing["idempotency_key"] == _idempotency_key(idempotency_key)
                    and existing["session_date"] == session.isoformat()
                ):
                    return str(existing["event_id"])
                raise LedgerAlreadyInitializedError("the ledger already has a different opening snapshot")
            if connection.execute("SELECT 1 FROM ledger_events LIMIT 1").fetchone() is not None:
                raise LedgerIntegrityError("events exist before the opening snapshot")
            receipt = self._append_event(
                connection,
                event_type="opening_snapshot",
                source_class="user_confirmed",
                session=session,
                occurred_at=_session_timestamp(session),
                idempotency_key=_idempotency_key(idempotency_key),
                payload=payload,
                created_at=recorded_at,
            )
            return receipt.event_id

    @_fixed_decimal_context
    def record_user_confirmed_fill(
        self,
        session: dt.date | str,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        *,
        fees: Decimal = ZERO,
        occurred_at: dt.datetime | str | None = None,
        idempotency_key: str | None = None,
        replaces_modeled_event_id: str | None = None,
    ) -> str:
        """Append an owner-confirmed buy or sell; suggestions never call this."""
        normalized_session = _date(session)
        normalized_symbol = _symbol(symbol)
        normalized_side = str(side).strip().lower()
        if normalized_side not in {"buy", "sell"}:
            raise LedgerValidationError("fill side must be 'buy' or 'sell'")
        normalized_quantity = _decimal(quantity, "quantity", positive=True)
        normalized_price = _decimal(price, "price", positive=True)
        normalized_fees = _decimal(fees, "fees", minimum=ZERO)
        normalized_time = _event_time(occurred_at, normalized_session, idempotency_key)
        replacement = None if replaces_modeled_event_id is None else str(replaces_modeled_event_id).strip()
        if replaces_modeled_event_id is not None and not replacement:
            raise LedgerValidationError("replaces_modeled_event_id may not be empty")
        payload = {
            "symbol": normalized_symbol,
            "side": normalized_side,
            "quantity": normalized_quantity,
            "price": normalized_price,
            "fees": normalized_fees,
            "currency": self.policy.currency,
            "cost_basis_semantics": "economic_carrying_cost_not_tax_basis",
            "replaces_modeled_event_id": replacement,
        }
        key = _optional_or_auto_key(idempotency_key, "user_fill", normalized_time, payload)
        with self._transaction() as connection:
            self._validate_session_not_before_opening(connection, normalized_session)
            if replacement is not None:
                if normalized_side != "buy":
                    raise LedgerValidationError("only a confirmed buy can replace a modeled DCA fill")
                target = connection.execute(
                    "SELECT event_id, event_type, session_date, payload_json "
                    "FROM ledger_events WHERE event_id = ?",
                    (replacement,),
                ).fetchone()
                if target is None or target["event_type"] != "modeled_dca_fill":
                    raise LedgerValidationError("replacement target must be a modeled DCA fill")
                target_payload = json.loads(target["payload_json"])
                if target_payload.get("symbol") != normalized_symbol:
                    raise LedgerValidationError("replacement fill symbol does not match the modeled event")
                if target_payload.get("side") != "buy":
                    raise LedgerValidationError("replacement target must be a modeled DCA buy")
                if _date(target["session_date"]) != normalized_session:
                    raise LedgerValidationError("replacement fill must use the modeled DCA session")
                active_batch = self._active_batch_for_child(
                    connection,
                    target_payload.get("batch_key"),
                )
                if active_batch is None:
                    raise LedgerValidationError("replacement target is not in an active atomic DCA batch")
                if target_payload.get("batch_event_id") != active_batch["event_id"]:
                    raise LedgerIntegrityError("modeled DCA child does not identify its atomic marker")
                if replacement in self._reversed_event_ids(connection):
                    raise LedgerValidationError("a reversed modeled event cannot be replaced")
                active_replacements = self._active_modeled_replacements(connection)
                if replacement in active_replacements:
                    prior = active_replacements[replacement]
                    existing = connection.execute(
                        "SELECT event_id FROM ledger_events WHERE idempotency_key = ?",
                        (key,),
                    ).fetchone()
                    if existing is None or str(existing["event_id"]) != prior:
                        raise LedgerIdempotencyConflict("the modeled DCA fill is already replaced")
            receipt = self._append_event(
                connection,
                event_type="user_confirmed_fill",
                source_class="user_confirmed",
                session=normalized_session,
                occurred_at=normalized_time,
                idempotency_key=key,
                payload=payload,
            )
            if not receipt.idempotent_replay:
                self._assert_no_valuation_on_or_after(
                    connection,
                    normalized_session,
                    ("confirmed", "modeled"),
                )
            # Replaying before commit catches cash/quantity conservation defects.
            self._project_connection(connection, "confirmed", normalized_session)
            self._project_connection(connection, "modeled", normalized_session)
            return receipt.event_id

    @_fixed_decimal_context
    def record_cash_flow(
        self,
        session: dt.date | str,
        amount: Decimal,
        *,
        description: str = "",
        occurred_at: dt.datetime | str | None = None,
        valuation_weight: Decimal | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Append a signed owner-confirmed external cash contribution/withdrawal."""
        normalized_session = _date(session)
        normalized_amount = _decimal(amount, "amount")
        if normalized_amount == ZERO:
            raise LedgerValidationError("cash-flow amount may not be zero")
        if (occurred_at is None) != (valuation_weight is None):
            raise LedgerValidationError(
                "occurred_at and valuation_weight must be supplied together"
            )
        if occurred_at is None and valuation_weight is None:
            normalized_time = _session_close_timestamp(normalized_session)
            normalized_weight = ZERO
        else:
            normalized_time = (
                _session_close_timestamp(normalized_session)
                if occurred_at is None
                else _occurred_at(occurred_at)
            )
            normalized_weight = _decimal(valuation_weight, "valuation_weight")
            if normalized_weight < ZERO or normalized_weight > ONE:
                raise LedgerValidationError("valuation_weight must be between 0 and 1")
        payload = {
            "amount": normalized_amount,
            "currency": self.policy.currency,
            "description": str(description).strip(),
            "external_flow": True,
            "valuation_weight": normalized_weight,
        }
        key = _optional_or_auto_key(idempotency_key, "cash_flow", normalized_time, payload)
        with self._transaction() as connection:
            self._validate_session_not_before_opening(connection, normalized_session)
            receipt = self._append_event(
                connection,
                event_type="cash_flow",
                source_class="user_confirmed",
                session=normalized_session,
                occurred_at=normalized_time,
                idempotency_key=key,
                payload=payload,
            )
            if not receipt.idempotent_replay:
                self._assert_no_valuation_on_or_after(
                    connection,
                    normalized_session,
                    ("confirmed", "modeled"),
                )
            try:
                self._project_connection(connection, "confirmed", normalized_session)
                self._project_connection(connection, "modeled", normalized_session)
            except LedgerProjectionError as exc:
                raise LedgerInsufficientCash(
                    "cash flow would make a confirmed or modeled cash balance negative"
                ) from exc
            return receipt.event_id

    @_fixed_decimal_context
    def record_income(
        self,
        session: dt.date | str,
        amount: Decimal,
        *,
        symbol: str | None = None,
        description: str = "",
        occurred_at: dt.datetime | str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Append confirmed income; it changes P/L and cash, not external flow."""
        normalized_session = _date(session)
        normalized_amount = _decimal(amount, "amount", positive=True)
        normalized_time = _event_time(occurred_at, normalized_session, idempotency_key)
        normalized_symbol = None if symbol is None else _symbol(symbol)
        payload = {
            "amount": normalized_amount,
            "symbol": normalized_symbol,
            "currency": self.policy.currency,
            "description": str(description).strip(),
            "external_flow": False,
        }
        key = _optional_or_auto_key(idempotency_key, "income", normalized_time, payload)
        with self._transaction() as connection:
            self._validate_session_not_before_opening(connection, normalized_session)
            receipt = self._append_event(
                connection,
                event_type="income",
                source_class="user_confirmed",
                session=normalized_session,
                occurred_at=normalized_time,
                idempotency_key=key,
                payload=payload,
            )
            if not receipt.idempotent_replay:
                self._assert_no_valuation_on_or_after(
                    connection,
                    normalized_session,
                    ("confirmed", "modeled"),
                )
            try:
                self._project_connection(connection, "confirmed", normalized_session)
                self._project_connection(connection, "modeled", normalized_session)
            except LedgerProjectionError as exc:
                raise LedgerInsufficientCash(
                    "income could not be replayed consistently in both books"
                ) from exc
            return receipt.event_id

    @_fixed_decimal_context
    def record_fee(
        self,
        session: dt.date | str,
        amount: Decimal,
        *,
        description: str = "",
        occurred_at: dt.datetime | str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Append a confirmed standalone fee; it is not an external cash flow."""
        normalized_session = _date(session)
        normalized_amount = _decimal(amount, "amount", positive=True)
        normalized_time = _event_time(occurred_at, normalized_session, idempotency_key)
        payload = {
            "amount": normalized_amount,
            "currency": self.policy.currency,
            "description": str(description).strip(),
            "external_flow": False,
        }
        key = _optional_or_auto_key(idempotency_key, "fee", normalized_time, payload)
        with self._transaction() as connection:
            self._validate_session_not_before_opening(connection, normalized_session)
            receipt = self._append_event(
                connection,
                event_type="fee",
                source_class="user_confirmed",
                session=normalized_session,
                occurred_at=normalized_time,
                idempotency_key=key,
                payload=payload,
            )
            if not receipt.idempotent_replay:
                self._assert_no_valuation_on_or_after(
                    connection,
                    normalized_session,
                    ("confirmed", "modeled"),
                )
            self._project_connection(connection, "confirmed", normalized_session)
            self._project_connection(connection, "modeled", normalized_session)
            return receipt.event_id

    @_fixed_decimal_context
    def record_split(
        self,
        session: dt.date | str,
        symbol: str,
        ratio: Decimal,
        *,
        occurred_at: dt.datetime | str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Append a confirmed split ratio while preserving total economic cost."""
        normalized_session = _date(session)
        normalized_symbol = _symbol(symbol)
        normalized_ratio = _decimal(ratio, "ratio", positive=True)
        normalized_time = _event_time(occurred_at, normalized_session, idempotency_key)
        payload = {"symbol": normalized_symbol, "ratio": normalized_ratio}
        key = _optional_or_auto_key(idempotency_key, "split", normalized_time, payload)
        with self._transaction() as connection:
            self._validate_session_not_before_opening(connection, normalized_session)
            confirmed = self._project_connection(connection, "confirmed", normalized_session)
            modeled = self._project_connection(connection, "modeled", normalized_session)
            if normalized_symbol not in confirmed.by_symbol and normalized_symbol not in modeled.by_symbol:
                raise LedgerProjectionError("split symbol is absent from both books")
            receipt = self._append_event(
                connection,
                event_type="split",
                source_class="user_confirmed",
                session=normalized_session,
                occurred_at=normalized_time,
                idempotency_key=key,
                payload=payload,
            )
            if not receipt.idempotent_replay:
                self._assert_no_valuation_on_or_after(
                    connection,
                    normalized_session,
                    ("confirmed", "modeled"),
                )
            self._project_connection(connection, "confirmed", normalized_session)
            self._project_connection(connection, "modeled", normalized_session)
            return receipt.event_id

    @_fixed_decimal_context
    def record_dca_override(
        self,
        session: dt.date | str,
        plan_id: str,
        plan_version: str,
        *,
        action: str = "skip",
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> str:
        """Append an explicit per-session modeled-DCA override."""
        normalized_session = _date(session)
        normalized_plan = str(plan_id).strip()
        normalized_version = str(plan_version).strip()
        normalized_action = str(action).strip().lower()
        if not normalized_plan or not normalized_version:
            raise LedgerValidationError("plan_id and plan_version may not be empty")
        if normalized_action != "skip":
            raise LedgerValidationError("the only supported DCA override action is 'skip'")
        payload = {
            "plan_id": normalized_plan,
            "plan_version": normalized_version,
            "action": normalized_action,
            "reason": str(reason).strip(),
        }
        key = _idempotency_key(
            idempotency_key or f"dca-override:{normalized_plan}:{normalized_version}:{normalized_session.isoformat()}"
        )
        with self._transaction() as connection:
            self._validate_session_not_before_opening(connection, normalized_session)
            if self._find_dca_batch_event(connection, normalized_plan, normalized_version, normalized_session):
                raise LedgerSettlementBlocked("a settled DCA batch cannot be overwritten by an override")
            receipt = self._append_event(
                connection,
                event_type="dca_override",
                source_class="user_confirmed",
                session=normalized_session,
                occurred_at=_session_timestamp(normalized_session),
                idempotency_key=key,
                payload=payload,
            )
            if not receipt.idempotent_replay:
                self._assert_no_valuation_on_or_after(
                    connection,
                    normalized_session,
                    ("confirmed", "modeled"),
                )
            return receipt.event_id

    @_fixed_decimal_context
    def reverse_event(
        self,
        target_event_id: str,
        *,
        reason: str,
        idempotency_key: str | None = None,
    ) -> str:
        """Append a reversal marker; the target event is never edited or deleted."""
        target = str(target_event_id).strip()
        normalized_reason = str(reason).strip()
        if not target or not normalized_reason:
            raise LedgerValidationError("target_event_id and reversal reason may not be empty")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT event_id, event_type, session_date FROM ledger_events WHERE event_id = ?",
                (target,),
            ).fetchone()
            if row is None:
                raise LedgerValidationError("reversal target does not exist")
            if row["event_type"] in {
                "opening_snapshot",
                "reversal",
                "valuation",
                "modeled_dca_batch",
                "modeled_dca_batch_reversal",
                "modeled_dca_fill",
                "modeled_external_contribution",
            }:
                if row["event_type"] in {"modeled_dca_fill", "modeled_external_contribution"}:
                    raise LedgerValidationError(
                        "an atomic DCA child cannot be reversed directly"
                    )
                raise LedgerValidationError("this event type cannot be reversed directly")
            existing_reversals = self._reversal_rows(connection)
            if target in existing_reversals:
                existing = existing_reversals[target]
                existing_payload = json.loads(existing["payload_json"])
                requested_key = _idempotency_key(idempotency_key or f"reversal:{target}")
                if existing_payload.get("reason") == normalized_reason and existing["idempotency_key"] == requested_key:
                    return str(existing["event_id"])
                raise LedgerIdempotencyConflict("the event already has an active reversal")
            session = _date(row["session_date"])
            payload = {"target_event_id": target, "reason": normalized_reason}
            receipt = self._append_event(
                connection,
                event_type="reversal",
                source_class="user_confirmed",
                session=session,
                occurred_at=_session_timestamp(session),
                idempotency_key=_idempotency_key(idempotency_key or f"reversal:{target}"),
                payload=payload,
            )
            if not receipt.idempotent_replay:
                self._assert_no_valuation_on_or_after(
                    connection,
                    session,
                    ("confirmed", "modeled"),
                )
            self._project_connection(connection, "confirmed", None)
            self._project_connection(connection, "modeled", None)
            return receipt.event_id

    @_fixed_decimal_context
    def reverse_dca_batch(
        self,
        batch_event_id: str,
        *,
        reason: str,
        idempotency_key: str | None = None,
    ) -> str:
        """Atomically deactivate one modeled DCA marker and every derived child."""
        target = str(batch_event_id).strip()
        normalized_reason = str(reason).strip()
        if not target or not normalized_reason:
            raise LedgerValidationError("batch_event_id and reversal reason may not be empty")
        requested_key = _idempotency_key(idempotency_key or f"dca-batch-reversal:{target}")
        with self._transaction() as connection:
            marker = connection.execute(
                "SELECT * FROM ledger_events WHERE event_id = ?",
                (target,),
            ).fetchone()
            if marker is None or marker["event_type"] != "modeled_dca_batch":
                raise LedgerValidationError("batch reversal target must be a modeled DCA batch")
            existing = self._dca_batch_reversal_rows(connection).get(target)
            if existing is not None:
                payload = json.loads(existing["payload_json"])
                if payload.get("reason") == normalized_reason and existing["idempotency_key"] == requested_key:
                    return str(existing["event_id"])
                raise LedgerIdempotencyConflict("the modeled DCA batch is already reversed")
            marker_payload = json.loads(marker["payload_json"])
            batch_key = str(marker_payload["batch_key"])
            child_rows = self._dca_child_rows(connection, batch_key)
            active_replacements = self._active_modeled_replacements(connection)
            replaced_children = sorted(
                str(row["event_id"])
                for row in child_rows
                if str(row["event_id"]) in active_replacements
            )
            if replaced_children:
                raise LedgerSettlementBlocked(
                    "reverse owner-confirmed replacements before reversing their DCA batch"
                )
            session = _date(marker["session_date"])
            self._assert_no_valuation_on_or_after(
                connection,
                session,
                ("confirmed", "modeled"),
            )
            receipt = self._append_event(
                connection,
                event_type="modeled_dca_batch_reversal",
                source_class="user_confirmed",
                session=session,
                occurred_at=_session_close_timestamp(session),
                idempotency_key=requested_key,
                payload={
                    "target_batch_event_id": target,
                    "batch_key": batch_key,
                    "child_event_ids": [str(row["event_id"]) for row in child_rows],
                    "reason": normalized_reason,
                },
            )
            self._project_connection(connection, "modeled", None)
            return receipt.event_id

    def _strict_close_lineage(
        self,
        close: AcceptedClose,
        symbol: str,
        session: dt.date,
        *,
        purpose: str,
    ) -> ValuationCloseLineage:
        """Recheck provider evidence instead of trusting aggregate gate booleans."""
        normalized_symbol = _symbol(symbol)
        if purpose == "settlement":
            allowed_statuses = {"accepted"}
            allowed_finality = {"confirmed"}
            purpose_permitted = close.price_gate_permitted
        elif purpose == "valuation":
            allowed_statuses = {"accepted", "warning"}
            allowed_finality = {"confirmed", "confirmed_with_warning"}
            purpose_permitted = close.valuation_permitted
        else:
            raise LedgerValidationError("unknown strict-close validation purpose")
        if (
            close.instrument.canonical_symbol != normalized_symbol
            or close.expected_session != session
            or close.status not in allowed_statuses
            or close.finality not in allowed_finality
            or not purpose_permitted
            or not close.price_gate_permitted
            or close.atomic_batch_permitted is not True
            or not close.eligible_for_ledger_input
            or close.selected_price is None
            or close.selected_observation_id is None
            or close.currency != self.policy.currency
        ):
            raise LedgerSettlementBlocked(
                f"{normalized_symbol} close is not eligible for strict final atomic input"
            )
        acceptance_policy = CloseAcceptancePolicy(required_currency=self.policy.currency)
        rejected_provider_ids = {
            reason.split(":", 1)[0]
            for reason in close.reasons
            if ":" in reason and not reason.startswith("atomic_batch")
        }
        eligible_observations = tuple(
            observation
            for observation in close.observations
            if not acceptance_policy.rejection_reasons(
                observation,
                close.instrument,
                session,
            )
            and observation.settlement_eligible
            and observation.provider_id not in rejected_provider_ids
            and observation.source_tier in acceptance_policy.settlement_source_tiers
        )
        independent_groups = {item.independence_group for item in eligible_observations}
        if (
            len(independent_groups) < acceptance_policy.min_independent_sources
            or close.independent_source_count != len(independent_groups)
        ):
            raise LedgerSettlementBlocked(
                f"{normalized_symbol} close lacks two actual independent settlement sources"
            )
        selected_observations = tuple(
            item
            for item in eligible_observations
            if item.observation_id == close.selected_observation_id
        )
        if (
            len(selected_observations) != 1
            or selected_observations[0].raw_close != close.selected_price
        ):
            raise LedgerSettlementBlocked(
                f"{normalized_symbol} close has inconsistent selected-source lineage"
            )
        selected_provider_id = close.selected_provider_id
        if (
            selected_provider_id is None
            or selected_provider_id != selected_observations[0].provider_id
        ):
            raise LedgerSettlementBlocked(
                f"{normalized_symbol} close has no selected-provider lineage"
            )
        return ValuationCloseLineage(
            accepted_close_id=close.accepted_close_id,
            selected_provider_id=selected_provider_id,
        )

    @_fixed_decimal_context
    def settle_modeled_dca_batch(
        self,
        plan: DcaPlan,
        accepted_close_batch: AcceptedCloseBatch,
        calendar_as_of: dt.datetime,
        corporate_action_statuses: Mapping[str, str],
    ) -> DcaSettlementResult:
        """Atomically post configured base DCA entries after all safety gates pass."""
        if not isinstance(plan, DcaPlan):
            raise LedgerValidationError("plan must be a DcaPlan")
        if not isinstance(accepted_close_batch, AcceptedCloseBatch):
            raise LedgerValidationError("accepted_close_batch has the wrong type")
        normalized_calendar_as_of = _aware_datetime(calendar_as_of, "calendar_as_of")
        if plan.currency != self.policy.currency:
            raise LedgerSettlementBlocked("DCA plan currency does not match the ledger")
        session = _date(accepted_close_batch.expected_session)
        if (
            accepted_close_batch.status != "accepted"
            or not accepted_close_batch.price_gate_permitted
            or not accepted_close_batch.eligible_for_ledger_input
        ):
            raise LedgerSettlementBlocked("the accepted-close atomic price gate did not pass")
        close_symbols = [item.instrument.canonical_symbol for item in accepted_close_batch.closes]
        if len(close_symbols) != len(set(close_symbols)):
            raise LedgerSettlementBlocked("the accepted-close batch contains duplicate symbols")
        plan_symbols = set(plan.base_amounts)
        if set(close_symbols) != plan_symbols:
            raise LedgerSettlementBlocked("the accepted-close batch must exactly cover the DCA plan")
        normalized_actions: dict[str, str] = {}
        for key, value in corporate_action_statuses.items():
            normalized_symbol = _symbol(key)
            if normalized_symbol in normalized_actions:
                raise LedgerSettlementBlocked(
                    f"duplicate corporate-action status for {normalized_symbol}"
                )
            normalized_actions[normalized_symbol] = str(value).strip().lower()
        if set(normalized_actions) != plan_symbols:
            raise LedgerSettlementBlocked("corporate-action statuses must exactly cover the DCA plan")
        unclear = sorted(
            symbol
            for symbol, status in normalized_actions.items()
            if status not in self.policy.corporate_action_clear_statuses
        )
        if unclear:
            raise LedgerSettlementBlocked("unresolved corporate actions: " + ",".join(unclear))

        calendar_proofs: dict[str, dict[str, str]] = {}
        for mic in sorted({item.instrument.exchange_mic for item in accepted_close_batch.closes}):
            try:
                close_at = self.calendar_resolver.session_close(session, mic)
                provenance = self.calendar_resolver.provenance(mic)
            except ExchangeSessionError as exc:
                raise LedgerSettlementBlocked(
                    f"calendar rejected {session.isoformat()} for {mic}"
                ) from exc
            if close_at > normalized_calendar_as_of:
                raise LedgerSettlementBlocked(
                    f"{mic} session has not reached its official close"
                )
            calendar_proofs[mic] = {
                "official_close": _occurred_at(close_at),
                "instrument_mic": provenance.instrument_mic,
                "calendar_name": provenance.calendar_name,
                "calendar_version": provenance.calendar_version,
                "exchange_timezone": provenance.exchange_timezone,
            }

        close_by_symbol = {item.instrument.canonical_symbol: item for item in accepted_close_batch.closes}
        scale = self.policy.share_scale if plan.share_scale is None else plan.share_scale
        quantum = ONE.scaleb(-scale)
        fill_specs: list[dict[str, Any]] = []
        for symbol, amount in plan.base_amounts.items():
            close = close_by_symbol[symbol]
            self._strict_close_lineage(
                close,
                symbol,
                session,
                purpose="settlement",
            )
            if close.selected_price is None:
                raise LedgerIntegrityError("strict close validation lost its selected price")
            price = _decimal(close.selected_price, f"accepted close {symbol}", positive=True)
            quantity = (amount / price).quantize(quantum, rounding=ROUND_DOWN)
            if quantity <= ZERO:
                raise LedgerSettlementBlocked(f"{symbol} base amount rounds to zero shares")
            spend = quantity * price
            residual = amount - spend
            if residual < ZERO:
                raise LedgerIntegrityError("ROUND_DOWN produced a negative DCA residual")
            fill_specs.append(
                {
                    "symbol": symbol,
                    "configured_amount": amount,
                    "price": price,
                    "quantity": quantity,
                    "spend": spend,
                    "residual": residual,
                    "accepted_close_id": close.accepted_close_id,
                    "selected_observation_id": close.selected_observation_id,
                }
            )

        total_configured = sum((item["configured_amount"] for item in fill_specs), ZERO)
        total_spend = sum((item["spend"] for item in fill_specs), ZERO)
        total_residual = total_configured - total_spend
        plan_definition = {
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "base_amounts": plan.base_amounts,
            "effective_share_scale": scale,
            "funding_mode": plan.funding_mode,
            "currency": plan.currency,
        }
        plan_definition_hash = _sha256_text(_canonical_json(plan_definition))
        input_contract = {
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "session": session,
            "currency": plan.currency,
            "funding_mode": plan.funding_mode,
            "share_scale": scale,
            "accepted_close_batch_id": accepted_close_batch.batch_id,
            "calendar_proofs": calendar_proofs,
            "corporate_action_statuses": normalized_actions,
            "fills": fill_specs,
        }
        input_hash = _sha256_text(_canonical_json(input_contract))
        with self._transaction() as connection:
            self._validate_session_not_before_opening(connection, session)
            self._assert_plan_definition_consistent(
                connection,
                plan.plan_id,
                plan.version,
                plan_definition_hash,
            )
            if self._find_active_dca_override(connection, plan.plan_id, plan.version, session) is not None:
                return DcaSettlementResult(
                    status="skipped",
                    session=session,
                    plan_id=plan.plan_id,
                    plan_version=plan.version,
                    accepted_close_batch_id=accepted_close_batch.batch_id,
                    batch_event_id=None,
                    fill_event_ids=(),
                    fill_receipts=(),
                    contribution_event_id=None,
                    total_configured_amount=total_configured,
                    total_spend=ZERO,
                    total_residual=total_configured,
                    skipped=True,
                )
            any_session_batch = self._find_any_dca_batch_event(connection, session)
            if any_session_batch is not None:
                any_payload = json.loads(any_session_batch["payload_json"])
                if (
                    any_payload.get("plan_id") != plan.plan_id
                    or any_payload.get("plan_version") != plan.version
                ):
                    raise LedgerIdempotencyConflict(
                        "the ledger already settled a different DCA plan for this session"
                    )
            existing = self._find_dca_batch_event(connection, plan.plan_id, plan.version, session)
            if existing is not None:
                existing_payload = json.loads(existing["payload_json"])
                if existing_payload.get("input_hash") != input_hash:
                    raise LedgerIdempotencyConflict(
                        "the plan/session DCA key already contains different accepted-close content"
                    )
                return self._dca_result_from_connection(connection, existing, idempotent_replay=True)

            self._assert_no_valuation_on_or_after(
                connection,
                session,
                ("confirmed", "modeled"),
            )
            projection = self._project_connection(connection, "modeled", session)
            if plan.funding_mode == "existing_cash" and projection.cash < total_spend:
                raise LedgerInsufficientCash("modeled DCA batch exceeds existing modeled cash")
            attempt = self._next_dca_attempt(connection, session)
            batch_key = (
                f"{plan.plan_id}:{plan.version}:{session.isoformat()}:attempt-{attempt}"
            )
            settlement_time = max(
                proof["official_close"] for proof in calendar_proofs.values()
            )
            marker_payload = {
                "batch_key": batch_key,
                "input_hash": input_hash,
                "plan_definition_hash": plan_definition_hash,
                "plan_definition": plan_definition,
                "plan_id": plan.plan_id,
                "plan_version": plan.version,
                "session": session,
                "currency": plan.currency,
                "funding_mode": plan.funding_mode,
                "share_scale": scale,
                "accepted_close_batch_id": accepted_close_batch.batch_id,
                "calendar_as_of": _occurred_at(normalized_calendar_as_of),
                "calendar_proofs": calendar_proofs,
                "corporate_action_statuses": normalized_actions,
                "total_configured_amount": total_configured,
                "total_spend": total_spend,
                "total_residual": total_residual,
            }
            marker = self._append_event(
                connection,
                event_type="modeled_dca_batch",
                source_class="modeled",
                session=session,
                occurred_at=settlement_time,
                idempotency_key=f"modeled-dca-batch:{batch_key}",
                payload=marker_payload,
            )
            if plan.funding_mode == "modeled_external_contribution":
                self._append_event(
                    connection,
                    event_type="modeled_external_contribution",
                    source_class="modeled",
                    session=session,
                    occurred_at=settlement_time,
                    idempotency_key=f"modeled-dca-contribution:{batch_key}",
                    payload={
                        "batch_key": batch_key,
                        "batch_event_id": marker.event_id,
                        "amount": total_configured,
                        "currency": plan.currency,
                        "external_flow": True,
                        "valuation_weight": ZERO,
                        "timing": "session_close",
                    },
                )
            for item in fill_specs:
                fill = self._append_event(
                    connection,
                    event_type="modeled_dca_fill",
                    source_class="modeled",
                    session=session,
                    occurred_at=settlement_time,
                    idempotency_key=f"modeled-dca-fill:{batch_key}:{item['symbol']}",
                    payload={
                        "batch_key": batch_key,
                        "batch_event_id": marker.event_id,
                        "symbol": item["symbol"],
                        "side": "buy",
                        "quantity": item["quantity"],
                        "price": item["price"],
                        "fees": ZERO,
                        "configured_amount": item["configured_amount"],
                        "spend": item["spend"],
                        "residual": item["residual"],
                        "currency": plan.currency,
                        "accepted_close_batch_id": accepted_close_batch.batch_id,
                        "accepted_close_id": item["accepted_close_id"],
                        "selected_observation_id": item["selected_observation_id"],
                        "cost_basis_semantics": "economic_carrying_cost_not_tax_basis",
                    },
                )
            # The post-state replay is inside the same IMMEDIATE transaction.
            self._project_connection(connection, "modeled", session)
            marker_row = connection.execute(
                "SELECT * FROM ledger_events WHERE event_id = ?",
                (marker.event_id,),
            ).fetchone()
            if marker_row is None:
                raise LedgerIntegrityError("DCA marker disappeared inside its transaction")
            return self._dca_result_from_connection(
                connection,
                marker_row,
                idempotent_replay=False,
            )

    @_fixed_decimal_context
    def project(
        self,
        book_kind: str = "modeled",
        as_of: dt.date | str | None = None,
    ) -> LedgerProjection:
        """Replay a confirmed or modeled point-in-time book."""
        normalized_book = _book_kind(book_kind)
        normalized_as_of = None if as_of is None else _date(as_of)
        connection = self._connect()
        try:
            self._verify_hash_chain_connection(connection)
            self._require_initialized(connection)
            self._validated_valuation_chains_connection(connection)
            return self._project_connection(connection, normalized_book, normalized_as_of)
        finally:
            connection.close()

    @_fixed_decimal_context
    def opening_checkpoint(self) -> OpeningCheckpoint:
        """Return the hash-verified immutable opening snapshot."""
        connection = self._connect()
        try:
            self._verify_hash_chain_connection(connection)
            row = self._require_initialized(connection)
            self._validated_valuation_chains_connection(connection)
            payload = _json_object(row["payload_json"], "opening snapshot")
            raw_positions = payload.get("positions")
            if not isinstance(raw_positions, list):
                raise LedgerIntegrityError("opening snapshot positions are malformed")
            positions: list[OpeningPosition] = []
            seen: set[str] = set()
            for raw_position in raw_positions:
                if not isinstance(raw_position, Mapping):
                    raise LedgerIntegrityError("opening snapshot position is malformed")
                try:
                    position = OpeningPosition(
                        symbol=raw_position["symbol"],
                        quantity=raw_position["quantity"],
                        average_economic_cost=raw_position["average_economic_cost"],
                    )
                except KeyError as exc:
                    raise LedgerIntegrityError(
                        "opening snapshot position is missing a required field"
                    ) from exc
                if position.symbol in seen:
                    raise LedgerIntegrityError("opening snapshot contains duplicate symbols")
                seen.add(position.symbol)
                positions.append(position)
            return OpeningCheckpoint(
                opening_event_id=_sha256_digest(row["event_id"], "opening event id"),
                opening_event_hash=_sha256_digest(row["event_hash"], "opening event hash"),
                idempotency_key=_idempotency_key(row["idempotency_key"]),
                created_at=_stored_utc_datetime(
                    row["created_at"],
                    "opening created_at",
                ),
                session=_date(row["session_date"]),
                currency=str(payload.get("currency", "")).strip().upper(),
                cash=_decimal(payload.get("cash"), "opening cash", minimum=ZERO),
                positions=tuple(sorted(positions, key=lambda item: item.symbol)),
            )
        finally:
            connection.close()

    @_fixed_decimal_context
    def valuation_at(
        self,
        book_kind: str,
        session: dt.date | str,
    ) -> LedgerValuation | None:
        """Return one hash-verified valuation without exposing storage internals."""
        normalized_book = _book_kind(book_kind)
        normalized_session = _date(session)
        connection = self._connect()
        try:
            self._verify_hash_chain_connection(connection)
            self._require_initialized(connection)
            chains = self._validated_valuation_chains_connection(connection)
            return next(
                (
                    valuation
                    for valuation in chains[normalized_book]
                    if valuation.session == normalized_session
                ),
                None,
            )
        finally:
            connection.close()

    @_fixed_decimal_context
    def latest_common_valuation_session(self) -> dt.date | None:
        """Return the newest session with both confirmed and modeled valuations."""
        connection = self._connect()
        try:
            self._verify_hash_chain_connection(connection)
            self._require_initialized(connection)
            return self._latest_common_valuation_session_connection(connection)
        finally:
            connection.close()

    @_fixed_decimal_context
    def latest_common_valuation(self) -> CommonLedgerValuation | None:
        """Return both books at their newest common hash-verified valuation session."""
        connection = self._connect()
        try:
            self._verify_hash_chain_connection(connection)
            self._require_initialized(connection)
            chains = self._validated_valuation_chains_connection(connection)
            common_sessions = {
                item.session for item in chains["confirmed"]
            } & {item.session for item in chains["modeled"]}
            session = max(common_sessions) if common_sessions else None
            if session is None:
                return None
            confirmed = next(item for item in chains["confirmed"] if item.session == session)
            modeled = next(item for item in chains["modeled"] if item.session == session)
            return CommonLedgerValuation(
                session=session,
                confirmed=confirmed,
                modeled=modeled,
            )
        finally:
            connection.close()

    @_fixed_decimal_context
    def session_audit(self, session: dt.date | str) -> LedgerSessionAudit:
        """Recover DCA receipts and both valuations, including partial valuation state."""
        normalized_session = _date(session)
        connection = self._connect()
        try:
            self._verify_hash_chain_connection(connection)
            self._validate_session_not_before_opening(connection, normalized_session)
            chains = self._validated_valuation_chains_connection(connection)
            marker = self._find_any_dca_batch_event(connection, normalized_session)
            override_row = self._find_any_active_dca_override(
                connection,
                normalized_session,
            )
            owner_skip = (
                None
                if override_row is None
                else self._dca_skip_from_row(connection, override_row)
            )
            if marker is not None and owner_skip is not None:
                raise LedgerIntegrityError(
                    "session cannot have both an active DCA batch and owner skip"
                )
            confirmed = next(
                (
                    item
                    for item in chains["confirmed"]
                    if item.session == normalized_session
                ),
                None,
            )
            modeled = next(
                (
                    item
                    for item in chains["modeled"]
                    if item.session == normalized_session
                ),
                None,
            )
            last_row = connection.execute(
                "SELECT event_hash FROM ledger_events WHERE session_date <= ? "
                "ORDER BY sequence_no DESC LIMIT 1",
                (normalized_session.isoformat(),),
            ).fetchone()
            if last_row is None:
                raise LedgerIntegrityError("session audit has no opening checkpoint")
            return LedgerSessionAudit(
                session=normalized_session,
                dca_settlement=(
                    None
                    if marker is None
                    else self._dca_result_from_connection(
                        connection,
                        marker,
                        idempotent_replay=True,
                    )
                ),
                owner_skip=owner_skip,
                confirmed_valuation=confirmed,
                modeled_valuation=modeled,
                last_event_hash=_sha256_digest(last_row["event_hash"], "session event hash"),
            )
        finally:
            connection.close()

    def contains_event_hash(self, event_hash: str) -> bool:
        """Check a hash-chain checkpoint without allowing callers to query SQLite."""
        normalized_hash = _sha256_digest(event_hash, "event_hash")
        connection = self._connect()
        try:
            self._verify_hash_chain_connection(connection)
            self._validated_valuation_chains_connection(connection)
            row = connection.execute(
                "SELECT 1 FROM ledger_events WHERE event_hash = ? LIMIT 1",
                (normalized_hash,),
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    @_fixed_decimal_context
    def record_valuation(
        self,
        book_kind: str,
        accepted_close_batch: AcceptedCloseBatch,
    ) -> LedgerValuation:
        """Record a final valuation only when every open position has a close."""
        normalized_book = _book_kind(book_kind)
        if not isinstance(accepted_close_batch, AcceptedCloseBatch):
            raise LedgerValidationError("accepted_close_batch has the wrong type")
        session = _date(accepted_close_batch.expected_session)
        if (
            accepted_close_batch.status != "accepted"
            or not accepted_close_batch.price_gate_permitted
            or not accepted_close_batch.eligible_for_ledger_input
        ):
            raise LedgerSettlementBlocked("valuation requires an accepted atomic close batch")
        with self._transaction() as connection:
            self._validate_session_not_before_opening(connection, session)
            projection = self._project_connection(connection, normalized_book, session)
            closes: dict[str, Any] = {}
            for close in accepted_close_batch.closes:
                symbol = close.instrument.canonical_symbol
                if symbol in closes:
                    raise LedgerSettlementBlocked("valuation close batch contains duplicate symbols")
                closes[symbol] = close
            required_symbols = {
                item.symbol for item in projection.positions if item.quantity != ZERO
            }
            missing = sorted(required_symbols - set(closes))
            if missing:
                raise LedgerSettlementBlocked("valuation is missing open positions: " + ",".join(missing))
            prices: dict[str, Decimal] = {}
            accepted_close_lineage: dict[str, ValuationCloseLineage] = {}
            for symbol in sorted(required_symbols):
                close = closes[symbol]
                lineage = self._strict_close_lineage(
                    close,
                    symbol,
                    session,
                    purpose="valuation",
                )
                if close.selected_price is None:
                    raise LedgerIntegrityError("strict close validation lost its selected price")
                prices[symbol] = _decimal(close.selected_price, f"valuation price {symbol}", positive=True)
                accepted_close_lineage[symbol] = lineage
            securities_value = sum(
                (item.quantity * prices[item.symbol] for item in projection.positions if item.quantity != ZERO),
                ZERO,
            )
            nav = projection.cash + securities_value
            if nav < ZERO:
                raise LedgerProjectionError("portfolio NAV may not be negative")

            self._validated_valuation_chains_connection(connection)
            valuation_rows = self._valuation_rows(connection, normalized_book)
            later = [row for row in valuation_rows if _date(row["session_date"]) > session]
            if later:
                raise LedgerIdempotencyConflict("a later valuation already depends on this book history")
            previous_rows = [row for row in valuation_rows if _date(row["session_date"]) < session]
            previous = previous_rows[-1] if previous_rows else None
            previous_payload = (
                None
                if previous is None
                else _json_object(previous["payload_json"], "previous valuation")
            )
            previous_valuation = (
                None if previous is None else self._valuation_from_row(previous)
            )
            prior_cumulative_flow = (
                ZERO
                if previous_payload is None
                else _decimal(previous_payload["cumulative_external_flow"], "prior cumulative flow")
            )
            net_external_flow = projection.net_external_flow - prior_cumulative_flow
            weighted_external_flow = self._weighted_external_flow(
                connection,
                normalized_book,
                None if previous is None else _date(previous["session_date"]),
                session,
            )
            if previous_payload is None:
                daily_pnl: Decimal | None = None
                daily_return: Decimal | None = None
                cumulative_twr: Decimal | None = None
                prior_nav: Decimal | None = None
                prior_cumulative_twr: Decimal | None = None
                previous_event_id: str | None = None
            else:
                if previous_valuation is None:
                    raise LedgerIntegrityError("previous valuation could not be recovered")
                prior_nav = previous_valuation.nav
                prior_cumulative_twr = previous_valuation.cumulative_twr
                daily_pnl = nav - prior_nav - net_external_flow
                denominator = prior_nav + weighted_external_flow
                if denominator <= ZERO:
                    raise LedgerProjectionError("weighted starting capital must be positive")
                daily_return = daily_pnl / denominator
                if prior_cumulative_twr is None:
                    # Avoid avoidable add/subtract cancellation: the first linked
                    # period's cumulative return is exactly its daily return.
                    cumulative_twr = daily_return
                else:
                    cumulative_twr = (
                        (ONE + prior_cumulative_twr) * (ONE + daily_return) - ONE
                    )
                previous_event_id = str(previous["event_id"])

            input_contract = {
                "contract_version": _VALUATION_CONTRACT_VERSION,
                "book_kind": normalized_book,
                "session": session,
                "accepted_close_batch_id": accepted_close_batch.batch_id,
                "prices": prices,
                "accepted_close_lineage": {
                    symbol: {
                        "accepted_close_id": item.accepted_close_id,
                        "selected_provider_id": item.selected_provider_id,
                    }
                    for symbol, item in accepted_close_lineage.items()
                },
                "cash": projection.cash,
                "positions": {
                    item.symbol: item.quantity for item in projection.positions if item.quantity != ZERO
                },
                "securities_value": securities_value,
                "nav": nav,
                "net_external_flow": net_external_flow,
                "weighted_external_flow": weighted_external_flow,
                "prior_nav": prior_nav,
                "prior_cumulative_twr": prior_cumulative_twr,
                "previous_valuation_event_id": previous_event_id,
            }
            input_hash = _sha256_text(_canonical_json(input_contract))
            idempotency_key = f"valuation:{normalized_book}:{session.isoformat()}"
            existing = connection.execute(
                "SELECT * FROM ledger_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                existing_payload = json.loads(existing["payload_json"])
                if existing_payload.get("contract_version") != _VALUATION_CONTRACT_VERSION:
                    raise LedgerIntegrityError(
                        "legacy valuation payload lacks v2 close lineage; rebuild the private ledger"
                    )
                if existing_payload.get("input_hash") != input_hash:
                    raise LedgerIdempotencyConflict("the book/session valuation already has different content")
                return self._valuation_from_row(existing, idempotent_replay=True)
            payload = {
                "contract_version": _VALUATION_CONTRACT_VERSION,
                "input_hash": input_hash,
                "book_kind": normalized_book,
                "accepted_close_batch_id": accepted_close_batch.batch_id,
                "currency": self.policy.currency,
                "cash": projection.cash,
                "securities_value": securities_value,
                "nav": nav,
                "prices": prices,
                "accepted_close_lineage": {
                    symbol: {
                        "accepted_close_id": item.accepted_close_id,
                        "selected_provider_id": item.selected_provider_id,
                    }
                    for symbol, item in accepted_close_lineage.items()
                },
                "prior_nav": prior_nav,
                "prior_cumulative_twr": prior_cumulative_twr,
                "daily_pnl": daily_pnl,
                "daily_return": daily_return,
                "cumulative_twr": cumulative_twr,
                "net_external_flow": net_external_flow,
                "weighted_external_flow": weighted_external_flow,
                "cumulative_external_flow": projection.net_external_flow,
                "previous_valuation_event_id": previous_event_id,
            }
            receipt = self._append_event(
                connection,
                event_type="valuation",
                source_class="system",
                session=session,
                occurred_at=_session_close_timestamp(session),
                idempotency_key=idempotency_key,
                payload=payload,
            )
            row = connection.execute(
                "SELECT * FROM ledger_events WHERE event_id = ?",
                (receipt.event_id,),
            ).fetchone()
            if row is None:
                raise LedgerIntegrityError("valuation event disappeared inside its transaction")
            chains = self._validated_valuation_chains_connection(connection)
            return next(
                item
                for item in chains[normalized_book]
                if item.valuation_event_id == receipt.event_id
            )

    def verify_hash_chain(self) -> bool:
        """Verify event ordering, previous hashes and SHA-256 event hashes."""
        connection = self._connect()
        try:
            self._verify_hash_chain_connection(connection)
            return True
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        """Create the private append-only store with durable SQLite settings."""
        if str(self.database_path) != ":memory:":
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_events (
                    sequence_no INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    session_date TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source_class TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_events_session
                    ON ledger_events(session_date, sequence_no);
                CREATE INDEX IF NOT EXISTS idx_ledger_events_type_session
                    ON ledger_events(event_type, session_date, sequence_no);
                CREATE TRIGGER IF NOT EXISTS ledger_events_no_update
                BEFORE UPDATE ON ledger_events
                BEGIN
                    SELECT RAISE(ABORT, 'ledger_events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS ledger_events_no_delete
                BEFORE DELETE ON ledger_events
                BEGIN
                    SELECT RAISE(ABORT, 'ledger_events are append-only');
                END;
                """
            )
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=self.policy.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.policy.busy_timeout_ms}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self, *, require_initialized: bool = True) -> Iterable[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_hash_chain_connection(connection)
            if require_initialized:
                self._require_initialized(connection)
            self._validated_valuation_chains_connection(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        source_class: str,
        session: dt.date | str,
        occurred_at: dt.datetime | str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        created_at: dt.datetime | str | None = None,
    ) -> _AppendReceipt:
        """Append one canonical event using the caller's active transaction."""
        normalized_type = str(event_type).strip().lower()
        normalized_source = str(source_class).strip().lower()
        if not normalized_type:
            raise LedgerValidationError("event_type may not be empty")
        if normalized_source not in {"user_confirmed", "modeled", "system"}:
            raise LedgerValidationError("unknown event source_class")
        normalized_session = _date(session)
        normalized_occurred_at = _occurred_at(occurred_at)
        normalized_key = _idempotency_key(idempotency_key)
        payload_json = _canonical_json(payload)
        identity = {
            "idempotency_key": normalized_key,
            "session_date": normalized_session.isoformat(),
            "occurred_at": normalized_occurred_at,
            "event_type": normalized_type,
            "source_class": normalized_source,
            "payload": json.loads(payload_json),
        }
        event_id = _sha256_text(_canonical_json(identity))
        existing = connection.execute(
            "SELECT event_id, event_hash FROM ledger_events WHERE idempotency_key = ?",
            (normalized_key,),
        ).fetchone()
        if existing is not None:
            if str(existing["event_id"]) != event_id:
                raise LedgerIdempotencyConflict("idempotency key was reused for different event content")
            return _AppendReceipt(
                event_id=event_id,
                event_hash=str(existing["event_hash"]),
                idempotent_replay=True,
            )
        prior = connection.execute(
            "SELECT event_hash FROM ledger_events ORDER BY sequence_no DESC LIMIT 1"
        ).fetchone()
        previous_hash = _GENESIS_HASH if prior is None else str(prior["event_hash"])
        normalized_created_at = (
            _now_rfc3339() if created_at is None else _occurred_at(created_at)
        )
        hash_body = {
            "event_id": event_id,
            **identity,
            "created_at": normalized_created_at,
        }
        event_hash = _chain_hash(previous_hash, hash_body)
        connection.execute(
            """
            INSERT INTO ledger_events (
                event_id, idempotency_key, session_date, occurred_at,
                event_type, source_class, payload_json, previous_hash,
                event_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                normalized_key,
                normalized_session.isoformat(),
                normalized_occurred_at,
                normalized_type,
                normalized_source,
                payload_json,
                previous_hash,
                event_hash,
                normalized_created_at,
            ),
        )
        return _AppendReceipt(event_id=event_id, event_hash=event_hash, idempotent_replay=False)

    def _verify_hash_chain_connection(self, connection: sqlite3.Connection) -> None:
        expected_previous = _GENESIS_HASH
        rows = connection.execute("SELECT * FROM ledger_events ORDER BY sequence_no").fetchall()
        opening_count = 0
        for row in rows:
            if str(row["previous_hash"]) != expected_previous:
                raise LedgerIntegrityError(
                    f"event hash-chain predecessor mismatch at sequence {row['sequence_no']}"
                )
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as exc:
                raise LedgerIntegrityError("event payload is not valid JSON") from exc
            if _canonical_json(payload) != row["payload_json"]:
                raise LedgerIntegrityError("event payload is not canonical JSON")
            identity = {
                "idempotency_key": row["idempotency_key"],
                "session_date": row["session_date"],
                "occurred_at": row["occurred_at"],
                "event_type": row["event_type"],
                "source_class": row["source_class"],
                "payload": payload,
            }
            expected_event_id = _sha256_text(_canonical_json(identity))
            if str(row["event_id"]) != expected_event_id:
                raise LedgerIntegrityError(
                    f"event identity mismatch at sequence {row['sequence_no']}"
                )
            hash_body = {
                "event_id": expected_event_id,
                **identity,
                "created_at": row["created_at"],
            }
            expected_hash = _chain_hash(expected_previous, hash_body)
            if str(row["event_hash"]) != expected_hash:
                raise LedgerIntegrityError(
                    f"event hash mismatch at sequence {row['sequence_no']}"
                )
            expected_previous = expected_hash
            if row["event_type"] == "opening_snapshot":
                opening_count += 1
        if opening_count > 1:
            raise LedgerIntegrityError("the event stream contains more than one opening snapshot")

    def _require_initialized(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM ledger_events WHERE event_type = 'opening_snapshot' "
            "ORDER BY sequence_no LIMIT 1"
        ).fetchone()
        if row is None:
            raise LedgerNotInitializedError("initialize the ledger before recording events")
        payload = json.loads(row["payload_json"])
        opening_currency = str(payload.get("currency", "")).strip().upper()
        if opening_currency != self.policy.currency:
            raise LedgerValidationError(
                "LedgerPolicy currency does not match the immutable opening snapshot"
            )
        return row

    def _validate_session_not_before_opening(
        self,
        connection: sqlite3.Connection,
        session: dt.date,
    ) -> None:
        opening = self._require_initialized(connection)
        if session < _date(opening["session_date"]):
            raise LedgerValidationError("event session precedes the opening snapshot")

    def _assert_no_valuation_on_or_after(
        self,
        connection: sqlite3.Connection,
        session: dt.date,
        book_kinds: Sequence[str],
    ) -> None:
        frozen_books = {_book_kind(item) for item in book_kinds}
        rows = connection.execute(
            "SELECT session_date, payload_json FROM ledger_events "
            "WHERE event_type = 'valuation' ORDER BY session_date, sequence_no"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("book_kind") in frozen_books and _date(row["session_date"]) >= session:
                raise LedgerSettlementBlocked(
                    "an existing valuation freezes economic events on or before its session"
                )

    def _reversal_rows(self, connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
        result: dict[str, sqlite3.Row] = {}
        rows = connection.execute(
            "SELECT * FROM ledger_events WHERE event_type = 'reversal' ORDER BY sequence_no"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            target = str(payload.get("target_event_id", ""))
            if target in result:
                raise LedgerIntegrityError("an event has more than one active reversal")
            result[target] = row
        return result

    def _reversed_event_ids(self, connection: sqlite3.Connection) -> set[str]:
        return set(self._reversal_rows(connection))

    def _dca_batch_reversal_rows(self, connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
        result: dict[str, sqlite3.Row] = {}
        rows = connection.execute(
            "SELECT * FROM ledger_events WHERE event_type = 'modeled_dca_batch_reversal' "
            "ORDER BY sequence_no"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            target = str(payload.get("target_batch_event_id", ""))
            if not target:
                raise LedgerIntegrityError("DCA batch reversal has no target")
            if target in result:
                raise LedgerIntegrityError("a modeled DCA batch has more than one reversal")
            result[target] = row
        return result

    def _reversed_dca_batch_ids(self, connection: sqlite3.Connection) -> set[str]:
        return set(self._dca_batch_reversal_rows(connection))

    def _dca_child_rows(
        self,
        connection: sqlite3.Connection,
        batch_key: str,
    ) -> list[sqlite3.Row]:
        rows = connection.execute(
            "SELECT * FROM ledger_events WHERE event_type IN "
            "('modeled_external_contribution', 'modeled_dca_fill') ORDER BY sequence_no"
        ).fetchall()
        return [
            row
            for row in rows
            if json.loads(row["payload_json"]).get("batch_key") == batch_key
        ]

    def _active_batch_for_child(
        self,
        connection: sqlite3.Connection,
        batch_key: Any,
    ) -> sqlite3.Row | None:
        if not isinstance(batch_key, str) or not batch_key:
            return None
        reversed_batches = self._reversed_dca_batch_ids(connection)
        rows = connection.execute(
            "SELECT * FROM ledger_events WHERE event_type = 'modeled_dca_batch' "
            "ORDER BY sequence_no"
        ).fetchall()
        matches = [
            row
            for row in rows
            if row["event_id"] not in reversed_batches
            and json.loads(row["payload_json"]).get("batch_key") == batch_key
        ]
        if len(matches) > 1:
            raise LedgerIntegrityError("a batch_key identifies multiple active DCA batches")
        return matches[0] if matches else None

    def _assert_plan_definition_consistent(
        self,
        connection: sqlite3.Connection,
        plan_id: str,
        plan_version: str,
        definition_hash: str,
    ) -> None:
        rows = connection.execute(
            "SELECT payload_json FROM ledger_events WHERE event_type = 'modeled_dca_batch' "
            "ORDER BY sequence_no"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("plan_id") != plan_id or payload.get("plan_version") != plan_version:
                continue
            stored = payload.get("plan_definition_hash")
            if stored != definition_hash:
                raise LedgerIdempotencyConflict(
                    "a DCA plan_id/version must retain one immutable definition"
                )

    def _next_dca_attempt(self, connection: sqlite3.Connection, session: dt.date) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM ledger_events "
            "WHERE event_type = 'modeled_dca_batch' AND session_date = ?",
            (session.isoformat(),),
        ).fetchone()
        return int(row["count"]) + 1

    def _active_modeled_replacements(self, connection: sqlite3.Connection) -> dict[str, str]:
        reversed_ids = self._reversed_event_ids(connection)
        replacements: dict[str, str] = {}
        rows = connection.execute(
            "SELECT event_id, payload_json FROM ledger_events "
            "WHERE event_type = 'user_confirmed_fill' ORDER BY sequence_no"
        ).fetchall()
        for row in rows:
            if row["event_id"] in reversed_ids:
                continue
            payload = json.loads(row["payload_json"])
            target = payload.get("replaces_modeled_event_id")
            if target:
                if target in replacements:
                    raise LedgerIntegrityError("a modeled DCA fill has multiple active replacements")
                replacements[str(target)] = str(row["event_id"])
        return replacements

    def _find_dca_batch_event(
        self,
        connection: sqlite3.Connection,
        plan_id: str,
        plan_version: str,
        session: dt.date,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            "SELECT * FROM ledger_events WHERE event_type = 'modeled_dca_batch' "
            "AND session_date = ? ORDER BY sequence_no",
            (session.isoformat(),),
        ).fetchall()
        reversed_batches = self._reversed_dca_batch_ids(connection)
        matches = []
        for row in rows:
            if row["event_id"] in reversed_batches:
                continue
            payload = json.loads(row["payload_json"])
            if payload.get("plan_id") == plan_id and payload.get("plan_version") == plan_version:
                matches.append(row)
        if len(matches) > 1:
            raise LedgerIntegrityError("duplicate modeled DCA batch events exist")
        return matches[0] if matches else None

    def _find_any_dca_batch_event(
        self,
        connection: sqlite3.Connection,
        session: dt.date,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            "SELECT * FROM ledger_events WHERE event_type = 'modeled_dca_batch' "
            "AND session_date = ? ORDER BY sequence_no",
            (session.isoformat(),),
        ).fetchall()
        reversed_batches = self._reversed_dca_batch_ids(connection)
        rows = [row for row in rows if row["event_id"] not in reversed_batches]
        if len(rows) > 1:
            raise LedgerIntegrityError("more than one modeled DCA batch exists for a session")
        return rows[0] if rows else None

    def _find_active_dca_override(
        self,
        connection: sqlite3.Connection,
        plan_id: str,
        plan_version: str,
        session: dt.date,
    ) -> sqlite3.Row | None:
        reversed_ids = self._reversed_event_ids(connection)
        rows = connection.execute(
            "SELECT * FROM ledger_events WHERE event_type = 'dca_override' "
            "AND session_date = ? ORDER BY sequence_no",
            (session.isoformat(),),
        ).fetchall()
        matches = []
        for row in rows:
            if row["event_id"] in reversed_ids:
                continue
            payload = json.loads(row["payload_json"])
            if payload.get("plan_id") == plan_id and payload.get("plan_version") == plan_version:
                matches.append(row)
        if len(matches) > 1:
            raise LedgerIntegrityError("duplicate active DCA overrides exist")
        return matches[0] if matches else None

    def _find_any_active_dca_override(
        self,
        connection: sqlite3.Connection,
        session: dt.date,
    ) -> sqlite3.Row | None:
        reversed_ids = self._reversed_event_ids(connection)
        rows = connection.execute(
            "SELECT * FROM ledger_events WHERE event_type = 'dca_override' "
            "AND session_date = ? ORDER BY sequence_no",
            (session.isoformat(),),
        ).fetchall()
        active = [row for row in rows if str(row["event_id"]) not in reversed_ids]
        if len(active) > 1:
            raise LedgerIntegrityError("multiple active owner DCA skips exist for one session")
        return active[0] if active else None

    def _dca_skip_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> DcaSkipReceipt:
        event_id = _sha256_digest(row["event_id"], "DCA skip override event id")
        if event_id in self._reversed_event_ids(connection):
            raise LedgerIntegrityError("reversed DCA override cannot be an active owner skip")
        if row["event_type"] != "dca_override" or row["source_class"] != "user_confirmed":
            raise LedgerIntegrityError("DCA skip is not an owner-confirmed override")
        payload = _json_object(row["payload_json"], "DCA skip override")
        if payload.get("action") != "skip":
            raise LedgerIntegrityError("DCA override action is not skip")
        return DcaSkipReceipt(
            session=_date(row["session_date"]),
            plan_id=payload.get("plan_id", ""),
            plan_version=payload.get("plan_version", ""),
            override_event_id=event_id,
            reason=payload.get("reason", ""),
        )

    def _dca_result_from_connection(
        self,
        connection: sqlite3.Connection,
        marker: sqlite3.Row,
        *,
        idempotent_replay: bool,
    ) -> DcaSettlementResult:
        if (
            marker["event_type"] != "modeled_dca_batch"
            or marker["source_class"] != "modeled"
        ):
            raise LedgerIntegrityError("DCA receipt marker has the wrong event type")
        payload = _json_object(marker["payload_json"], "modeled DCA marker")
        marker_session = _date(marker["session_date"])
        if (
            _date(payload.get("session")) != marker_session
            or str(payload.get("currency", "")).strip().upper() != self.policy.currency
        ):
            raise LedgerIntegrityError("modeled DCA marker has inconsistent session or currency")
        batch_key = str(payload.get("batch_key", "")).strip()
        if not batch_key:
            raise LedgerIntegrityError("modeled DCA marker has no batch key")
        marker_id = _sha256_digest(marker["event_id"], "DCA marker event id")
        rows = self._dca_child_rows(connection, batch_key)
        receipts: list[DcaFillReceipt] = []
        contributions: list[sqlite3.Row] = []
        configured_by_symbol: dict[str, Decimal] = {}
        for row in rows:
            child_payload = _json_object(row["payload_json"], "modeled DCA child")
            if child_payload.get("batch_event_id") != marker_id:
                raise LedgerIntegrityError("modeled DCA child identifies the wrong marker")
            if (
                row["source_class"] != "modeled"
                or _date(row["session_date"]) != marker_session
                or str(child_payload.get("currency", "")).strip().upper()
                != self.policy.currency
            ):
                raise LedgerIntegrityError("modeled DCA child has inconsistent source lineage")
            if row["event_type"] == "modeled_external_contribution":
                contributions.append(row)
                continue
            if (
                child_payload.get("side") != "buy"
                or _decimal(child_payload.get("fees"), "DCA fill fees", minimum=ZERO)
                != ZERO
            ):
                raise LedgerIntegrityError("modeled DCA receipt is not a zero-fee buy")
            try:
                symbol = _symbol(child_payload["symbol"])
                quantity = _decimal(child_payload["quantity"], "DCA quantity", positive=True)
                price = _decimal(child_payload["price"], "DCA price", positive=True)
                spend = _decimal(child_payload["spend"], "DCA spend", positive=True)
                residual = _decimal(child_payload["residual"], "DCA residual", minimum=ZERO)
                configured_amount = _decimal(
                    child_payload["configured_amount"],
                    "DCA configured amount",
                    positive=True,
                )
                accepted_close_id = child_payload["accepted_close_id"]
            except KeyError as exc:
                raise LedgerIntegrityError("modeled DCA child is missing receipt data") from exc
            if symbol in configured_by_symbol:
                raise LedgerIntegrityError("modeled DCA batch contains duplicate fill symbols")
            if configured_amount != spend + residual:
                raise LedgerIntegrityError("modeled DCA fill does not conserve configured cash")
            if child_payload.get("accepted_close_batch_id") != payload.get(
                "accepted_close_batch_id"
            ):
                raise LedgerIntegrityError("modeled DCA fill has inconsistent accepted-close batch")
            configured_by_symbol[symbol] = configured_amount
            receipts.append(
                DcaFillReceipt(
                    symbol=symbol,
                    quantity=quantity,
                    price=price,
                    spend=spend,
                    residual=residual,
                    accepted_close_id=accepted_close_id,
                    settlement_event_id=row["event_id"],
                )
            )
        plan_definition = payload.get("plan_definition")
        if not isinstance(plan_definition, Mapping):
            raise LedgerIntegrityError("modeled DCA marker has no immutable plan definition")
        raw_base_amounts = plan_definition.get("base_amounts")
        if not isinstance(raw_base_amounts, Mapping):
            raise LedgerIntegrityError("modeled DCA plan base amounts are malformed")
        expected_amounts: dict[str, Decimal] = {}
        for raw_symbol, amount in raw_base_amounts.items():
            symbol = _symbol(raw_symbol)
            if symbol in expected_amounts:
                raise LedgerIntegrityError("modeled DCA plan has duplicate normalized symbols")
            expected_amounts[symbol] = _decimal(
                amount,
                f"DCA plan amount {symbol}",
                positive=True,
            )
        if configured_by_symbol != expected_amounts:
            raise LedgerIntegrityError("modeled DCA receipts do not cover the immutable plan")
        total_configured = sum(configured_by_symbol.values(), ZERO)
        total_spend = sum((item.spend for item in receipts), ZERO)
        total_residual = sum((item.residual for item in receipts), ZERO)
        if (
            _decimal(payload.get("total_configured_amount"), "total configured")
            != total_configured
            or _decimal(payload.get("total_spend"), "total spend") != total_spend
            or _decimal(payload.get("total_residual"), "total residual") != total_residual
            or total_configured != total_spend + total_residual
        ):
            raise LedgerIntegrityError("modeled DCA marker totals do not match its receipts")
        funding_mode = str(payload.get("funding_mode", "")).strip().lower()
        if funding_mode == "modeled_external_contribution":
            if len(contributions) != 1:
                raise LedgerIntegrityError("modeled-funded DCA must have one contribution event")
            contribution_payload = _json_object(
                contributions[0]["payload_json"],
                "modeled DCA contribution",
            )
            if (
                _decimal(contribution_payload.get("amount"), "DCA contribution")
                != total_configured
                or contribution_payload.get("external_flow") is not True
                or _decimal(
                    contribution_payload.get("valuation_weight"),
                    "DCA contribution valuation weight",
                )
                != ZERO
            ):
                raise LedgerIntegrityError("modeled DCA contribution does not fund the plan")
            contribution_id = _sha256_digest(
                contributions[0]["event_id"],
                "DCA contribution event id",
            )
        elif funding_mode == "existing_cash":
            if contributions:
                raise LedgerIntegrityError("existing-cash DCA unexpectedly has a contribution")
            contribution_id = None
        else:
            raise LedgerIntegrityError("modeled DCA marker has an unknown funding mode")
        ordered_receipts = tuple(sorted(receipts, key=lambda item: item.symbol))
        return DcaSettlementResult(
            status="settled",
            session=marker_session,
            plan_id=str(payload["plan_id"]),
            plan_version=str(payload["plan_version"]),
            accepted_close_batch_id=str(payload["accepted_close_batch_id"]),
            batch_event_id=marker_id,
            fill_event_ids=tuple(item.settlement_event_id for item in ordered_receipts),
            fill_receipts=ordered_receipts,
            contribution_event_id=contribution_id,
            total_configured_amount=total_configured,
            total_spend=total_spend,
            total_residual=total_residual,
            idempotent_replay=idempotent_replay,
        )

    @_fixed_decimal_context
    def _project_connection(
        self,
        connection: sqlite3.Connection,
        book_kind: str,
        as_of: dt.date | None,
    ) -> LedgerProjection:
        normalized_book = _book_kind(book_kind)
        parameters: tuple[Any, ...] = ()
        sql = "SELECT * FROM ledger_events"
        if as_of is not None:
            sql += " WHERE session_date <= ?"
            parameters = (as_of.isoformat(),)
        sql += " ORDER BY sequence_no"
        audit_rows = list(connection.execute(sql, parameters).fetchall())
        if not audit_rows:
            raise LedgerNotInitializedError("no opening snapshot is available at this date")
        rows = sorted(
            audit_rows,
            key=lambda row: (
                str(row["session_date"]),
                str(row["occurred_at"]),
                int(row["sequence_no"]),
            ),
        )
        reversal_targets: set[str] = set()
        for row in rows:
            if row["event_type"] == "reversal":
                payload = json.loads(row["payload_json"])
                reversal_targets.add(str(payload["target_event_id"]))
        replacement_targets: set[str] = set()
        for row in rows:
            if row["event_type"] != "user_confirmed_fill" or row["event_id"] in reversal_targets:
                continue
            payload = json.loads(row["payload_json"])
            target = payload.get("replaces_modeled_event_id")
            if target:
                if str(target) in replacement_targets:
                    raise LedgerIntegrityError("a modeled fill has multiple active replacements")
                replacement_targets.add(str(target))
        reversed_batch_ids: set[str] = set()
        for row in rows:
            if row["event_type"] == "modeled_dca_batch_reversal":
                payload = json.loads(row["payload_json"])
                reversed_batch_ids.add(str(payload["target_batch_event_id"]))
        reversed_batch_keys: set[str] = set()
        for row in rows:
            if row["event_type"] == "modeled_dca_batch" and str(row["event_id"]) in reversed_batch_ids:
                reversed_batch_keys.add(str(json.loads(row["payload_json"])["batch_key"]))

        cash: Decimal | None = None
        positions: dict[str, _MutablePosition] = {}
        fees = ZERO
        non_position_realized = ZERO
        net_external_flow = ZERO
        opening_seen = False
        for row in rows:
            event_id = str(row["event_id"])
            event_type = str(row["event_type"])
            if event_id in reversal_targets:
                continue
            payload = json.loads(row["payload_json"])
            if payload.get("batch_key") in reversed_batch_keys:
                continue
            if event_type == "opening_snapshot":
                if opening_seen:
                    raise LedgerIntegrityError("more than one opening snapshot is active")
                opening_seen = True
                cash = _decimal(payload["cash"], "opening cash", minimum=ZERO)
                for item in payload["positions"]:
                    symbol = _symbol(item["symbol"])
                    quantity = _decimal(item["quantity"], "opening quantity", minimum=ZERO)
                    average_cost = _decimal(
                        item["average_economic_cost"],
                        "opening average economic cost",
                        minimum=ZERO,
                    )
                    positions[symbol] = _MutablePosition(
                        quantity=quantity,
                        economic_cost=quantity * average_cost,
                    )
                continue
            if not opening_seen:
                raise LedgerIntegrityError("an economic event precedes the opening snapshot")
            if event_type in {
                "valuation",
                "modeled_dca_batch",
                "modeled_dca_batch_reversal",
                "dca_override",
                "reversal",
            }:
                continue
            applies = event_type in {
                "user_confirmed_fill",
                "cash_flow",
                "income",
                "fee",
                "split",
            }
            if normalized_book == "modeled" and event_type in {
                "modeled_dca_fill",
                "modeled_external_contribution",
            }:
                applies = True
            if not applies:
                continue
            if event_type == "modeled_dca_fill" and event_id in replacement_targets:
                continue
            if cash is None:
                raise LedgerIntegrityError("opening cash is unavailable")
            if event_type in {"user_confirmed_fill", "modeled_dca_fill"}:
                symbol = _symbol(payload["symbol"])
                side = str(payload["side"]).lower()
                quantity = _decimal(payload["quantity"], "fill quantity", positive=True)
                price = _decimal(payload["price"], "fill price", positive=True)
                fill_fee = _decimal(payload.get("fees", "0"), "fill fees", minimum=ZERO)
                state = positions.setdefault(symbol, _MutablePosition())
                gross = quantity * price
                if side == "buy":
                    cash -= gross + fill_fee
                    state.quantity += quantity
                    state.economic_cost += gross + fill_fee
                    if event_type == "modeled_dca_fill":
                        state.modeled_quantity += quantity
                elif side == "sell":
                    if state.quantity < quantity:
                        raise LedgerProjectionError(f"sell exceeds position for {symbol}")
                    before_quantity = state.quantity
                    removed_cost = (
                        ZERO
                        if before_quantity == ZERO
                        else state.economic_cost * quantity / before_quantity
                    )
                    modeled_reduction = (
                        ZERO
                        if before_quantity == ZERO
                        else state.modeled_quantity * quantity / before_quantity
                    )
                    cash += gross - fill_fee
                    state.quantity -= quantity
                    state.economic_cost -= removed_cost
                    state.modeled_quantity -= modeled_reduction
                    state.realized_pnl += gross - fill_fee - removed_cost
                    if state.quantity == ZERO:
                        state.economic_cost = ZERO
                        state.modeled_quantity = ZERO
                else:
                    raise LedgerIntegrityError("stored fill has an invalid side")
                fees += fill_fee
            elif event_type in {"cash_flow", "modeled_external_contribution"}:
                amount = _decimal(payload["amount"], "cash-flow amount")
                cash += amount
                net_external_flow += amount
            elif event_type == "income":
                amount = _decimal(payload["amount"], "income amount", positive=True)
                cash += amount
                symbol_value = payload.get("symbol")
                if symbol_value:
                    positions.setdefault(_symbol(symbol_value), _MutablePosition()).realized_pnl += amount
                else:
                    non_position_realized += amount
            elif event_type == "fee":
                amount = _decimal(payload["amount"], "fee amount", positive=True)
                cash -= amount
                fees += amount
                non_position_realized -= amount
            elif event_type == "split":
                symbol = _symbol(payload["symbol"])
                ratio = _decimal(payload["ratio"], "split ratio", positive=True)
                state = positions.get(symbol)
                if state is not None:
                    state.quantity *= ratio
                    state.modeled_quantity *= ratio
            if cash < ZERO:
                raise LedgerProjectionError(
                    f"event {event_id} makes {normalized_book} cash negative"
                )
        if not opening_seen or cash is None:
            raise LedgerNotInitializedError("no opening snapshot is available at this date")
        position_states: list[PositionState] = []
        for symbol, state in sorted(positions.items()):
            if state.quantity < ZERO or state.economic_cost < ZERO or state.modeled_quantity < ZERO:
                raise LedgerProjectionError(f"negative replayed state for {symbol}")
            average_cost = ZERO if state.quantity == ZERO else state.economic_cost / state.quantity
            position_states.append(
                PositionState(
                    symbol=symbol,
                    quantity=state.quantity,
                    average_economic_cost=average_cost,
                    economic_cost=state.economic_cost,
                    realized_pnl=state.realized_pnl,
                    modeled_quantity=state.modeled_quantity if normalized_book == "modeled" else ZERO,
                )
            )
        last_hash = str(audit_rows[-1]["event_hash"]) if audit_rows else _GENESIS_HASH
        realized = non_position_realized + sum(
            (item.realized_pnl for item in position_states), ZERO
        )
        return LedgerProjection(
            book_kind=normalized_book,
            as_of=as_of,
            currency=self.policy.currency,
            cash=cash,
            positions=tuple(position_states),
            realized_pnl=realized,
            fees=fees,
            net_external_flow=net_external_flow,
            event_count=len(audit_rows),
            last_event_hash=last_hash,
        )

    def _valuation_rows(
        self,
        connection: sqlite3.Connection,
        book_kind: str,
    ) -> list[sqlite3.Row]:
        rows = connection.execute(
            "SELECT * FROM ledger_events WHERE event_type = 'valuation' "
            "ORDER BY session_date, sequence_no"
        ).fetchall()
        return [
            row
            for row in rows
            if _json_object(row["payload_json"], "valuation").get("book_kind") == book_kind
        ]

    @_fixed_decimal_context
    def _validated_valuation_chains_connection(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, tuple[LedgerValuation, ...]]:
        """Bind every v2 prior field to the actual direct predecessor per book."""
        chains: dict[str, list[LedgerValuation]] = {"confirmed": [], "modeled": []}
        cumulative_flows: dict[str, Decimal] = {"confirmed": ZERO, "modeled": ZERO}
        rows = connection.execute(
            "SELECT * FROM ledger_events WHERE event_type = 'valuation' "
            "ORDER BY session_date, sequence_no"
        ).fetchall()
        for row in rows:
            valuation = self._valuation_from_row(row)
            payload = _json_object(row["payload_json"], "valuation")
            chain = chains[valuation.book_kind]
            previous = chain[-1] if chain else None
            cumulative_external_flow = _decimal(
                payload.get("cumulative_external_flow"),
                "valuation cumulative external flow",
            )
            previous_event_id = payload.get("previous_valuation_event_id")
            if previous is None:
                if previous_event_id is not None or valuation.prior_nav is not None:
                    raise LedgerIntegrityError(
                        "initial valuation is linked to a nonexistent predecessor"
                    )
                if cumulative_external_flow != valuation.net_external_flow:
                    raise LedgerIntegrityError(
                        "initial valuation cumulative flow is inconsistent"
                    )
            else:
                if valuation.session <= previous.session:
                    raise LedgerIntegrityError(
                        "valuation sessions must increase strictly within each book"
                    )
                if previous_event_id != previous.valuation_event_id:
                    raise LedgerIntegrityError(
                        "valuation does not identify its direct book predecessor"
                    )
                if (
                    valuation.prior_nav != previous.nav
                    or valuation.prior_cumulative_twr != previous.cumulative_twr
                ):
                    raise LedgerIntegrityError(
                        "valuation prior NAV or TWR differs from its predecessor"
                    )
                if (
                    cumulative_external_flow
                    != cumulative_flows[valuation.book_kind] + valuation.net_external_flow
                ):
                    raise LedgerIntegrityError(
                        "valuation cumulative external flow is inconsistent"
                    )
            cumulative_flows[valuation.book_kind] = cumulative_external_flow
            chain.append(valuation)
        return {book: tuple(values) for book, values in chains.items()}

    def _latest_common_valuation_session_connection(
        self,
        connection: sqlite3.Connection,
    ) -> dt.date | None:
        chains = self._validated_valuation_chains_connection(connection)
        sessions = {
            book: {item.session for item in values}
            for book, values in chains.items()
        }
        common = sessions["confirmed"] & sessions["modeled"]
        return max(common) if common else None

    @_fixed_decimal_context
    def _valuation_from_row(
        self,
        row: sqlite3.Row,
        *,
        idempotent_replay: bool = False,
    ) -> LedgerValuation:
        payload = _json_object(row["payload_json"], "valuation")
        if payload.get("contract_version") != _VALUATION_CONTRACT_VERSION:
            raise LedgerIntegrityError(
                "legacy valuation payload lacks v2 close lineage; rebuild the private ledger"
            )
        raw_prices = payload.get("prices")
        raw_lineage = payload.get("accepted_close_lineage")
        if not isinstance(raw_prices, Mapping) or not isinstance(raw_lineage, Mapping):
            raise LedgerIntegrityError("valuation prices or accepted-close lineage are malformed")
        prices: dict[str, Decimal] = {}
        for raw_symbol, value in raw_prices.items():
            symbol = _symbol(raw_symbol)
            if symbol in prices:
                raise LedgerIntegrityError("valuation contains duplicate normalized price symbols")
            prices[symbol] = _decimal(value, f"valuation price {symbol}", positive=True)
        lineage: dict[str, ValuationCloseLineage] = {}
        for raw_symbol, raw_value in raw_lineage.items():
            symbol = _symbol(raw_symbol)
            if symbol in lineage:
                raise LedgerIntegrityError("valuation contains duplicate normalized lineage symbols")
            if not isinstance(raw_value, Mapping):
                raise LedgerIntegrityError("valuation close-lineage item is malformed")
            try:
                lineage[symbol] = ValuationCloseLineage(
                    accepted_close_id=raw_value["accepted_close_id"],
                    selected_provider_id=raw_value["selected_provider_id"],
                )
            except KeyError as exc:
                raise LedgerIntegrityError(
                    "valuation close-lineage item is missing a required field"
                ) from exc
        if set(prices) != set(lineage):
            raise LedgerIntegrityError("valuation prices and accepted-close lineage differ")
        cash = _decimal(payload.get("cash"), "valuation cash", minimum=ZERO)
        securities_value = _decimal(
            payload.get("securities_value"),
            "securities value",
            minimum=ZERO,
        )
        nav = _decimal(payload.get("nav"), "valuation NAV", minimum=ZERO)
        if cash + securities_value != nav:
            raise LedgerIntegrityError("valuation NAV does not equal cash plus securities value")
        prior_nav = (
            None
            if payload.get("prior_nav") is None
            else _decimal(payload["prior_nav"], "prior valuation NAV", minimum=ZERO)
        )
        prior_cumulative_twr = (
            None
            if payload.get("prior_cumulative_twr") is None
            else _decimal(payload["prior_cumulative_twr"], "prior cumulative TWR")
        )
        daily_pnl = (
            None
            if payload.get("daily_pnl") is None
            else _decimal(payload["daily_pnl"], "daily P/L")
        )
        daily_return = (
            None
            if payload.get("daily_return") is None
            else _decimal(payload["daily_return"], "daily return")
        )
        cumulative_twr = (
            None
            if payload.get("cumulative_twr") is None
            else _decimal(payload["cumulative_twr"], "cumulative TWR")
        )
        net_external_flow = _decimal(
            payload.get("net_external_flow"),
            "net external flow",
        )
        weighted_external_flow = _decimal(
            payload.get("weighted_external_flow"),
            "weighted external flow",
        )
        previous_event_id = payload.get("previous_valuation_event_id")
        if prior_nav is None:
            if any(
                value is not None
                for value in (
                    prior_cumulative_twr,
                    daily_pnl,
                    daily_return,
                    cumulative_twr,
                    previous_event_id,
                )
            ):
                raise LedgerIntegrityError("initial valuation contains linked-period values")
        else:
            if daily_pnl is None or daily_return is None or cumulative_twr is None:
                raise LedgerIntegrityError("linked valuation is missing performance values")
            _sha256_digest(previous_event_id, "previous valuation event id")
            if daily_pnl != nav - prior_nav - net_external_flow:
                raise LedgerIntegrityError("valuation daily P/L is inconsistent")
            denominator = prior_nav + weighted_external_flow
            if denominator <= ZERO or daily_return != daily_pnl / denominator:
                raise LedgerIntegrityError("valuation daily return is inconsistent")
            expected_twr = (
                daily_return
                if prior_cumulative_twr is None
                else (ONE + prior_cumulative_twr) * (ONE + daily_return) - ONE
            )
            if cumulative_twr != expected_twr:
                raise LedgerIntegrityError("valuation cumulative TWR is inconsistent")
        currency = str(payload.get("currency", "")).strip().upper()
        if currency != self.policy.currency:
            raise LedgerIntegrityError("valuation currency differs from the immutable ledger policy")
        return LedgerValuation(
            valuation_event_id=_sha256_digest(row["event_id"], "valuation event id"),
            book_kind=_book_kind(payload.get("book_kind")),
            session=_date(row["session_date"]),
            accepted_close_batch_id=_sha256_digest(
                payload.get("accepted_close_batch_id"),
                "accepted close batch id",
            ),
            currency=currency,
            cash=cash,
            securities_value=securities_value,
            nav=nav,
            prices=prices,
            accepted_close_lineage=lineage,
            prior_nav=prior_nav,
            prior_cumulative_twr=prior_cumulative_twr,
            daily_pnl=daily_pnl,
            daily_return=daily_return,
            cumulative_twr=cumulative_twr,
            net_external_flow=net_external_flow,
            weighted_external_flow=weighted_external_flow,
            idempotent_replay=idempotent_replay,
        )

    @_fixed_decimal_context
    def _weighted_external_flow(
        self,
        connection: sqlite3.Connection,
        book_kind: str,
        after_session: dt.date | None,
        through_session: dt.date,
    ) -> Decimal:
        if after_session is None:
            return ZERO
        rows = connection.execute(
            "SELECT * FROM ledger_events WHERE session_date > ? AND session_date <= ? "
            "ORDER BY sequence_no",
            (after_session.isoformat(), through_session.isoformat()),
        ).fetchall()
        reversed_ids = {
            str(json.loads(row["payload_json"])["target_event_id"])
            for row in rows
            if row["event_type"] == "reversal"
        }
        reversed_batch_keys = {
            str(json.loads(row["payload_json"])["batch_key"])
            for row in rows
            if row["event_type"] == "modeled_dca_batch_reversal"
        }
        weighted = ZERO
        for row in rows:
            if row["event_id"] in reversed_ids:
                continue
            payload = json.loads(row["payload_json"])
            if payload.get("batch_key") in reversed_batch_keys:
                continue
            if row["event_type"] == "cash_flow" or (
                book_kind == "modeled" and row["event_type"] == "modeled_external_contribution"
            ):
                amount = _decimal(payload["amount"], "external flow")
                weight = _decimal(payload.get("valuation_weight", "0"), "valuation weight")
                if weight < ZERO or weight > ONE:
                    raise LedgerIntegrityError("stored valuation weight is outside [0, 1]")
                weighted += amount * weight
        return weighted


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LedgerIntegrityError(f"{field_name} payload is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise LedgerIntegrityError(f"{field_name} payload must be a JSON object")
    return parsed


def _sha256_digest(value: Any, field_name: str) -> str:
    digest = str(value).strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise LedgerValidationError(f"{field_name} must be a SHA-256 hex digest")
    return digest


def _symbol(value: Any) -> str:
    symbol = str(value).strip().upper()
    if not symbol:
        raise LedgerValidationError("symbol may not be empty")
    return symbol


def _date(value: dt.date | str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError as exc:
            raise LedgerValidationError("session must be ISO YYYY-MM-DD") from exc
    raise LedgerValidationError("session must be a date or ISO YYYY-MM-DD text")


def _book_kind(value: Any) -> str:
    normalized = str(value).strip().lower()
    if normalized not in _BOOK_KINDS:
        raise LedgerValidationError("book_kind must be 'confirmed' or 'modeled'")
    return normalized


def _aware_datetime(value: Any, field_name: str) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        raise LedgerValidationError(f"{field_name} must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise LedgerValidationError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(dt.timezone.utc)


def _occurred_at(value: dt.datetime | str | None) -> str:
    if value is None:
        moment = dt.datetime.now(dt.timezone.utc)
    elif isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise LedgerValidationError("occurred_at may not be empty")
        try:
            moment = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError as exc:
            raise LedgerValidationError("occurred_at must be an ISO datetime") from exc
    else:
        raise LedgerValidationError("occurred_at must be an ISO datetime")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise LedgerValidationError("occurred_at must include a timezone")
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stored_utc_datetime(value: Any, field_name: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerIntegrityError(f"{field_name} must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LedgerIntegrityError(f"{field_name} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _session_timestamp(session: dt.date) -> str:
    return f"{session.isoformat()}T00:00:00Z"


def _session_close_timestamp(session: dt.date) -> str:
    # The provider/calendar layer establishes finality.  This neutral audit
    # timestamp only records that settlement belongs to that completed session.
    return f"{session.isoformat()}T23:59:59Z"


def _now_rfc3339() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _idempotency_key(value: Any) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise LedgerValidationError("idempotency_key may not be empty")
    if len(normalized) > 512:
        raise LedgerValidationError("idempotency_key is too long")
    return normalized


def _optional_or_auto_key(
    supplied: str | None,
    event_type: str,
    occurred_at: str,
    payload: Mapping[str, Any],
) -> str:
    if supplied is not None:
        return _idempotency_key(supplied)
    fingerprint = _sha256_text(_canonical_json({"occurred_at": occurred_at, "payload": payload}))
    return f"{event_type}:{fingerprint}"


def _event_time(
    occurred_at: dt.datetime | str | None,
    session: dt.date,
    idempotency_key: str | None,
) -> str:
    """Keep explicit-key retries stable when the owner omits an intraday time."""
    if occurred_at is None and idempotency_key is not None:
        return _session_timestamp(session)
    return _occurred_at(occurred_at)


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise LedgerValidationError("event payload decimals must be finite")
        return _decimal_text(value)
    if isinstance(value, dt.datetime):
        return _occurred_at(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if text_key in normalized:
                raise LedgerValidationError("event payload has duplicate normalized keys")
            normalized[text_key] = _canonical_value(item)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, float):
        raise LedgerValidationError("binary floating point is forbidden in ledger payloads")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise LedgerValidationError(f"unsupported event payload type: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _chain_hash(previous_hash: str, hash_body: Mapping[str, Any]) -> str:
    return _sha256_text(previous_hash + "\n" + _canonical_json(hash_body))


def _decimal(
    value: Any,
    field_name: str,
    *,
    minimum: Decimal | None = None,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, (float, bool)):
        raise LedgerValidationError(f"{field_name} must not use binary floating point")
    if not isinstance(value, (Decimal, int, str)):
        raise LedgerValidationError(f"{field_name} must be Decimal-compatible text or integer")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise LedgerValidationError(f"{field_name} is not a valid decimal") from exc
    if not result.is_finite():
        raise LedgerValidationError(f"{field_name} must be finite")
    if positive and result <= ZERO:
        raise LedgerValidationError(f"{field_name} must be positive")
    if minimum is not None and result < minimum:
        raise LedgerValidationError(f"{field_name} must be at least {_decimal_text(minimum)}")
    return result


def _meaningful_decimal_places(value: Decimal) -> int:
    """Count fractional digits after insignificant trailing zeroes, context-free."""
    sign, digits_tuple, exponent = value.as_tuple()
    del sign
    digits = list(digits_tuple)
    while exponent < 0 and digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return max(0, -exponent)


def _decimal_text(value: Decimal) -> str:
    if value == ZERO:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


__all__ = [
    "CommonLedgerValuation",
    "DcaFillReceipt",
    "DcaPlan",
    "DcaSkipReceipt",
    "DcaSettlementResult",
    "LedgerAlreadyInitializedError",
    "LedgerIdempotencyConflict",
    "LedgerInsufficientCash",
    "LedgerIntegrityError",
    "LedgerNotInitializedError",
    "LedgerPolicy",
    "LedgerProjection",
    "LedgerProjectionError",
    "LedgerSettlementBlocked",
    "LedgerSessionAudit",
    "LedgerValidationError",
    "LedgerValuation",
    "OpeningCheckpoint",
    "OpeningPosition",
    "PortfolioLedger",
    "PortfolioLedgerError",
    "PositionState",
    "ValuationCloseLineage",
]
