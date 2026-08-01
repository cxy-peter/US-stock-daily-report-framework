"""Trump Policy Transmission Index (TPTI).

The index measures policy transmission, not media mention volume. It separates
source authority, policy stage, magnitude, confidence, horizon, recency and
asset sensitivity. It is a bounded research overlay and cannot independently
create a portfolio action.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .common import clamp


SOURCE_WEIGHTS: dict[str, float] = {
    "signed_official_action": 1.00,
    "implemented_official_action": 1.00,
    "official_action": 0.90,
    "official_statement": 0.75,
    "direct_quote_primary": 0.60,
    "direct_quote_media": 0.45,
    "media_analysis": 0.25,
    "social_summary": 0.10,
}

STAGE_WEIGHTS: dict[str, float] = {
    "implemented": 1.00,
    "signed": 0.90,
    "formal_proposal": 0.70,
    "official_statement": 0.50,
    "campaign_or_interview": 0.30,
    "media_interpretation": 0.15,
}

POLICY_TOPICS = frozenset(
    {
        "trade_tariff",
        "ai_semiconductor",
        "energy",
        "defense_geopolitics",
        "immigration_labor",
        "healthcare",
        "fiscal_tax",
        "financial_regulation",
        "fed_rates",
    }
)

ALLOWED_ACTORS = frozenset(
    {
        "donald_trump",
        "trump_administration",
        "white_house",
        "us_executive_branch",
    }
)


@dataclass(frozen=True)
class PolicyEvent:
    event_id: str
    observed_at: dt.datetime
    actor: str
    source_tier: str
    stage: str
    policy_topic: str
    direction: float
    magnitude: float
    confidence: float
    horizon_days: int
    asset_impacts: Mapping[str, float] = field(default_factory=dict)
    title: str = ""
    invalidation: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "PolicyEvent":
        observed = data.get("observed_at")
        if isinstance(observed, dt.datetime):
            observed_at = observed
        else:
            text = str(observed or "").strip().replace("Z", "+00:00")
            if not text:
                raise ValueError("observed_at is required")
            observed_at = dt.datetime.fromisoformat(text)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must include timezone")
        actor = str(data.get("actor") or "").strip().casefold()
        if actor not in ALLOWED_ACTORS:
            raise ValueError("event actor is outside the Trump-policy scope")
        source_tier = str(data.get("source_tier") or "").strip().casefold()
        if source_tier not in SOURCE_WEIGHTS:
            raise ValueError(f"unsupported source_tier: {source_tier}")
        stage = str(data.get("stage") or "").strip().casefold()
        if stage not in STAGE_WEIGHTS:
            raise ValueError(f"unsupported policy stage: {stage}")
        topic = str(data.get("policy_topic") or "").strip().casefold()
        if topic not in POLICY_TOPICS:
            raise ValueError(f"unsupported policy_topic: {topic}")
        direction = clamp(float(data.get("direction", 0.0)), -1.0, 1.0)
        magnitude = clamp(float(data.get("magnitude", 0.0)), 0.0, 1.0)
        confidence = clamp(float(data.get("confidence", 0.0)), 0.0, 1.0)
        horizon_days = int(data.get("horizon_days") or 0)
        if horizon_days < 1 or horizon_days > 3650:
            raise ValueError("horizon_days must be between 1 and 3650")
        impacts = {
            str(key).strip().upper(): clamp(float(value), -1.0, 1.0)
            for key, value in dict(data.get("asset_impacts") or {}).items()
            if str(key).strip()
        }
        return cls(
            event_id=str(data.get("event_id") or "").strip(),
            observed_at=observed_at.astimezone(dt.timezone.utc),
            actor=actor,
            source_tier=source_tier,
            stage=stage,
            policy_topic=topic,
            direction=direction,
            magnitude=magnitude,
            confidence=confidence,
            horizon_days=horizon_days,
            asset_impacts=impacts,
            title=str(data.get("title") or "").strip(),
            invalidation=str(data.get("invalidation") or "").strip(),
        )


@dataclass(frozen=True)
class PolicyEventScore:
    event_id: str
    policy_topic: str
    event_score: float
    persistence_score: float
    age_days: float
    asset_scores: Mapping[str, float]
    title: str


@dataclass(frozen=True)
class TrumpPolicyIndexResult:
    status: str
    as_of: str
    event_count: int
    accepted_count: int
    rejected_count: int
    composite_score: float
    policy_persistence: float
    confidence: float
    decision_score_contribution: float
    risk_budget_multiplier: float
    topic_scores: Mapping[str, float]
    asset_scores: Mapping[str, float]
    events: tuple[PolicyEventScore, ...]
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "as_of": self.as_of,
            "event_count": self.event_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "composite_score": self.composite_score,
            "policy_persistence": self.policy_persistence,
            "confidence": self.confidence,
            "decision_score_contribution": self.decision_score_contribution,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "topic_scores": dict(self.topic_scores),
            "asset_scores": dict(self.asset_scores),
            "events": [
                {
                    "event_id": item.event_id,
                    "policy_topic": item.policy_topic,
                    "event_score": item.event_score,
                    "persistence_score": item.persistence_score,
                    "age_days": item.age_days,
                    "asset_scores": dict(item.asset_scores),
                    "title": item.title,
                }
                for item in self.events
            ],
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def _event_decay(event: PolicyEvent, as_of: dt.datetime) -> tuple[float, float]:
    age_days = max(0.0, (as_of - event.observed_at).total_seconds() / 86_400.0)
    half_life = clamp(max(7.0, event.horizon_days * 0.50), 7.0, 180.0)
    return age_days, math.exp(-math.log(2.0) * age_days / half_life)


def compute_trump_policy_index(
    events: Iterable[PolicyEvent | Mapping[str, Any]],
    *,
    as_of: dt.datetime | None = None,
    max_decision_weight: float = 0.05,
) -> TrumpPolicyIndexResult:
    """Compute a bounded medium/long-horizon policy transmission overlay."""

    now = as_of or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("as_of must include timezone")
    now = now.astimezone(dt.timezone.utc)
    max_decision_weight = clamp(max_decision_weight, 0.0, 0.05)

    parsed: list[PolicyEvent] = []
    rejected = 0
    warnings: list[str] = [
        "TPTI is a bounded research overlay; it cannot independently create a trade.",
        "Media volume is not used as a substitute for policy-stage evidence.",
    ]
    raw_events = list(events)
    for item in raw_events:
        try:
            event = item if isinstance(item, PolicyEvent) else PolicyEvent.from_mapping(item)
            if event.observed_at > now + dt.timedelta(minutes=5):
                raise ValueError("event is future-dated")
            parsed.append(event)
        except (TypeError, ValueError):
            rejected += 1

    if not parsed:
        return TrumpPolicyIndexResult(
            status="blocked" if raw_events else "no_data",
            as_of=now.isoformat(),
            event_count=len(raw_events),
            accepted_count=0,
            rejected_count=rejected,
            composite_score=0.0,
            policy_persistence=0.0,
            confidence=0.0,
            decision_score_contribution=0.0,
            risk_budget_multiplier=1.0,
            topic_scores={},
            asset_scores={},
            events=(),
            warnings=tuple(warnings + ["No valid point-in-time Trump policy event was available."]),
        )

    scored: list[PolicyEventScore] = []
    topic_numerators: dict[str, float] = {}
    topic_denominators: dict[str, float] = {}
    asset_numerators: dict[str, float] = {}
    asset_denominators: dict[str, float] = {}
    total_weight = 0.0
    total_score = 0.0
    total_persistence = 0.0
    for event in parsed:
        age_days, decay = _event_decay(event, now)
        authority = SOURCE_WEIGHTS[event.source_tier]
        persistence = STAGE_WEIGHTS[event.stage]
        evidence_weight = authority * persistence * event.confidence * event.magnitude * decay
        event_score = event.direction * evidence_weight
        total_score += event_score
        total_weight += max(evidence_weight, 1e-9)
        total_persistence += persistence * evidence_weight
        topic_numerators[event.policy_topic] = topic_numerators.get(event.policy_topic, 0.0) + event_score
        topic_denominators[event.policy_topic] = topic_denominators.get(event.policy_topic, 0.0) + evidence_weight
        event_asset_scores: dict[str, float] = {}
        for asset, sensitivity in event.asset_impacts.items():
            contribution = event_score * sensitivity
            event_asset_scores[asset] = contribution
            asset_numerators[asset] = asset_numerators.get(asset, 0.0) + contribution
            asset_denominators[asset] = asset_denominators.get(asset, 0.0) + evidence_weight * abs(sensitivity)
        scored.append(
            PolicyEventScore(
                event_id=event.event_id or f"event-{len(scored) + 1}",
                policy_topic=event.policy_topic,
                event_score=round(event_score, 6),
                persistence_score=round(persistence, 6),
                age_days=round(age_days, 4),
                asset_scores={key: round(value, 6) for key, value in event_asset_scores.items()},
                title=event.title,
            )
        )

    composite = clamp(total_score / total_weight, -1.0, 1.0)
    persistence_score = clamp(total_persistence / total_weight, 0.0, 1.0)
    coverage = min(1.0, len(parsed) / 8.0)
    authority_average = sum(SOURCE_WEIGHTS[event.source_tier] for event in parsed) / len(parsed)
    confidence = clamp(0.45 * coverage + 0.35 * authority_average + 0.20 * persistence_score, 0.0, 1.0)
    decision_contribution = clamp(
        composite * confidence * max_decision_weight,
        -max_decision_weight,
        max_decision_weight,
    )
    risk_multiplier = 1.0 + (
        0.02 * max(decision_contribution, 0.0) / max(max_decision_weight, 1e-9)
    )
    risk_multiplier += (
        0.05 * min(decision_contribution, 0.0) / max(max_decision_weight, 1e-9)
    )
    risk_multiplier = clamp(risk_multiplier, 0.95, 1.02)

    topic_scores = {
        key: round(
            clamp(topic_numerators[key] / max(topic_denominators[key], 1e-9), -1.0, 1.0),
            6,
        )
        for key in sorted(topic_numerators)
    }
    asset_scores = {
        key: round(
            clamp(asset_numerators[key] / max(asset_denominators[key], 1e-9), -1.0, 1.0),
            6,
        )
        for key in sorted(asset_numerators)
    }
    status = "ok" if rejected == 0 else "partial"
    if confidence < 0.35:
        warnings.append("Policy coverage is too weak for more than research context.")
    return TrumpPolicyIndexResult(
        status=status,
        as_of=now.isoformat(),
        event_count=len(raw_events),
        accepted_count=len(parsed),
        rejected_count=rejected,
        composite_score=round(composite, 6),
        policy_persistence=round(persistence_score, 6),
        confidence=round(confidence, 6),
        decision_score_contribution=round(decision_contribution, 6),
        risk_budget_multiplier=round(risk_multiplier, 6),
        topic_scores=topic_scores,
        asset_scores=asset_scores,
        events=tuple(sorted(scored, key=lambda item: abs(item.event_score), reverse=True)),
        warnings=tuple(warnings),
    )
