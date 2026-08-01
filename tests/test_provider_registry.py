from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
import requests

from serenity_monitor.provider_registry import (
    AlphaVantageCloseProvider,
    CloseAcceptancePolicy,
    CloseObservation,
    CloseProviderError,
    InstrumentRef,
    ProviderRegistry,
    RetryPolicy,
    TwelveDataCloseProvider,
)


SESSION = date(2026, 7, 31)
RETRIEVED_AT = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"HTTP {self.status_code}",
                response=self,
            )


class FakeSession:
    def __init__(self, outcomes: list[FakeResponse | Exception]) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class StubProvider:
    def __init__(
        self,
        provider_id: str,
        observations: CloseObservation | dict[str, CloseObservation] | Exception,
    ) -> None:
        self.provider_id = provider_id
        self.observations = observations
        self.calls: list[tuple[str, date]] = []
        representative = (
            next(iter(observations.values()))
            if isinstance(observations, dict)
            else observations
        )
        if isinstance(representative, CloseObservation):
            self.provider_version = representative.provider_version
            self.independence_group = representative.independence_group
            self.source_tier = representative.source_tier
            self.settlement_eligible = representative.settlement_eligible
        else:
            self.provider_version = "test-v1"
            self.independence_group = provider_id
            self.source_tier = "primary"
            self.settlement_eligible = True

    def fetch_close(
        self,
        instrument: InstrumentRef,
        expected_session: date,
    ) -> CloseObservation:
        self.calls.append((instrument.canonical_symbol, expected_session))
        if isinstance(self.observations, Exception):
            raise self.observations
        if isinstance(self.observations, dict):
            return self.observations[instrument.canonical_symbol]
        return self.observations


def _instrument(symbol: str = "DEMO_EQ") -> InstrumentRef:
    return InstrumentRef(
        canonical_symbol=symbol,
        asset_type="etf",
        exchange_mic="XNAS",
        currency="USD",
        calendar_id="XNYS",
        provider_symbols={
            "twelve_data": symbol,
            "alpha_vantage": symbol,
        },
    )


def _observation(
    provider_id: str,
    price: str,
    *,
    symbol: str = "DEMO_EQ",
    provider_symbol: str | None = None,
    independence_group: str | None = None,
    source_tier: str = "primary",
    session_date: date = SESSION,
    currency: str = "USD",
    exchange_mic: str = "XNAS",
    bar_kind: str = "regular_session_close",
    adjustment_mode: str = "none",
    price_unit_multiplier: str = "1",
    settlement_eligible: bool = True,
    provider_drift_status: str = "healthy",
    exchange_timezone: str = "America/New_York",
    is_mock: bool = False,
    is_snapshot: bool = False,
) -> CloseObservation:
    return CloseObservation(
        provider_id=provider_id,
        provider_version="test-v1",
        independence_group=independence_group or provider_id,
        source_tier=source_tier,
        settlement_eligible=settlement_eligible,
        canonical_symbol=symbol,
        provider_symbol=provider_symbol or symbol,
        asset_type="etf",
        exchange_mic=exchange_mic,
        exchange_mic_provenance="provider_meta",
        calendar_id="XNYS",
        session_date=session_date,
        raw_close=Decimal(price),
        currency=currency,
        currency_provenance="provider_meta",
        exchange_timezone=exchange_timezone,
        bar_kind=bar_kind,
        adjustment_mode=adjustment_mode,
        price_unit_multiplier=Decimal(price_unit_multiplier),
        retrieved_at="2026-08-01T08:00:00Z",
        payload_sha256=(provider_id.encode().hex() + "0" * 64)[:64],
        corporate_action_status="not_checked",
        provider_drift_status=provider_drift_status,
        is_mock=is_mock,
        is_snapshot=is_snapshot,
    )


def _registry(*observations: CloseObservation, **policy_kwargs: Any) -> ProviderRegistry:
    providers = [StubProvider(item.provider_id, item) for item in observations]
    return ProviderRegistry(
        providers,
        policy=CloseAcceptancePolicy(**policy_kwargs),
        clock=lambda: RETRIEVED_AT,
    )


