"""Cross-market narrative and subjective-sentiment research.

This module converts already-authorized public-source observations into bounded,
auditable factors. It does not collect gated content, place orders, mutate a
ledger, or let one media/community source create a portfolio action.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


@dataclass(frozen=True)
class NarrativeObservation:
    event_id: str
    observed_at: str
    source: str
    source_class: str
    independence_group: str
    topic: str
    direction: float
    magnitude: float
    confidence: float
    weight: float
    context_only: bool
    title: str
    url: str
    asset_impacts: Mapping[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "observed_at": self.observed_at,
            "source": self.source,
            "source_class": self.source_class,
            "independence_group": self.independence_group,
            "topic": self.topic,
            "direction": self.direction,
            "magnitude": self.magnitude,
            "confidence": self.confidence,
            "weight": self.weight,
            "context_only": self.context_only,
            "title": self.title,
            "url": self.url,
            "asset_impacts": dict(self.asset_impacts),
        }


@dataclass(frozen=True)
class GlobalNarrativeResult:
    status: str
    accepted_count: int
    weighted_count: int
    context_only_count: int
    independent_groups: int
    topic_scores: Mapping[str, float]
    asset_scores: Mapping[str, float]
    community_sentiment: float
    media_disagreement: float
    crowding_penalty: float
    risk_budget_multiplier: float
    decision_score_contribution: float
    observations: tuple[NarrativeObservation, ...]
    source_health: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "accepted_count": self.accepted_count,
            "weighted_count": self.weighted_count,
            "context_only_count": self.context_only_count,
            "independent_groups": self.independent_groups,
            "topic_scores": dict(self.topic_scores),
            "asset_scores": dict(self.asset_scores),
            "community_sentiment": self.community_sentiment,
            "media_disagreement": self.media_disagreement,
            "crowding_penalty": self.crowding_penalty,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "decision_score_contribution": self.decision_score_contribution,
            "observations": [item.to_dict() for item in self.observations],
            "source_health": [dict(item) for item in self.source_health],
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


_SOURCE_WEIGHTS = {
    "issuer_primary": 1.00,
    "government_primary": 1.00,
    "regulatory_primary": 1.00,
    "company_primary": 0.95,
    "major_media": 0.78,
    "regional_media": 0.70,
    "industry_media": 0.68,
    "financial_news": 0.72,
    "independent_research": 0.45,
    "kol": 0.28,
    "community": 0.18,
    "social": 0.15,
    "public_search_context": 0.06,
    "q_and_a_context": 0.00,
    "unknown": 0.08,
}

_TOPIC_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "oil_supply": {
        "keywords": (
            "oil", "crude", "brent", "wti", "opec", "refinery", "tanker",
            "strait of hormuz", "oil field", "원유", "유가", "석유", "호르무즈",
        ),
        "positive": (
            "attack", "explosion", "sanction", "closure", "halt", "cut output",
            "disruption", "shortage", "strike", "war", "blockade", "공급 차질",
            "감산", "봉쇄",
        ),
        "negative": (
            "ceasefire", "reopen", "resume", "output increase", "supply increase",
            "peace deal", "restored", "증산", "재개",
        ),
    },
    "middle_east_escalation": {
        "keywords": (
            "iran", "israel", "gaza", "red sea", "houthi", "middle east",
            "persian gulf", "saudi", "yemen", "lebanon", "이란", "이스라엘",
            "중동", "홍해",
        ),
        "positive": (
            "attack", "strike", "war", "missile", "drone", "escalat", "deadly",
            "retaliat", "conflict", "bomb", "공격", "전쟁", "미사일", "보복",
        ),
        "negative": (
            "ceasefire", "truce", "peace", "talks", "deal", "de-escalat",
            "휴전", "협상", "평화",
        ),
    },
    "shipping_disruption": {
        "keywords": (
            "shipping", "freight", "container", "port", "canal", "tanker",
            "red sea", "suez", "supply chain", "해운", "운임", "항만", "수에즈",
        ),
        "positive": (
            "disruption", "closure", "reroute", "delay", "attack", "blockade",
            "congestion", "halt", "차질", "봉쇄", "지연",
        ),
        "negative": ("reopen", "normalize", "resume", "clear backlog", "정상화", "재개"),
    },
    "memory_hbm_demand": {
        "keywords": (
            "hbm", "high bandwidth memory", "dram", "memory chip", "sk hynix",
            "micron", "ai memory", "hbm3", "hbm4", "cxl", "sk하이닉스",
            "하이닉스", "메모리", "반도체",
        ),
        "positive": (
            "partnership", "contract", "demand", "sold out", "capacity expansion",
            "record", "growth", "upgrade", "supply agreement", "strong orders",
            "협력", "수요", "계약", "증설", "성장", "공급",
        ),
        "negative": (
            "oversupply", "inventory", "price decline", "delay", "cancel",
            "production cut", "weak demand", "glut", "재고", "가격 하락",
            "감산", "수요 둔화",
        ),
    },
    "memory_oversupply": {
        "keywords": (
            "memory oversupply", "dram oversupply", "nand oversupply",
            "inventory correction", "memory glut", "메모리 공급 과잉", "재고 조정",
        ),
        "positive": (
            "oversupply", "glut", "inventory correction", "price decline",
            "공급 과잉", "재고",
        ),
        "negative": (
            "inventory normalized", "shortage", "tight supply", "재고 정상화", "공급 부족",
        ),
    },
    "semiconductor_export_controls": {
        "keywords": (
            "chip export", "semiconductor export", "export control", "chip ban",
            "entity list", "advanced chips", "반도체 수출", "수출 통제",
            "대중 반도체", "규제",
        ),
        "positive": (
            "tighten", "ban", "restrict", "expand controls", "sanction",
            "강화", "금지", "제한",
        ),
        "negative": ("ease", "waiver", "license approved", "relax", "완화", "허가"),
    },
    "korea_semiconductor_policy": {
        "keywords": (
            "korea semiconductor", "korean chip", "semiconductor cluster",
            "chip subsidy", "chip tax credit", "반도체 클러스터", "반도체 지원",
            "세액 공제", "보조금",
        ),
        "positive": (
            "subsidy", "support", "tax credit", "investment", "funding",
            "지원", "투자", "세액",
        ),
        "negative": ("cut support", "delay", "revoke", "축소", "지연", "철회"),
    },
    "trade_tariff": {
        "keywords": (
            "tariff", "trade war", "customs duty", "import duty",
            "export restriction", "관세", "무역 전쟁", "수입 규제",
        ),
        "positive": (
            "raise tariff", "new tariff", "retaliatory", "escalat",
            "increase duty", "인상", "보복", "강화",
        ),
        "negative": (
            "remove tariff", "waiver", "trade deal", "reduce duty",
            "철폐", "완화", "합의",
        ),
    },
    "china_demand": {
        "keywords": (
            "china demand", "chinese demand", "china stimulus", "china growth",
            "중국 수요", "중국 경기", "중국 부양",
        ),
        "positive": (
            "stimulus", "recovery", "accelerat", "strong demand", "support",
            "부양", "회복", "증가",
        ),
        "negative": (
            "slowdown", "weak demand", "contraction", "default",
            "둔화", "감소", "침체",
        ),
    },
    "rates_inflation": {
        "keywords": (
            "inflation", "interest rate", "bond yield", "central bank", "fed",
            "rate hike", "rate cut", "물가", "금리", "채권 수익률", "연준",
        ),
        "positive": (
            "hot inflation", "rate hike", "higher for longer", "yield surge",
            "물가 상승", "금리 인상", "급등",
        ),
        "negative": (
            "rate cut", "cool inflation", "disinflation", "yield fall",
            "금리 인하", "물가 둔화", "하락",
        ),
    },
}

# Positive topic score means more of the named condition: e.g. oil-supply shock,
# geopolitical escalation, HBM demand, oversupply, or tighter export controls.
_TRANSMISSION: dict[str, dict[str, float]] = {
    "oil_supply": {
        "USO": 1.00, "XLE": 0.78, "GLD": 0.22, "SPY": -0.24, "VOO": -0.24,
        "QQQ": -0.30, "QQQM": -0.30, "MU": -0.22, "SMH": -0.20,
        "SCHD": -0.10, "TLT": -0.18,
    },
    "middle_east_escalation": {
        "USO": 0.55, "XLE": 0.42, "GLD": 0.35, "SPY": -0.28, "VOO": -0.28,
        "QQQ": -0.24, "QQQM": -0.24, "MU": -0.20, "SMH": -0.18,
        "SCHD": -0.12,
    },
    "shipping_disruption": {
        "USO": 0.22, "XLE": 0.12, "SPY": -0.22, "VOO": -0.22,
        "QQQ": -0.18, "QQQM": -0.18, "MU": -0.16, "SMH": -0.14,
        "SCHD": -0.10,
    },
    "memory_hbm_demand": {
        "MU": 1.00, "SMH": 0.65, "NVDA": 0.32, "QQQ": 0.20,
        "QQQM": 0.20, "SPY": 0.08, "VOO": 0.08,
    },
    "memory_oversupply": {
        "MU": -1.00, "SMH": -0.55, "NVDA": -0.12, "QQQ": -0.16,
        "QQQM": -0.16, "SPY": -0.05, "VOO": -0.05,
    },
    "semiconductor_export_controls": {
        "MU": -0.72, "SMH": -0.72, "NVDA": -0.68, "QQQ": -0.30,
        "QQQM": -0.30, "SPY": -0.14, "VOO": -0.14,
    },
    "korea_semiconductor_policy": {
        "SMH": 0.18, "QQQ": 0.08, "QQQM": 0.08, "SPY": 0.03,
        "VOO": 0.03,
    },
    "trade_tariff": {
        "MU": -0.50, "SMH": -0.44, "NVDA": -0.42, "QQQ": -0.30,
        "QQQM": -0.30, "SPY": -0.24, "VOO": -0.24, "SCHD": -0.12,
        "XLI": -0.30,
    },
    "china_demand": {
        "MU": 0.30, "SMH": 0.25, "QQQ": 0.16, "QQQM": 0.16,
        "SPY": 0.12, "VOO": 0.12, "XLI": 0.20,
    },
    "rates_inflation": {
        "TLT": -0.85, "QQQ": -0.45, "QQQM": -0.45, "MU": -0.35,
        "SMH": -0.32, "SPY": -0.18, "VOO": -0.18, "SCHD": -0.08,
        "XLF": 0.12,
    },
}

_BULLISH = (
    "beat", "growth", "record", "strong demand", "upgrade", "partnership",
    "agreement", "recovery", "support", "approved", "increase", "surge",
    "bullish", "호조", "성장", "상승", "확대", "개선",
)
_BEARISH = (
    "miss", "weak demand", "downgrade", "delay", "cancel", "decline",
    "shortage", "disruption", "attack", "war", "sanction", "risk", "fall",
    "bearish", "둔화", "하락", "차질", "위험", "공격",
)


def _get(item: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(item, Mapping) and name in item:
            return item[name]
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _clip(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _parse_time(value: Any, fallback: dt.datetime) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return fallback
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return ""


def _classify_source(item: Any, source: str, url: str) -> tuple[str, bool]:
    source_id = str(_get(item, "source_id", default="") or "").casefold()
    kind = str(_get(item, "source_kind", "source_type", default="") or "").casefold()
    domain = _domain(url)
    material = f"{source} {source_id} {kind} {domain}".casefold()
    primary = bool(_get(item, "is_primary_source", default=False))
    if primary or kind in {"filing", "signed_official_action", "official_order"}:
        if "sec" in material or "regulat" in material:
            return "regulatory_primary", False
        if "government" in material or "white house" in material:
            return "government_primary", False
        return "company_primary", False
    if "news.skhynix.com" in material or "sk hynix newsroom" in material:
        return "issuer_primary", False
    if "quora" in material:
        return "q_and_a_context", True
    if "reddit" in material:
        return "community", False
    if any(
        name in material
        for name in (
            "aljazeera", "reuters", "bloomberg", "financial times", "wsj", "ap news"
        )
    ):
        return "major_media", False
    if any(
        name in material
        for name in (
            "koreaherald", "korea herald", "yna.co.kr", "yonhap",
            "koreatimes", "korea times",
        )
    ):
        return "regional_media", False
    if kind in {"community", "social"}:
        return kind, False
    if kind == "kol":
        return "kol", False
    if "public web search" in material or "search snippet" in material:
        return "public_search_context", False
    if source_id == "financial_news" or kind == "news":
        return "financial_news", False
    return "unknown", False


def _independence_group(item: Any, source_class: str, source: str, url: str) -> str:
    raw = str(_get(item, "independence_group", default="") or "").strip().casefold()
    if raw and raw not in {"unverified_social", "anonymous_social"}:
        return raw
    domain = _domain(url)
    material = f"{source} {domain}".casefold()
    if source_class == "community":
        return "social_media_reddit"
    if source_class in {"q_and_a_context", "public_search_context"}:
        return "search_context"
    if "news.skhynix.com" in material:
        return "sk_hynix_primary"
    if "aljazeera" in material:
        return "aljazeera"
    if "yonhap" in material or "yna.co.kr" in material:
        return "korean_wire"
    if "koreaherald" in material or "korea herald" in material:
        return "korean_press"
    if domain:
        return domain.replace(".", "_")
    return re.sub(r"[^a-z0-9]+", "_", source.casefold()).strip("_") or "unknown"


def _lexical_sentiment(text: str) -> float:
    lowered = text.casefold()
    positive = sum(lowered.count(term) for term in _BULLISH)
    negative = sum(lowered.count(term) for term in _BEARISH)
    total = positive + negative
    if total == 0:
        return 0.0
    return max(-1.0, min(1.0, (positive - negative) / math.sqrt(total + 1.0)))


def _topic_direction(topic: str, text: str, explicit: Any) -> float:
    if explicit not in (None, ""):
        try:
            return max(-1.0, min(1.0, float(explicit)))
        except (TypeError, ValueError):
            pass
    lowered = text.casefold()
    rule = _TOPIC_RULES[topic]
    positive = sum(lowered.count(term) for term in rule["positive"])
    negative = sum(lowered.count(term) for term in rule["negative"])
    if positive > negative:
        return 1.0
    if negative > positive:
        return -1.0
    if topic in {"middle_east_escalation", "oil_supply", "shipping_disruption"}:
        return 0.25
    return _lexical_sentiment(text)


def _topics(text: str, explicit: Any) -> list[str]:
    if explicit:
        raw = [explicit] if isinstance(explicit, str) else list(explicit)
        selected = [str(item).strip().casefold() for item in raw if str(item).strip()]
        return [item for item in selected if item in _TOPIC_RULES]
    lowered = text.casefold()
    scores = []
    for topic, rule in _TOPIC_RULES.items():
        count = sum(lowered.count(term) for term in rule["keywords"])
        if count:
            scores.append((count, topic))
    return [topic for _, topic in sorted(scores, reverse=True)[:3]]


def _float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def score_global_narratives(
    items: Iterable[Any],
    *,
    as_of: dt.datetime | None = None,
    lookback_days: int = 7,
    portfolio_tickers: Iterable[str] = (),
    max_observations: int = 20,
) -> GlobalNarrativeResult:
    """Aggregate global media, regional media and community observations.

    Item timestamps are filtered point-in-time. Correlated items are compressed
    by independence-group/topic before aggregation. Quora is context-only and
    Reddit/community data cannot independently increase risk or create trades.
    """

    now = as_of or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    now = now.astimezone(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=max(1, int(lookback_days)))
    target_assets = {
        str(item).upper() for item in portfolio_tickers if str(item).strip()
    }
    observations: list[NarrativeObservation] = []
    seen: set[str] = set()
    source_stats: dict[str, dict[str, int]] = {}
    warnings: list[str] = []

    for raw in items:
        title = _clip(_get(raw, "title", default=""), 240)
        body = _clip(_get(raw, "text", "body", default=""), 1000)
        text = f"{title} {body}".strip()
        if not text:
            continue
        url = str(_get(raw, "url", "source_url", default="") or "")
        source = _clip(_get(raw, "source", "outlet", default="unknown"), 120)
        source_class, context_only = _classify_source(raw, source, url)
        group = _independence_group(raw, source_class, source, url)
        observed = _parse_time(
            _get(raw, "published", "observed_at", default=""), now
        )
        if observed > now or observed < cutoff:
            continue
        item_id = str(
            _get(raw, "item_id", "document_id", "event_id", default="") or ""
        )
        if not item_id:
            item_id = hashlib.sha256(
                f"{source}|{url}|{title}".encode("utf-8")
            ).hexdigest()
        if item_id in seen:
            continue
        seen.add(item_id)
        selected_topics = _topics(
            text, _get(raw, "topics", "topic", default=None)
        )
        if not selected_topics:
            source_stats.setdefault(
                source_class, {"items": 0, "weighted": 0, "context": 0}
            )["items"] += 1
            continue

        base_weight = _SOURCE_WEIGHTS[source_class]
        credibility = _float(
            _get(raw, "research_weight", "credibility", default=1.0), 1.0
        )
        if credibility > 1.0:
            credibility /= 100.0
        credibility = max(0.0, min(1.0, credibility))
        age_days = max(0.0, (now - observed).total_seconds() / 86400.0)
        recency = math.exp(
            -math.log(2.0) * age_days / max(1.0, lookback_days / 2.0)
        )
        magnitude = max(
            0.05,
            min(1.0, _float(_get(raw, "magnitude", default=0.60), 0.60)),
        )
        confidence = max(
            0.05,
            min(1.0, _float(_get(raw, "confidence", default=0.65), 0.65)),
        )
        weight = base_weight * credibility * recency
        if context_only:
            weight = 0.0

        for topic in selected_topics:
            direction = _topic_direction(
                topic,
                text,
                _get(raw, "direction", "claim_direction", default=None),
            )
            impacts = {
                asset: round(direction * magnitude * coefficient, 6)
                for asset, coefficient in _TRANSMISSION.get(topic, {}).items()
                if not target_assets
                or asset in target_assets
                or asset in {"SPY", "QQQ", "USO", "XLE", "GLD", "TLT"}
            }
            observations.append(
                NarrativeObservation(
                    event_id=f"{item_id}:{topic}",
                    observed_at=observed.isoformat().replace("+00:00", "Z"),
                    source=source,
                    source_class=source_class,
                    independence_group=group,
                    topic=topic,
                    direction=round(direction, 6),
                    magnitude=round(magnitude, 6),
                    confidence=round(confidence, 6),
                    weight=round(weight, 6),
                    context_only=context_only,
                    title=title or body[:180],
                    url=url,
                    asset_impacts=impacts,
                )
            )
            stat = source_stats.setdefault(
                source_class, {"items": 0, "weighted": 0, "context": 0}
            )
            stat["items"] += 1
            stat["context" if context_only else "weighted"] += 1

    accepted_count = len(observations)
    context_count = sum(item.context_only for item in observations)
    weighted = [item for item in observations if item.weight > 0]
    groups = {item.independence_group for item in weighted}

    # Within each group/topic retain the strongest observation rather than
    # summing repeated headlines, syndication or reposts.
    collapsed: dict[tuple[str, str], NarrativeObservation] = {}
    for item in weighted:
        key = (item.independence_group, item.topic)
        contribution = abs(
            item.direction * item.magnitude * item.confidence * item.weight
        )
        current = collapsed.get(key)
        current_contribution = (
            -1.0
            if current is None
            else abs(
                current.direction
                * current.magnitude
                * current.confidence
                * current.weight
            )
        )
        if contribution > current_contribution:
            collapsed[key] = item

    topic_values: dict[str, list[float]] = {}
    asset_values: dict[str, list[float]] = {}
    community_values: list[float] = []
    for item in collapsed.values():
        score = item.direction * item.magnitude * item.confidence * item.weight
        topic_values.setdefault(item.topic, []).append(score)
        for asset, raw_impact in item.asset_impacts.items():
            asset_values.setdefault(asset, []).append(
                raw_impact * item.confidence * item.weight
            )
        if item.source_class in {"community", "social", "kol"}:
            community_values.append(score)

    topic_scores = {
        topic: round(max(-1.0, min(1.0, sum(values))), 6)
        for topic, values in topic_values.items()
    }
    asset_scores = {
        asset: round(max(-1.0, min(1.0, sum(values))), 6)
        for asset, values in asset_values.items()
    }
    community_sentiment = (
        sum(community_values) / len(community_values)
        if community_values
        else 0.0
    )

    # Disagreement is cross-group sign dispersion, not headline count.
    disagreement_samples: list[float] = []
    for values in topic_values.values():
        if len(values) >= 2:
            signs = [
                1.0 if value > 0 else (-1.0 if value < 0 else 0.0)
                for value in values
            ]
            disagreement_samples.append(max(signs) - min(signs))
    media_disagreement = (
        min(
            1.0,
            sum(disagreement_samples) / (2.0 * len(disagreement_samples)),
        )
        if disagreement_samples
        else 0.0
    )

    noncommunity_groups = {
        item.independence_group
        for item in collapsed.values()
        if item.source_class not in {"community", "social", "kol"}
    }
    community_groups = {
        item.independence_group
        for item in collapsed.values()
        if item.source_class in {"community", "social", "kol"}
    }
    crowding_penalty = 0.0
    if (
        community_groups
        and abs(community_sentiment) >= 0.18
        and len(noncommunity_groups) < 2
    ):
        crowding_penalty = min(1.0, abs(community_sentiment) * 1.5)

    broad_assets = ("SPY", "VOO", "QQQ", "QQQM")
    broad_downside = max(
        [0.0] + [-asset_scores.get(asset, 0.0) for asset in broad_assets]
    )
    risk_tightening = min(
        0.10,
        0.075 * broad_downside
        + 0.025 * crowding_penalty
        + 0.020 * media_disagreement,
    )
    risk_budget_multiplier = 1.0 - risk_tightening

    # Subjective/global information is bounded and cannot independently add risk.
    directional_portfolio = 0.0
    if target_assets:
        values = [asset_scores.get(asset, 0.0) for asset in target_assets]
        values = [value for value in values if value != 0.0]
        directional_portfolio = (
            sum(values) / len(values) if values else 0.0
        )
    decision_contribution = max(
        -0.04, min(0.04, 0.04 * directional_portfolio)
    )
    if decision_contribution > 0:
        decision_contribution = min(decision_contribution, 0.01)

    if not observations:
        status = "no_data"
    elif not weighted:
        status = "context_only"
    elif len(groups) >= 2:
        status = "healthy"
    else:
        status = "research_only"

    if context_count:
        warnings.append(
            "Quora/search-only observations are context and have zero direct decision weight."
        )
    if community_groups:
        warnings.append(
            "Reddit/community sentiment is one correlated group and cannot independently trade or add risk."
        )
    if len(groups) < 2 and weighted:
        warnings.append(
            "Fewer than two independent weighted source groups; output remains research-only."
        )
    warnings.append(
        "Global narratives are event and transmission factors, not causal proof or automatic trade instructions."
    )

    source_health = tuple(
        {
            "source_class": source_class,
            "status": (
                "context_only"
                if stats["weighted"] == 0 and stats["context"]
                else "healthy"
            ),
            "items": stats["items"],
            "weighted_items": stats["weighted"],
            "context_items": stats["context"],
        }
        for source_class, stats in sorted(source_stats.items())
    )
    ranked = sorted(
        observations,
        key=lambda item: (
            abs(
                item.direction
                * item.magnitude
                * item.confidence
                * item.weight
            ),
            item.observed_at,
        ),
        reverse=True,
    )[: max(1, int(max_observations))]

    return GlobalNarrativeResult(
        status=status,
        accepted_count=accepted_count,
        weighted_count=len(weighted),
        context_only_count=context_count,
        independent_groups=len(groups),
        topic_scores=topic_scores,
        asset_scores=asset_scores,
        community_sentiment=round(community_sentiment, 6),
        media_disagreement=round(media_disagreement, 6),
        crowding_penalty=round(crowding_penalty, 6),
        risk_budget_multiplier=round(risk_budget_multiplier, 6),
        decision_score_contribution=round(decision_contribution, 6),
        observations=tuple(ranked),
        source_health=source_health,
        warnings=tuple(warnings),
    )


__all__ = [
    "GlobalNarrativeResult",
    "NarrativeObservation",
    "score_global_narratives",
]
