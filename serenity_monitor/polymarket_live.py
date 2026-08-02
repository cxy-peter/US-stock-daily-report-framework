"""Read-only pre-resolution Polymarket research signals.

Public Gamma and CLOB endpoints are used for market discovery, prices, spread,
order-book depth and history.  No wallet, authentication or order endpoint is
implemented.  A live market price is treated as an aggregated, noisy forecast
and market-sentiment measure—not as an objective probability or an independent
trade trigger.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import requests


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
MAX_LIVE_POLYMARKET_DECISION_WEIGHT = 0.03


class PolymarketDataError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code).strip().casefold() or "polymarket_data_error"
        super().__init__(self.code)


@dataclass(frozen=True)
class PricePoint:
    timestamp: dt.datetime
    probability: float


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class LiveMarketSnapshot:
    market_id: str
    question: str
    slug: str
    token_id: str
    topic: str
    resolution_source: str
    observed_at: dt.datetime
    end_date: dt.datetime | None
    probability: float
    spread: float | None
    liquidity: float | None
    volume: float | None
    open_interest: float | None
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    price_history: tuple[PricePoint, ...]
    asset_sensitivities: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveMarketSignal:
    market_id: str
    question: str
    topic: str
    probability: float
    change_1h: float | None
    change_6h: float | None
    change_24h: float | None
    change_7d: float | None
    velocity_score: float
    entropy: float
    orderbook_imbalance: float | None
    spread: float | None
    reliability: float
    time_to_resolution_hours: float | None
    sentiment_direction: float
    weighted_signal: float
    asset_scores: Mapping[str, float]
    status: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class LivePolymarketResult:
    status: str
    as_of: str
    market_count: int
    signals: tuple[LiveMarketSignal, ...]
    topic_scores: Mapping[str, float]
    asset_scores: Mapping[str, float]
    uncertainty_score: float
    decision_score_contribution: float
    risk_budget_multiplier: float
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "as_of": self.as_of,
            "market_count": self.market_count,
            "signals": [signal.__dict__ for signal in self.signals],
            "topic_scores": dict(self.topic_scores),
            "asset_scores": dict(self.asset_scores),
            "uncertainty_score": self.uncertainty_score,
            "decision_score_contribution": self.decision_score_contribution,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def _clamp(value: float, lower: float, upper: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("value must be finite")
    return min(max(value, lower), upper)


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _utc(value: dt.datetime | str | int | float) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
    else:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _safe_url(url: str, allowed_host: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != allowed_host:
        raise ValueError("unexpected Polymarket endpoint")
    return url


def _probability_at(history: Sequence[PricePoint], as_of: dt.datetime, lookback: dt.timedelta) -> float | None:
    target = as_of - lookback
    candidates = [point for point in history if point.timestamp <= target]
    if not candidates:
        return None
    return candidates[-1].probability


def _change(current: float, prior: float | None) -> float | None:
    return None if prior is None else current - prior


def _binary_entropy(probability: float) -> float:
    p = _clamp(probability, 1e-9, 1.0 - 1e-9)
    return -(p * math.log(p, 2) + (1.0 - p) * math.log(1.0 - p, 2))


def _book_imbalance(bids: Sequence[BookLevel], asks: Sequence[BookLevel], levels: int = 5) -> float | None:
    bid_size = sum(level.size for level in bids[:levels])
    ask_size = sum(level.size for level in asks[:levels])
    total = bid_size + ask_size
    return None if total <= 1e-12 else (bid_size - ask_size) / total


def _current_probability(
    outcome_prices: Sequence[Any],
    bids: Sequence[BookLevel],
    asks: Sequence[BookLevel],
    last_trade: float | None,
) -> float:
    if bids and asks:
        return _clamp((max(level.price for level in bids) + min(level.price for level in asks)) / 2.0, 0.0, 1.0)
    if outcome_prices:
        value = _float(outcome_prices[0])
        if value is not None:
            return _clamp(value, 0.0, 1.0)
    if last_trade is not None:
        return _clamp(last_trade, 0.0, 1.0)
    raise PolymarketDataError("market_probability_missing")


class PolymarketPublicClient:
    """Read-only public market-data client; no trading methods are exposed."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        user_agent: str = "serenity-polymarket-research/1.0",
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        self.user_agent = str(user_agent)

    def _get(self, url: str, *, params: Mapping[str, Any] | None = None) -> Any:
        allowed = "gamma-api.polymarket.com" if url.startswith(GAMMA_BASE) else "clob.polymarket.com"
        _safe_url(url, allowed)
        try:
            response = self.session.get(
                url,
                params=params,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            if len(response.content) > 25_000_000:
                raise PolymarketDataError("response_too_large")
            return response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            raise PolymarketDataError("public_market_request_failed") from exc

    def list_markets(
        self,
        *,
        limit: int = 100,
        active: bool = True,
        closed: bool = False,
        tag_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit), 500)),
            "active": str(bool(active)).casefold(),
            "closed": str(bool(closed)).casefold(),
        }
        if tag_id:
            params["tag_id"] = str(tag_id)
        data = self._get(f"{GAMMA_BASE}/markets", params=params)
        if not isinstance(data, list):
            raise PolymarketDataError("markets_payload_invalid")
        return [row for row in data if isinstance(row, Mapping)]

    def market_by_slug(self, slug: str) -> Mapping[str, Any]:
        data = self._get(f"{GAMMA_BASE}/markets/slug/{str(slug).strip()}")
        if not isinstance(data, Mapping):
            raise PolymarketDataError("market_payload_invalid")
        return data

    def price_history(
        self,
        token_id: str,
        *,
        start: dt.datetime | None = None,
        end: dt.datetime | None = None,
        fidelity_minutes: int = 60,
    ) -> tuple[PricePoint, ...]:
        params: dict[str, Any] = {
            "market": str(token_id),
            "interval": "all",
            "fidelity": max(1, min(int(fidelity_minutes), 1_440)),
        }
        if start:
            params["startTs"] = int(_utc(start).timestamp())
        if end:
            params["endTs"] = int(_utc(end).timestamp())
        data = self._get(f"{CLOB_BASE}/prices-history", params=params)
        rows = data.get("history") if isinstance(data, Mapping) else None
        if not isinstance(rows, list):
            return ()
        points: list[PricePoint] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            probability = _float(row.get("p"))
            timestamp = row.get("t")
            if probability is None or timestamp is None:
                continue
            points.append(PricePoint(_utc(timestamp), _clamp(probability, 0.0, 1.0)))
        return tuple(sorted(points, key=lambda item: item.timestamp))

    def order_book(self, token_id: str) -> tuple[tuple[BookLevel, ...], tuple[BookLevel, ...], float | None]:
        data = self._get(f"{CLOB_BASE}/book", params={"token_id": str(token_id)})
        if not isinstance(data, Mapping):
            return (), (), None
        def levels(key: str, reverse: bool) -> tuple[BookLevel, ...]:
            result: list[BookLevel] = []
            for row in data.get(key) or []:
                if not isinstance(row, Mapping):
                    continue
                price, size = _float(row.get("price")), _float(row.get("size"))
                if price is None or size is None or size < 0:
                    continue
                result.append(BookLevel(_clamp(price, 0.0, 1.0), size))
            return tuple(sorted(result, key=lambda item: item.price, reverse=reverse))
        return levels("bids", True), levels("asks", False), _float(data.get("last_trade_price"))

    def spread(self, token_id: str) -> float | None:
        data = self._get(f"{CLOB_BASE}/spread", params={"token_id": str(token_id)})
        return _float(data.get("spread")) if isinstance(data, Mapping) else None

    def snapshot(
        self,
        market: Mapping[str, Any],
        *,
        topic: str,
        asset_sensitivities: Mapping[str, float] | None = None,
        history_days: int = 14,
    ) -> LiveMarketSnapshot:
        token_ids = [str(item) for item in _json_list(market.get("clobTokenIds"))]
        if not token_ids:
            raise PolymarketDataError("yes_token_missing")
        yes_token = token_ids[0]
        now = dt.datetime.now(dt.timezone.utc)
        bids, asks, last_trade = self.order_book(yes_token)
        spread = self.spread(yes_token)
        history = self.price_history(
            yes_token,
            start=now - dt.timedelta(days=max(1, min(history_days, 365))),
            end=now,
        )
        probability = _current_probability(
            _json_list(market.get("outcomePrices")), bids, asks, last_trade
        )
        end_date = None
        if market.get("endDate"):
            try:
                end_date = _utc(str(market["endDate"]))
            except ValueError:
                end_date = None
        return LiveMarketSnapshot(
            market_id=str(market.get("id") or market.get("conditionId") or yes_token),
            question=str(market.get("question") or "").strip(),
            slug=str(market.get("slug") or "").strip(),
            token_id=yes_token,
            topic=str(topic).casefold(),
            resolution_source=str(market.get("resolutionSource") or "").strip(),
            observed_at=now,
            end_date=end_date,
            probability=probability,
            spread=spread,
            liquidity=_float(market.get("liquidity")),
            volume=_float(market.get("volume")),
            open_interest=_float(market.get("openInterest")),
            bids=bids,
            asks=asks,
            price_history=history,
            asset_sensitivities={
                str(key).upper(): _clamp(float(value), -1.0, 1.0)
                for key, value in dict(asset_sensitivities or {}).items()
            },
        )


