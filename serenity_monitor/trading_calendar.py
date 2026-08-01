"""Fail-closed exchange-session resolution for private daily accounting.

The public framework keeps an instrument's canonical ISO 10383 exchange MIC
as identity metadata.  Human-readable venue aliases are normalized before
provenance is emitted.
For U.S. equities, NASDAQ and NYSE share the holiday and regular-session
schedule represented by ``exchange_calendars``' XNYS calendar.  Mapping XNAS
to XNYS here therefore changes only the schedule implementation; it never
rewrites the instrument MIC.

All ``as_of`` values must be timezone-aware.  Callers receive standard-library
``date`` and aware ``datetime`` values so that pandas timestamps cannot leak
into the ledger contract.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final

import exchange_calendars as xcals


class ExchangeSessionError(ValueError):
    """Raised when a session cannot be resolved without making an assumption."""


@dataclass(frozen=True)
class CalendarProvenance:
    """Serializable identity for the calendar used for an instrument."""

    instrument_mic: str
    calendar_name: str
    calendar_version: str
    exchange_timezone: str


class ExchangeSessionResolver:
    """Resolve completed U.S. equity sessions against a pinned calendar.

    The supported MIC set is deliberately small.  Unknown venues fail closed
    instead of silently borrowing an unrelated exchange's hours.
    """

    _MIC_ALIASES: Final[dict[str, str]] = {
        "NYSE": "XNYS",
        "NASDAQ": "XNAS",
        "NYSEARCA": "ARCX",
        "CBOEBZX": "BATS",
    }
    _MIC_TO_CALENDAR: Final[dict[str, str]] = {
        "XNYS": "XNYS",
        "XNAS": "XNYS",
        "ARCX": "XNYS",
        "BATS": "XNYS",
    }

    def __init__(self) -> None:
        self._calendars: dict[str, object] = {}

    @property
    def calendar_version(self) -> str:
        """Return the exact exchange-calendars data/code version in use."""

        return str(xcals.__version__)

    def provenance(self, mic: str) -> CalendarProvenance:
        """Return schedule provenance while preserving the instrument MIC."""

        instrument_mic, calendar_name, calendar = self._resolve_calendar(mic)
        return CalendarProvenance(
            instrument_mic=instrument_mic,
            calendar_name=calendar_name,
            calendar_version=self.calendar_version,
            exchange_timezone=str(calendar.tz),
        )

    def last_completed_session(self, as_of: dt.datetime, mic: str) -> dt.date:
        """Return the latest session whose official close is not after ``as_of``."""

        instant = self._aware_utc(as_of)
        _, _, calendar = self._resolve_calendar(mic)
        local_date = instant.astimezone(calendar.tz).date()

        try:
            candidate = calendar.date_to_session(local_date, direction="previous")
            close_at = calendar.session_close(candidate).to_pydatetime()
            if close_at > instant:
                candidate = calendar.previous_session(candidate)
        except Exception as exc:  # exchange-calendars exposes several bound errors
            raise ExchangeSessionError(
                "as_of is outside the supported exchange-calendar range"
            ) from exc
        return candidate.date()

    def unsettled_sessions(
        self,
        last_settled: dt.date | str,
        as_of: dt.datetime,
        mic: str,
    ) -> tuple[dt.date, ...]:
        """Return completed sessions strictly after ``last_settled``.

        A missing/non-session checkpoint or a checkpoint later than the most
        recently completed session indicates ambiguous ledger state and raises
        instead of guessing a backlog start.
        """

        settled = self._session_date(last_settled, field_name="last_settled")
        latest = self.last_completed_session(as_of, mic)
        _, _, calendar = self._resolve_calendar(mic)

        try:
            is_session = bool(calendar.is_session(settled))
        except Exception as exc:
            raise ExchangeSessionError(
                "last_settled is outside the supported exchange-calendar range"
            ) from exc
        if not is_session:
            raise ExchangeSessionError("last_settled must be an exchange session")
        if settled > latest:
            raise ExchangeSessionError(
                "last_settled is later than the last completed session"
            )
        if settled == latest:
            return ()

        try:
            first_unsettled = calendar.next_session(settled)
            labels = calendar.sessions_in_range(first_unsettled, latest)
        except Exception as exc:
            raise ExchangeSessionError("could not resolve unsettled sessions") from exc
        return tuple(label.date() for label in labels)

    def session_close(self, session: dt.date | str, mic: str) -> dt.datetime:
        """Return the official close instant for one real session in UTC."""

        session_date = self._session_date(session, field_name="session")
        _, _, calendar = self._resolve_calendar(mic)
        try:
            if not calendar.is_session(session_date):
                raise ExchangeSessionError("session must be an exchange session")
            close_at = calendar.session_close(session_date).to_pydatetime()
        except ExchangeSessionError:
            raise
        except Exception as exc:
            raise ExchangeSessionError(
                "session is outside the supported exchange-calendar range"
            ) from exc
        if close_at.tzinfo is None or close_at.utcoffset() is None:
            raise ExchangeSessionError("calendar returned a naive close timestamp")
        return close_at.astimezone(dt.timezone.utc)

    def future_session_offsets(
        self,
        session: dt.date | str,
        offsets: tuple[int, ...] | list[int] | range,
        mic: str,
    ) -> dict[int, dt.date]:
        """Resolve positive trading-session offsets from one real session.

        Offset ``1`` is the immediately following exchange session.  Calendar
        labels, not civil-day arithmetic, determine every result.  The method
        intentionally rejects duplicate, boolean, zero, and negative offsets
        so callers cannot silently reinterpret a horizon contract.
        """

        anchor = self._session_date(session, field_name="session")
        if isinstance(offsets, (str, bytes)):
            raise ExchangeSessionError("offsets must be positive integers")
        try:
            supplied = tuple(offsets)
        except TypeError as exc:
            raise ExchangeSessionError("offsets must be an iterable of positive integers") from exc
        if not supplied:
            raise ExchangeSessionError("offsets may not be empty")
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in supplied):
            raise ExchangeSessionError("offsets must be positive integers")
        if len(supplied) != len(set(supplied)):
            raise ExchangeSessionError("offsets may not contain duplicates")

        _, _, calendar = self._resolve_calendar(mic)
        try:
            if not calendar.is_session(anchor):
                raise ExchangeSessionError("session must be an exchange session")
            wanted = set(supplied)
            resolved: dict[int, dt.date] = {}
            cursor = anchor
            for offset in range(1, max(supplied) + 1):
                cursor = calendar.next_session(cursor)
                if offset in wanted:
                    resolved[offset] = cursor.date()
        except ExchangeSessionError:
            raise
        except Exception as exc:
            raise ExchangeSessionError(
                "session offsets are outside the supported exchange-calendar range"
            ) from exc
        return {offset: resolved[offset] for offset in sorted(resolved)}

    def _resolve_calendar(self, mic: str) -> tuple[str, str, object]:
        supplied_mic = str(mic).strip().upper()
        instrument_mic = self._MIC_ALIASES.get(supplied_mic, supplied_mic)
        calendar_name = self._MIC_TO_CALENDAR.get(instrument_mic)
        if calendar_name is None:
            raise ExchangeSessionError(f"unsupported exchange MIC: {instrument_mic or '<empty>'}")
        calendar = self._calendars.get(calendar_name)
        if calendar is None:
            try:
                calendar = xcals.get_calendar(calendar_name)
            except Exception as exc:
                raise ExchangeSessionError(
                    f"calendar is unavailable for exchange MIC: {instrument_mic}"
                ) from exc
            self._calendars[calendar_name] = calendar
        return instrument_mic, calendar_name, calendar

    @staticmethod
    def _aware_utc(value: dt.datetime) -> dt.datetime:
        if not isinstance(value, dt.datetime):
            raise ExchangeSessionError("as_of must be a timezone-aware datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ExchangeSessionError("as_of must be a timezone-aware datetime")
        return value.astimezone(dt.timezone.utc)

    @staticmethod
    def _session_date(value: dt.date | str, *, field_name: str) -> dt.date:
        if isinstance(value, dt.datetime):
            raise ExchangeSessionError(f"{field_name} must be a date, not a datetime")
        if isinstance(value, dt.date):
            return value
        if not isinstance(value, str) or not value.strip():
            raise ExchangeSessionError(f"{field_name} must be an ISO YYYY-MM-DD date")
        try:
            parsed = dt.date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ExchangeSessionError(
                f"{field_name} must be an ISO YYYY-MM-DD date"
            ) from exc
        return parsed


__all__ = [
    "CalendarProvenance",
    "ExchangeSessionError",
    "ExchangeSessionResolver",
]
