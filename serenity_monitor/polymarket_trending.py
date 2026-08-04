"""Official Polymarket trending topics and both-side probabilities.

The Gamma API supplies active events/markets sorted by official 24-hour volume.
Each market's `outcomes` and `outcomePrices` arrays are parsed one-to-one so the
report can show both Yes/No probabilities (or the leading outcomes in a
multi-outcome event).  The result is a bounded market-sentiment context layer;
it does not trade and it does not treat probability as objective truth.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import requests


GAMMA_BASE = "https://gamma-api.polymarket.com"
MAX_POLYMARKET_TREND_RISK_REDUCTION = 0.06
MAX_POLYMARKET_TREND_RISK_EXPANSION = 0.01
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-]{1,}", re.I)
_STOPWORDS = frozenset(
    {
        "will", "the", "a", "an", "of", "to", "in", "on", "by", "before",
        "after", "at", "for", "from", "and", "or", "be", "is", "are", "this",
        "that", "with", "than", "over", "under", "end", "during", "any",
        "market", "event", "yes", "no", "win", "winner", "happen", "reach",
        "above", "below", "more", "less", "between", "2026", "2027", "2028",
    }
)
_KEYWORD_PHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Fed", ("federal reserve", "fed decision", "fomc")),
    ("rate cut", ("rate cut", "cut rates", "lower interest rates")),
    ("rate hike", ("rate hike", "raise rates", "increase interest rates")),
    ("inflation", ("inflation", "cpi", "pce")),
    ("recession", ("recession", "economic contraction", "hard landing")),
    ("soft landing", ("soft landing",)),
    ("tariff", ("tariff", "import duty", "trade war")),
    ("trade deal", ("trade deal", "trade agreement")),
    ("government shutdown", ("government shutdown",)),
    ("default", ("default", "debt ceiling breach")),
    ("oil", ("oil", "crude", "brent", "wti", "opec")),
    ("Middle East", ("iran", "israel", "gaza", "hormuz", "red sea")),
    ("war/ceasefire", ("war", "invasion", "military strike", "ceasefire")),
    ("China", ("china", "chinese", "taiwan")),
    ("AI", ("artificial intelligence", " ai ", "ai model")),
    ("semiconductor", ("semiconductor", "chip", "gpu")),
    ("crypto", ("bitcoin", "crypto", "ethereum", "stablecoin")),
    ("election", ("election", "president", "senate", "house seat")),
)
_RISK_RULES: tuple[tuple[str, tuple[str, ...], float, str], ...] = (
    ("recession", ("recession", "hard landing", "economic contraction"), -1.0, "macro_growth"),
    ("rate_hike", ("rate hike", "raise rates", "increase interest rates"), -0.9, "rates"),
    ("high_inflation", ("inflation above", "cpi above", "pce above"), -0.8, "inflation"),
    ("tariff", ("tariff", "trade war", "import duty"), -0.8, "trade"),
    ("shutdown", ("government shutdown",), -0.6, "fiscal"),
    ("default", ("default", "debt ceiling breach"), -1.0, "fiscal"),
    ("war", ("invasion", "military strike", "war with", "escalation"), -0.9, "geopolitics"),
    ("ceasefire", ("ceasefire", "peace deal"), 0.7, "geopolitics"),
    ("trade_deal", ("trade deal", "trade agreement"), 0.6, "trade"),
    ("rate_cut", ("rate cut", "cut rates", "lower interest rates"), 0.35, "rates"),
    ("soft_landing", ("soft landing", "no recession"), 0.6, "macro_growth"),
    ("oil_spike", ("oil above", "brent above", "wti above"), -0.7, "energy_inflation"),
)
_FINANCIAL_TERMS = frozenset(
    {
        "fed", "rate", "rates", "inflation", "cpi", "pce", "recession",
        "economy", "economic", "tariff", "trade", "oil", "crude", "opec",
        "war", "ceasefire", "iran", "israel", "china", "taiwan", "bitcoin",
        "crypto", "ai", "semiconductor", "chip", "shutdown", "default",
        "treasury", "yield", "unemployment", "jobs", "election",
    }
)


def _clamp(value: float, lower: float, upper: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        return lower
    return min(max(number, lower), upper)


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _safe_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "gamma-api.polymarket.com":
        raise ValueError("unexpected Polymarket endpoint")
    return url


def _volume(row: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        value = _float(row.get(key))
        if value is not None and value >= 0:
            return value
    return 0.0


def _keywords(text: str) -> tuple[str, ...]:
    padded = f" {text.casefold()} "
    selected: list[str] = []
    for label, phrases in _KEYWORD_PHRASES:
        if any(phrase in padded for phrase in phrases):
            selected.append(label)
    if selected:
        return tuple(dict.fromkeys(selected))
    tokens = [
        token.casefold()
        for token in _TOKEN_RE.findall(text)
        if token.casefold() not in _STOPWORDS
    ]
    return tuple(dict.fromkeys(tokens[:4]))


def _risk_rule(text: str) -> tuple[str, float, str] | None:
    padded = f" {text.casefold()} "
    for label, phrases, direction, topic in _RISK_RULES:
        if any(phrase in padded for phrase in phrases):
            return label, direction, topic
    return None


def _financially_relevant(text: str) -> bool:
    tokens = {token.casefold() for token in _TOKEN_RE.findall(text)}
    return bool(tokens & _FINANCIAL_TERMS) or _risk_rule(text) is not None


@dataclass(frozen=True)
class OutcomeProbability:
    outcome: str
    probability: float
    token_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class TrendingMarket:
    event_id: str
    market_id: str
    event_title: str
    question: str
    slug: str
    observed_at: str
    end_date: str | None
    outcomes: tuple[OutcomeProbability, ...]
    yes_probability: float | None
    no_probability: float | None
    leading_outcome: str
    leading_probability: float
    volume_24h: float
    liquidity: float
    hot_keywords: tuple[str, ...]
    risk_rule: str | None
    risk_topic: str | None
    risk_direction: float
    sentiment_score: float
    probability_sum_error: float | None
    data_quality: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "outcomes": [row.to_dict() for row in self.outcomes],
            "hot_keywords": list(self.hot_keywords),
        }


@dataclass(frozen=True)
class PolymarketTrendingResult:
    status: str
    as_of: str
    event_count: int
    market_count: int
    markets: tuple[TrendingMarket, ...]
    hot_keywords: Mapping[str, float]
    risk_topic_scores: Mapping[str, float]
    aggregate_sentiment: float
    uncertainty_score: float
    risk_budget_multiplier: float
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "as_of": self.as_of,
            "event_count": self.event_count,
            "market_count": self.market_count,
            "markets": [row.to_dict() for row in self.markets],
            "hot_keywords": dict(self.hot_keywords),
            "risk_topic_scores": dict(self.risk_topic_scores),
            "aggregate_sentiment": self.aggregate_sentiment,
            "uncertainty_score": self.uncertainty_score,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


class PolymarketTrendingClient:
    """Read-only official Gamma API client with no order methods."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        user_agent: str = "serenity-polymarket-trending/1.0",
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = float(timeout)
        self.user_agent = str(user_agent)

    def top_events(self, *, limit: int = 30) -> list[Mapping[str, Any]]:
        url = _safe_url(f"{GAMMA_BASE}/events")
        try:
            response = self.session.get(
                url,
                params={
                    "active": "true",
                    "closed": "false",
                    "order": "volume_24hr",
                    "ascending": "false",
                    "limit": max(1, min(int(limit), 100)),
                },
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            if len(response.content) > 25_000_000:
                raise ValueError("response too large")
            payload = response.json()
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("official_polymarket_events_failed") from exc
        if isinstance(payload, Mapping):
            rows = payload.get("data") or payload.get("events") or []
        else:
            rows = payload
        if not isinstance(rows, list):
            raise RuntimeError("official_polymarket_events_invalid")
        return [row for row in rows if isinstance(row, Mapping)]


def _parse_market(
    event: Mapping[str, Any],
    market: Mapping[str, Any],
    *,
    observed_at: dt.datetime,
) -> TrendingMarket | None:
    event_title = str(event.get("title") or event.get("question") or "").strip()
    question = str(market.get("question") or market.get("title") or event_title).strip()
    if not question:
        return None
    outcomes_raw = [str(value).strip() for value in _json_list(market.get("outcomes"))]
    prices_raw = [_float(value) for value in _json_list(market.get("outcomePrices"))]
    tokens_raw = [str(value) for value in _json_list(market.get("clobTokenIds"))]
    if not outcomes_raw or len(outcomes_raw) != len(prices_raw):
        return None
    outcomes: list[OutcomeProbability] = []
    for index, (outcome, probability) in enumerate(zip(outcomes_raw, prices_raw)):
        if probability is None:
            return None
        outcomes.append(
            OutcomeProbability(
                outcome=outcome,
                probability=round(_clamp(probability, 0.0, 1.0), 6),
                token_id=tokens_raw[index] if index < len(tokens_raw) else "",
            )
        )
    sorted_outcomes = sorted(outcomes, key=lambda row: row.probability, reverse=True)
    yes = next((row.probability for row in outcomes if row.outcome.casefold() == "yes"), None)
    no = next((row.probability for row in outcomes if row.outcome.casefold() == "no"), None)
    probability_sum = sum(row.probability for row in outcomes)
    sum_error = abs(probability_sum - 1.0) if len(outcomes) == 2 else None
    quality = "healthy"
    if sum_error is not None and sum_error > 0.05:
        quality = "degraded"
    combined_text = f"{event_title}. {question}"
    rule = _risk_rule(combined_text)
    rule_name, direction, risk_topic = (rule if rule is not None else (None, 0.0, None))
    if yes is not None:
        event_probability = yes
    else:
        event_probability = sorted_outcomes[0].probability
    sentiment = direction * (2.0 * event_probability - 1.0)
    volume24 = max(
        _volume(market, "volume24hr", "volume_24hr", "volume24Hr"),
        _volume(event, "volume24hr", "volume_24hr", "volume24Hr"),
    )
    liquidity = max(
        _volume(market, "liquidity", "liquidityNum"),
        _volume(event, "liquidity", "liquidityNum"),
    )
    end_date = market.get("endDate") or market.get("end_date") or event.get("endDate")
    return TrendingMarket(
        event_id=str(event.get("id") or event.get("eventId") or ""),
        market_id=str(market.get("id") or market.get("conditionId") or ""),
        event_title=event_title,
        question=question,
        slug=str(market.get("slug") or event.get("slug") or ""),
        observed_at=observed_at.isoformat(),
        end_date=None if not end_date else str(end_date),
        outcomes=tuple(outcomes),
        yes_probability=yes,
        no_probability=no,
        leading_outcome=sorted_outcomes[0].outcome,
        leading_probability=sorted_outcomes[0].probability,
        volume_24h=round(volume24, 4),
        liquidity=round(liquidity, 4),
        hot_keywords=_keywords(combined_text),
        risk_rule=rule_name,
        risk_topic=risk_topic,
        risk_direction=round(direction, 6),
        sentiment_score=round(_clamp(sentiment, -1.0, 1.0), 6),
        probability_sum_error=None if sum_error is None else round(sum_error, 6),
        data_quality=quality,
    )


def score_trending_events(
    events: Sequence[Mapping[str, Any]],
    *,
    as_of: dt.datetime | None = None,
    max_markets: int = 12,
) -> PolymarketTrendingResult:
    now = as_of or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    now = now.astimezone(dt.timezone.utc)
    parsed: list[TrendingMarket] = []
    fallback_context: list[TrendingMarket] = []
    for event in events:
        markets = event.get("markets") or []
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, Mapping):
                continue
            row = _parse_market(event, market, observed_at=now)
            if row is None:
                continue
            if _financially_relevant(f"{row.event_title} {row.question}"):
                parsed.append(row)
            else:
                fallback_context.append(row)
    parsed.sort(key=lambda row: (row.volume_24h, row.liquidity), reverse=True)
    fallback_context.sort(key=lambda row: (row.volume_24h, row.liquidity), reverse=True)
    selected = parsed[:max(1, max_markets)]
    if len(selected) < min(4, max_markets):
        selected.extend(fallback_context[: min(4, max_markets) - len(selected)])

    keyword_num: dict[str, float] = {}
    keyword_den = 0.0
    topic_num: dict[str, float] = {}
    topic_den: dict[str, float] = {}
    sentiment_num = 0.0
    sentiment_den = 0.0
    uncertainty_num = 0.0
    for row in selected:
        activity = math.log1p(max(row.volume_24h, row.liquidity * 0.10))
        quality = 1.0 if row.data_quality == "healthy" else 0.50
        weight = max(0.10, activity) * quality
        keyword_den += weight
        for keyword in row.hot_keywords:
            keyword_num[keyword] = keyword_num.get(keyword, 0.0) + weight
        if row.risk_topic and row.risk_direction:
            topic_num[row.risk_topic] = topic_num.get(row.risk_topic, 0.0) + row.sentiment_score * weight
            topic_den[row.risk_topic] = topic_den.get(row.risk_topic, 0.0) + weight
            sentiment_num += row.sentiment_score * weight
            sentiment_den += weight
        p = row.yes_probability if row.yes_probability is not None else row.leading_probability
        uncertainty_num += (1.0 - abs(2.0 * p - 1.0)) * weight

    hot = {
        key: round(value / max(keyword_den, 1e-12), 6)
        for key, value in sorted(keyword_num.items(), key=lambda item: item[1], reverse=True)[:12]
    }
    topics = {
        key: round(_clamp(topic_num[key] / max(topic_den[key], 1e-12), -1.0, 1.0), 6)
        for key in sorted(topic_num)
    }
    aggregate = 0.0 if sentiment_den <= 1e-12 else sentiment_num / sentiment_den
    uncertainty = 1.0 if not selected else uncertainty_num / max(keyword_den, 1e-12)
    if aggregate < 0:
        multiplier = 1.0 + MAX_POLYMARKET_TREND_RISK_REDUCTION * aggregate
    else:
        multiplier = 1.0 + MAX_POLYMARKET_TREND_RISK_EXPANSION * aggregate
    multiplier = _clamp(multiplier, 0.94, 1.01)
    status = "ok" if selected else ("no_data" if not events else "blocked")
    warnings = (
        "Market outcomes and outcomePrices are mapped one-to-one from the official Gamma API.",
        "A Yes/No price is an implied crowd probability, not an objective probability or independent trade trigger.",
        "Trending importance is based on official 24-hour volume/liquidity; correlated markets remain one prediction-market evidence family.",
        "Question polarity is scored only for a conservative allowlist of macro/geopolitical risk patterns; ambiguous markets are context-only.",
        "The layer may tighten a tactical index risk budget but cannot sell the strategic core or place an order.",
    )
    return PolymarketTrendingResult(
        status=status,
        as_of=now.isoformat(),
        event_count=len(events),
        market_count=len(selected),
        markets=tuple(selected),
        hot_keywords=hot,
        risk_topic_scores=topics,
        aggregate_sentiment=round(_clamp(aggregate, -1.0, 1.0), 6),
        uncertainty_score=round(_clamp(uncertainty, 0.0, 1.0), 6),
        risk_budget_multiplier=round(multiplier, 6),
        warnings=warnings,
    )