def score_live_market(
    snapshot: LiveMarketSnapshot,
    *,
    baseline_probability: float = 0.50,
    calibration_multiplier: float = 0.50,
) -> LiveMarketSignal:
    """Score one unresolved market as a bounded sentiment/forecast input."""

    current = _clamp(snapshot.probability, 0.0, 1.0)
    history = tuple(sorted(snapshot.price_history, key=lambda item: item.timestamp))
    as_of = snapshot.observed_at
    change_1h = _change(current, _probability_at(history, as_of, dt.timedelta(hours=1)))
    change_6h = _change(current, _probability_at(history, as_of, dt.timedelta(hours=6)))
    change_24h = _change(current, _probability_at(history, as_of, dt.timedelta(hours=24)))
    change_7d = _change(current, _probability_at(history, as_of, dt.timedelta(days=7)))
    changes = [value for value in (change_1h, change_6h, change_24h, change_7d) if value is not None]
    velocity = 0.0
    if changes:
        weights = [0.35, 0.30, 0.25, 0.10][: len(changes)]
        velocity = sum(value * weight for value, weight in zip(changes, weights)) / sum(weights)
    velocity_score = _clamp(velocity / 0.20, -1.0, 1.0)

    spread = snapshot.spread
    spread_quality = 0.50 if spread is None else _clamp(1.0 - spread / 0.15, 0.0, 1.0)
    liquidity = snapshot.liquidity or 0.0
    volume = snapshot.volume or 0.0
    depth = sum(level.size for level in snapshot.bids[:5]) + sum(level.size for level in snapshot.asks[:5])
    liquidity_quality = _clamp(math.log1p(max(liquidity, volume * 0.10, depth)) / 14.0, 0.0, 1.0)
    imbalance = _book_imbalance(snapshot.bids, snapshot.asks)
    resolution_hours = None
    resolution_quality = 1.0
    warnings: list[str] = []
    if snapshot.end_date is not None:
        resolution_hours = (snapshot.end_date - snapshot.observed_at).total_seconds() / 3_600.0
        if resolution_hours < 0:
            resolution_quality = 0.0
            warnings.append("Market end date has passed; live signal is invalid.")
        elif resolution_hours < 6:
            resolution_quality = 0.45
            warnings.append("Market is very near resolution and more exposed to microstructure noise.")
        elif resolution_hours < 24:
            resolution_quality = 0.70
    calibration = _clamp(calibration_multiplier, 0.0, 1.0)
    reliability = _clamp(
        0.30 * spread_quality
        + 0.30 * liquidity_quality
        + 0.20 * resolution_quality
        + 0.20 * calibration,
        0.0,
        1.0,
    )
    surprise = current - _clamp(baseline_probability, 0.0, 1.0)
    imbalance_component = 0.0 if imbalance is None else 0.15 * imbalance
    sentiment_direction = _clamp(
        0.55 * (surprise / 0.50) + 0.30 * velocity_score + imbalance_component,
        -1.0,
        1.0,
    )
    weighted_signal = sentiment_direction * reliability
    asset_scores = {
        asset: round(weighted_signal * sensitivity, 6)
        for asset, sensitivity in snapshot.asset_sensitivities.items()
    }
    if spread is not None and spread > 0.10:
        warnings.append("Bid-ask spread exceeds 10 percentage points; signal is heavily discounted.")
    if liquidity_quality < 0.25:
        warnings.append("Liquidity/depth is low; price may not represent broad belief aggregation.")
    warnings.extend(
        [
            "Market prices are noisy aggregated forecasts, not objective probabilities.",
            "The live signal can modify research priority or risk caution but cannot independently trade.",
        ]
    )
    status = "active" if reliability >= 0.55 and resolution_quality > 0 else (
        "research_only" if reliability >= 0.25 and resolution_quality > 0 else "blocked"
    )
    return LiveMarketSignal(
        market_id=snapshot.market_id,
        question=snapshot.question,
        topic=snapshot.topic,
        probability=round(current, 6),
        change_1h=None if change_1h is None else round(change_1h, 6),
        change_6h=None if change_6h is None else round(change_6h, 6),
        change_24h=None if change_24h is None else round(change_24h, 6),
        change_7d=None if change_7d is None else round(change_7d, 6),
        velocity_score=round(velocity_score, 6),
        entropy=round(_binary_entropy(current), 6),
        orderbook_imbalance=None if imbalance is None else round(imbalance, 6),
        spread=None if spread is None else round(spread, 6),
        reliability=round(reliability, 6),
        time_to_resolution_hours=None if resolution_hours is None else round(resolution_hours, 4),
        sentiment_direction=round(sentiment_direction, 6),
        weighted_signal=round(weighted_signal, 6),
        asset_scores=asset_scores,
        status=status,
        warnings=tuple(warnings),
    )


