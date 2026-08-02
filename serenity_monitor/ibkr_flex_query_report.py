"""Parse the multi-statement IBKR Flex Activity report shape used for daily review.

The existing read-only Flex client focuses on one normalized account snapshot.
This module additionally supports a FlexQueryResponse containing one statement
per report date, as produced by an Activity Flex Query covering many days.  It
extracts the latest account/position snapshot and aggregates period tax and cost
facts without retaining the raw account identifier.
"""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class FlexQueryReportError(ValueError):
    pass


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise FlexQueryReportError("invalid decimal in Flex report") from exc
    if not result.is_finite():
        raise FlexQueryReportError("non-finite decimal in Flex report")
    return result


def _account_hash(value: str | None) -> str:
    return hashlib.sha256(str(value or "UNKNOWN").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FlexQueryPosition:
    symbol: str
    quantity: Decimal
    market_value: Decimal
    cost_basis: Decimal
    unrealized_pnl: Decimal
    currency: str | None
    asset_category: str | None


@dataclass(frozen=True)
class FlexQueryPeriodMetrics:
    realized_pnl: Decimal
    change_in_unrealized_pnl: Decimal
    deposits_withdrawals: Decimal
    gross_dividends: Decimal
    withholding_tax: Decimal
    withholding_871m: Decimal
    interest: Decimal
    broker_fees: Decimal
    advisor_fees: Decimal
    other_fees: Decimal
    transaction_tax: Decimal
    trade_commissions: Decimal
    trade_notional: Decimal
    recurring_trade_commissions: Decimal
    recurring_trade_notional: Decimal
    recurring_fill_count: int
    nonrecurring_trade_commissions: Decimal
    nonrecurring_trade_notional: Decimal
    nonrecurring_fill_count: int
    corporate_action_count: int

    @property
    def recurring_effective_commission_rate(self) -> Decimal | None:
        if self.recurring_trade_notional == 0:
            return None
        return self.recurring_trade_commissions / self.recurring_trade_notional

    @property
    def nonrecurring_effective_commission_rate(self) -> Decimal | None:
        if self.nonrecurring_trade_notional == 0:
            return None
        return self.nonrecurring_trade_commissions / self.nonrecurring_trade_notional


@dataclass(frozen=True)
class FlexQueryReportSummary:
    query_name: str
    statement_count: int
    generated_at: str | None
    latest_report_date: str
    account_id_hash: str
    base_currency: str | None
    tax_lot_matching_method: str | None
    recurring_instruction_count: int
    recurring_instruction_amounts: tuple[Decimal, ...]
    net_liquidation_value: Decimal
    cash: Decimal
    stock_market_value: Decimal
    dividend_accruals: Decimal
    positions: tuple[FlexQueryPosition, ...]
    period_metrics: FlexQueryPeriodMetrics
    source_sha256: str

    def to_private_dict(self) -> dict[str, Any]:
        """Return JSON-friendly private facts without the raw account ID."""

        metrics = self.period_metrics
        return {
            "query_name": self.query_name,
            "statement_count": self.statement_count,
            "generated_at": self.generated_at,
            "latest_report_date": self.latest_report_date,
            "account_id_hash": self.account_id_hash,
            "base_currency": self.base_currency,
            "tax_lot_matching_method": self.tax_lot_matching_method,
            "recurring_instruction_count": self.recurring_instruction_count,
            "recurring_instruction_amounts": [str(item) for item in self.recurring_instruction_amounts],
            "net_liquidation_value": str(self.net_liquidation_value),
            "cash": str(self.cash),
            "stock_market_value": str(self.stock_market_value),
            "dividend_accruals": str(self.dividend_accruals),
            "positions": [
                {
                    "symbol": item.symbol,
                    "quantity": str(item.quantity),
                    "market_value": str(item.market_value),
                    "cost_basis": str(item.cost_basis),
                    "unrealized_pnl": str(item.unrealized_pnl),
                    "currency": item.currency,
                    "asset_category": item.asset_category,
                }
                for item in self.positions
            ],
            "period_metrics": {
                "realized_pnl": str(metrics.realized_pnl),
                "change_in_unrealized_pnl": str(metrics.change_in_unrealized_pnl),
                "deposits_withdrawals": str(metrics.deposits_withdrawals),
                "gross_dividends": str(metrics.gross_dividends),
                "withholding_tax": str(metrics.withholding_tax),
                "withholding_871m": str(metrics.withholding_871m),
                "interest": str(metrics.interest),
                "broker_fees": str(metrics.broker_fees),
                "advisor_fees": str(metrics.advisor_fees),
                "other_fees": str(metrics.other_fees),
                "transaction_tax": str(metrics.transaction_tax),
                "trade_commissions": str(metrics.trade_commissions),
                "trade_notional": str(metrics.trade_notional),
                "recurring_trade_commissions": str(metrics.recurring_trade_commissions),
                "recurring_trade_notional": str(metrics.recurring_trade_notional),
                "recurring_fill_count": metrics.recurring_fill_count,
                "nonrecurring_trade_commissions": str(metrics.nonrecurring_trade_commissions),
                "nonrecurring_trade_notional": str(metrics.nonrecurring_trade_notional),
                "nonrecurring_fill_count": metrics.nonrecurring_fill_count,
                "corporate_action_count": metrics.corporate_action_count,
                "recurring_effective_commission_rate": (
                    None
                    if metrics.recurring_effective_commission_rate is None
                    else str(metrics.recurring_effective_commission_rate)
                ),
                "nonrecurring_effective_commission_rate": (
                    None
                    if metrics.nonrecurring_effective_commission_rate is None
                    else str(metrics.nonrecurring_effective_commission_rate)
                ),
            },
            "source_sha256": self.source_sha256,
        }


def _recurring_instructions(account_info: ET.Element | None) -> tuple[Decimal, ...]:
    if account_info is None:
        return ()
    raw = account_info.attrib.get("recurringTransactions", "")
    amounts: list[Decimal] = []
    for segment in raw.split(";"):
        parts = [part for part in segment.split("~") if part]
        if len(parts) < 7 or parts[0] != "SHARE_PURCHASE":
            continue
        try:
            amounts.append(_decimal(parts[6]))
        except FlexQueryReportError:
            continue
    return tuple(amounts)


def parse_flex_query_report(xml_text: str) -> FlexQueryReportSummary:
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise FlexQueryReportError("Flex XML is empty")
    if len(xml_text.encode("utf-8")) > 200_000_000:
        raise FlexQueryReportError("Flex XML exceeds the parser limit")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FlexQueryReportError("Flex XML is not well formed") from exc
    if root.tag != "FlexQueryResponse":
        raise FlexQueryReportError("expected FlexQueryResponse root")
    statements = root.findall(".//FlexStatement")
    if not statements:
        raise FlexQueryReportError("Flex report contains no statements")
    statements.sort(key=lambda item: item.attrib.get("toDate", ""))
    latest = statements[-1]
    latest_date = latest.attrib.get("toDate") or latest.attrib.get("fromDate")
    if not latest_date:
        raise FlexQueryReportError("latest statement has no report date")

    account_info = latest.find(".//AccountInformation")
    account_id = latest.attrib.get("accountId") or (
        None if account_info is None else account_info.attrib.get("accountId")
    )
    equity_rows = latest.findall(".//EquitySummaryByReportDateInBase")
    if not equity_rows:
        raise FlexQueryReportError("latest statement lacks EquitySummaryByReportDateInBase")
    equity_rows.sort(key=lambda item: item.attrib.get("reportDate", ""))
    equity = equity_rows[-1]

    positions: list[FlexQueryPosition] = []
    for row in latest.findall(".//OpenPosition"):
        symbol = str(row.attrib.get("symbol") or row.attrib.get("underlyingSymbol") or "").strip().upper()
        quantity = _decimal(row.attrib.get("position") or row.attrib.get("quantity"))
        if not symbol or quantity == 0:
            continue
        positions.append(
            FlexQueryPosition(
                symbol=symbol,
                quantity=quantity,
                market_value=_decimal(row.attrib.get("positionValue") or row.attrib.get("marketValue")),
                cost_basis=_decimal(row.attrib.get("costBasisMoney") or row.attrib.get("costBasis")),
                unrealized_pnl=_decimal(row.attrib.get("fifoPnlUnrealized") or row.attrib.get("unrealizedPnl")),
                currency=row.attrib.get("currency"),
                asset_category=row.attrib.get("assetCategory"),
            )
        )
    positions.sort(key=lambda item: (-abs(item.market_value), item.symbol))

    change_fields = {
        "realized_pnl": "realized",
        "change_in_unrealized_pnl": "changeInUnrealized",
        "deposits_withdrawals": "depositsWithdrawals",
        "gross_dividends": "dividends",
        "withholding_tax": "withholdingTax",
        "withholding_871m": "withholding871m",
        "interest": "interest",
        "broker_fees": "brokerFees",
        "advisor_fees": "advisorFees",
        "other_fees": "otherFees",
        "transaction_tax": "transactionTax",
    }
    aggregates = {key: Decimal("0") for key in change_fields}
    all_trades: list[ET.Element] = []
    corporate_action_count = 0
    for statement in statements:
        change = statement.find("./ChangeInNAV")
        if change is not None:
            for key, attribute in change_fields.items():
                aggregates[key] += _decimal(change.attrib.get(attribute))
        all_trades.extend(statement.findall(".//Trade"))
        corporate_action_count += len(statement.findall(".//CorporateAction"))

    recurring_notional = Decimal("0")
    recurring_commission = Decimal("0")
    nonrecurring_notional = Decimal("0")
    nonrecurring_commission = Decimal("0")
    recurring_count = 0
    nonrecurring_count = 0
    for trade in all_trades:
        notional = abs(_decimal(trade.attrib.get("tradeMoney") or trade.attrib.get("proceeds")))
        commission = abs(_decimal(trade.attrib.get("ibCommission") or trade.attrib.get("commission")))
        if trade.attrib.get("exchange") == "IBRECINV":
            recurring_count += 1
            recurring_notional += notional
            recurring_commission += commission
        else:
            nonrecurring_count += 1
            nonrecurring_notional += notional
            nonrecurring_commission += commission

    recurring_amounts = _recurring_instructions(account_info)
    metrics = FlexQueryPeriodMetrics(
        realized_pnl=aggregates["realized_pnl"],
        change_in_unrealized_pnl=aggregates["change_in_unrealized_pnl"],
        deposits_withdrawals=aggregates["deposits_withdrawals"],
        gross_dividends=aggregates["gross_dividends"],
        withholding_tax=aggregates["withholding_tax"],
        withholding_871m=aggregates["withholding_871m"],
        interest=aggregates["interest"],
        broker_fees=aggregates["broker_fees"],
        advisor_fees=aggregates["advisor_fees"],
        other_fees=aggregates["other_fees"],
        transaction_tax=aggregates["transaction_tax"],
        trade_commissions=recurring_commission + nonrecurring_commission,
        trade_notional=recurring_notional + nonrecurring_notional,
        recurring_trade_commissions=recurring_commission,
        recurring_trade_notional=recurring_notional,
        recurring_fill_count=recurring_count,
        nonrecurring_trade_commissions=nonrecurring_commission,
        nonrecurring_trade_notional=nonrecurring_notional,
        nonrecurring_fill_count=nonrecurring_count,
        corporate_action_count=corporate_action_count,
    )
    return FlexQueryReportSummary(
        query_name=str(root.attrib.get("queryName") or "").strip(),
        statement_count=len(statements),
        generated_at=latest.attrib.get("whenGenerated"),
        latest_report_date=latest_date,
        account_id_hash=_account_hash(account_id),
        base_currency=equity.attrib.get("currency") or latest.attrib.get("currency"),
        tax_lot_matching_method=(
            None if account_info is None else account_info.attrib.get("taxLotMatchingMethod")
        ),
        recurring_instruction_count=len(recurring_amounts),
        recurring_instruction_amounts=recurring_amounts,
        net_liquidation_value=_decimal(equity.attrib.get("total")),
        cash=_decimal(equity.attrib.get("cash")),
        stock_market_value=_decimal(equity.attrib.get("stock")),
        dividend_accruals=_decimal(equity.attrib.get("dividendAccruals")),
        positions=tuple(positions),
        period_metrics=metrics,
        source_sha256=hashlib.sha256(xml_text.encode("utf-8")).hexdigest(),
    )


__all__ = [
    "FlexQueryPeriodMetrics",
    "FlexQueryPosition",
    "FlexQueryReportError",
    "FlexQueryReportSummary",
    "parse_flex_query_report",
]
