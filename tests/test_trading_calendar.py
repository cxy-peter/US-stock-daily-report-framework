import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from serenity_monitor.trading_calendar import (
    ExchangeSessionError,
    ExchangeSessionResolver,
)


@pytest.fixture(scope="module")
def resolver() -> ExchangeSessionResolver:
    return ExchangeSessionResolver()


def test_calendar_provenance_preserves_canonical_nasdaq_mic_and_pinned_version(resolver):
    xnas = resolver.provenance("XNAS")
    alias = resolver.provenance("NASDAQ")

    assert xnas.instrument_mic == "XNAS"
    assert alias.instrument_mic == "XNAS"
    assert xnas.calendar_name == alias.calendar_name == "XNYS"
    assert xnas.calendar_version == resolver.calendar_version == "4.13.2"
    assert xnas.exchange_timezone == "America/New_York"


def test_calendar_provenance_normalizes_nyse_arca_alias_to_canonical_mic(resolver):
    arcx = resolver.provenance("ARCX")
    alias = resolver.provenance("NYSEARCA")

    assert arcx.instrument_mic == "ARCX"
    assert alias.instrument_mic == "ARCX"
    assert arcx.calendar_name == alias.calendar_name == "XNYS"
    assert resolver.session_close("2026-07-31", "ARCX") == dt.datetime(
        2026, 7, 31, 20, 0, tzinfo=dt.timezone.utc
    )


def test_dst_spring_and_fall_change_utc_close_without_changing_local_close(resolver):
    assert resolver.session_close("2026-03-06", "XNAS") == dt.datetime(
        2026, 3, 6, 21, 0, tzinfo=dt.timezone.utc
    )
    assert resolver.session_close("2026-03-09", "XNAS") == dt.datetime(
        2026, 3, 9, 20, 0, tzinfo=dt.timezone.utc
    )
    assert resolver.session_close("2026-10-30", "XNAS") == dt.datetime(
        2026, 10, 30, 20, 0, tzinfo=dt.timezone.utc
    )
    assert resolver.session_close("2026-11-02", "XNAS") == dt.datetime(
        2026, 11, 2, 21, 0, tzinfo=dt.timezone.utc
    )


def test_early_closes_are_calendar_driven(resolver):
    assert resolver.session_close("2025-07-03", "XNAS") == dt.datetime(
        2025, 7, 3, 17, 0, tzinfo=dt.timezone.utc
    )
    assert resolver.session_close("2025-11-28", "XNAS") == dt.datetime(
        2025, 11, 28, 18, 0, tzinfo=dt.timezone.utc
    )


def test_shanghai_boundaries_for_july_third_and_black_friday_early_closes(resolver):
    shanghai = ZoneInfo("Asia/Shanghai")

    assert resolver.last_completed_session(
        dt.datetime(2025, 7, 4, 0, 59, 59, tzinfo=shanghai),
        "XNAS",
    ) == dt.date(2025, 7, 2)
    assert resolver.last_completed_session(
        dt.datetime(2025, 7, 4, 1, 0, tzinfo=shanghai),
        "XNAS",
    ) == dt.date(2025, 7, 3)

    assert resolver.unsettled_sessions(
        "2025-11-25",
        dt.datetime(2025, 11, 29, 1, 59, 59, tzinfo=shanghai),
        "XNAS",
    ) == (dt.date(2025, 11, 26),)
    assert resolver.unsettled_sessions(
        "2025-11-25",
        dt.datetime(2025, 11, 29, 2, 0, tzinfo=shanghai),
        "XNAS",
    ) == (dt.date(2025, 11, 26), dt.date(2025, 11, 28))


def test_2026_july_third_is_not_a_session(resolver):
    with pytest.raises(ExchangeSessionError, match="must be an exchange session"):
        resolver.session_close("2026-07-03", "XNAS")

    assert resolver.last_completed_session(
        dt.datetime(2026, 7, 3, 23, 0, tzinfo=dt.timezone.utc), "XNAS"
    ) == dt.date(2026, 7, 2)


def test_shanghai_saturday_resolves_friday_close(resolver):
    as_of = dt.datetime(
        2026, 8, 1, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert resolver.last_completed_session(as_of, "XNAS") == dt.date(2026, 7, 31)


def test_session_is_not_completed_until_its_close_in_dst_seasons(resolver):
    before_spring_close = dt.datetime(
        2026, 3, 9, 15, 59, 59, tzinfo=ZoneInfo("America/New_York")
    )
    at_spring_close = dt.datetime(
        2026, 3, 9, 16, 0, tzinfo=ZoneInfo("America/New_York")
    )
    before_fall_close = dt.datetime(
        2026, 11, 2, 15, 59, 59, tzinfo=ZoneInfo("America/New_York")
    )
    at_fall_close = dt.datetime(
        2026, 11, 2, 16, 0, tzinfo=ZoneInfo("America/New_York")
    )

    assert resolver.last_completed_session(before_spring_close, "XNAS") == dt.date(2026, 3, 6)
    assert resolver.last_completed_session(at_spring_close, "XNAS") == dt.date(2026, 3, 9)
    assert resolver.last_completed_session(before_fall_close, "XNAS") == dt.date(2026, 10, 30)
    assert resolver.last_completed_session(at_fall_close, "XNAS") == dt.date(2026, 11, 2)


def test_unsettled_sessions_returns_ordered_backlog_and_then_empty(resolver):
    as_of = dt.datetime(
        2026, 8, 1, 13, 15, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert resolver.unsettled_sessions("2026-07-28", as_of, "XNAS") == (
        dt.date(2026, 7, 29),
        dt.date(2026, 7, 30),
        dt.date(2026, 7, 31),
    )
    assert resolver.unsettled_sessions("2026-07-31", as_of, "XNAS") == ()


def test_unsettled_sessions_rejects_missing_non_session_and_reverse_state(resolver):
    as_of = dt.datetime(2026, 8, 1, 5, 15, tzinfo=dt.timezone.utc)

    for invalid in (None, "", "not-a-date"):
        with pytest.raises(ExchangeSessionError):
            resolver.unsettled_sessions(invalid, as_of, "XNAS")
    with pytest.raises(ExchangeSessionError, match="must be an exchange session"):
        resolver.unsettled_sessions("2026-07-26", as_of, "XNAS")
    with pytest.raises(ExchangeSessionError, match="later than"):
        resolver.unsettled_sessions("2026-08-03", as_of, "XNAS")


def test_naive_as_of_and_unknown_mic_fail_closed(resolver):
    with pytest.raises(ExchangeSessionError, match="timezone-aware"):
        resolver.last_completed_session(dt.datetime(2026, 8, 1, 5, 15), "XNAS")
    with pytest.raises(ExchangeSessionError, match="unsupported exchange MIC"):
        resolver.last_completed_session(
            dt.datetime(2026, 8, 1, 5, 15, tzinfo=dt.timezone.utc), "XHKG"
        )
    with pytest.raises(ExchangeSessionError, match="unsupported exchange MIC"):
        resolver.provenance("")
