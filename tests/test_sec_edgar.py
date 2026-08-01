from __future__ import annotations

from serenity_monitor.sec_edgar import collect_sec_filings


class FakeResponse:
    def __init__(self, payload: dict, status_error: Exception | None = None):
        self.payload = payload
        self.status_error = status_error

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get(self, *_args, **_kwargs):
        return next(self.responses)


def test_sec_filings_are_primary_source_ready():
    ticker_map = {"0": {"ticker": "TEST", "cik_str": 123456}}
    submission = {
        "filings": {
            "recent": {
                "form": ["10-Q", "4"],
                "filingDate": ["2026-07-30", "2026-07-30"],
                "accessionNumber": ["0000123456-26-000001", "other"],
                "primaryDocument": ["test-20260730.htm", "x.xml"],
            }
        }
    }
    result = collect_sec_filings(
        [{"ticker": "TEST", "framework": "serenity_stock"}],
        session=FakeSession([FakeResponse(ticker_map), FakeResponse(submission)]),
        user_agent="test test@example.com",
        lookback_days=100_000,
        request_pause_seconds=0,
    )
    assert result.status == "ok"
    assert len(result.filings) == 1
    assert result.filings[0].form == "10-Q"
    assert "sec.gov/Archives" in result.filings[0].url


def test_sec_failure_is_visible_not_silent_no_filings():
    result = collect_sec_filings(
        [{"ticker": "TEST", "framework": "serenity_stock"}],
        session=FakeSession(
            [FakeResponse({}, status_error=RuntimeError("SEC unavailable"))]
        ),
        user_agent="test test@example.com",
        request_pause_seconds=0,
    )
    assert result.status == "error"
    assert "failed" in result.detail
    assert result.filings == []