def aggregate_live_markets(
    signals: Iterable[LiveMarketSignal],
    *,
    max_decision_weight: float = MAX_LIVE_POLYMARKET_DECISION_WEIGHT,
) -> LivePolymarketResult:
    """Aggregate unresolved markets without pretending they are independent."""

    rows = list(signals)
    active = [row for row in rows if row.status in {"active", "research_only"}]
    topic_num: dict[str, float] = {}
    topic_den: dict[str, float] = {}
    asset_num: dict[str, float] = {}
    asset_den: dict[str, float] = {}
    for row in active:
        weight = row.reliability * (1.0 if row.status == "active" else 0.25)
        topic_num[row.topic] = topic_num.get(row.topic, 0.0) + row.weighted_signal * weight
        topic_den[row.topic] = topic_den.get(row.topic, 0.0) + weight
        for asset, score in row.asset_scores.items():
            asset_num[asset] = asset_num.get(asset, 0.0) + score * weight
            asset_den[asset] = asset_den.get(asset, 0.0) + weight
    topic_scores = {
        key: round(_clamp(topic_num[key] / max(topic_den[key], 1e-12), -1.0, 1.0), 6)
        for key in sorted(topic_num)
    }
    asset_scores = {
        key: round(_clamp(asset_num[key] / max(asset_den[key], 1e-12), -1.0, 1.0), 6)
        for key in sorted(asset_num)
    }
    aggregate = (
        0.0
        if not topic_scores
        else sum(topic_scores.values()) / max(len(topic_scores), 1)
    )
    uncertainty = (
        1.0
        if not active
        else sum(row.entropy * (1.0 - row.reliability) for row in active) / len(active)
    )
    cap = _clamp(max_decision_weight, 0.0, MAX_LIVE_POLYMARKET_DECISION_WEIGHT)
    contribution = _clamp(aggregate * cap * (1.0 - 0.50 * uncertainty), -cap, cap)
    # Pre-resolution markets can tighten risk more than they can expand it.
    risk_multiplier = 1.0 + (0.01 if contribution >= 0 else 0.05) * contribution / max(cap, 1e-12)
    risk_multiplier = _clamp(risk_multiplier, 0.95, 1.01)
    warnings = (
        "Live Polymarket data is one correlated prediction-market evidence group.",
        "Liquidity, spread, order-book depth, time-to-resolution and calibration determine weight.",
        "No public market-data method in this module can place an order.",
    )
    return LivePolymarketResult(
        status="ok" if active else "blocked",
        as_of=dt.datetime.now(dt.timezone.utc).isoformat(),
        market_count=len(rows),
        signals=tuple(sorted(rows, key=lambda row: abs(row.weighted_signal) * row.reliability, reverse=True)),
        topic_scores=topic_scores,
        asset_scores=asset_scores,
        uncertainty_score=round(_clamp(uncertainty, 0.0, 1.0), 6),
        decision_score_contribution=round(contribution, 6),
        risk_budget_multiplier=round(risk_multiplier, 6),
        warnings=warnings,
    )


__all__ = [
    "BookLevel",
    "LiveMarketSignal",
    "LiveMarketSnapshot",
    "LivePolymarketResult",
    "MAX_LIVE_POLYMARKET_DECISION_WEIGHT",
    "PolymarketDataError",
    "PolymarketPublicClient",
    "PricePoint",
    "aggregate_live_markets",
    "score_live_market",
]
