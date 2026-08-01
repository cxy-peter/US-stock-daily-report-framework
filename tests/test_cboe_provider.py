from __future__ import annotations

import pytest
from datetime import date, timedelta

from serenity_monitor.data import CboeIndexProvider


class Response:
    today = date.today()
    yesterday = today - timedelta(days=1)
    text = (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        f"{yesterday:%m/%d/%Y},18.0,19.0,17.5,18.5\n"
        f"{today:%m/%d/%Y},19.0,20.0,18.0,19.5\n"
    )

    def raise_for_status(self):
        return None


class Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response()


def test_cboe_provider_uses_official_history_and_labels_lineage():
    session = Session()
    quote = CboeIndexProvider(session).get("^VIX", period="1y")
    assert quote.ticker == "^VIX"
    assert quote.price == 19.5
    assert quote.asset_type == "index"
    assert quote.source == "cboe"
    assert quote.as_of == date.today().isoformat()
    assert "cdn.cboe.com" in session.calls[0][0]
    assert session.calls[0][1]["timeout"] == 20


def test_cboe_provider_rejects_unapproved_symbols():
    with pytest.raises(ValueError, match="unsupported Cboe index"):
        CboeIndexProvider(Session()).get("^NOT_AN_INDEX")