def collect_official_polymarket_trends(
    *,
    session: requests.Session | None = None,
    limit_events: int = 30,
    max_markets: int = 12,
    as_of: dt.datetime | None = None,
    network_enabled: bool = True,
) -> PolymarketTrendingResult:
    now = as_of or dt.datetime.now(dt.timezone.utc)
    if not network_enabled:
        result = score_trending_events((), as_of=now, max_markets=max_markets)
        return PolymarketTrendingResult(
            **{**result.__dict__, "status": "disabled"}
        )
    try:
        events = PolymarketTrendingClient(session=session).top_events(limit=limit_events)
    except RuntimeError:
        result = score_trending_events((), as_of=now, max_markets=max_markets)
        return PolymarketTrendingResult(
            **{
                **result.__dict__,
                "status": "error",
                "warnings": result.warnings + ("Official Gamma event collection failed.",),
            }
        )
    return score_trending_events(events, as_of=now, max_markets=max_markets)


def _outcome_text(outcomes: Iterable[OutcomeProbability]) -> str:
    ranked = sorted(outcomes, key=lambda row: row.probability, reverse=True)
    return " / ".join(f"{row.outcome} {row.probability:.0%}" for row in ranked[:4])


def render_polymarket_trending_markdown(result: PolymarketTrendingResult) -> str:
    hot = "、".join(list(result.hot_keywords)[:8]) or "无可用热词"
    lines = [
        "## Polymarket 官方热词与双方概率",
        f"- 状态：`{result.status}`；市场数：{result.market_count}；热词：{hot}。",
        f"- 宏观风险情绪：{result.aggregate_sentiment:+.3f}；不确定度："
        f"{result.uncertainty_score:.1%}；战术风险预算乘数：{result.risk_budget_multiplier:.1%}。",
        "",
        "| 市场 | 双方/主要结果概率 | 24h成交量 | 流动性 | 热词 | 风险解释 |",
        "|---|---|---:|---:|---|---|",
    ]
    for row in result.markets:
        interpretation = (
            "context_only"
            if not row.risk_rule
            else f"{row.risk_rule}: {row.sentiment_score:+.2f}"
        )
        lines.append(
            f"| {row.question.replace('|', '/')[:190]} | "
            f"{_outcome_text(row.outcomes)} | ${row.volume_24h:,.0f} | "
            f"${row.liquidity:,.0f} | {'、'.join(row.hot_keywords[:4])} | {interpretation} |"
        )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "OutcomeProbability",
    "PolymarketTrendingClient",
    "PolymarketTrendingResult",
    "TrendingMarket",
    "collect_official_polymarket_trends",
    "render_polymarket_trending_markdown",
    "score_trending_events",
]