def test_twelve_data_requests_exact_raw_regular_session_close_and_parses_lineage():
    session = FakeSession(
        [
            FakeResponse(
                {
                    "meta": {
                        "symbol": "DEMO_EQ",
                        "interval": "1day",
                        "currency": "USD",
                        "exchange": "NASDAQ",
                        "mic_code": "XNAS",
                        "exchange_timezone": "America/New_York",
                    },
                    "values": [
                        {
                            "datetime": SESSION.isoformat(),
                            "open": "99.00",
                            "high": "101.00",
                            "low": "98.00",
                            "close": "100.25",
                            "volume": "123456",
                        }
                    ],
                    "status": "ok",
                }
            )
        ]
    )
    provider = TwelveDataCloseProvider(
        session=session,
        environ={"TWELVE_DATA_API_KEY": "test-key"},
        clock=lambda: RETRIEVED_AT,
        retry_policy=RetryPolicy(max_attempts=1),
    )

    observation = provider.fetch_close(_instrument(), SESSION)

    url, kwargs = session.calls[0]
    assert url == "https://api.twelvedata.com/time_series"
    assert kwargs["params"]["symbol"] == "DEMO_EQ"
    assert kwargs["params"]["interval"] == "1day"
    assert kwargs["params"]["date"] == SESSION.isoformat()
    assert kwargs["params"]["adjust"] == "none"
    assert kwargs["params"]["prepost"] == "false"
    assert kwargs["params"]["timezone"] == "Exchange"
    assert kwargs["params"]["outputsize"] in {1, "1"}
    assert kwargs["params"]["order"] == "ASC"
    assert kwargs["params"]["apikey"] == "test-key"
    assert kwargs["timeout"] > 0
    assert observation.provider_id == "twelve_data"
    assert observation.session_date == SESSION
    assert observation.raw_close == Decimal("100.25")
    assert observation.currency == "USD"
    assert observation.exchange_mic == "XNAS"
    assert observation.adjustment_mode == "none"
    assert observation.bar_kind == "regular_session_close"
    assert observation.payload_sha256


def test_twelve_data_rejects_schema_drift_to_a_non_daily_interval():
    provider = TwelveDataCloseProvider(
        session=FakeSession(
            [
                FakeResponse(
                    {
                        "meta": {
                            "symbol": "DEMO_EQ",
                            "interval": "1h",
                            "currency": "USD",
                            "mic_code": "XNAS",
                            "exchange_timezone": "America/New_York",
                        },
                        "values": [
                            {"datetime": SESSION.isoformat(), "close": "100.25"}
                        ],
                        "status": "ok",
                    }
                )
            ]
        ),
        environ={"TWELVE_DATA_API_KEY": "test-key"},
        clock=lambda: RETRIEVED_AT,
        retry_policy=RetryPolicy(max_attempts=1),
    )

    with pytest.raises(RuntimeError, match="interval"):
        provider.fetch_close(_instrument(), SESSION)


def test_alpha_vantage_requests_raw_daily_endpoint_and_requires_exact_date():
    session = FakeSession(
        [
            FakeResponse(
                {
                    "Meta Data": {
                        "1. Information": "Daily Prices (open, high, low, close) and Volumes",
                        "2. Symbol": "DEMO_EQ",
                        "3. Last Refreshed": SESSION.isoformat(),
                    },
                    "Time Series (Daily)": {
                        SESSION.isoformat(): {
                            "1. open": "99.00",
                            "2. high": "101.00",
                            "3. low": "98.00",
                            "4. close": "100.20",
                            "5. volume": "123456",
                        }
                    },
                }
            )
        ]
    )
    provider = AlphaVantageCloseProvider(
        session=session,
        environ={"ALPHA_VANTAGE_API_KEY": "test-key"},
        clock=lambda: RETRIEVED_AT,
        retry_policy=RetryPolicy(max_attempts=1),
    )

    observation = provider.fetch_close(_instrument(), SESSION)

    url, kwargs = session.calls[0]
    assert url == "https://www.alphavantage.co/query"
    assert kwargs["params"] == {
        "function": "TIME_SERIES_DAILY",
        "symbol": "DEMO_EQ",
        "outputsize": "compact",
        "datatype": "json",
        "apikey": "test-key",
    }
    assert kwargs["timeout"] > 0
    assert observation.provider_id == "alpha_vantage"
    assert observation.session_date == SESSION
    assert observation.raw_close == Decimal("100.20")
    assert observation.exchange_mic == "XNAS"
    assert observation.currency == "USD"
    assert observation.adjustment_mode == "none"


