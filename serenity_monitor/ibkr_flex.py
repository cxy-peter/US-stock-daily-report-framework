"""Read-only Interactive Brokers Flex reconciliation.

The module requests a pre-configured Flex Web Service query, parses selected
account facts and compares them with an owner-confirmed local book.  It has no
broker-session, order, cancellation or ledger-mutation API.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests


SEND_REQUEST_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/"
    "FlexWebService/SendRequest"
)
GET_STATEMENT_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/"
    "FlexWebService/GetStatement"
)
_ALLOWED_HOSTS = frozenset(
    {"ndcdyn.interactivebrokers.com", "www.interactivebrokers.com"}
)


class FlexError(RuntimeError):
    """Sanitized base error for the read-only Flex boundary."""

    def __init__(self, code: str) -> None:
        self.code = str(code).strip().casefold() or "flex_error"
        super().__init__(self.code)


class FlexPending(FlexError):
    """The report was accepted but is not ready."""


class FlexParseError(FlexError):
    """The XML response did not satisfy the expected contract."""


@dataclass(frozen=True)
class FlexRequestTicket:
    reference_code: str
    response_url: str
    requested_at: str


@dataclass(frozen=True)
class FlexPosition:
    symbol: str
    conid: str | None
    asset_category: str | None
    currency: str | None
    quantity: Decimal
    cost_basis: Decimal | None
    mark_price: Decimal | None
    market_value: Decimal | None
    unrealized_pnl: Decimal | None


@dataclass(frozen=True)
class FlexTrade:
    trade_id: str | None
    transaction_id: str | None
    symbol: str
    conid: str | None
    asset_category: str | None
    currency: str | None
    trade_date: str | None
    side: str | None
    quantity: Decimal | None
    trade_price: Decimal | None
    proceeds: Decimal | None
    commission: Decimal | None


@dataclass(frozen=True)
class FlexCashTransaction:
    transaction_id: str | None
    transaction_type: str | None
    symbol: str | None
    currency: str | None
    amount: Decimal | None
    date: str | None
    description: str | None


@dataclass(frozen=True)
class FlexFee:
    transaction_id: str | None
    fee_type: str | None
    symbol: str | None
    currency: str | None
    amount: Decimal | None
    date: str | None
    description: str | None


@dataclass(frozen=True)
class FlexCorporateAction:
    transaction_id: str | None
    action_type: str | None
    symbol: str | None
    conid: str | None
    quantity: Decimal | None
    amount: Decimal | None
    currency: str | None
    date: str | None
    description: str | None


@dataclass(frozen=True)
class FlexAccountSnapshot:
    account_id_hash: str
    period_from: str | None
    period_to: str | None
    base_currency: str | None
    net_liquidation_value: Decimal | None
    ending_cash: Decimal | None
    buying_power: Decimal | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    positions: tuple[FlexPosition, ...]
    trades: tuple[FlexTrade, ...]
    cash_transactions: tuple[FlexCashTransaction, ...]
    fees: tuple[FlexFee, ...]
    corporate_actions: tuple[FlexCorporateAction, ...]
    statement_sha256: str
    source_status: str = "healthy"

    def public_summary(self) -> dict[str, Any]:
        """Return a redacted summary without the raw account identifier."""

        return {
            "account_id_hash": self.account_id_hash,
            "period_from": self.period_from,
            "period_to": self.period_to,
            "base_currency": self.base_currency,
            "position_count": len(self.positions),
            "trade_count": len(self.trades),
            "cash_transaction_count": len(self.cash_transactions),
            "fee_count": len(self.fees),
            "corporate_action_count": len(self.corporate_actions),
            "statement_sha256": self.statement_sha256,
            "source_status": self.source_status,
        }


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    symbol: str | None
    severity: str
    broker_value: str | None
    local_value: str | None
    detail: str


@dataclass(frozen=True)
class FlexReconciliationResult:
    status: str
    matched: bool
    issues: tuple[ReconciliationIssue, ...]
    snapshot_hash: str
    automatic_ledger_mutation_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "matched": self.matched,
            "issues": [issue.__dict__ for issue in self.issues],
            "snapshot_hash": self.snapshot_hash,
            "automatic_ledger_mutation_permitted": False,
        }


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _rfc3339(value: dt.datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None
    return result if result.is_finite() else None


def _first(attributes: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = attributes.get(name)
        if value not in (None, ""):
            return value
    return None


def _parse_xml(text: str, code: str) -> ET.Element:
    if not text.strip() or len(text.encode("utf-8")) > 100_000_000:
        raise FlexParseError(code)
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise FlexParseError(code) from exc


def _safe_response_url(value: str | None) -> str:
    url = str(value or GET_STATEMENT_URL).strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
        raise FlexParseError("response_url_not_allowed")
    return url


def _hash_account(value: str | None) -> str:
    text = str(value or "UNKNOWN").strip()
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_request_response(
    xml_text: str, *, requested_at: dt.datetime
) -> FlexRequestTicket:
    root = _parse_xml(xml_text, "request_response_invalid_xml")
    status = (root.findtext("Status") or "").strip().casefold()
    if status != "success":
        code = (root.findtext("ErrorCode") or "unknown").strip()
        if code in {"1018", "1019", "1021"}:
            raise FlexPending("request_pending")
        raise FlexError(f"request_failed_{code}")
    reference = (root.findtext("ReferenceCode") or "").strip()
    if not reference.isdigit():
        raise FlexParseError("reference_code_invalid")
    return FlexRequestTicket(
        reference_code=reference,
        response_url=_safe_response_url(root.findtext("url")),
        requested_at=_rfc3339(requested_at),
    )


def _check_statement_failure(root: ET.Element) -> None:
    if root.tag != "FlexStatementResponse":
        return
    status = (root.findtext("Status") or "").strip().casefold()
    if status == "success":
        return
    code = (root.findtext("ErrorCode") or "unknown").strip()
    if code in {"1003", "1018", "1019", "1021"}:
        raise FlexPending("statement_pending")
    raise FlexError(f"statement_failed_{code}")


def _summary(statement: ET.Element) -> dict[str, Decimal | str | None]:
    result: dict[str, Decimal | str | None] = {
        "base_currency": statement.attrib.get("currency"),
        "net_liquidation_value": None,
        "ending_cash": None,
        "buying_power": None,
        "realized_pnl": None,
        "unrealized_pnl": None,
    }
    mappings = {
        "net_liquidation_value": ("netLiquidationValue", "endingNAV", "endingValue"),
        "ending_cash": ("endingCash", "endingCashSecurities", "cash"),
        "buying_power": ("buyingPower", "availableFunds"),
        "realized_pnl": ("realizedPnL", "realizedPnl"),
        "unrealized_pnl": ("unrealizedPnL", "unrealizedPnl"),
    }
    elements = (
        list(statement.findall(".//EquitySummaryInBase"))
        + list(statement.findall(".//CashReport"))
        + list(statement.findall(".//AccountInformation"))
    )
    for element in elements:
        attrs = element.attrib
        if not result["base_currency"]:
            result["base_currency"] = _first(attrs, "currency", "baseCurrency")
        for key, names in mappings.items():
            if result[key] is None:
                result[key] = _decimal(_first(attrs, *names))
    return result


def _positions(statement: ET.Element) -> tuple[FlexPosition, ...]:
    rows: list[FlexPosition] = []
    for element in statement.findall(".//OpenPosition"):
        attrs = element.attrib
        symbol = str(_first(attrs, "symbol", "underlyingSymbol") or "").strip().upper()
        quantity = _decimal(_first(attrs, "position", "quantity", "openQuantity"))
        if not symbol or quantity is None:
            continue
        rows.append(
            FlexPosition(
                symbol=symbol,
                conid=_first(attrs, "conid", "conidEx"),
                asset_category=_first(attrs, "assetCategory", "assetClass"),
                currency=_first(attrs, "currency", "listingExchangeCurrency"),
                quantity=quantity,
                cost_basis=_decimal(_first(attrs, "costBasisMoney", "costBasis")),
                mark_price=_decimal(_first(attrs, "markPrice", "closePrice", "positionValuePrice")),
                market_value=_decimal(_first(attrs, "positionValue", "marketValue")),
                unrealized_pnl=_decimal(_first(attrs, "fifoPnlUnrealized", "unrealizedPnl")),
            )
        )
    return tuple(sorted(rows, key=lambda item: (item.symbol, item.conid or "")))


def _trades(statement: ET.Element) -> tuple[FlexTrade, ...]:
    rows: list[FlexTrade] = []
    for element in statement.findall(".//Trade"):
        attrs = element.attrib
        symbol = str(_first(attrs, "symbol", "underlyingSymbol") or "").strip().upper()
        if not symbol:
            continue
        rows.append(
            FlexTrade(
                trade_id=_first(attrs, "tradeID", "tradeId"),
                transaction_id=_first(attrs, "transactionID", "transactionId"),
                symbol=symbol,
                conid=_first(attrs, "conid", "conidEx"),
                asset_category=_first(attrs, "assetCategory", "assetClass"),
                currency=_first(attrs, "currency"),
                trade_date=_first(attrs, "tradeDate", "dateTime"),
                side=_first(attrs, "buySell", "side"),
                quantity=_decimal(_first(attrs, "quantity", "shares")),
                trade_price=_decimal(_first(attrs, "tradePrice", "price")),
                proceeds=_decimal(_first(attrs, "proceeds", "netCash")),
                commission=_decimal(_first(attrs, "ibCommission", "commission")),
            )
        )
    return tuple(rows)


def _cash_transactions(statement: ET.Element) -> tuple[FlexCashTransaction, ...]:
    return tuple(
        FlexCashTransaction(
            transaction_id=_first(element.attrib, "transactionID", "transactionId"),
            transaction_type=_first(element.attrib, "type", "transactionType"),
            symbol=_first(element.attrib, "symbol"),
            currency=_first(element.attrib, "currency"),
            amount=_decimal(_first(element.attrib, "amount", "netCash")),
            date=_first(element.attrib, "dateTime", "settleDate", "tradeDate"),
            description=_first(element.attrib, "description", "activityDescription"),
        )
        for element in statement.findall(".//CashTransaction")
    )


def _fees(statement: ET.Element) -> tuple[FlexFee, ...]:
    return tuple(
        FlexFee(
            transaction_id=_first(element.attrib, "transactionID", "transactionId"),
            fee_type=_first(element.attrib, "type", "feeType", "code"),
            symbol=_first(element.attrib, "symbol", "underlyingSymbol"),
            currency=_first(element.attrib, "currency"),
            amount=_decimal(_first(element.attrib, "amount", "netCash", "fee")),
            date=_first(element.attrib, "dateTime", "reportDate", "settleDate", "tradeDate"),
            description=_first(element.attrib, "description", "activityDescription"),
        )
        for element in statement.findall(".//Fee")
    )


def _corporate_actions(statement: ET.Element) -> tuple[FlexCorporateAction, ...]:
    return tuple(
        FlexCorporateAction(
            transaction_id=_first(element.attrib, "transactionID", "transactionId"),
            action_type=_first(element.attrib, "type", "actionType", "code"),
            symbol=_first(element.attrib, "symbol", "underlyingSymbol"),
            conid=_first(element.attrib, "conid", "conidEx"),
            quantity=_decimal(_first(element.attrib, "quantity", "shares")),
            amount=_decimal(_first(element.attrib, "amount", "proceeds")),
            currency=_first(element.attrib, "currency"),
            date=_first(element.attrib, "dateTime", "reportDate", "settleDate"),
            description=_first(element.attrib, "description", "actionDescription"),
        )
        for element in statement.findall(".//CorporateAction")
    )


def parse_flex_statement(xml_text: str) -> tuple[FlexAccountSnapshot, ...]:
    root = _parse_xml(xml_text, "statement_invalid_xml")
    _check_statement_failure(root)
    statements = [root] if root.tag == "FlexStatement" else root.findall(".//FlexStatement")
    if not statements:
        raise FlexParseError("flex_statement_missing")
    digest = hashlib.sha256(xml_text.encode("utf-8")).hexdigest()
    snapshots: list[FlexAccountSnapshot] = []
    for statement in statements:
        account_id = _first(statement.attrib, "accountId", "accountID")
        if not account_id:
            info = statement.find(".//AccountInformation")
            account_id = None if info is None else _first(info.attrib, "accountId", "accountID")
        summary = _summary(statement)
        snapshots.append(
            FlexAccountSnapshot(
                account_id_hash=_hash_account(account_id),
                period_from=statement.attrib.get("fromDate") or statement.attrib.get("whenGenerated"),
                period_to=statement.attrib.get("toDate") or statement.attrib.get("whenGenerated"),
                base_currency=None if summary["base_currency"] is None else str(summary["base_currency"]),
                net_liquidation_value=summary["net_liquidation_value"],
                ending_cash=summary["ending_cash"],
                buying_power=summary["buying_power"],
                realized_pnl=summary["realized_pnl"],
                unrealized_pnl=summary["unrealized_pnl"],
                positions=_positions(statement),
                trades=_trades(statement),
                cash_transactions=_cash_transactions(statement),
                fees=_fees(statement),
                corporate_actions=_corporate_actions(statement),
                statement_sha256=digest,
            )
        )
    return tuple(snapshots)


class IBKRFlexClient:
    """Minimal read-only Flex v3 client with bounded polling."""

    def __init__(
        self,
        *,
        token: str,
        query_id: str,
        user_agent: str = "Python/3 serenity-readonly-flex/1.0",
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
        clock: Callable[[], dt.datetime] = _now_utc,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not str(token).strip() or not str(query_id).strip():
            raise ValueError("token and query_id are required")
        if not str(query_id).strip().isdigit():
            raise ValueError("query_id must be numeric")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._token = str(token).strip()
        self.query_id = str(query_id).strip()
        self.user_agent = str(user_agent).strip() or "Python/3"
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock
        self.sleeper = sleeper

    def __repr__(self) -> str:
        return (
            f"IBKRFlexClient(query_id={self.query_id!r}, "
            f"user_agent={self.user_agent!r}, token=<redacted>)"
        )

    @property
    def headers(self) -> Mapping[str, str]:
        return {"User-Agent": self.user_agent, "Accept": "application/xml,text/xml"}

    def request_statement(
        self,
        *,
        from_date: dt.date | None = None,
        to_date: dt.date | None = None,
        period_days: int | None = None,
    ) -> FlexRequestTicket:
        if period_days is not None and (from_date is not None or to_date is not None):
            raise ValueError("period_days cannot be combined with date overrides")
        params: dict[str, str] = {"t": self._token, "q": self.query_id, "v": "3"}
        if period_days is not None:
            if period_days < 1 or period_days > 365:
                raise ValueError("period_days must be between 1 and 365")
            params["p"] = str(period_days)
        if from_date is not None or to_date is not None:
            if from_date is None or to_date is None or from_date > to_date:
                raise ValueError("a valid from_date/to_date range is required")
            if (to_date - from_date).days > 365:
                raise ValueError("date override may not exceed 365 days")
            params["fd"] = from_date.strftime("%Y%m%d")
            params["td"] = to_date.strftime("%Y%m%d")
        try:
            response = self.session.get(
                SEND_REQUEST_URL,
                params=params,
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise FlexError("request_transport_failed") from exc
        return parse_request_response(response.text, requested_at=self.clock())

    def retrieve_statement(
        self, ticket: FlexRequestTicket
    ) -> tuple[FlexAccountSnapshot, ...]:
        try:
            response = self.session.get(
                _safe_response_url(ticket.response_url),
                params={"t": self._token, "q": ticket.reference_code, "v": "3"},
                headers=self.headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise FlexError("statement_transport_failed") from exc
        return parse_flex_statement(response.text)

    def fetch_statement(
        self,
        *,
        max_attempts: int = 6,
        initial_delay_seconds: float = 2.0,
        from_date: dt.date | None = None,
        to_date: dt.date | None = None,
        period_days: int | None = None,
    ) -> tuple[FlexAccountSnapshot, ...]:
        if max_attempts < 1 or max_attempts > 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must be non-negative")
        ticket = self.request_statement(
            from_date=from_date, to_date=to_date, period_days=period_days
        )
        delay = float(initial_delay_seconds)
        for attempt in range(max_attempts):
            try:
                return self.retrieve_statement(ticket)
            except FlexPending:
                if attempt == max_attempts - 1:
                    raise FlexPending("statement_generation_timeout")
                self.sleeper(delay)
                delay = min(max(delay * 1.8, 0.5), 30.0)
        raise FlexPending("statement_generation_timeout")  # pragma: no cover


def reconcile_flex_snapshot(
    snapshot: FlexAccountSnapshot,
    *,
    local_positions: Mapping[str, Decimal | str | float | int],
    local_cash: Decimal | str | float | int | None,
    quantity_tolerance: Decimal = Decimal("0.000001"),
    cash_tolerance: Decimal = Decimal("1.00"),
) -> FlexReconciliationResult:
    """Compare broker facts with a confirmed local book without mutating it."""

    broker_positions: dict[str, Decimal] = {}
    for position in snapshot.positions:
        broker_positions[position.symbol] = (
            broker_positions.get(position.symbol, Decimal("0")) + position.quantity
        )
    normalized_local = {
        str(symbol).upper(): _decimal(value) or Decimal("0")
        for symbol, value in local_positions.items()
    }
    issues: list[ReconciliationIssue] = []
    for symbol in sorted(set(broker_positions) | set(normalized_local)):
        broker = broker_positions.get(symbol, Decimal("0"))
        local = normalized_local.get(symbol, Decimal("0"))
        if abs(broker - local) <= quantity_tolerance:
            continue
        if symbol not in normalized_local:
            code, detail = (
                "UNEXPECTED_BROKER_EVENT",
                "Broker position is absent from the confirmed local book.",
            )
        elif symbol not in broker_positions:
            code, detail = (
                "MISSING_LOCAL_EVENT",
                "Confirmed local position is absent from the broker statement.",
            )
        else:
            code, detail = (
                "QUANTITY_MISMATCH",
                "Broker and confirmed local quantities differ.",
            )
        issues.append(
            ReconciliationIssue(
                code=code,
                symbol=symbol,
                severity="blocking",
                broker_value=str(broker),
                local_value=str(local),
                detail=detail,
            )
        )

    local_cash_value = _decimal(local_cash)
    if snapshot.ending_cash is None or local_cash_value is None:
        issues.append(
            ReconciliationIssue(
                code="NEED_OWNER_CONFIRMATION",
                symbol=None,
                severity="warning",
                broker_value=None if snapshot.ending_cash is None else str(snapshot.ending_cash),
                local_value=None if local_cash_value is None else str(local_cash_value),
                detail="Cash comparison is unavailable because one side is missing.",
            )
        )
    elif abs(snapshot.ending_cash - local_cash_value) > cash_tolerance:
        issues.append(
            ReconciliationIssue(
                code="CASH_MISMATCH",
                symbol=None,
                severity="blocking",
                broker_value=str(snapshot.ending_cash),
                local_value=str(local_cash_value),
                detail="Broker and confirmed local cash differ beyond tolerance.",
            )
        )

    if snapshot.fees:
        issues.append(
            ReconciliationIssue(
                code="NEED_OWNER_CONFIRMATION",
                symbol=None,
                severity="warning",
                broker_value=str(len(snapshot.fees)),
                local_value=None,
                detail="Broker statement contains explicit fees requiring reconciliation.",
            )
        )
    if snapshot.corporate_actions:
        issues.append(
            ReconciliationIssue(
                code="NEED_OWNER_CONFIRMATION",
                symbol=None,
                severity="warning",
                broker_value=str(len(snapshot.corporate_actions)),
                local_value=None,
                detail="Broker statement contains corporate actions requiring reconciliation.",
            )
        )

    matched = not any(issue.severity == "blocking" for issue in issues)
    status = "MATCHED" if not issues else (
        "NEED_OWNER_CONFIRMATION" if matched else "MISMATCH"
    )
    return FlexReconciliationResult(
        status=status,
        matched=matched,
        issues=tuple(issues),
        snapshot_hash=snapshot.statement_sha256,
    )


__all__ = [
    "FlexAccountSnapshot",
    "FlexCashTransaction",
    "FlexCorporateAction",
    "FlexError",
    "FlexFee",
    "FlexParseError",
    "FlexPending",
    "FlexPosition",
    "FlexReconciliationResult",
    "FlexRequestTicket",
    "FlexTrade",
    "IBKRFlexClient",
    "ReconciliationIssue",
    "parse_flex_statement",
    "parse_request_response",
    "reconcile_flex_snapshot",
]
