from __future__ import annotations

from decimal import Decimal

from serenity_monitor.ibkr_flex_query_report import parse_flex_query_report


XML = """<FlexQueryResponse queryName="gpt_us_stock_report" type="AF">
<FlexStatements count="2">
<FlexStatement accountId="U123" fromDate="20260730" toDate="20260730" whenGenerated="20260801;020000">
<AccountInformation accountId="U123" taxLotMatchingMethod="FIFO" recurringTransactions="~SHARE_PURCHASE~CASH~DAILY~2026-08-03~9999-12-31~USD~20.0;~SHARE_PURCHASE~CASH~DAILY~2026-08-03~9999-12-31~USD~20.0" />
<EquitySummaryInBase><EquitySummaryByReportDateInBase currency="USD" reportDate="20260730" cash="100" stock="900" dividendAccruals="1" total="1001" /></EquitySummaryInBase>
<ChangeInNAV realized="10" changeInUnrealized="-2" depositsWithdrawals="500" dividends="3" withholdingTax="-0.3" withholding871m="0" interest="0.1" brokerFees="0" advisorFees="0" otherFees="0" transactionTax="0" />
<OpenPositions><OpenPosition symbol="AAA" position="2" positionValue="200" costBasisMoney="180" fifoPnlUnrealized="20" currency="USD" assetCategory="STK" /></OpenPositions>
<Trades><Trade symbol="AAA" exchange="IBRECINV" tradeMoney="20" ibCommission="-0.2" /></Trades>
<CorporateActions></CorporateActions>
</FlexStatement>
<FlexStatement accountId="U123" fromDate="20260731" toDate="20260731" whenGenerated="20260802;022747">
<AccountInformation accountId="U123" taxLotMatchingMethod="FIFO" recurringTransactions="~SHARE_PURCHASE~CASH~DAILY~2026-08-03~9999-12-31~USD~20.0;~SHARE_PURCHASE~CASH~DAILY~2026-08-03~9999-12-31~USD~20.0" />
<EquitySummaryInBase><EquitySummaryByReportDateInBase currency="USD" reportDate="20260731" cash="120" stock="980" dividendAccruals="0.8" total="1100.8" /></EquitySummaryInBase>
<ChangeInNAV realized="5" changeInUnrealized="4" depositsWithdrawals="0" dividends="2" withholdingTax="-0.2" withholding871m="0" interest="0" brokerFees="0" advisorFees="0" otherFees="0" transactionTax="0" />
<OpenPositions>
<OpenPosition symbol="AAA" position="2.5" positionValue="260" costBasisMoney="225" fifoPnlUnrealized="35" currency="USD" assetCategory="STK" />
<OpenPosition symbol="BBB" position="5" positionValue="720" costBasisMoney="700" fifoPnlUnrealized="20" currency="USD" assetCategory="STK" />
</OpenPositions>
<Trades><Trade symbol="BBB" exchange="NYSE" tradeMoney="1000" ibCommission="-0.5" /></Trades>
<CorporateActions><CorporateAction symbol="AAA" type="Split" /></CorporateActions>
</FlexStatement>
</FlexStatements></FlexQueryResponse>"""


def test_parse_real_flex_query_shape_and_aggregate_costs():
    report = parse_flex_query_report(XML)
    assert report.query_name == "gpt_us_stock_report"
    assert report.statement_count == 2
    assert report.latest_report_date == "20260731"
    assert report.account_id_hash != "U123"
    assert report.tax_lot_matching_method == "FIFO"
    assert report.recurring_instruction_count == 2
    assert report.recurring_instruction_amounts == (Decimal("20.0"), Decimal("20.0"))
    assert report.net_liquidation_value == Decimal("1100.8")
    assert report.cash == Decimal("120")
    assert report.stock_market_value == Decimal("980")
    assert [item.symbol for item in report.positions] == ["BBB", "AAA"]

    metrics = report.period_metrics
    assert metrics.realized_pnl == Decimal("15")
    assert metrics.change_in_unrealized_pnl == Decimal("2")
    assert metrics.deposits_withdrawals == Decimal("500")
    assert metrics.gross_dividends == Decimal("5")
    assert metrics.withholding_tax == Decimal("-0.5")
    assert metrics.trade_commissions == Decimal("0.7")
    assert metrics.trade_notional == Decimal("1020")
    assert metrics.recurring_fill_count == 1
    assert metrics.recurring_effective_commission_rate == Decimal("0.01")
    assert metrics.nonrecurring_effective_commission_rate == Decimal("0.0005")
    assert metrics.corporate_action_count == 1


def test_private_dict_never_contains_raw_account_identifier():
    payload = parse_flex_query_report(XML).to_private_dict()
    assert "U123" not in str(payload)
    assert payload["period_metrics"]["recurring_effective_commission_rate"] == "0.01"