@pytest.mark.parametrize(
    "payload",
    [
        {"Error Message": "Invalid API call"},
        {"Note": "API call frequency limit reached"},
        {"Information": "API rate limit reached"},
        {
            "Meta Data": {"2. Symbol": "DEMO_EQ"},
            "Time Series (Daily)": {
                "2026-07-30": {"4. close": "100.00"},
            },
        },
    ],
)
def test_alpha_vantage_error_payloads_and_wrong_session_are_never_success(payload):
    provider = AlphaVantageCloseProvider(
        session=FakeSession([FakeResponse(payload)]),
        environ={"ALPHA_VANTAGE_API_KEY": "top-secret-key"},
        clock=lambda: RETRIEVED_AT,
        retry_policy=RetryPolicy(max_attempts=1),
    )

    with pytest.raises(RuntimeError) as exc_info:
        provider.fetch_close(_instrument(), SESSION)

    assert "top-secret-key" not in str(exc_info.value)


def test_missing_api_key_fails_before_network_and_never_leaks_environment(monkeypatch):
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    session = FakeSession([])
    provider = TwelveDataCloseProvider(
        session=session,
        environ={},
        clock=lambda: RETRIEVED_AT,
        retry_policy=RetryPolicy(max_attempts=1),
    )

    with pytest.raises(RuntimeError, match="(?i)(api key|credential|configured)"):
        provider.fetch_close(_instrument(), SESSION)

    assert session.calls == []


def test_two_independent_sources_within_30_bps_accept_primary_without_averaging():
    result = _registry(
        _observation("twelve_data", "100.00"),
        _observation("alpha_vantage", "100.20"),
    ).resolve(_instrument(), SESSION)

    assert result.status == "accepted"
    assert result.finality == "confirmed"
    assert result.valuation_permitted is True
    assert result.price_gate_permitted is True
    assert result.independent_source_count == 2
    assert result.agreement_bps <= Decimal("30")
    selected = next(
        item for item in result.observations
        if item.observation_id == result.selected_observation_id
    )
    assert selected.provider_id == "twelve_data"
    assert result.selected_price == Decimal("100.00")
    assert result.selected_price != Decimal("100.10")


def test_30_to_75_bps_is_warning_and_blocks_settlement_by_default():
    result = _registry(
        _observation("twelve_data", "100.00"),
        _observation("alpha_vantage", "100.50"),
    ).resolve(_instrument(), SESSION)

    assert result.status == "warning"
    assert result.valuation_permitted is True
    assert result.price_gate_permitted is False
    assert Decimal("30") < result.agreement_bps <= Decimal("75")


def test_over_75_bps_blocks_close_instead_of_averaging_or_choosing_a_winner():
    result = _registry(
        _observation("twelve_data", "100.00"),
        _observation("alpha_vantage", "101.00"),
    ).resolve(_instrument(), SESSION)

    assert result.status == "blocked"
    assert result.finality == "blocked"
    assert result.valuation_permitted is False
    assert result.price_gate_permitted is False
    assert result.agreement_bps > Decimal("75")
    assert result.selected_price is None


def test_one_eligible_source_is_provisional_valuation_only():
    result = _registry(
        _observation("twelve_data", "100.00"),
    ).resolve(_instrument(), SESSION)

    assert result.status == "degraded"
    assert result.valuation_permitted is True
    assert result.price_gate_permitted is False
    assert result.independent_source_count == 1
    assert result.selected_price == Decimal("100.00")


def test_two_feeds_in_same_independence_group_count_as_one_source():
    result = _registry(
        _observation("feed_a", "100.00", independence_group="same_vendor"),
        _observation("feed_b", "100.10", independence_group="same_vendor"),
    ).resolve(_instrument(), SESSION)

    assert result.status == "degraded"
    assert result.independent_source_count == 1
    assert result.price_gate_permitted is False


@pytest.mark.parametrize(
    ("registered_field", "registered_value"),
    [
        ("provider_version", "trusted-v1"),
        ("independence_group", "trusted_upstream"),
        ("source_tier", "secondary"),
        ("settlement_eligible", False),
    ],
)
def test_observation_cannot_forge_registered_provider_lineage(
    registered_field,
    registered_value,
):
    provider = StubProvider(
        "twelve_data",
        _observation("twelve_data", "100.00"),
    )
    setattr(provider, registered_field, registered_value)
    registry = ProviderRegistry([provider], clock=lambda: RETRIEVED_AT)

    result = registry.resolve(_instrument(), SESSION)

    assert result.observations == ()
    assert result.price_gate_permitted is False
    assert result.attempts[0].status == "lineage_mismatch"


