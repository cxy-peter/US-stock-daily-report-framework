"""Private, manual-only daily accounting orchestration.

The runtime never connects to a broker or infers a trade from prose or silence.
It may consume only an immutable event envelope that the owner approved through
the separate random TTY challenge.  It projects an immutable base DCA plan at
accepted official closes, keeps confirmed and modeled books separate, and
prepares one owner-only report through the durable outbox.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Context, Decimal, localcontext
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .daily_outbox import DailyReportOutbox
from .manual_owner_event import (
    ManualEventApproval,
    ManualOwnerEventError,
    load_manual_event_queue,
    publish_manual_event_receipt,
    record_approved_event,
)
from .opening_owner_attestation import (
    OpeningLedgerBinding,
    OpeningOwnerAttestationError,
    audit_opening_owner_attestation,
    opening_ledger_idempotency_key,
    publish_opening_intent,
    publish_opening_receipt,
    recover_opening_control_publications,
    require_opening_ledger_pristine,
    require_opening_outbox_pristine,
    validate_opening_commit_time,
)
from .portfolio_ledger import (
    CommonLedgerValuation,
    DcaFillReceipt,
    DcaSettlementResult,
    LedgerAlreadyInitializedError,
    LedgerInsufficientCash,
    LedgerNotInitializedError,
    LedgerProjection,
    LedgerSettlementBlocked,
    LedgerValuation,
    OpeningPosition,
    PortfolioLedger,
)
from .private_daily_report import (
    compute_target_key_sha256,
    finalize_private_daily_report,
)
from .private_report_store import PrivateReportFiles, persist_private_daily_report
from .private_research_adapter import (
    PrivateResearchAdapterError,
    PrivateResearchInput,
    PrivateResearchProjection,
    build_private_research_projection,
    validate_private_research_projection,
)
from .private_runtime_config import PrivateDailyRuntimeConfig
from .private_runtime_paths import PrivateRuntimePaths
from .provider_registry import AcceptedCloseBatch, ProviderAttempt, ProviderRegistry
from .trading_calendar import ExchangeSessionError, ExchangeSessionResolver


Clock = Callable[[], dt.datetime]
_DECIMAL_CONTEXT = Context(prec=50)
_ALPHA_VANTAGE_FREE_DAILY_BUDGET = 25
_ZERO = Decimal("0")
_PREFLIGHT_BLOCK_REASONS = frozenset(
    {
        "provider_credentials_missing",
        "receiver_capability_unverified",
    }
)


class PrivateDailyRuntimeError(RuntimeError):
    """Sanitized runtime failure that is safe to map to a fixed CLI code."""

    def __init__(self, code: str) -> None:
        self.code = str(code).strip().lower() or "private_daily_runtime_failed"
        super().__init__(self.code)


class PrivateDailyNotInitialized(PrivateDailyRuntimeError):
    """The opening snapshot and both opening valuations are not complete."""


class PrivateDailyIntegrityError(PrivateDailyRuntimeError):
    """Persisted checkpoints or recovered accounting facts disagree."""


@dataclass(frozen=True, repr=False)
class PrivateLedgerInitializationResult:
    status: str
    session: dt.date
    confirmed_valuation_id: str
    modeled_valuation_id: str


@dataclass(frozen=True, repr=False)
class PrivateDailyRunResult:
    status: str
    report_status: str | None
    delivery_id: str | None
    report_id: str | None
    processed_sessions: tuple[dt.date, ...]
    blocked_session: dt.date | None
    report_files: PrivateReportFiles | None = field(default=None, repr=False)


@dataclass(frozen=True, repr=False)
class _SessionOutcome:
    session: dt.date
    status: str
    close_batch_id: str | None
    ledger_batch_id: str | None
    calendar_gate: str
    price_gate: str
    corporate_action_gate: str
    funding_gate: str
    confirmed_valuation_status: str
    modeled_valuation_status: str
    confirmed_valuation_id: str | None
    modeled_valuation_id: str | None
    reason_codes: tuple[str, ...]
    receipts: Mapping[str, DcaFillReceipt] = field(default_factory=dict, repr=False)

    def report_row(self, latest_session: dt.date) -> dict[str, Any]:
        return {
            "session_date": self.session.isoformat(),
            "status": self.status,
            "is_backfill": self.session < latest_session,
            "close_batch_id": self.close_batch_id,
            "ledger_batch_id": self.ledger_batch_id,
            "calendar_gate": self.calendar_gate,
            "price_gate": self.price_gate,
            "corporate_action_gate": self.corporate_action_gate,
            "funding_gate": self.funding_gate,
            "dca_status": self.status,
            "confirmed_valuation_status": self.confirmed_valuation_status,
            "modeled_valuation_status": self.modeled_valuation_status,
            "confirmed_valuation_id": self.confirmed_valuation_id,
            "modeled_valuation_id": self.modeled_valuation_id,
            "reason_codes": list(self.reason_codes),
        }


def _aware_utc(value: dt.datetime, field_name: str) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PrivateDailyRuntimeError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(dt.timezone.utc)


def _utc_text(value: dt.datetime) -> str:
    return _aware_utc(value, "timestamp").isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc_text(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrivateDailyIntegrityError("source_timestamp_invalid") from exc
    return _aware_utc(parsed, "source_timestamp")


def _verify_opening_matches_config(
    config: PrivateDailyRuntimeConfig,
    ledger: PortfolioLedger,
) -> None:
    checkpoint = ledger.opening_checkpoint()
    expected_positions = tuple(sorted(config.opening.positions, key=lambda item: item.symbol))
    if (
        checkpoint.session != config.opening.session
        or checkpoint.currency != config.ledger_policy.currency
        or checkpoint.cash != config.opening.cash
        or checkpoint.positions != expected_positions
    ):
        raise PrivateDailyIntegrityError("opening_checkpoint_configuration_mismatch")


def _opening_binding(ledger: PortfolioLedger) -> OpeningLedgerBinding:
    checkpoint = ledger.opening_checkpoint()
    return OpeningLedgerBinding(
        opening_event_id=checkpoint.opening_event_id,
        opening_event_hash=checkpoint.opening_event_hash,
        idempotency_key=checkpoint.idempotency_key,
        created_at=checkpoint.created_at,
    )


def _instrument_universe(
    config: PrivateDailyRuntimeConfig,
    ledger: PortfolioLedger,
    session: dt.date,
) -> tuple[Any, ...] | None:
    symbols = set(config.dca_plan.base_amounts)
    for book_kind in ("confirmed", "modeled"):
        projection = ledger.project(book_kind, session)
        symbols.update(item.symbol for item in projection.positions if item.quantity != _ZERO)
    by_symbol = config.by_symbol
    if any(symbol not in by_symbol for symbol in symbols):
        return None
    return tuple(by_symbol[symbol] for symbol in sorted(symbols))


def _calendar_provenance(
    config: PrivateDailyRuntimeConfig,
    calendar: ExchangeSessionResolver,
) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    for mic in sorted({item.exchange_mic for item in config.instruments}):
        proof = calendar.provenance(mic)
        result.append(
            {
                "instrument_mic": proof.instrument_mic,
                "calendar_name": proof.calendar_name,
                "calendar_version": proof.calendar_version,
                "exchange_timezone": proof.exchange_timezone,
            }
        )
    return tuple(result)


def _batch_source_health(
    batch: AcceptedCloseBatch,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for close in batch.closes:
        symbol = close.instrument.canonical_symbol
        for attempt in close.attempts:
            rows.append(_attempt_source_health(batch.expected_session, symbol, attempt))
    rows.append(
        {
            "source_id": f"close_batch.{batch.expected_session.isoformat()}",
            "source_type": "accepted_close",
            "status": "accepted" if batch.price_gate_permitted else "blocked",
            "required": True,
            "observed_at": None,
            "detail_code": (
                "atomic_close_batch_accepted"
                if batch.price_gate_permitted
                else "atomic_close_batch_blocked"
            ),
        }
    )
    return rows


def _attempt_source_health(
    session: dt.date,
    symbol: str,
    attempt: ProviderAttempt,
) -> dict[str, Any]:
    if attempt.status == "success":
        status = "healthy"
    elif attempt.status == "rejected":
        status = "blocked"
    elif attempt.status in {"rate_limited", "transient_error"}:
        status = "degraded"
    elif attempt.status == "missing_credentials":
        status = "blocked"
    else:
        status = "error"
    return {
        "source_id": (
            f"close.{session.isoformat()}.{symbol}.{attempt.provider_id}"
        ),
        "source_type": "accepted_close",
        "status": status,
        "required": True,
        "observed_at": attempt.observed_at,
        "detail_code": attempt.status,
    }


def _blocked_outcome(
    session: dt.date,
    *,
    reason: str,
    calendar_gate: str,
    price_gate: str,
    corporate_action_gate: str,
    funding_gate: str,
    close_batch_id: str | None = None,
) -> _SessionOutcome:
    return _SessionOutcome(
        session=session,
        status="blocked",
        close_batch_id=close_batch_id,
        ledger_batch_id=None,
        calendar_gate=calendar_gate,
        price_gate=price_gate,
        corporate_action_gate=corporate_action_gate,
        funding_gate=funding_gate,
        confirmed_valuation_status="unavailable",
        modeled_valuation_status="unavailable",
        confirmed_valuation_id=None,
        modeled_valuation_id=None,
        reason_codes=(reason,),
    )


def _not_attempted_outcome(session: dt.date) -> _SessionOutcome:
    return _SessionOutcome(
        session=session,
        status="not_attempted_prior_session_blocked",
        close_batch_id=None,
        ledger_batch_id=None,
        calendar_gate="not_attempted",
        price_gate="not_attempted",
        corporate_action_gate="not_attempted",
        funding_gate="not_attempted",
        confirmed_valuation_status="not_attempted",
        modeled_valuation_status="not_attempted",
        confirmed_valuation_id=None,
        modeled_valuation_id=None,
        reason_codes=("prior_session_blocked",),
    )


def _recovered_outcome(
    session: dt.date,
    audit: Any,
    config: PrivateDailyRuntimeConfig,
) -> _SessionOutcome:
    confirmed = audit.confirmed_valuation
    modeled = audit.modeled_valuation
    if confirmed is None or modeled is None:
        raise PrivateDailyIntegrityError("complete_session_audit_missing_valuation")
    if confirmed.accepted_close_batch_id != modeled.accepted_close_batch_id:
        raise PrivateDailyIntegrityError("common_valuation_batch_identity_mismatch")
    settlement = audit.dca_settlement
    receipts: Mapping[str, DcaFillReceipt] = {}
    if settlement is None:
        owner_skip = audit.owner_skip
        if owner_skip is None:
            raise PrivateDailyIntegrityError("recovered_dca_evidence_missing")
        if (
            owner_skip.plan_id != config.dca_plan.plan_id
            or owner_skip.plan_version != config.dca_plan.version
        ):
            raise PrivateDailyIntegrityError("recovered_dca_plan_identity_mismatch")
        status = "skipped_by_owner"
        batch_event_id = None
    else:
        if (
            settlement.plan_id != config.dca_plan.plan_id
            or settlement.plan_version != config.dca_plan.version
        ):
            raise PrivateDailyIntegrityError("recovered_dca_plan_identity_mismatch")
        receipts = settlement.receipts_by_symbol
        configured_amounts = config.dca_plan.base_amounts
        if set(receipts) != set(configured_amounts):
            raise PrivateDailyIntegrityError("recovered_dca_receipt_universe_mismatch")
        with localcontext(_DECIMAL_CONTEXT):
            if any(
                receipts[symbol].spend + receipts[symbol].residual != amount
                for symbol, amount in configured_amounts.items()
            ):
                raise PrivateDailyIntegrityError("recovered_dca_receipt_amount_mismatch")
            if settlement.total_configured_amount != sum(
                configured_amounts.values(),
                _ZERO,
            ):
                raise PrivateDailyIntegrityError("recovered_dca_total_mismatch")
        status = "already_settled"
        batch_event_id = settlement.batch_event_id
    return _SessionOutcome(
        session=session,
        status=status,
        close_batch_id=confirmed.accepted_close_batch_id,
        ledger_batch_id=batch_event_id,
        calendar_gate="passed",
        price_gate="passed",
        corporate_action_gate="passed",
        funding_gate="passed",
        confirmed_valuation_status="fresh",
        modeled_valuation_status="fresh",
        confirmed_valuation_id=confirmed.valuation_event_id,
        modeled_valuation_id=modeled.valuation_event_id,
        reason_codes=(),
        receipts=receipts,
    )


def _settled_outcome(
    session: dt.date,
    settlement: DcaSettlementResult,
    confirmed: LedgerValuation,
    modeled: LedgerValuation,
    valuation_batch: AcceptedCloseBatch,
) -> _SessionOutcome:
    if settlement.skipped:
        status = "skipped_by_owner"
    elif settlement.idempotent_replay:
        status = "already_settled"
    else:
        status = "settled"
    receipts = settlement.receipts_by_symbol if not settlement.skipped else {}
    return _SessionOutcome(
        session=session,
        status=status,
        close_batch_id=valuation_batch.batch_id,
        ledger_batch_id=settlement.batch_event_id,
        calendar_gate="passed",
        price_gate="passed",
        corporate_action_gate="passed",
        funding_gate="passed",
        confirmed_valuation_status="fresh",
        modeled_valuation_status="fresh",
        confirmed_valuation_id=confirmed.valuation_event_id,
        modeled_valuation_id=modeled.valuation_event_id,
        reason_codes=(),
        receipts=receipts,
    )


def _unavailable_book(projection: LedgerProjection) -> dict[str, Any]:
    positions = []
    for item in projection.positions:
        if item.quantity == _ZERO:
            continue
        positions.append(
            {
                "symbol": item.symbol,
                "quantity": item.quantity,
                "modeled_quantity": item.modeled_quantity,
                "accepted_close": None,
                "accepted_close_id": None,
                "selected_provider_id": None,
                "price_session": None,
                "market_value": None,
                "economic_cost": item.economic_cost,
                "average_economic_cost": item.average_economic_cost,
                "unrealized_pnl": None,
                "portfolio_weight": None,
            }
        )
    return {
        "valuation_status": "unavailable",
        "cash": projection.cash,
        "nav": None,
        "market_value": None,
        "total_economic_cost": projection.total_economic_cost,
        "realized_pnl": projection.realized_pnl,
        "fees": projection.fees,
        "performance": {
            "valuation_session": None,
            "prior_nav": None,
            "prior_cumulative_twr": None,
            "net_external_flow": _ZERO,
            "weighted_external_flow": _ZERO,
            "daily_pnl": None,
            "daily_return": None,
            "cumulative_twr": None,
        },
        "positions": positions,
    }


def _valued_book(
    projection: LedgerProjection,
    valuation: LedgerValuation,
    *,
    fresh: bool,
) -> dict[str, Any]:
    positions: list[dict[str, Any]] = []
    with localcontext(_DECIMAL_CONTEXT):
        for item in projection.positions:
            if item.quantity == _ZERO:
                continue
            price = valuation.prices.get(item.symbol)
            lineage = valuation.accepted_close_lineage.get(item.symbol)
            if price is None or lineage is None:
                raise PrivateDailyIntegrityError("valuation_position_lineage_missing")
            market_value = item.quantity * price
            weight = (
                _ZERO
                if valuation.securities_value == _ZERO
                else market_value / valuation.securities_value
            )
            positions.append(
                {
                    "symbol": item.symbol,
                    "quantity": item.quantity,
                    "modeled_quantity": item.modeled_quantity,
                    "accepted_close": price,
                    "accepted_close_id": lineage.accepted_close_id,
                    "selected_provider_id": lineage.selected_provider_id,
                    "price_session": valuation.session.isoformat(),
                    "market_value": market_value,
                    "economic_cost": item.economic_cost,
                    "average_economic_cost": item.average_economic_cost,
                    "unrealized_pnl": market_value - item.economic_cost,
                    "portfolio_weight": weight,
                }
            )
    return {
        "valuation_status": "fresh" if fresh else "carried_forward_display_only",
        "cash": valuation.cash,
        "nav": valuation.nav,
        "market_value": valuation.securities_value,
        "total_economic_cost": projection.total_economic_cost,
        "realized_pnl": projection.realized_pnl,
        "fees": projection.fees,
        "performance": {
            "valuation_session": valuation.session.isoformat(),
            "prior_nav": valuation.prior_nav,
            "prior_cumulative_twr": valuation.prior_cumulative_twr,
            "net_external_flow": valuation.net_external_flow,
            "weighted_external_flow": valuation.weighted_external_flow,
            "daily_pnl": valuation.daily_pnl if fresh else None,
            "daily_return": valuation.daily_return if fresh else None,
            "cumulative_twr": valuation.cumulative_twr,
        },
        "positions": positions,
    }


def _dca_report(
    config: PrivateDailyRuntimeConfig,
    outcomes: Sequence[_SessionOutcome],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for symbol, configured_amount in config.dca_plan.base_amounts.items():
        sessions: list[dict[str, Any]] = []
        for outcome in outcomes:
            receipt = outcome.receipts.get(symbol)
            if outcome.status in {"settled", "already_settled"}:
                if receipt is None:
                    raise PrivateDailyIntegrityError("accounted_dca_receipt_missing")
                session_row = {
                    "session_date": outcome.session.isoformat(),
                    "status": outcome.status,
                    "amount": configured_amount,
                    "spend": receipt.spend,
                    "residual": receipt.residual,
                    "quantity": receipt.quantity,
                    "accepted_close": receipt.price,
                    "accepted_close_id": receipt.accepted_close_id,
                    "settlement_event_id": receipt.settlement_event_id,
                }
            else:
                session_row = {
                    "session_date": outcome.session.isoformat(),
                    "status": outcome.status,
                    "amount": _ZERO,
                    "spend": _ZERO,
                    "residual": _ZERO,
                    "quantity": _ZERO,
                    "accepted_close": None,
                    "accepted_close_id": None,
                    "settlement_event_id": None,
                }
            sessions.append(session_row)
        items.append(
            {
                "symbol": symbol,
                "configured": {"amount": configured_amount},
                "proposed": {
                    "amount": configured_amount,
                    "action": "maintain",
                    "rationale_codes": ["fixed_plan_accounting_only"],
                    "automatic_execution": False,
                },
                "modeled": {"execution_claim": False, "sessions": sessions},
                "broker_confirmed": {
                    "availability": "unavailable",
                    "status": "not_connected",
                    "amount": None,
                    "quantity": None,
                    "price": None,
                    "trade_id": None,
                },
            }
        )
    return {
        "plan_id": config.dca_plan.plan_id,
        "version": config.dca_plan.version,
        "currency": config.dca_plan.currency,
        "funding_mode": config.dca_plan.funding_mode,
        "items": items,
    }


def _base_actions(report_status: str, reason_codes: Sequence[str]) -> list[dict[str, Any]]:
    if report_status == "blocked":
        return [
            {
                "action_id": "data.block_new_risk",
                "scope": "data",
                "symbol": None,
                "action": "BLOCK_NEW_RISK",
                "priority": "high",
                "status": "blocked",
                "owner_confirmation_required": False,
                "automatic_execution": False,
                "rationale_codes": sorted(set(reason_codes)),
            }
        ]
    rationale = (
        ["no_new_official_close"]
        if report_status == "no_new_close"
        else ["no_new_owner_confirmed_event"]
    )
    return [
        {
            "action_id": "portfolio.hold",
            "scope": "portfolio",
            "symbol": None,
            "action": "HOLD",
            "priority": "normal",
            "status": "informational",
            "owner_confirmation_required": False,
            "automatic_execution": False,
            "rationale_codes": rationale,
        }
    ]


def initialize_private_ledger(
    config: PrivateDailyRuntimeConfig,
    *,
    runtime_paths: PrivateRuntimePaths,
    config_bytes_sha256: str,
    ledger: PortfolioLedger | None,
    close_registry: ProviderRegistry,
    calendar: ExchangeSessionResolver,
    clock: Clock,
    ledger_factory: Callable[[], PortfolioLedger] | None = None,
) -> PrivateLedgerInitializationResult:
    """Consume the owner claim, bind it to the opening event, then value it."""

    observed_at = _aware_utc(clock(), "clock")
    try:
        recover_opening_control_publications(runtime_paths)
    except OpeningOwnerAttestationError as exc:
        raise PrivateDailyIntegrityError(
            "opening_attestation_publication_recovery_failed"
        ) from exc
    existing = True
    binding: OpeningLedgerBinding | None = None
    if ledger is None:
        existing = False
    else:
        try:
            _verify_opening_matches_config(config, ledger)
            binding = _opening_binding(ledger)
        except LedgerNotInitializedError:
            existing = False
        except LedgerAlreadyInitializedError as exc:  # pragma: no cover - defensive alias
            raise PrivateDailyIntegrityError("opening_checkpoint_invalid") from exc

    try:
        attestation = audit_opening_owner_attestation(
            config,
            runtime_paths,
            config_bytes_sha256=config_bytes_sha256,
            now=observed_at,
            ledger_binding=binding,
        )
    except OpeningOwnerAttestationError as exc:
        raise PrivateDailyIntegrityError("opening_owner_attestation_invalid") from exc

    if existing:
        if ledger is None:  # pragma: no cover - state invariant
            raise PrivateDailyIntegrityError("opening_ledger_missing")
        if attestation.state == "recovery_available":
            if attestation.claim is None or attestation.intent is None or binding is None:
                raise PrivateDailyIntegrityError("opening_attestation_recovery_invalid")
            try:
                publish_opening_receipt(
                    attestation.claim,
                    attestation.intent,
                    binding,
                    runtime_paths,
                    clock=clock,
                )
            except OpeningOwnerAttestationError as exc:
                raise PrivateDailyIntegrityError(
                    "opening_attestation_receipt_recovery_failed"
                ) from exc
        elif attestation.state != "consumed_verified":
            raise PrivateDailyIntegrityError("opening_owner_attestation_not_consumed")
    elif attestation.state not in {"pending_verified", "resume_available"} or attestation.claim is None:
        raise PrivateDailyIntegrityError("opening_owner_attestation_not_pending")

    if not existing:
        try:
            require_opening_outbox_pristine(runtime_paths)
        except OpeningOwnerAttestationError as exc:
            raise PrivateDailyIntegrityError(
                "opening_initialization_outbox_not_pristine"
            ) from exc

    if existing:
        confirmed_existing = ledger.valuation_at("confirmed", config.opening.session)
        modeled_existing = ledger.valuation_at("modeled", config.opening.session)
        if confirmed_existing is not None and modeled_existing is not None:
            return PrivateLedgerInitializationResult(
                status="existing",
                session=config.opening.session,
                confirmed_valuation_id=confirmed_existing.valuation_event_id,
                modeled_valuation_id=modeled_existing.valuation_event_id,
            )

    symbols = {
        item.symbol for item in config.opening.positions if item.quantity != _ZERO
    } | set(config.dca_plan.base_amounts)
    by_symbol = config.by_symbol
    if any(symbol not in by_symbol for symbol in symbols):
        raise PrivateDailyIntegrityError("opening_instrument_identity_missing")
    instruments = tuple(by_symbol[symbol] for symbol in sorted(symbols))
    try:
        for mic in sorted({item.exchange_mic for item in instruments}):
            if calendar.session_close(config.opening.session, mic) > observed_at:
                raise PrivateDailyRuntimeError("opening_session_not_completed")
    except ExchangeSessionError as exc:
        raise PrivateDailyRuntimeError("opening_calendar_gate_blocked") from exc

    batch = close_registry.resolve_batch(instruments, config.opening.session)
    if not batch.price_gate_permitted:
        raise PrivateDailyRuntimeError("opening_price_gate_blocked")
    action_statuses = config.corporate_action_statuses(
        config.opening.session,
        as_of=observed_at,
        symbols=tuple(sorted(symbols)),
    )
    if action_statuses is None:
        raise PrivateDailyRuntimeError("opening_corporate_action_gate_blocked")

    if not existing:
        try:
            if ledger is None:
                if ledger_factory is None:
                    raise PrivateDailyIntegrityError("opening_ledger_factory_missing")
                ledger = ledger_factory()
            require_opening_ledger_pristine(runtime_paths)
            require_opening_outbox_pristine(runtime_paths)
            try:
                ledger.opening_checkpoint()
            except LedgerNotInitializedError:
                pass
            else:
                raise PrivateDailyIntegrityError(
                    "opening_ledger_changed_during_initialization"
                )

            # Re-check the claim after schema creation and before durable intent.
            current = audit_opening_owner_attestation(
                config,
                runtime_paths,
                config_bytes_sha256=config_bytes_sha256,
                now=_aware_utc(clock(), "clock"),
                ledger_binding=None,
            )
            if current.state not in {"pending_verified", "resume_available"} or current.claim is None:
                raise PrivateDailyIntegrityError("opening_owner_attestation_not_pending")
            if current.state == "resume_available":
                if current.intent is None:
                    raise PrivateDailyIntegrityError(
                        "opening_attestation_resume_intent_missing"
                    )
                intent = current.intent
                recorded_at = validate_opening_commit_time(
                    current.claim,
                    intent,
                    _aware_utc(clock(), "clock"),
                )
            else:
                intent = publish_opening_intent(
                    current.claim,
                    runtime_paths,
                    clock=clock,
                )
                recorded_at = validate_opening_commit_time(
                    current.claim,
                    intent,
                    _aware_utc(clock(), "clock"),
                )
            ledger.initialize(
                config.opening.session,
                config.opening.cash,
                config.opening.positions,
                idempotency_key=opening_ledger_idempotency_key(
                    current.claim,
                    intent,
                ),
                recorded_at=recorded_at,
            )
            _verify_opening_matches_config(config, ledger)
            binding = _opening_binding(ledger)
            publish_opening_receipt(
                current.claim,
                intent,
                binding,
                runtime_paths,
                clock=clock,
            )
        except OpeningOwnerAttestationError as exc:
            raise PrivateDailyIntegrityError(
                "opening_owner_attestation_commit_failed"
            ) from exc
    if ledger is None:  # pragma: no cover - state invariant
        raise PrivateDailyIntegrityError("opening_ledger_missing")
    _verify_opening_matches_config(config, ledger)
    confirmed = ledger.record_valuation("confirmed", batch)
    modeled = ledger.record_valuation("modeled", batch)
    return PrivateLedgerInitializationResult(
        status="existing" if existing else "initialized",
        session=config.opening.session,
        confirmed_valuation_id=confirmed.valuation_event_id,
        modeled_valuation_id=modeled.valuation_event_id,
    )


def _manual_approvals_by_session(
    approvals: Sequence[ManualEventApproval],
) -> dict[dt.date, tuple[ManualEventApproval, ...]]:
    grouped: dict[dt.date, list[ManualEventApproval]] = {}
    replacement_keys: set[tuple[dt.date, str]] = set()
    for approval in approvals:
        grouped.setdefault(approval.session, []).append(approval)
        if approval.phase == "post_dca_replacement":
            key = (approval.session, str(approval.request.payload["symbol"]))
            if key in replacement_keys:
                raise PrivateDailyIntegrityError(
                    "duplicate_manual_dca_replacement"
                )
            replacement_keys.add(key)
    return {
        session: tuple(
            sorted(
                items,
                key=lambda item: (
                    1 if item.phase == "post_dca_replacement" else 0,
                    item.request.occurred_at
                    or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
                    item.event_nonce,
                ),
            )
        )
        for session, items in grouped.items()
    }


def _record_manual_approval(
    approval: ManualEventApproval,
    *,
    ledger: PortfolioLedger,
    config: PrivateDailyRuntimeConfig,
    runtime_paths: PrivateRuntimePaths,
    clock: Clock,
    modeled_replacement_event_id: str | None = None,
) -> None:
    try:
        receipt = record_approved_event(
            approval,
            ledger,
            config,
            modeled_replacement_event_id=modeled_replacement_event_id,
            clock=clock,
        )
        publish_manual_event_receipt(runtime_paths, receipt)
    except ManualOwnerEventError as exc:
        raise PrivateDailyIntegrityError(
            "manual_owner_event_recording_failed"
        ) from exc


class PrivateDailyRuntime:
    """Prepare exactly one private report without executing any trade."""

    def __init__(
        self,
        config: PrivateDailyRuntimeConfig,
        *,
        calendar: ExchangeSessionResolver,
        close_registry: ProviderRegistry,
        ledger: PortfolioLedger,
        outbox: DailyReportOutbox,
        report_directory: str | Path,
        clock: Clock,
        runtime_paths: PrivateRuntimePaths | None = None,
        config_bytes_sha256: str | None = None,
    ) -> None:
        self.config = config
        self.calendar = calendar
        self.close_registry = close_registry
        self.ledger = ledger
        self.outbox = outbox
        self.report_directory = Path(report_directory)
        self.clock = clock
        self.runtime_paths = runtime_paths
        self.config_bytes_sha256 = config_bytes_sha256

    def prepare(
        self,
        target_key: str,
        *,
        preflight_block_reason: str | None = None,
        research_input: PrivateResearchInput | None = None,
        research_projection: PrivateResearchProjection | None = None,
    ) -> PrivateDailyRunResult:
        if (
            preflight_block_reason is not None
            and preflight_block_reason not in _PREFLIGHT_BLOCK_REASONS
        ):
            raise PrivateDailyRuntimeError("unsupported_preflight_block_reason")
        started_at = _aware_utc(self.clock(), "clock")
        report_zone = ZoneInfo(self.config.report_timezone)
        delivery_date = started_at.astimezone(report_zone).date()

        try:
            _verify_opening_matches_config(self.config, self.ledger)
            if not self.config.simulation:
                if self.runtime_paths is None or self.config_bytes_sha256 is None:
                    raise PrivateDailyIntegrityError(
                        "opening_attestation_runtime_boundary_missing"
                    )
                opening_audit = audit_opening_owner_attestation(
                    self.config,
                    self.runtime_paths,
                    config_bytes_sha256=self.config_bytes_sha256,
                    now=started_at,
                    ledger_binding=_opening_binding(self.ledger),
                )
                if opening_audit.state != "consumed_verified":
                    raise PrivateDailyIntegrityError(
                        "opening_owner_attestation_not_consumed"
                    )
            common_before = self.ledger.latest_common_valuation()
        except OpeningOwnerAttestationError as exc:
            raise PrivateDailyIntegrityError(
                "opening_owner_attestation_invalid"
            ) from exc
        except LedgerNotInitializedError as exc:
            raise PrivateDailyNotInitialized("private_ledger_not_initialized") from exc
        if common_before is None:
            raise PrivateDailyNotInitialized("opening_valuations_not_initialized")

        same_day = self.outbox.find_slot(
            target_key,
            self.config.delivery_channel,
            delivery_date,
        )
        if same_day is not None:
            if (
                same_day.ledger_last_event_hash is None
                or not self.ledger.contains_event_hash(
                    same_day.ledger_last_event_hash
                )
                or same_day.ledger_last_event_hash
                != self.ledger.last_event_hash()
            ):
                raise PrivateDailyIntegrityError(
                    "same_day_report_not_in_current_ledger_chain"
                )
            content = self.outbox.load_validated_content(same_day.delivery_id)
            files = persist_private_daily_report(
                content.report,
                self.report_directory,
                runtime_paths=self.runtime_paths,
            )
            return PrivateDailyRunResult(
                status="existing",
                report_status=str(content.report["report_status"]),
                delivery_id=content.delivery_id,
                report_id=content.report_id,
                processed_sessions=(),
                blocked_session=None,
                report_files=files,
            )

        pending = self.outbox.oldest_pending(
            target_key,
            self.config.delivery_channel,
            before_delivery_date=delivery_date,
        )
        if pending is not None:
            if (
                pending.ledger_last_event_hash is None
                or not self.ledger.contains_event_hash(
                    pending.ledger_last_event_hash
                )
                or pending.ledger_last_event_hash
                != self.ledger.last_event_hash()
            ):
                raise PrivateDailyIntegrityError(
                    "pending_report_not_in_current_ledger_chain"
                )
            return PrivateDailyRunResult(
                status="pending_prior_delivery",
                report_status=None,
                delivery_id=pending.delivery_id,
                report_id=pending.report_id,
                processed_sessions=(),
                blocked_session=None,
            )

        if research_input is not None and research_projection is not None:
            raise PrivateDailyRuntimeError("multiple_research_snapshots_forbidden")
        if research_input is None and research_projection is None:
            research_document: Mapping[str, Any] | None = None
            research_source_health: tuple[Mapping[str, Any], ...] = ()
        else:
            try:
                candidate_projection = (
                    build_private_research_projection(
                        research_input,
                        prepared_at=started_at,
                    )
                    if research_input is not None
                    else research_projection
                )
                validated_projection = validate_private_research_projection(
                    candidate_projection,
                    prepared_at=started_at,
                )
            except PrivateResearchAdapterError as exc:
                raise PrivateDailyRuntimeError("research_snapshot_invalid") from exc
            research_document = validated_projection.research
            research_source_health = validated_projection.source_health

        delivered = self.outbox.latest_delivered_checkpoint(
            target_key,
            self.config.delivery_channel,
        )
        if delivered is None:
            last_reported = self.config.opening.session
        else:
            if delivered.ledger_last_event_hash is None:
                raise PrivateDailyIntegrityError("delivered_checkpoint_missing_ledger_hash")
            if not self.ledger.contains_event_hash(delivered.ledger_last_event_hash):
                raise PrivateDailyIntegrityError("delivered_checkpoint_not_in_ledger_chain")
            last_reported = (
                self.config.opening.session
                if delivered.portfolio_as_of_session is None
                else delivered.portfolio_as_of_session
            )
        if last_reported < self.config.opening.session:
            raise PrivateDailyIntegrityError("delivered_checkpoint_precedes_opening")

        # Validate the entire outbox, not only the current receiver scope,
        # before any owner event, modeled DCA or valuation can append to the
        # ledger.  This prevents a target rotation from hiding stale prepared
        # or ambiguous content behind a different target digest.
        self.outbox.require_ledger_mutation_allowed(
            self.ledger.contains_event_hash,
        )

        manual_approvals: tuple[ManualEventApproval, ...] = ()
        if not self.config.simulation:
            if self.runtime_paths is None:  # pragma: no cover - opening guard
                raise PrivateDailyIntegrityError(
                    "manual_owner_event_runtime_boundary_missing"
                )
            try:
                manual_approvals = load_manual_event_queue(
                    self.config,
                    self.runtime_paths,
                    self.ledger.event_checkpoint,
                    self.ledger.latest_valuation_watermark(),
                )
                for approval in manual_approvals:
                    self.calendar.session_close(
                        approval.session,
                        self.config.primary_mic,
                    )
                    if approval.approved_at > started_at + dt.timedelta(seconds=30):
                        raise ManualOwnerEventError(
                            "manual_event_approval_time_in_future"
                        )
                    if (
                        approval.request.occurred_at is not None
                        and approval.request.occurred_at
                        > started_at + dt.timedelta(seconds=30)
                    ):
                        raise ManualOwnerEventError(
                            "manual_event_time_in_future"
                        )
            except (ManualOwnerEventError, ExchangeSessionError) as exc:
                raise PrivateDailyIntegrityError(
                    "manual_owner_event_queue_invalid"
                ) from exc
        manual_by_session = _manual_approvals_by_session(manual_approvals)

        latest_session = self.calendar.last_completed_session(
            started_at,
            self.config.primary_mic,
        )
        sessions = self.calendar.unsettled_sessions(
            last_reported,
            started_at,
            self.config.primary_mic,
        )
        provenance = _calendar_provenance(self.config, self.calendar)
        primary = next(
            (item for item in provenance if item["instrument_mic"] == self.config.primary_mic),
            None,
        )
        if primary is None:
            raise PrivateDailyIntegrityError("primary_calendar_provenance_missing")

        source_health: list[dict[str, Any]] = [
            {
                "source_id": f"calendar.{item['instrument_mic'].lower()}",
                "source_type": "calendar",
                "status": "healthy",
                "required": True,
                "observed_at": _utc_text(started_at),
                "detail_code": "official_exchange_calendar",
            }
            for item in provenance
        ]
        source_health.extend(dict(item) for item in research_source_health)

        audits = {session: self.ledger.session_audit(session) for session in sessions}
        unresolved = [session for session in sessions if audits[session].valuation_state != "complete"]
        block_reason = preflight_block_reason
        if block_reason is None and len(unresolved) > self.config.max_backfill_sessions:
            block_reason = "backfill_limit_exceeded"
        if block_reason is None and not self.config.simulation:
            estimated_alpha_calls = 0
            for session in unresolved:
                universe = _instrument_universe(self.config, self.ledger, session)
                if universe is None:
                    block_reason = "instrument_identity_missing"
                    break
                prospective_symbols = {
                    item.canonical_symbol for item in universe
                }
                prospective_symbols.update(
                    str(approval.request.payload["symbol"])
                    for approval in manual_by_session.get(session, ())
                    if approval.event_kind in {
                        "confirmed_fill",
                        "income",
                        "split",
                    }
                    and approval.request.payload.get("symbol") is not None
                )
                estimated_alpha_calls += len(prospective_symbols)
            if (
                block_reason is None
                and estimated_alpha_calls > _ALPHA_VANTAGE_FREE_DAILY_BUDGET
            ):
                block_reason = "live_provider_call_budget_exceeded"

        outcomes: list[_SessionOutcome] = []
        prior_blocked = False
        for session in sessions:
            if prior_blocked:
                outcomes.append(_not_attempted_outcome(session))
                continue
            audit = audits[session]
            if audit.valuation_state == "complete":
                outcomes.append(_recovered_outcome(session, audit, self.config))
                source_health.append(
                    {
                        "source_id": f"ledger.recovery.{session.isoformat()}",
                        "source_type": "ledger",
                        "status": "healthy",
                        "required": True,
                        "observed_at": _utc_text(started_at),
                        "detail_code": "hash_verified_session_recovery",
                    }
                )
                continue

            session_manual = manual_by_session.get(session, ())
            pre_dca_manual = tuple(
                item for item in session_manual if item.phase == "pre_dca"
            )
            post_dca_manual = tuple(
                item
                for item in session_manual
                if item.phase == "post_dca_replacement"
            )
            if pre_dca_manual:
                if self.runtime_paths is None:  # pragma: no cover - live guard
                    raise PrivateDailyIntegrityError(
                        "manual_owner_event_runtime_boundary_missing"
                    )
                for approval in pre_dca_manual:
                    _record_manual_approval(
                        approval,
                        ledger=self.ledger,
                        config=self.config,
                        runtime_paths=self.runtime_paths,
                        clock=self.clock,
                    )
                source_health.append(
                    {
                        "source_id": f"manual_owner_events.pre.{session.isoformat()}",
                        "source_type": "ledger",
                        "status": "accepted",
                        "required": True,
                        "observed_at": _utc_text(started_at),
                        "detail_code": "owner_confirmed_events_applied_pre_dca",
                    }
                )

            universe = _instrument_universe(self.config, self.ledger, session)
            if universe is None:
                outcome = _blocked_outcome(
                    session,
                    reason="instrument_identity_missing",
                    calendar_gate="passed",
                    price_gate="blocked",
                    corporate_action_gate="not_attempted",
                    funding_gate="not_attempted",
                )
                outcomes.append(outcome)
                prior_blocked = True
                continue
            try:
                for mic in sorted({item.exchange_mic for item in universe}):
                    if self.calendar.session_close(session, mic) > started_at:
                        raise ExchangeSessionError("session close is not completed")
            except ExchangeSessionError:
                outcomes.append(
                    _blocked_outcome(
                        session,
                        reason="calendar_gate_blocked",
                        calendar_gate="blocked",
                        price_gate="not_attempted",
                        corporate_action_gate="not_attempted",
                        funding_gate="not_attempted",
                    )
                )
                prior_blocked = True
                continue

            if block_reason is not None:
                outcomes.append(
                    _blocked_outcome(
                        session,
                        reason=block_reason,
                        calendar_gate="passed",
                        price_gate="blocked",
                        corporate_action_gate="not_attempted",
                        funding_gate="not_attempted",
                    )
                )
                source_health.append(
                    {
                        "source_id": f"close_preflight.{session.isoformat()}",
                        "source_type": "accepted_close",
                        "status": "blocked",
                        "required": True,
                        "observed_at": _utc_text(started_at),
                        "detail_code": block_reason,
                    }
                )
                prior_blocked = True
                continue

            valuation_batch = self.close_registry.resolve_batch(universe, session)
            source_health.extend(_batch_source_health(valuation_batch))
            if not valuation_batch.price_gate_permitted:
                outcomes.append(
                    _blocked_outcome(
                        session,
                        reason="accepted_close_price_gate_blocked",
                        calendar_gate="passed",
                        price_gate="blocked",
                        corporate_action_gate="not_attempted",
                        funding_gate="not_attempted",
                        close_batch_id=valuation_batch.batch_id,
                    )
                )
                prior_blocked = True
                continue

            universe_symbols = tuple(
                item.canonical_symbol for item in universe
            )
            action_statuses = self.config.corporate_action_statuses(
                session,
                as_of=started_at,
                symbols=universe_symbols,
            )
            if action_statuses is None:
                outcomes.append(
                    _blocked_outcome(
                        session,
                        reason="corporate_action_attestation_missing",
                        calendar_gate="passed",
                        price_gate="passed",
                        corporate_action_gate="blocked",
                        funding_gate="not_attempted",
                        close_batch_id=valuation_batch.batch_id,
                    )
                )
                source_health.append(
                    {
                        "source_id": f"corporate_actions.{session.isoformat()}",
                        "source_type": "other",
                        "status": "blocked",
                        "required": True,
                        "observed_at": _utc_text(started_at),
                        "detail_code": "manual_attestation_missing",
                    }
                )
                prior_blocked = True
                continue
            source_health.append(
                {
                    "source_id": f"corporate_actions.{session.isoformat()}",
                    "source_type": "other",
                    "status": "accepted",
                    "required": True,
                    "observed_at": _utc_text(started_at),
                    "detail_code": "manual_attestation_accepted",
                }
            )

            plan_instruments = tuple(
                self.config.by_symbol[symbol]
                for symbol in sorted(self.config.dca_plan.base_amounts)
            )
            plan_batch = (
                valuation_batch
                if tuple(item.canonical_symbol for item in universe)
                == tuple(item.canonical_symbol for item in plan_instruments)
                else self.close_registry.resolve_batch(plan_instruments, session)
            )
            if not plan_batch.price_gate_permitted:
                raise PrivateDailyIntegrityError("cached_dca_price_batch_disagreed")
            plan_actions = {
                symbol: action_statuses[symbol]
                for symbol in self.config.dca_plan.base_amounts
            }
            try:
                settlement = self.ledger.settle_modeled_dca_batch(
                    self.config.dca_plan,
                    plan_batch,
                    started_at,
                    plan_actions,
                )
            except LedgerInsufficientCash:
                outcomes.append(
                    _blocked_outcome(
                        session,
                        reason="modeled_dca_funding_insufficient",
                        calendar_gate="passed",
                        price_gate="passed",
                        corporate_action_gate="passed",
                        funding_gate="blocked",
                        close_batch_id=valuation_batch.batch_id,
                    )
                )
                prior_blocked = True
                continue
            except LedgerSettlementBlocked as exc:
                raise PrivateDailyIntegrityError("unexpected_ledger_settlement_block") from exc

            if post_dca_manual:
                if self.runtime_paths is None:  # pragma: no cover - live guard
                    raise PrivateDailyIntegrityError(
                        "manual_owner_event_runtime_boundary_missing"
                    )
                if (
                    settlement.status != "settled"
                    or settlement.plan_id != self.config.dca_plan.plan_id
                    or settlement.plan_version != self.config.dca_plan.version
                ):
                    raise PrivateDailyIntegrityError(
                        "manual_dca_replacement_without_settlement"
                    )
                replacements = settlement.receipts_by_symbol
                for approval in post_dca_manual:
                    symbol = str(approval.request.payload["symbol"])
                    modeled = replacements.get(symbol)
                    if modeled is None:
                        raise PrivateDailyIntegrityError(
                            "manual_dca_replacement_symbol_missing"
                        )
                    _record_manual_approval(
                        approval,
                        ledger=self.ledger,
                        config=self.config,
                        runtime_paths=self.runtime_paths,
                        clock=self.clock,
                        modeled_replacement_event_id=(
                            modeled.settlement_event_id
                        ),
                    )
                source_health.append(
                    {
                        "source_id": f"manual_owner_events.post.{session.isoformat()}",
                        "source_type": "ledger",
                        "status": "accepted",
                        "required": True,
                        "observed_at": _utc_text(started_at),
                        "detail_code": "owner_reported_dca_replacements_applied",
                    }
                )

            confirmed = self.ledger.record_valuation("confirmed", valuation_batch)
            modeled = self.ledger.record_valuation("modeled", valuation_batch)
            outcomes.append(
                _settled_outcome(
                    session,
                    settlement,
                    confirmed,
                    modeled,
                    valuation_batch,
                )
            )

        common_after = self.ledger.latest_common_valuation()
        if common_after is None:
            raise PrivateDailyNotInitialized("common_valuation_unavailable")
        if common_after.session > latest_session:
            raise PrivateDailyIntegrityError("ledger_valuation_after_latest_close")

        if not sessions:
            report_status = "no_new_close"
        elif any(item.status == "blocked" for item in outcomes):
            report_status = "blocked"
        else:
            report_status = "complete"
        fresh_session = any(
            item.session == common_after.session
            and item.confirmed_valuation_status == "fresh"
            and item.modeled_valuation_status == "fresh"
            for item in outcomes
        )
        confirmed_projection = self.ledger.project("confirmed", common_after.session)
        modeled_projection = self.ledger.project("modeled", common_after.session)
        confirmed_book = _valued_book(
            confirmed_projection,
            common_after.confirmed,
            fresh=fresh_session and report_status != "no_new_close",
        )
        modeled_book = _valued_book(
            modeled_projection,
            common_after.modeled,
            fresh=fresh_session and report_status != "no_new_close",
        )
        current_hash = self.ledger.project("modeled").last_event_hash
        source_health.append(
            {
                "source_id": "ledger.hash_chain",
                "source_type": "ledger",
                "status": "healthy",
                "required": True,
                "observed_at": _utc_text(started_at),
                "detail_code": "hash_chain_verified",
            }
        )

        observed_times = [
            _parse_utc_text(item["observed_at"])
            for item in source_health
            if item["observed_at"] is not None
        ]
        prepared_at = max([started_at, _aware_utc(self.clock(), "clock"), *observed_times])
        if prepared_at.astimezone(report_zone).date() != delivery_date:
            raise PrivateDailyRuntimeError("run_crossed_delivery_date")

        block_reasons = sorted(
            {
                reason
                for item in outcomes
                if item.status in {"blocked", "not_attempted_prior_session_blocked"}
                for reason in item.reason_codes
            }
        )
        draft = {
            "classification": (
                "synthetic_example"
                if self.config.simulation
                else "private_owner_only"
            ),
            "simulation": self.config.simulation,
            "report_status": report_status,
            "prepared_at": _utc_text(prepared_at),
            "delivery": {
                "delivery_date": delivery_date.isoformat(),
                "timezone": self.config.report_timezone,
                "channel": self.config.delivery_channel,
            },
            "calendar": {
                "calendar_id": primary["calendar_name"],
                "exchange_mic": self.config.primary_mic,
                "exchange_timezone": primary["exchange_timezone"],
                "report_timezone": self.config.report_timezone,
                "as_of": _utc_text(started_at),
                "mode": "none" if not sessions else "single" if len(sessions) == 1 else "backfill",
                "latest_completed_session": latest_session.isoformat(),
                "last_settled_session_before_run": last_reported.isoformat(),
                "unsettled_sessions": [item.isoformat() for item in sessions],
                "provenance": list(provenance),
                "new_sessions_count": len(sessions),
                "no_new_close": report_status == "no_new_close",
            },
            "session_results": [
                item.report_row(latest_session) for item in outcomes
            ],
            "portfolio": {
                "currency": self.config.ledger_policy.currency,
                "as_of_session": common_after.session.isoformat(),
                "ledger_last_event_hash": current_hash,
                "confirmed": confirmed_book,
                "modeled": modeled_book,
            },
            "dca": _dca_report(self.config, outcomes),
            "research": (
                dict(research_document)
                if research_document is not None
                else {
                    "overall_view": (
                        "会计运行层仅按已确认正式收盘价更新两本账；"
                        "未连接券商，也未执行交易。"
                    ),
                    "market_regime": "unknown",
                    "risk_budget_multiplier": _ZERO,
                    "fund_monitoring": [],
                    "social_attention": [],
                    "notes": ["accounting_only_research_adapter_not_yet_connected"],
                }
            ),
            "source_health": sorted(source_health, key=lambda item: item["source_id"]),
            "actions": _base_actions(report_status, block_reasons),
            "manual_trade_prompt": {
                "required": False,
                "prompt": None,
                "accepted_response_kinds": [
                    "cash_flow",
                    "confirmed_fill",
                    "fee",
                    "income",
                    "no_manual_trade",
                    "skip_dca",
                    "split",
                ],
                "default_if_no_response": "no_new_owner_confirmed_event",
                "broker_execution_available": False,
            },
            "privacy": {
                "contains_private_portfolio_data": not self.config.simulation,
                "contains_target_identifier": False,
                "github_persistence_allowed": False,
                "public_artifact_allowed": False,
                "gpt_owner_delivery_only": True,
                "redaction_status": (
                    "synthetic_only"
                    if self.config.simulation
                    else "private_owner_only"
                ),
                "warnings": [],
            },
        }
        target_hash = compute_target_key_sha256(target_key)
        report = finalize_private_daily_report(
            draft,
            target_key_sha256=target_hash,
        )
        enqueue_now = max(prepared_at, _aware_utc(self.clock(), "clock"))
        if enqueue_now.astimezone(report_zone).date() != delivery_date:
            raise PrivateDailyRuntimeError("run_crossed_delivery_date")
        enqueue = self.outbox.enqueue(
            report,
            target_key,
            current_hash,
            now=enqueue_now,
        )
        files = persist_private_daily_report(
            report,
            self.report_directory,
            runtime_paths=self.runtime_paths,
        )
        blocked_session = next(
            (item.session for item in outcomes if item.status == "blocked"),
            None,
        )
        processed = tuple(
            item.session
            for item in outcomes
            if item.status != "not_attempted_prior_session_blocked"
        )
        return PrivateDailyRunResult(
            status="prepared",
            report_status=report_status,
            delivery_id=enqueue.delivery_id,
            report_id=enqueue.report_id,
            processed_sessions=processed,
            blocked_session=blocked_session,
            report_files=files,
        )


__all__ = [
    "PrivateDailyIntegrityError",
    "PrivateDailyNotInitialized",
    "PrivateDailyRunResult",
    "PrivateDailyRuntime",
    "PrivateDailyRuntimeError",
    "PrivateLedgerInitializationResult",
    "initialize_private_ledger",
]
