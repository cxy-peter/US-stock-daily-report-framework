"""Policy communication analysis for speeches, interviews, social posts and media.

The model does not count mentions as a market signal.  It extracts complete
sentences that contain policy commitments, authorities, dates, quantities or
explicit economic views, then ranks the resulting claims by actor authority,
source directness, implementation stage, specificity, novelty and relevance to
configured portfolio/industry tags.

The output is research-only.  A political communication, media interpretation,
or social-media post cannot independently create or execute a trade.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


MAX_POLITICAL_DECISION_WEIGHT = 0.08
MAX_POSITIVE_RISK_EXPANSION = 0.02
MAX_NEGATIVE_RISK_REDUCTION = 0.10

SOURCE_WEIGHTS: dict[str, float] = {
    "signed_official_action": 1.00,
    "implemented_official_action": 1.00,
    "official_order": 0.95,
    "official_fact_sheet": 0.90,
    "official_speech": 0.85,
    "official_press_briefing": 0.78,
    "official_interview": 0.74,
    "official_x": 0.70,
    "official_social": 0.68,
    "agency_statement": 0.76,
    "company_official_statement": 0.70,
    "media_direct_quote": 0.58,
    "media_analysis": 0.32,
    "commentary": 0.16,
}

STAGE_WEIGHTS: dict[str, float] = {
    "implemented": 1.00,
    "signed": 0.92,
    "directed": 0.82,
    "formal_proposal": 0.70,
    "negotiating": 0.58,
    "announced_intent": 0.50,
    "conditional_view": 0.34,
    "general_view": 0.20,
    "media_interpretation": 0.12,
}

HORIZON_DAYS: dict[str, int] = {
    "intraday": 1,
    "short": 5,
    "medium": 60,
    "long": 365,
    "structural": 1095,
}

POLICY_TOPICS: dict[str, tuple[str, ...]] = {
    "trade_tariff": (
        "tariff", "trade deal", "trade agreement", "reciprocal trade",
        "section 301", "import duty", "export restriction", "customs",
    ),
    "ai_semiconductor": (
        "artificial intelligence", " ai ", "semiconductor", "chip", "gpu",
        "data center", "advanced computing", "export control", "compute",
    ),
    "energy_power": (
        "energy", "electricity", "power grid", "natural gas", "oil", "nuclear",
        "utility", "ratepayer", "renewable", "generation capacity",
    ),
    "fiscal_tax": (
        "tax", "fiscal", "budget", "deficit", "treasury issuance", "spending",
        "appropriation", "debt ceiling", "subsidy", "credit program",
    ),
    "monetary_rates": (
        "federal reserve", "interest rate", "inflation", "monetary policy",
        "rate cut", "rate hike", "yield", "price stability",
    ),
    "financial_regulation": (
        "bank regulation", "capital requirement", "financial regulation",
        "consumer finance", "securities regulation", "sec", "banking system",
    ),
    "digital_assets": (
        "digital asset", "crypto", "bitcoin", "stablecoin", "blockchain",
        "token", "strategic reserve", "digital financial technology",
    ),
    "defense_geopolitics": (
        "defense", "war", "sanction", "national security", "military",
        "iran", "china", "russia", "ukraine", "middle east", "nato",
    ),
    "immigration_labor": (
        "immigration", "border", "visa", "labor", "worker", "wage",
        "employment", "deportation", "workforce",
    ),
    "healthcare": (
        "healthcare", "health care", "drug price", "medicare", "medicaid",
        "hospital", "pharmaceutical", "fda", "insurance premium",
    ),
    "infrastructure": (
        "infrastructure", "road", "bridge", "broadband", "construction",
        "permitting", "critical infrastructure", "rail", "port",
    ),
    "space": (
        "space", "launch", "satellite", "nasa", "space force", "moon", "mars",
    ),
    "housing": (
        "housing", "mortgage", "fannie mae", "freddie mac", "homebuilder",
        "home affordability", "federal housing",
    ),
    "antitrust_competition": (
        "antitrust", "competition", "merger", "monopoly", "ftc", "market power",
    ),
}

POSITIVE_PHRASES = (
    "approve", "support", "expand", "accelerate", "invest", "fund", "reduce tax",
    "cut tax", "deregulate", "open market", "increase capacity", "promote",
    "protect innovation", "lower costs", "repeal restriction",
)
NEGATIVE_PHRASES = (
    "ban", "restrict", "tariff", "sanction", "investigate", "penalty", "fine",
    "raise tax", "tighten", "block", "prohibit", "export control", "revoke",
)

COMMITMENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("implemented", ("has taken effect", "is now in force", "implemented", "effective today")),
    ("signed", ("signed", "enacted", "approved into law", "executive order")),
    ("directed", ("directs", "ordered", "instructed", "shall", "must")),
    ("formal_proposal", ("proposes", "legislative framework", "will submit", "formal proposal")),
    ("negotiating", ("negotiating", "talks", "seeking an agreement", "considering a deal")),
    ("announced_intent", ("will", "plans to", "intend to", "we are going to", "committed to")),
    ("conditional_view", ("may", "might", "could", "if necessary", "consider")),
)

SPECIFICITY_PATTERNS = (
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|percent|billion|million|trillion|days?|months?|years?)\b", re.I),
    re.compile(r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\b", re.I),
    re.compile(r"\b(?:department|agency|commission|treasury|commerce|ustr|sec|federal reserve|white house)\b", re.I),
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _clamp(value: float, lower: float, upper: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("value must be finite")
    return min(max(value, lower), upper)


def _aware_utc(value: dt.datetime | str) -> dt.datetime:
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(text.casefold()))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return 0.0 if not union else len(left & right) / len(union)


def _sentence_hash(text: str) -> str:
    return hashlib.sha256(" ".join(sorted(_tokens(text))).encode("utf-8")).hexdigest()


def _sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text or " ").strip()
    if not normalized:
        return []
    result = [part.strip(" \t\r\n-–—") for part in _SENTENCE_SPLIT.split(normalized)]
    return [item for item in result if 25 <= len(item) <= 1200]


def _topic_matches(sentence: str) -> dict[str, int]:
    padded = f" {sentence.casefold()} "
    matches: dict[str, int] = {}
    for topic, phrases in POLICY_TOPICS.items():
        count = sum(1 for phrase in phrases if phrase in padded)
        if count:
            matches[topic] = count
    return matches


def _stage(sentence: str, source_type: str) -> str:
    lowered = sentence.casefold()
    for stage, phrases in COMMITMENT_PATTERNS:
        if any(phrase in lowered for phrase in phrases):
            return stage
    if source_type in {"media_analysis", "commentary"}:
        return "media_interpretation"
    return "general_view"


def _direction(sentence: str) -> float:
    lowered = sentence.casefold()
    positive = sum(1 for phrase in POSITIVE_PHRASES if phrase in lowered)
    negative = sum(1 for phrase in NEGATIVE_PHRASES if phrase in lowered)
    if positive == negative == 0:
        return 0.0
    return _clamp((positive - negative) / max(positive + negative, 1), -1.0, 1.0)


def _specificity(sentence: str) -> float:
    score = 0.12
    score += 0.18 * sum(1 for pattern in SPECIFICITY_PATTERNS if pattern.search(sentence))
    lowered = sentence.casefold()
    if any(token in lowered for token in ("will", "shall", "effective", "deadline", "beginning")):
        score += 0.15
    if any(token in lowered for token in ("because", "in order to", "therefore", "which will")):
        score += 0.10
    return _clamp(score, 0.0, 1.0)


def _horizon(sentence: str, stage: str) -> str:
    lowered = sentence.casefold()
    if any(token in lowered for token in ("today", "tomorrow", "immediately", "this week")):
        return "intraday"
    if any(token in lowered for token in ("next week", "within days", "short term")):
        return "short"
    if any(token in lowered for token in ("this quarter", "this year", "coming months")):
        return "medium"
    if any(token in lowered for token in ("next year", "over the next years", "long term")):
        return "long"
    if stage in {"implemented", "signed", "directed"}:
        return "medium"
    return "long"


@dataclass(frozen=True)
class ActorProfile:
    actor_id: str
    display_name: str
    role: str
    institution: str
    actor_type: str
    base_weight: float
    policy_authority: float
    monitored_topics: tuple[str, ...] = ()
    holdings_tags: tuple[str, ...] = ()
    official_handles: tuple[str, ...] = ()
    official_urls: tuple[str, ...] = ()
    as_of: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ActorProfile":
        actor_id = str(data.get("actor_id") or "").strip().casefold()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,127}", actor_id):
            raise ValueError("actor_id is invalid")
        topics = tuple(str(item).strip().casefold() for item in data.get("monitored_topics") or ())
        unknown = set(topics) - set(POLICY_TOPICS)
        if unknown:
            raise ValueError(f"unknown monitored topic: {sorted(unknown)[0]}")
        return cls(
            actor_id=actor_id,
            display_name=str(data.get("display_name") or "").strip(),
            role=str(data.get("role") or "").strip(),
            institution=str(data.get("institution") or "").strip(),
            actor_type=str(data.get("actor_type") or "other").strip().casefold(),
            base_weight=_clamp(float(data.get("base_weight", 0.25)), 0.0, 1.0),
            policy_authority=_clamp(float(data.get("policy_authority", 0.25)), 0.0, 1.0),
            monitored_topics=topics,
            holdings_tags=tuple(str(item).strip().casefold() for item in data.get("holdings_tags") or ()),
            official_handles=tuple(str(item).strip().lstrip("@").casefold() for item in data.get("official_handles") or ()),
            official_urls=tuple(str(item).strip() for item in data.get("official_urls") or ()),
            as_of=None if data.get("as_of") in (None, "") else str(data.get("as_of")),
        )


@dataclass(frozen=True)
class CommunicationDocument:
    document_id: str
    actor_id: str
    observed_at: dt.datetime
    source_type: str
    title: str
    body: str
    source_url: str = ""
    outlet: str = ""
    engagement: float = 0.0
    direct_quote: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CommunicationDocument":
        source_type = str(data.get("source_type") or "").strip().casefold()
        if source_type not in SOURCE_WEIGHTS:
            raise ValueError(f"unsupported source_type: {source_type}")
        document_id = str(data.get("document_id") or "").strip()
        if not document_id:
            material = "|".join(
                [str(data.get("actor_id") or ""), str(data.get("observed_at") or ""), str(data.get("title") or "")]
            )
            document_id = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return cls(
            document_id=document_id,
            actor_id=str(data.get("actor_id") or "").strip().casefold(),
            observed_at=_aware_utc(data.get("observed_at")),
            source_type=source_type,
            title=str(data.get("title") or "").strip(),
            body=str(data.get("body") or "").strip(),
            source_url=str(data.get("source_url") or "").strip(),
            outlet=str(data.get("outlet") or "").strip(),
            engagement=max(0.0, float(data.get("engagement") or 0.0)),
            direct_quote=bool(data.get("direct_quote", True)),
        )


@dataclass(frozen=True)
class MediaAssessment:
    assessment_id: str
    observed_at: dt.datetime
    outlet: str
    outlet_weight: float
    target_actor_id: str | None
    target_topic: str | None
    stance: float
    uncertainty: float
    summary: str
    source_url: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "MediaAssessment":
        topic = data.get("target_topic")
        if topic not in (None, "") and str(topic).casefold() not in POLICY_TOPICS:
            raise ValueError("media target_topic is unsupported")
        return cls(
            assessment_id=str(data.get("assessment_id") or hashlib.sha256(repr(sorted(data.items())).encode()).hexdigest()),
            observed_at=_aware_utc(data.get("observed_at")),
            outlet=str(data.get("outlet") or "").strip(),
            outlet_weight=_clamp(float(data.get("outlet_weight", 0.4)), 0.0, 1.0),
            target_actor_id=None if data.get("target_actor_id") in (None, "") else str(data.get("target_actor_id")).casefold(),
            target_topic=None if topic in (None, "") else str(topic).casefold(),
            stance=_clamp(float(data.get("stance", 0.0)), -1.0, 1.0),
            uncertainty=_clamp(float(data.get("uncertainty", 0.5)), 0.0, 1.0),
            summary=str(data.get("summary") or "").strip(),
            source_url=str(data.get("source_url") or "").strip(),
        )


@dataclass(frozen=True)
class PolicyClaim:
    claim_id: str
    actor_id: str
    actor_name: str
    topic: str
    observed_at: str
    source_type: str
    stage: str
    horizon: str
    evidence_sentence: str
    compact_summary: str
    direction: float
    importance: float
    confidence: float
    novelty: float
    specificity: float
    media_consensus: float | None
    media_disagreement: float | None
    affected_tags: tuple[str, ...]
    source_url: str


@dataclass(frozen=True)
class PoliticalBriefResult:
    status: str
    as_of: str
    document_count: int
    accepted_claim_count: int
    rejected_document_count: int
    top_claims: tuple[PolicyClaim, ...]
    topic_scores: Mapping[str, float]
    actor_scores: Mapping[str, float]
    uncertainty_score: float
    decision_score_contribution: float
    risk_budget_multiplier: float
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "as_of": self.as_of,
            "document_count": self.document_count,
            "accepted_claim_count": self.accepted_claim_count,
            "rejected_document_count": self.rejected_document_count,
            "top_claims": [claim.__dict__ for claim in self.top_claims],
            "topic_scores": dict(self.topic_scores),
            "actor_scores": dict(self.actor_scores),
            "uncertainty_score": self.uncertainty_score,
            "decision_score_contribution": self.decision_score_contribution,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def _compact_summary(sentence: str, max_chars: int = 220) -> str:
    text = re.sub(r"\s+", " ", sentence).strip()
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:") + "…"


def _media_for_claim(
    assessments: Sequence[MediaAssessment], actor_id: str, topic: str, observed_at: dt.datetime
) -> tuple[float | None, float | None, float]:
    rows = [
        item
        for item in assessments
        if (item.target_actor_id in (None, actor_id))
        and (item.target_topic in (None, topic))
        and abs((item.observed_at - observed_at).total_seconds()) <= 7 * 86_400
    ]
    if not rows:
        return None, None, 0.0
    weights = [item.outlet_weight * (1.0 - 0.5 * item.uncertainty) for item in rows]
    denominator = sum(weights)
    if denominator <= 1e-12:
        return None, None, 0.0
    consensus = sum(item.stance * weight for item, weight in zip(rows, weights)) / denominator
    mean = consensus
    disagreement = math.sqrt(
        sum(weight * (item.stance - mean) ** 2 for item, weight in zip(rows, weights)) / denominator
    )
    uncertainty = sum(item.uncertainty * weight for item, weight in zip(rows, weights)) / denominator
    return _clamp(consensus, -1.0, 1.0), _clamp(disagreement, 0.0, 1.0), _clamp(uncertainty, 0.0, 1.0)


def build_political_brief(
    actors: Iterable[ActorProfile | Mapping[str, Any]],
    documents: Iterable[CommunicationDocument | Mapping[str, Any]],
    *,
    media_assessments: Iterable[MediaAssessment | Mapping[str, Any]] = (),
    previous_claims: Iterable[str] = (),
    portfolio_tags: Iterable[str] = (),
    as_of: dt.datetime | None = None,
    max_claims: int = 12,
    max_decision_weight: float = MAX_POLITICAL_DECISION_WEIGHT,
) -> PoliticalBriefResult:
    """Extract and rank policy claims rather than count political keywords."""

    now = _aware_utc(as_of or dt.datetime.now(dt.timezone.utc))
    actor_map: dict[str, ActorProfile] = {}
    for raw in actors:
        actor = raw if isinstance(raw, ActorProfile) else ActorProfile.from_mapping(raw)
        actor_map[actor.actor_id] = actor
    if not actor_map:
        raise ValueError("at least one actor profile is required")

    assessments: list[MediaAssessment] = []
    for raw in media_assessments:
        try:
            assessments.append(raw if isinstance(raw, MediaAssessment) else MediaAssessment.from_mapping(raw))
        except (TypeError, ValueError):
            continue

    history_tokens = [_tokens(text) for text in previous_claims if str(text).strip()]
    tags = {str(item).strip().casefold() for item in portfolio_tags if str(item).strip()}
    raw_documents = list(documents)
    parsed_documents: list[CommunicationDocument] = []
    rejected = 0
    for raw in raw_documents:
        try:
            doc = raw if isinstance(raw, CommunicationDocument) else CommunicationDocument.from_mapping(raw)
            if doc.actor_id not in actor_map or doc.observed_at > now + dt.timedelta(minutes=5):
                raise ValueError("document actor/timestamp is invalid")
            parsed_documents.append(doc)
        except (TypeError, ValueError):
            rejected += 1

    claims: list[PolicyClaim] = []
    topic_numerator: dict[str, float] = defaultdict(float)
    topic_denominator: dict[str, float] = defaultdict(float)
    actor_numerator: dict[str, float] = defaultdict(float)
    actor_denominator: dict[str, float] = defaultdict(float)
    media_uncertainties: list[float] = []

    for document in parsed_documents:
        actor = actor_map[document.actor_id]
        source_weight = SOURCE_WEIGHTS[document.source_type]
        age_days = max(0.0, (now - document.observed_at).total_seconds() / 86_400.0)
        freshness = math.exp(-math.log(2.0) * age_days / 30.0)
        for sentence in _sentences(document.body):
            topics = _topic_matches(sentence)
            if not topics:
                continue
            stage = _stage(sentence, document.source_type)
            specificity = _specificity(sentence)
            commitment = STAGE_WEIGHTS[stage]
            sentence_tokens = _tokens(sentence)
            max_similarity = max((_jaccard(sentence_tokens, row) for row in history_tokens), default=0.0)
            novelty = _clamp(1.0 - max_similarity, 0.0, 1.0)
            for topic, topic_hits in topics.items():
                if actor.monitored_topics and topic not in actor.monitored_topics:
                    topic_scope = 0.55
                else:
                    topic_scope = 1.0
                relevant_tags = tuple(
                    sorted(
                        tag
                        for tag in set(actor.holdings_tags) | tags
                        if tag in sentence.casefold() or tag == topic
                    )
                )
                holdings_relevance = 1.0 if relevant_tags else (0.85 if tags else 1.0)
                media_consensus, media_disagreement, media_uncertainty = _media_for_claim(
                    assessments, actor.actor_id, topic, document.observed_at
                )
                media_uncertainties.append(media_uncertainty)
                directness = 1.0 if document.direct_quote else 0.75
                engagement_boost = min(math.log1p(document.engagement) / 20.0, 0.12)
                importance = (
                    actor.base_weight
                    * (0.55 + 0.45 * actor.policy_authority)
                    * source_weight
                    * commitment
                    * (0.45 + 0.55 * specificity)
                    * (0.55 + 0.45 * novelty)
                    * holdings_relevance
                    * topic_scope
                    * freshness
                    * directness
                    * (1.0 + engagement_boost)
                    * min(1.0, 0.65 + 0.12 * topic_hits)
                )
                if media_disagreement is not None:
                    importance *= 1.0 - 0.18 * media_disagreement
                direction = _direction(sentence)
                if direction == 0.0 and media_consensus is not None:
                    direction = 0.20 * media_consensus
                confidence = _clamp(
                    0.30 * source_weight
                    + 0.25 * actor.policy_authority
                    + 0.20 * specificity
                    + 0.15 * novelty
                    + 0.10 * (1.0 - media_uncertainty),
                    0.0,
                    1.0,
                )
                claim_id = hashlib.sha256(
                    f"{document.document_id}|{actor.actor_id}|{topic}|{_sentence_hash(sentence)}".encode("utf-8")
                ).hexdigest()
                claim = PolicyClaim(
                    claim_id=claim_id,
                    actor_id=actor.actor_id,
                    actor_name=actor.display_name or actor.role,
                    topic=topic,
                    observed_at=document.observed_at.isoformat(),
                    source_type=document.source_type,
                    stage=stage,
                    horizon=_horizon(sentence, stage),
                    evidence_sentence=sentence,
                    compact_summary=_compact_summary(sentence),
                    direction=round(direction, 6),
                    importance=round(_clamp(importance, 0.0, 1.0), 6),
                    confidence=round(confidence, 6),
                    novelty=round(novelty, 6),
                    specificity=round(specificity, 6),
                    media_consensus=None if media_consensus is None else round(media_consensus, 6),
                    media_disagreement=None if media_disagreement is None else round(media_disagreement, 6),
                    affected_tags=relevant_tags,
                    source_url=document.source_url,
                )
                claims.append(claim)
                weight = importance * confidence
                topic_numerator[topic] += direction * weight
                topic_denominator[topic] += weight
                actor_numerator[actor.actor_id] += direction * weight
                actor_denominator[actor.actor_id] += weight

    # Preserve different claims while removing near-identical reposts.
    claims.sort(key=lambda item: (item.importance * item.confidence, item.observed_at), reverse=True)
    deduplicated: list[PolicyClaim] = []
    seen_by_topic: dict[str, list[frozenset[str]]] = defaultdict(list)
    for claim in claims:
        token_set = _tokens(claim.evidence_sentence)
        if any(_jaccard(token_set, prior) >= 0.82 for prior in seen_by_topic[claim.topic]):
            continue
        seen_by_topic[claim.topic].append(token_set)
        deduplicated.append(claim)
        if len(deduplicated) >= max_claims:
            break

    topic_scores = {
        topic: round(_clamp(topic_numerator[topic] / max(topic_denominator[topic], 1e-12), -1.0, 1.0), 6)
        for topic in sorted(topic_numerator)
    }
    actor_scores = {
        actor_id: round(_clamp(actor_numerator[actor_id] / max(actor_denominator[actor_id], 1e-12), -1.0, 1.0), 6)
        for actor_id in sorted(actor_numerator)
    }
    total_weight = sum(topic_denominator.values())
    aggregate_direction = (
        0.0 if total_weight <= 1e-12 else sum(topic_numerator.values()) / total_weight
    )
    uncertainty = _clamp(
        (sum(media_uncertainties) / len(media_uncertainties)) if media_uncertainties else 0.50,
        0.0,
        1.0,
    )
    coverage = _clamp(len(deduplicated) / 10.0, 0.0, 1.0)
    decision_weight = _clamp(max_decision_weight, 0.0, MAX_POLITICAL_DECISION_WEIGHT)
    contribution = _clamp(
        aggregate_direction * coverage * (1.0 - 0.45 * uncertainty) * decision_weight,
        -decision_weight,
        decision_weight,
    )
    if contribution >= 0:
        risk_multiplier = 1.0 + MAX_POSITIVE_RISK_EXPANSION * contribution / max(decision_weight, 1e-12)
    else:
        risk_multiplier = 1.0 + MAX_NEGATIVE_RISK_REDUCTION * contribution / max(decision_weight, 1e-12)
    risk_multiplier = _clamp(risk_multiplier, 0.90, 1.02)

    warnings = [
        "Important policy claims are extracted from complete sentences; raw mention counts are not a trading factor.",
        "Media assessment modifies confidence/disagreement but cannot replace the original source.",
        "Political communications cannot independently create or execute a portfolio action.",
    ]
    if not deduplicated:
        warnings.append("No valid, specific and portfolio-relevant policy claim was available.")
    if rejected:
        warnings.append(f"{rejected} communication document(s) failed validation.")
    return PoliticalBriefResult(
        status="ok" if deduplicated and rejected == 0 else ("partial" if deduplicated else "blocked"),
        as_of=now.isoformat(),
        document_count=len(raw_documents),
        accepted_claim_count=len(deduplicated),
        rejected_document_count=rejected,
        top_claims=tuple(deduplicated),
        topic_scores=topic_scores,
        actor_scores=actor_scores,
        uncertainty_score=round(uncertainty, 6),
        decision_score_contribution=round(contribution, 6),
        risk_budget_multiplier=round(risk_multiplier, 6),
        warnings=tuple(warnings),
    )


__all__ = [
    "ActorProfile",
    "CommunicationDocument",
    "MAX_POLITICAL_DECISION_WEIGHT",
    "MediaAssessment",
    "POLICY_TOPICS",
    "PolicyClaim",
    "PoliticalBriefResult",
    "SOURCE_WEIGHTS",
    "build_political_brief",
]
