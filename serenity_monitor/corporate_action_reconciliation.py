"""Point-in-time corporate-action evidence reconciliation.

Broker observations are compared with issuer/exchange/regulatory evidence.
Nothing in this module changes quantities, cost basis, cash, or the confirmed
ledger.  A matched result is an approval input, not an automatic adjustment.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence


_ACTION_TYPES = frozenset(
    {
        "cash_dividend",
        "stock_dividend",
        "split",
        "reverse_split",
        "spinoff",
        "merger",
        "acquisition",
        "ticker_change",
        "delisting",
        "rights_issue",
        "return_of_capital",
        "distribution",
    }
)
_PRIMARY_SOURCES = frozenset({"issuer", "sec", "exchange", "fund_sponsor"})
_SECONDARY_SOURCES = frozenset({"broker", "market_data", "newswire"})


class CorporateActionError(ValueError):
    pass


def _aware(value: dt.datetime | str, name: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        result = value
    else:
        result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise CorporateActionError(f"{name} must be timezone-aware")
    return result.astimezone(dt.timezone.utc)


def _date(value: dt.date | str | None, name: str) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise CorporateActionError(f"{name} must be an ISO date") from exc


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CorporateActionError(f"{name} must be decimal-compatible") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise CorporateActionError(f"{name} is outside its allowed domain")
    return result


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CorporateActionObservation:
    observation_id: str
    source_type: str
    source_id: str
    observed_at: dt.datetime | str
    symbol: str
    action_type: str
    ex_date: dt.date | str | None = None
    effective_date: dt.date | str | None = None
    record_date: dt.date | str | None = None
    pay_date: dt.date | str | None = None
    ratio_numerator: Decimal | str | float | int | None = None
    ratio_denominator: Decimal | str | float | int | None = None
    cash_amount: Decimal | str | float | int | None = None
    currency: str | None = None
    new_symbol: str | None = None
    child_symbol: str | None = None
    description: str = ""
    evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        source = str(self.source_type).strip().casefold()
        if source not in _PRIMARY_SOURCES | _SECONDARY_SOURCES:
            raise CorporateActionError("unsupported source_type")
        action = str(self.action_type).strip().casefold()
        if action not in _ACTION_TYPES:
            raise CorporateActionError("unsupported action_type")
        symbol = str(self.symbol).strip().upper()
        if not symbol:
            raise CorporateActionError("symbol is required")
        numerator = _decimal(self.ratio_numerator, "ratio_numerator", positive=True)
        denominator = _decimal(self.ratio_denominator, "ratio_denominator", positive=True)
        if (numerator is None) != (denominator is None):
            raise CorporateActionError("ratio requires numerator and denominator")
        amount = _decimal(self.cash_amount, "cash_amount")
        currency = None if self.currency in (None, "") else str(self.currency).strip().upper()
        if amount is not None and (currency is None or len(currency) != 3):
            raise CorporateActionError("cash action requires a three-letter currency")
        digest = self.evidence_sha256
        if digest is None:
            digest = _sha(
                "|".join(
                    [
                        str(self.source_id),
                        symbol,
                        action,
                        str(self.effective_date or self.ex_date or ""),
                        str(numerator or ""),
                        str(denominator or ""),
                        str(amount or ""),
                    ]
                )
            )
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise CorporateActionError("evidence_sha256 must be lowercase SHA-256")
        object.__setattr__(self, "source_type", source)
        object.__setattr__(self, "action_type", action)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        object.__setattr__(self, "ex_date", _date(self.ex_date, "ex_date"))
        object.__setattr__(self, "effective_date", _date(self.effective_date, "effective_date"))
        object.__setattr__(self, "record_date", _date(self.record_date, "record_date"))
        object.__setattr__(self, "pay_date", _date(self.pay_date, "pay_date"))
        object.__setattr__(self, "ratio_numerator", numerator)
        object.__setattr__(self, "ratio_denominator", denominator)
        object.__setattr__(self, "cash_amount", amount)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "new_symbol", None if not self.new_symbol else str(self.new_symbol).upper())
        object.__setattr__(self, "child_symbol", None if not self.child_symbol else str(self.child_symbol).upper())
        object.__setattr__(self, "evidence_sha256", digest)

    @property
    def anchor_date(self) -> dt.date | None:
        return self.effective_date or self.ex_date or self.pay_date or self.record_date


@dataclass(frozen=True)
class CorporateActionIssue:
    status: str
    symbol: str
    action_type: str
    broker_observation_id: str | None
    primary_observation_id: str | None
    detail: str
    blocking: bool


@dataclass(frozen=True)
class CorporateActionReconciliationResult:
    status: str
    matched_count: int
    issue_count: int
    issues: tuple[CorporateActionIssue, ...]
    evidence_sha256: tuple[str, ...]
    automatic_adjustment_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "matched_count": self.matched_count,
            "issue_count": self.issue_count,
            "issues": [item.__dict__ for item in self.issues],
            "evidence_sha256": list(self.evidence_sha256),
            "automatic_adjustment_permitted": False,
        }


def _same_decimal(left: Decimal | None, right: Decimal | None, tolerance: Decimal) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def _compatible(
    broker: CorporateActionObservation,
    primary: CorporateActionObservation,
    *,
    date_tolerance_days: int,
    amount_tolerance: Decimal,
    ratio_tolerance: Decimal,
) -> tuple[bool, str]:
    if broker.symbol != primary.symbol or broker.action_type != primary.action_type:
        return False, "identity_mismatch"
    if broker.anchor_date and primary.anchor_date:
        if abs((broker.anchor_date - primary.anchor_date).days) > date_tolerance_days:
            return False, "date_mismatch"
    if not _same_decimal(broker.cash_amount, primary.cash_amount, amount_tolerance):
        return False, "cash_amount_mismatch"
    if not _same_decimal(broker.ratio_numerator, primary.ratio_numerator, ratio_tolerance):
        return False, "ratio_numerator_mismatch"
    if not _same_decimal(broker.ratio_denominator, primary.ratio_denominator, ratio_tolerance):
        return False, "ratio_denominator_mismatch"
    if broker.currency and primary.currency and broker.currency != primary.currency:
        return False, "currency_mismatch"
    if broker.new_symbol and primary.new_symbol and broker.new_symbol != primary.new_symbol:
        return False, "new_symbol_mismatch"
    if broker.child_symbol and primary.child_symbol and broker.child_symbol != primary.child_symbol:
        return False, "child_symbol_mismatch"
    return True, "matched"


def reconcile_corporate_actions(
    broker_observations: Iterable[CorporateActionObservation],
    evidence_observations: Iterable[CorporateActionObservation],
    *,
    as_of: dt.datetime | str,
    date_tolerance_days: int = 1,
    amount_tolerance: Decimal = Decimal("0.0001"),
    ratio_tolerance: Decimal = Decimal("0.00000001"),
) -> CorporateActionReconciliationResult:
    """Cross-check broker events against point-in-time primary evidence."""

    cutoff = _aware(as_of, "as_of")
    if date_tolerance_days < 0 or date_tolerance_days > 10:
        raise CorporateActionError("date_tolerance_days must be between 0 and 10")
    brokers = [item for item in broker_observations if item.observed_at <= cutoff]
    evidence = [item for item in evidence_observations if item.observed_at <= cutoff]
    primary = [item for item in evidence if item.source_type in _PRIMARY_SOURCES]
    secondary = [item for item in evidence if item.source_type in _SECONDARY_SOURCES]
    used_primary: set[str] = set()
    issues: list[CorporateActionIssue] = []
    matched = 0

    for broker in brokers:
        candidates = [
            item
            for item in primary
            if item.symbol == broker.symbol and item.action_type == broker.action_type
        ]
        if broker.anchor_date:
            candidates.sort(
                key=lambda item: 9999
                if item.anchor_date is None
                else abs((item.anchor_date - broker.anchor_date).days)
            )
        if not candidates:
            corroborating_secondary = any(
                item.symbol == broker.symbol and item.action_type == broker.action_type
                for item in secondary
            )
            issues.append(
                CorporateActionIssue(
                    status="NEED_PRIMARY_EVIDENCE",
                    symbol=broker.symbol,
                    action_type=broker.action_type,
                    broker_observation_id=broker.observation_id,
                    primary_observation_id=None,
                    detail=(
                        "Broker event has only secondary corroboration."
                        if corroborating_secondary
                        else "Broker event has no point-in-time primary corroboration."
                    ),
                    blocking=True,
                )
            )
            continue
        candidate = candidates[0]
        compatible, detail = _compatible(
            broker,
            candidate,
            date_tolerance_days=date_tolerance_days,
            amount_tolerance=amount_tolerance,
            ratio_tolerance=ratio_tolerance,
        )
        used_primary.add(candidate.observation_id)
        if compatible:
            matched += 1
        else:
            issues.append(
                CorporateActionIssue(
                    status="SOURCE_CONFLICT",
                    symbol=broker.symbol,
                    action_type=broker.action_type,
                    broker_observation_id=broker.observation_id,
                    primary_observation_id=candidate.observation_id,
                    detail=detail,
                    blocking=True,
                )
            )

    for item in primary:
        if item.observation_id in used_primary:
            continue
        if item.anchor_date and item.anchor_date > cutoff.date() + dt.timedelta(days=30):
            continue
        issues.append(
            CorporateActionIssue(
                status="MISSING_BROKER_ACTION",
                symbol=item.symbol,
                action_type=item.action_type,
                broker_observation_id=None,
                primary_observation_id=item.observation_id,
                detail="Primary source announces an action not present in the broker snapshot.",
                blocking=True,
            )
        )

    status = "MATCHED" if not issues else (
        "NEED_OWNER_CONFIRMATION"
        if not any(item.status == "SOURCE_CONFLICT" for item in issues)
        else "SOURCE_CONFLICT"
    )
    evidence_hashes = tuple(
        sorted({item.evidence_sha256 for item in brokers + evidence})
    )
    return CorporateActionReconciliationResult(
        status=status,
        matched_count=matched,
        issue_count=len(issues),
        issues=tuple(issues),
        evidence_sha256=evidence_hashes,
    )


__all__ = [
    "CorporateActionError",
    "CorporateActionIssue",
    "CorporateActionObservation",
    "CorporateActionReconciliationResult",
    "reconcile_corporate_actions",
]