def test_registry_rejects_duplicate_provider_ids_before_collection():
    observation = _observation("twelve_data", "100.00")

    with pytest.raises(ValueError, match="unique"):
        ProviderRegistry(
            [
                StubProvider("twelve_data", observation),
                StubProvider("twelve_data", observation),
            ],
            clock=lambda: RETRIEVED_AT,
        )


def test_registry_sanitizes_untrusted_provider_error_status_and_detail():
    provider = StubProvider(
        "twelve_data",
        CloseProviderError(
            "secret-key-in-status",
            "https://example.invalid/query?apikey=secret-key-in-detail",
        ),
    )
    registry = ProviderRegistry([provider], clock=lambda: RETRIEVED_AT)

    result = registry.resolve(_instrument(), SESSION)

    serialized = repr(result.attempts)
    assert "secret-key" not in serialized
    assert result.attempts[0].status == "provider_error"
    assert result.attempts[0].detail == "twelve_data: provider request failed"


@pytest.mark.parametrize(
    "invalid",
    [
        _observation("bad_session", "100", session_date=date(2026, 7, 30)),
        _observation("bad_currency", "100", currency="EUR"),
        _observation("bad_adjustment", "100", adjustment_mode="splits"),
        _observation("bad_bar", "100", bar_kind="after_hours_close"),
        _observation("bad_unit", "100", price_unit_multiplier="100"),
        _observation("bad_symbol", "100", provider_symbol="NOT_THE_SECURITY"),
        _observation("missing_timezone", "100", exchange_timezone=""),
        _observation("display_only", "100", settlement_eligible=False),
        _observation("mock", "100", source_tier="mock", is_mock=True),
        _observation("snapshot", "100", source_tier="snapshot", is_snapshot=True),
        _observation("quarantined", "100", provider_drift_status="quarantined"),
    ],
    ids=[
        "wrong-session",
        "wrong-currency",
        "adjusted",
        "after-hours",
        "wrong-unit",
        "wrong-provider-symbol",
        "missing-timezone",
        "not-settlement-eligible",
        "mock",
        "snapshot",
        "quarantined",
    ],
)
def test_invalid_lineage_can_never_make_a_close_settlement_eligible(invalid):
    valid = _observation("valid", "100.00")
    result = _registry(valid, invalid).resolve(_instrument(), SESSION)

    assert result.independent_source_count == 1
    assert result.price_gate_permitted is False
    assert result.reasons


def test_registry_caches_successful_provider_observation_by_symbol_and_session():
    provider = StubProvider("twelve_data", _observation("twelve_data", "100.00"))
    registry = ProviderRegistry(
        [provider],
        policy=CloseAcceptancePolicy(),
        clock=lambda: RETRIEVED_AT,
    )

    first = registry.resolve(_instrument(), SESSION)
    second = registry.resolve(_instrument(), SESSION)

    assert first.accepted_close_id == second.accepted_close_id
    assert provider.calls == [("DEMO_EQ", SESSION)]


def test_registry_does_not_cache_structurally_invalid_observation():
    provider = StubProvider(
        "twelve_data",
        _observation("twelve_data", "100.00", provider_symbol="WRONG"),
    )
    registry = ProviderRegistry(
        [provider],
        policy=CloseAcceptancePolicy(),
        clock=lambda: RETRIEVED_AT,
    )

    registry.resolve(_instrument(), SESSION)
    registry.resolve(_instrument(), SESSION)

    assert provider.calls == [("DEMO_EQ", SESSION), ("DEMO_EQ", SESSION)]


def test_cache_identity_includes_the_configured_provider_symbol():
    observations = {
        "DEMO_EQ": _observation("twelve_data", "100.00"),
    }
    provider = StubProvider("twelve_data", observations)
    registry = ProviderRegistry(
        [provider],
        policy=CloseAcceptancePolicy(),
        clock=lambda: RETRIEVED_AT,
    )
    first_instrument = _instrument()
    second_instrument = InstrumentRef(
        canonical_symbol="DEMO_EQ",
        asset_type="etf",
        exchange_mic="XNAS",
        currency="USD",
        calendar_id="XNYS",
        provider_symbols={"twelve_data": "DEMO_EQ_ALT"},
    )

    registry.resolve(first_instrument, SESSION)
    registry.resolve(second_instrument, SESSION)

    assert provider.calls == [("DEMO_EQ", SESSION), ("DEMO_EQ", SESSION)]


