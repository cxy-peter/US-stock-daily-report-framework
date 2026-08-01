"""Point-in-time Polymarket settlement event studies.

The study freezes the last observable probability before a configured embargo
and evaluates post-resolution market returns. Final probabilities observed
after settlement are never used as predictors.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .common import clamp, ridge_regression


DEFAULT_HORIZONS = (1, 5, 20, 60)


@dataclass(frozen=True)
class ProbabilityPoint:
    observed_at: dt.datetime
    probability: float


@dataclass(frozen=True)
class PricePoint:
    session: dt.date
    close: float


@dataclass(frozen=True)
class ResolvedMarketEvent:
    market_id: str
    question: str
    policy_topic: str
    resolved_at: dt.datetime
    outcome: float
    probabilities: tuple[ProbabilityPoint, ...]
    asset_prices: Mapping[str, tuple[PricePoint, ...]]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ResolvedMarketEvent":
        resolved_raw = str(data.get("resolved_at") or "").strip().replace("Z", "+00:00")
        if not resolved_raw:
            raise ValueError("resolved_at is required")
        resolved = dt.datetime.fromisoformat(resolved_raw)
        if resolved.tzinfo is None or resolved.utcoffset() is None:
            raise ValueError("resolved_at must include timezone")
        outcome = float(data.get("outcome"))
        if outcome not in {0.0, 1.0}:
            raise ValueError("outcome must be 0 or 1")
        probabilities: list[ProbabilityPoint] = []
        for row in data.get("probability_history") or []:
            timestamp = dt.datetime.fromisoformat(
                str(row.get("observed_at") or "").strip().replace("Z", "+00:00")
            )
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("probability timestamp must include timezone")
            probability = clamp(float(row.get("probability")), 0.0, 1.0)
            probabilities.append(
                ProbabilityPoint(timestamp.astimezone(dt.timezone.utc), probability)
            )
        if not probabilities:
            raise ValueError("probability_history is required")
        asset_prices: dict[str, tuple[PricePoint, ...]] = {}
        for asset, rows in dict(data.get("asset_prices") or {}).items():
            points = []
            for row in rows:
                session = dt.date.fromisoformat(str(row.get("session"))[:10])
                close = float(row.get("close"))
                if not np.isfinite(close) or close <= 0:
                    raise ValueError("asset close must be finite and positive")
                points.append(PricePoint(session, close))
            points.sort(key=lambda item: item.session)
            if points:
                asset_prices[str(asset).upper()] = tuple(points)
        if not asset_prices:
            raise ValueError("asset_prices is required")
        return cls(
            market_id=str(data.get("market_id") or "").strip(),
            question=str(data.get("question") or "").strip(),
            policy_topic=str(data.get("policy_topic") or "other").strip().casefold(),
            resolved_at=resolved.astimezone(dt.timezone.utc),
            outcome=outcome,
            probabilities=tuple(sorted(probabilities, key=lambda item: item.observed_at)),
            asset_prices=asset_prices,
        )


@dataclass(frozen=True)
class EventImpact:
    market_id: str
    policy_topic: str
    frozen_probability: float
    outcome: float
    surprise: float
    freeze_observed_at: str
    asset_returns: Mapping[str, Mapping[int, float]]


@dataclass(frozen=True)
class HorizonStudy:
    asset: str
    horizon: int
    sample_count: int
    surprise_beta: float | None
    intercept: float | None
    mean_return: float | None
    hit_rate: float | None
    status: str


@dataclass(frozen=True)
class PolymarketStudyResult:
    status: str
    event_count: int
    accepted_count: int
    rejected_count: int
    freeze_hours: int
    impacts: tuple[EventImpact, ...]
    studies: tuple[HorizonStudy, ...]
    decision_score_contribution: float
    risk_budget_multiplier: float
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "event_count": self.event_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "freeze_hours": self.freeze_hours,
            "impacts": [
                {
                    "market_id": item.market_id,
                    "policy_topic": item.policy_topic,
                    "frozen_probability": item.frozen_probability,
                    "outcome": item.outcome,
                    "surprise": item.surprise,
                    "freeze_observed_at": item.freeze_observed_at,
                    "asset_returns": {
                        asset: {str(horizon): value for horizon, value in values.items()}
                        for asset, values in item.asset_returns.items()
                    },
                }
                for item in self.impacts
            ],
            "studies": [
                {
                    "asset": item.asset,
                    "horizon": item.horizon,
                    "sample_count": item.sample_count,
                    "surprise_beta": item.surprise_beta,
                    "intercept": item.intercept,
                    "mean_return": item.mean_return,
                    "hit_rate": item.hit_rate,
                    "status": item.status,
                }
                for item in self.studies
            ],
            "decision_score_contribution": self.decision_score_contribution,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def _frozen_probability(event: ResolvedMarketEvent, freeze_hours: int) -> ProbabilityPoint:
    cutoff = event.resolved_at - dt.timedelta(hours=freeze_hours)
    candidates = [point for point in event.probabilities if point.observed_at <= cutoff]
    if not candidates:
        raise ValueError("no point-in-time probability exists before the embargo")
    return candidates[-1]


def _post_returns(
    points: Sequence[PricePoint],
    resolved_date: dt.date,
    horizons: Sequence[int],
) -> dict[int, float]:
    eligible = [point for point in points if point.session >= resolved_date]
    if not eligible:
        return {}
    base = eligible[0].close
    result: dict[int, float] = {}
    for horizon in horizons:
        if horizon < 1:
            continue
        if len(eligible) <= horizon:
            continue
        result[int(horizon)] = eligible[horizon].close / base - 1.0
    return result


def study_resolved_markets(
    events: Iterable[ResolvedMarketEvent | Mapping[str, Any]],
    *,
    freeze_hours: int = 24,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    min_samples: int = 5,
    max_decision_weight: float = 0.05,
) -> PolymarketStudyResult:
    """Evaluate resolved markets without using post-resolution probabilities."""

    if freeze_hours < 1 or freeze_hours > 168:
        raise ValueError("freeze_hours must be between 1 and 168")
    horizons = tuple(sorted({int(value) for value in horizons if int(value) > 0}))
    if not horizons:
        raise ValueError("at least one positive horizon is required")
    if min_samples < 3:
        raise ValueError("min_samples must be at least three")
    max_decision_weight = clamp(max_decision_weight, 0.0, 0.05)

    raw = list(events)
    parsed: list[ResolvedMarketEvent] = []
    rejected = 0
    impacts: list[EventImpact] = []
    for item in raw:
        try:
            event = item if isinstance(item, ResolvedMarketEvent) else ResolvedMarketEvent.from_mapping(item)
            frozen = _frozen_probability(event, freeze_hours)
            returns = {
                asset: _post_returns(points, event.resolved_at.date(), horizons)
                for asset, points in event.asset_prices.items()
            }
            returns = {asset: values for asset, values in returns.items() if values}
            if not returns:
                raise ValueError("event has no complete post-resolution horizon")
            parsed.append(event)
            impacts.append(
                EventImpact(
                    market_id=event.market_id or f"market-{len(impacts) + 1}",
                    policy_topic=event.policy_topic,
                    frozen_probability=round(frozen.probability, 6),
                    outcome=event.outcome,
                    surprise=round(event.outcome - frozen.probability, 6),
                    freeze_observed_at=frozen.observed_at.isoformat(),
                    asset_returns={
                        asset: {horizon: round(value, 8) for horizon, value in values.items()}
                        for asset, values in returns.items()
                    },
                )
            )
        except (TypeError, ValueError, KeyError):
            rejected += 1

    warnings = [
        "Only probabilities observed before the embargo are used.",
        "The study is an event-research overlay and cannot independently trade.",
    ]
    if not impacts:
        return PolymarketStudyResult(
            status="blocked" if raw else "no_data",
            event_count=len(raw),
            accepted_count=0,
            rejected_count=rejected,
            freeze_hours=freeze_hours,
            impacts=(),
            studies=(),
            decision_score_contribution=0.0,
            risk_budget_multiplier=1.0,
            warnings=tuple(warnings + ["No event passed the point-in-time settlement gates."]),
        )

    studies: list[HorizonStudy] = []
    grouped: dict[tuple[str, int], list[tuple[float, float]]] = {}
    for impact in impacts:
        for asset, values in impact.asset_returns.items():
            for horizon, return_value in values.items():
                grouped.setdefault((asset, horizon), []).append((impact.surprise, return_value))

    active_betas: list[float] = []
    for (asset, horizon), rows in sorted(grouped.items()):
        surprises = np.array([row[0] for row in rows], dtype=float)
        returns = np.array([row[1] for row in rows], dtype=float)
        sample_count = len(rows)
        beta: float | None = None
        intercept: float | None = None
        status = "research_only"
        if sample_count >= min_samples and np.std(surprises) > 1e-8:
            fit = ridge_regression(returns, surprises.reshape(-1, 1), ridge=1e-8)
            beta = float(fit.coefficients[0])
            intercept = float(fit.intercept)
            status = "active" if abs(beta) > 1e-6 else "neutral"
            active_betas.append(beta)
        directional = [
            1.0 if surprise * return_value > 0 else 0.0
            for surprise, return_value in rows
            if abs(surprise) > 1e-9 and abs(return_value) > 1e-12
        ]
        studies.append(
            HorizonStudy(
                asset=asset,
                horizon=horizon,
                sample_count=sample_count,
                surprise_beta=None if beta is None else round(beta, 8),
                intercept=None if intercept is None else round(intercept, 8),
                mean_return=round(float(np.mean(returns)), 8),
                hit_rate=None if not directional else round(float(np.mean(directional)), 6),
                status=status,
            )
        )

    if active_betas:
        median_beta = float(np.median(active_betas))
        calibration_strength = clamp(abs(median_beta) / 0.10, 0.0, 1.0)
        decision_contribution = min(
            max_decision_weight * 0.25,
            max_decision_weight * calibration_strength * 0.25,
        )
        risk_multiplier = 1.0
    else:
        decision_contribution = 0.0
        risk_multiplier = 1.0
        warnings.append("Settled sample size is insufficient for a live directional coefficient.")

    return PolymarketStudyResult(
        status="ok" if rejected == 0 else "partial",
        event_count=len(raw),
        accepted_count=len(parsed),
        rejected_count=rejected,
        freeze_hours=freeze_hours,
        impacts=tuple(impacts),
        studies=tuple(studies),
        decision_score_contribution=round(decision_contribution, 6),
        risk_budget_multiplier=round(risk_multiplier, 6),
        warnings=tuple(warnings),
    )
