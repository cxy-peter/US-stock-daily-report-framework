from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from serenity_monitor.ibkr_flex import (
    FlexError,
    FlexParseError,
    FlexPending,
    IBKRFlexClient,
    parse_flex_statement,
    parse_request_response,
    reconcile_flex_snapshot,
)


REQUEST_SUCCESS = """<?xml version="1.0" encoding="UTF-8"?>
<FlexStatementResponse>
  <Status>Success</Status>
  <ReferenceCode>1234567890</ReferenceCode>
  <url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</url>
</FlexStatementResponse>
"""

STATEMENT = """<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Owner Daily">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" fromDate="20260731" toDate="20260731" currency="USD">
      <AccountInformation accountId="U1234567" currency="USD" buyingPower="2000" />
      <EquitySummaryInBase netLiquidationValue="59464" endingCash="2004" realizedPnL="25" unrealizedPnL="-3107" />
      <OpenPositions>
        <OpenPosition symbol="MU" conid="123" assetCategory="STK" currency="USD" position="5.3384" costBasisMoney="680" markPrice="127.4" positionValue="680.1" fifoPnlUnrealized="135.28" />
        <OpenPosition symbol="VOO" conid="456" assetCategory="STK" currency="USD" position="5.2178" costBasisMoney="3260" markPrice="659.5" positionValue="3441" fifoPnlUnrealized="180.28" />
      </OpenPositions>
      <Trades>
        <Trade tradeID="t1" transactionID="x1" symbol="MU" conid="123" assetCategory="STK" currency="USD" tradeDate="20260731" buySell="BUY" quantity="0.1" tradePrice="127.4" proceeds="-12.74" ibCommission="-0.01" />
      </Trades>
      <CashTransactions>
        <CashTransaction transactionID="c1" type="Dividends" symbol="VOO" currency="USD" amount="5.25" dateTime="20260731" description="Dividend" />
      </CashTransactions>
      <Fees>
        <Fee transactionID="f1" type="Regulatory Fee" symbol="MU" currency="USD" amount="-0.02" dateTime="20260731" description="Synthetic fee" />
      </Fees>
      <CorporateActions>
        <CorporateAction transactionID="ca1" type="Split" symbol="MU" conid="123" quantity="0" amount="0" currency="USD" dateTime="20260731" description="Synthetic test action" />
      </CorporateActions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


def test_parse_request_response_and_reject_untrusted_url():
    now = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    ticket = parse_request_response(REQUEST_SUCCESS, requested_at=now)
    assert ticket.reference_code == "1234567890"
    assert ticket.response_url.startswith("https://ndcdyn.interactivebrokers.com/")

    malicious = REQUEST_SUCCESS.replace(
        "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement",
        "https://example.com/steal",
    )
    with pytest.raises(FlexParseError, match="response_url_not_allowed"):
        parse_request_response(malicious, requested_at=now)


def test_request_failure_and_pending_are_sanitized():
    pending = (
        "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1019</ErrorCode>"
        "</FlexStatementResponse>"
    )
    with pytest.raises(FlexPending):
        parse_request_response(
            pending, requested_at=dt.datetime.now(dt.timezone.utc)
        )
    failure = (
        "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode>"
        "<ErrorMessage>Token has expired.</ErrorMessage></FlexStatementResponse>"
    )
    with pytest.raises(FlexError, match="request_failed_1012"):
        parse_request_response(
            failure, requested_at=dt.datetime.now(dt.timezone.utc)
        )


def test_parse_statement_redacts_account_and_extracts_facts():
    snapshots = parse_flex_statement(STATEMENT)
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.account_id_hash.startswith("sha256:")
    assert "U1234567" not in snapshot.account_id_hash
    assert snapshot.net_liquidation_value == Decimal("59464")
    assert snapshot.ending_cash == Decimal("2004")
    assert snapshot.buying_power == Decimal("2000")
    assert [item.symbol for item in snapshot.positions] == ["MU", "VOO"]
    assert snapshot.positions[0].quantity == Decimal("5.3384")
    assert snapshot.positions[0].cost_basis == Decimal("680")
    assert len(snapshot.trades) == 1
    assert len(snapshot.cash_transactions) == 1
    assert len(snapshot.fees) == 1
    assert snapshot.fees[0].amount == Decimal("-0.02")
    assert len(snapshot.corporate_actions) == 1
    public = snapshot.public_summary()
    assert public["position_count"] == 2
    assert public["fee_count"] == 1
    assert "accountId" not in public


def test_reconciliation_never_mutates_and_reports_mismatch():
    snapshot = parse_flex_statement(STATEMENT)[0]
    result = reconcile_flex_snapshot(
        snapshot,
        local_positions={"MU": Decimal("5.3384"), "VOO": Decimal("5.0"), "SCHD": 1},
        local_cash=Decimal("1900"),
    )
    codes = {issue.code for issue in result.issues}
    assert "QUANTITY_MISMATCH" in codes
    assert "MISSING_LOCAL_EVENT" in codes
    assert "CASH_MISMATCH" in codes
    assert "NEED_OWNER_CONFIRMATION" in codes
    assert not result.matched
    assert not result.automatic_ledger_mutation_permitted


def test_matching_snapshot_without_fees_or_actions_is_matched():
    clean_xml = STATEMENT.replace(
        '<CorporateActions>\n        <CorporateAction transactionID="ca1" type="Split" symbol="MU" conid="123" quantity="0" amount="0" currency="USD" dateTime="20260731" description="Synthetic test action" />\n      </CorporateActions>',
        "",
    ).replace(
        '<Fees>\n        <Fee transactionID="f1" type="Regulatory Fee" symbol="MU" currency="USD" amount="-0.02" dateTime="20260731" description="Synthetic fee" />\n      </Fees>',
        "",
    )
    snapshot = parse_flex_statement(clean_xml)[0]
    result = reconcile_flex_snapshot(
        snapshot,
        local_positions={"MU": "5.3384", "VOO": "5.2178"},
        local_cash="2004",
    )
    assert result.status == "MATCHED"
    assert result.matched
    assert result.issues == ()


class _Response:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.responses.pop(0))


def test_client_uses_environment_style_inputs_and_bounded_polling():
    pending = (
        "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1019</ErrorCode>"
        "</FlexStatementResponse>"
    )
    session = _Session([REQUEST_SUCCESS, pending, STATEMENT])
    sleeps = []
    client = IBKRFlexClient(
        token="secret-token",
        query_id="123",
        session=session,
        sleeper=sleeps.append,
        clock=lambda: dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
    )
    snapshots = client.fetch_statement(max_attempts=2, initial_delay_seconds=0.5)
    assert len(snapshots) == 1
    assert sleeps == [0.5]
    assert session.calls[0][1]["params"]["t"] == "secret-token"
    assert "secret-token" not in repr(client)