def test_three_source_agreement_uses_max_pairwise_spread_not_primary_pair_only():
    result = _registry(
        _observation("primary", "100.00"),
        _observation("near", "100.10"),
        _observation("far", "100.80"),
    ).resolve(_instrument(), SESSION)

    assert result.independent_source_count == 3
    assert result.agreement_bps > Decimal("75")
    assert result.status == "blocked"
    assert result.price_gate_permitted is False


def test_batch_is_atomic_when_every_symbol_has_an_accepted_close():
    a = _instrument("DEMO_A")
    b = _instrument("DEMO_B")
    p1 = StubProvider(
        "twelve_data",
        {
            "DEMO_A": _observation("twelve_data", "100", symbol="DEMO_A"),
            "DEMO_B": _observation("twelve_data", "50", symbol="DEMO_B"),
        },
    )
    p2 = StubProvider(
        "alpha_vantage",
        {
            "DEMO_A": _observation("alpha_vantage", "100.1", symbol="DEMO_A"),
            "DEMO_B": _observation("alpha_vantage", "50.1", symbol="DEMO_B"),
        },
    )
    registry = ProviderRegistry(
        [p1, p2],
        policy=CloseAcceptancePolicy(),
        clock=lambda: RETRIEVED_AT,
    )

    batch = registry.accept_batch([a, b], SESSION)

    assert batch.status == "accepted"
    assert batch.price_gate_permitted is True
    assert set(batch.by_symbol) == {"DEMO_A", "DEMO_B"}
    assert all(item.price_gate_permitted for item in batch.by_symbol.values())
    assert all(item.eligible_for_ledger_input for item in batch.by_symbol.values())


def test_batch_id_and_close_order_are_canonical_across_input_order():
    a = _instrument("DEMO_A")
    b = _instrument("DEMO_B")
    p1 = StubProvider(
        "twelve_data",
        {
            "DEMO_A": _observation("twelve_data", "100", symbol="DEMO_A"),
            "DEMO_B": _observation("twelve_data", "50", symbol="DEMO_B"),
        },
    )
    p2 = StubProvider(
        "alpha_vantage",
        {
            "DEMO_A": _observation("alpha_vantage", "100.1", symbol="DEMO_A"),
            "DEMO_B": _observation("alpha_vantage", "50.1", symbol="DEMO_B"),
        },
    )
    registry = ProviderRegistry([p1, p2], clock=lambda: RETRIEVED_AT)

    first = registry.resolve_batch([a, b], SESSION)
    second = registry.resolve_batch([b, a], SESSION)

    assert first.batch_id == second.batch_id
    assert [item.instrument.canonical_symbol for item in first.closes] == ["DEMO_A", "DEMO_B"]
    assert [item.instrument.canonical_symbol for item in second.closes] == ["DEMO_A", "DEMO_B"]


def test_batch_rejects_duplicate_instruments():
    instrument = _instrument("DEMO_A")
    registry = _registry(
        _observation("twelve_data", "100", symbol="DEMO_A"),
        _observation("alpha_vantage", "100.1", symbol="DEMO_A"),
    )

    with pytest.raises(ValueError, match="duplicate"):
        registry.resolve_batch([instrument, instrument], SESSION)


def test_batch_blocks_every_settlement_if_any_symbol_fails_close_gate():
    a = _instrument("DEMO_A")
    b = _instrument("DEMO_B")
    p1 = StubProvider(
        "twelve_data",
        {
            "DEMO_A": _observation("twelve_data", "100", symbol="DEMO_A"),
            "DEMO_B": _observation("twelve_data", "50", symbol="DEMO_B"),
        },
    )
    p2 = StubProvider(
        "alpha_vantage",
        {
            "DEMO_A": _observation("alpha_vantage", "100.1", symbol="DEMO_A"),
            "DEMO_B": _observation("alpha_vantage", "51", symbol="DEMO_B"),
        },
    )
    registry = ProviderRegistry(
        [p1, p2],
        policy=CloseAcceptancePolicy(),
        clock=lambda: RETRIEVED_AT,
    )

    batch = registry.accept_batch([a, b], SESSION)

    assert batch.status == "blocked"
    assert batch.price_gate_permitted is False
    assert batch.by_symbol["DEMO_A"].price_gate_permitted is True
    assert batch.by_symbol["DEMO_B"].price_gate_permitted is False
    assert all(
        item.atomic_batch_permitted is False
        and item.eligible_for_ledger_input is False
        for item in batch.by_symbol.values()
    )
    assert any("DEMO_B" in reason for reason in batch.reasons)


def test_accepted_close_and_batch_ids_are_deterministic_not_wall_clock_based():
    observations = (
        _observation("twelve_data", "100.00"),
        _observation("alpha_vantage", "100.20"),
    )
    first_registry = ProviderRegistry(
        [StubProvider(item.provider_id, item) for item in observations],
        policy=CloseAcceptancePolicy(),
        clock=lambda: datetime(2026, 8, 1, 8, tzinfo=timezone.utc),
    )
    second_registry = ProviderRegistry(
        [StubProvider(item.provider_id, item) for item in observations],
        policy=CloseAcceptancePolicy(),
        clock=lambda: datetime(2026, 8, 2, 8, tzinfo=timezone.utc),
    )

    first = first_registry.resolve(_instrument(), SESSION)
    second = second_registry.resolve(_instrument(), SESSION)
    first_batch = first_registry.accept_batch([_instrument()], SESSION)
    second_batch = second_registry.accept_batch([_instrument()], SESSION)

    assert first.accepted_close_id == second.accepted_close_id
    assert first_batch.batch_id == second_batch.batch_id


def test_decision_ids_ignore_full_response_hash_changes_for_the_same_close_row():
    first_observations = (
        _observation("twelve_data", "100.00"),
        _observation("alpha_vantage", "100.20"),
    )
    second_observations = tuple(
        replace(item, payload_sha256=("f" if index == 0 else "e") * 64)
        for index, item in enumerate(first_observations)
    )
    first_registry = ProviderRegistry(
        [StubProvider(item.provider_id, item) for item in first_observations],
        clock=lambda: RETRIEVED_AT,
    )
    second_registry = ProviderRegistry(
        [StubProvider(item.provider_id, item) for item in second_observations],
        clock=lambda: RETRIEVED_AT,
    )

    first_close = first_registry.resolve(_instrument(), SESSION)
    second_close = second_registry.resolve(_instrument(), SESSION)
    first_batch = first_registry.resolve_batch([_instrument()], SESSION)
    second_batch = second_registry.resolve_batch([_instrument()], SESSION)

    assert first_observations[0].payload_sha256 != second_observations[0].payload_sha256
    assert first_observations[0].observation_id == second_observations[0].observation_id
    assert first_close.accepted_close_id == second_close.accepted_close_id
    assert first_batch.batch_id == second_batch.batch_id


def test_observation_requires_a_timezone_aware_rfc3339_retrieval_timestamp():
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(
            _observation("twelve_data", "100.00"),
            retrieved_at="2026-08-01T08:00:00",
        )


@pytest.mark.parametrize(
    ("first_outcome", "expected_calls"),
    [
        (requests.Timeout("timed out"), 2),
        (FakeResponse({}, status_code=429), 2),
        (FakeResponse({}, status_code=500), 2),
        (FakeResponse({}, status_code=401), 1),
        (FakeResponse({"status": "error", "message": "invalid symbol"}), 1),
    ],
    ids=["timeout", "rate-limit", "server-error", "auth-error", "schema-error"],
)
def test_retry_is_bounded_and_only_for_timeout_429_or_5xx(
    first_outcome,
    expected_calls,
):
    success = FakeResponse(
        {
            "meta": {
                "symbol": "DEMO_EQ",
                "interval": "1day",
                "currency": "USD",
                "mic_code": "XNAS",
                "exchange_timezone": "America/New_York",
            },
            "values": [{"datetime": SESSION.isoformat(), "close": "100.00"}],
            "status": "ok",
        }
    )
    session = FakeSession([first_outcome, success])
    sleeps: list[float] = []
    provider = TwelveDataCloseProvider(
        session=session,
        environ={"TWELVE_DATA_API_KEY": "secret-key"},
        clock=lambda: RETRIEVED_AT,
        retry_policy=RetryPolicy(max_attempts=2),
        sleep=sleeps.append,
    )

    if expected_calls == 2:
        observation = provider.fetch_close(_instrument(), SESSION)
        assert observation.raw_close == Decimal("100.00")
        assert len(sleeps) == 1
    else:
        with pytest.raises(RuntimeError) as exc_info:
            provider.fetch_close(_instrument(), SESSION)
        assert "secret-key" not in str(exc_info.value)
        assert sleeps == []
    assert len(session.calls) == expected_calls
