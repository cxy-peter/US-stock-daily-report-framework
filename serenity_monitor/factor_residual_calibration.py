"""Version-isolated calibration for factor-model residual forecasts.

A residual forecast is meaningful only relative to the exact factor definition,
training vintage, signal version, horizon and market regime that produced it.
This module therefore refuses to pool observations across factor-model versions.
It evaluates out-of-sample residual forecasts and returns research-weight states;
it has no portfolio mutation or broker API.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np


_ALLOWED_STATES = frozenset({"active", "decayed", "quarantined", "research_only"})
_ALLOWED_DIRECTIONS = frozenset({"bullish", "bearish", "neutral"})


class FactorCalibrationError(ValueError):
    """Raised when point-in-time or model-version boundaries are violated."""


def _aware(value: dt.datetime | str, name: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        result = value
    else:
        try:
            result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise FactorCalibrationError(f"{name} must be ISO date-time") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise FactorCalibrationError(f"{name} must be timezone-aware")
    return result.astimezone(dt.timezone.utc)


def _date(value: dt.date | str, name: str) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise FactorCalibrationError(f"{name} must be ISO date") from exc


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FactorCalibrationError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise FactorCalibrationError(f"{name} must be finite")
    return number


def _safe_id(value: Any, name: str) -> str:
    text = str(value or "").strip().casefold()
    if not text or len(text) > 128:
        raise FactorCalibrationError(f"{name} must be a non-empty identifier")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._:-" for character in text):
        raise FactorCalibrationError(f"{name} contains unsupported characters")
    return text


def _rank(values: np.ndarray) -> np.ndarray:
    """Average ranks with deterministic tie handling."""

    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    index = 0
    while index < len(values):
        end = index + 1
        while end < len(values) and values[order[end]] == values[order[index]]:
            end += 1
        average = 0.5 * (index + end - 1) + 1.0
        ranks[order[index:end]] = average
        index = end
    return ranks


def _correlation(left: np.ndarray, right: np.ndarray) -> float | None:
    if len(left) < 3 or float(np.std(left)) <= 1e-12 or float(np.std(right)) <= 1e-12:
        return None
    return float(np.corrcoef(left, right)[0, 1])


@dataclass(frozen=True)
class FactorModelDescriptor:
    factor_model_version: str
    factors: tuple[str, ...]
    trained_through: dt.date | str
    first_available_at: dt.datetime | str
    methodology_sha256: str

    def __post_init__(self) -> None:
        version = _safe_id(self.factor_model_version, "factor_model_version")
        factors = tuple(sorted({_safe_id(item, "factor") for item in self.factors}))
        if not factors:
            raise FactorCalibrationError("at least one factor is required")
        trained_through = _date(self.trained_through, "trained_through")
        first_available = _aware(self.first_available_at, "first_available_at")
        digest = str(self.methodology_sha256).strip().casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise FactorCalibrationError("methodology_sha256 must be lowercase SHA-256")
        if first_available.date() < trained_through:
            raise FactorCalibrationError("factor model cannot be available before training cutoff")
        object.__setattr__(self, "factor_model_version", version)
        object.__setattr__(self, "factors", factors)
        object.__setattr__(self, "trained_through", trained_through)
        object.__setattr__(self, "first_available_at", first_available)
        object.__setattr__(self, "methodology_sha256", digest)


@dataclass(frozen=True)
class ResidualForecastObservation:
    observation_id: str
    signal_model_version: str
    factor_model_version: str
    horizon_sessions: int
    market_regime: str
    first_observed_at: dt.datetime | str
    target_session: dt.date | str
    settled_at: dt.datetime | str
    predicted_residual_return: float
    realized_residual_return: float
    direction: str = "neutral"
    implementation_cost_return: float = 0.0
    calibration_eligible: bool = True

    def __post_init__(self) -> None:
        observation_id = _safe_id(self.observation_id, "observation_id")
        signal_version = _safe_id(self.signal_model_version, "signal_model_version")
        factor_version = _safe_id(self.factor_model_version, "factor_model_version")
        regime = _safe_id(self.market_regime, "market_regime")
        horizon = int(self.horizon_sessions)
        if horizon not in {1, 5, 20, 60}:
            raise FactorCalibrationError("horizon_sessions must be 1, 5, 20, or 60")
        first_observed = _aware(self.first_observed_at, "first_observed_at")
        target = _date(self.target_session, "target_session")
        settled = _aware(self.settled_at, "settled_at")
        if settled < first_observed or settled.date() < target:
            raise FactorCalibrationError("settlement violates point-in-time ordering")
        predicted = _finite(self.predicted_residual_return, "predicted_residual_return")
        realized = _finite(self.realized_residual_return, "realized_residual_return")
        cost = _finite(self.implementation_cost_return, "implementation_cost_return")
        if cost < 0:
            raise FactorCalibrationError("implementation_cost_return must be non-negative")
        direction = str(self.direction).strip().casefold()
        if direction not in _ALLOWED_DIRECTIONS:
            raise FactorCalibrationError("direction must be bullish, bearish, or neutral")
        if not isinstance(self.calibration_eligible, bool):
            raise FactorCalibrationError("calibration_eligible must be boolean")
        object.__setattr__(self, "observation_id", observation_id)
        object.__setattr__(self, "signal_model_version", signal_version)
        object.__setattr__(self, "factor_model_version", factor_version)
        object.__setattr__(self, "horizon_sessions", horizon)
        object.__setattr__(self, "market_regime", regime)
        object.__setattr__(self, "first_observed_at", first_observed)
        object.__setattr__(self, "target_session", target)
        object.__setattr__(self, "settled_at", settled)
        object.__setattr__(self, "predicted_residual_return", predicted)
        object.__setattr__(self, "realized_residual_return", realized)
        object.__setattr__(self, "implementation_cost_return", cost)
        object.__setattr__(self, "direction", direction)


@dataclass(frozen=True)
class FactorResidualCalibrationSummary:
    signal_model_version: str
    factor_model_version: str
    horizon_sessions: int
    market_regime: str
    sample_count: int
    recent_sample_count: int
    mean_predicted_residual: float
    mean_realized_residual: float
    mean_net_realized_residual: float
    mae: float
    rmse: float
    sign_hit_rate: float | None
    rank_ic: float | None
    recent_sign_hit_rate: float | None
    recent_mean_net_residual: float | None
    state: str
    weight_multiplier: float
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.state not in _ALLOWED_STATES:
            raise FactorCalibrationError("unknown calibration state")


@dataclass(frozen=True)
class FactorResidualCalibrationResult:
    status: str
    as_of: str
    eligible_observation_count: int
    excluded_observation_count: int
    summaries: tuple[FactorResidualCalibrationSummary, ...]
    factor_versions: tuple[str, ...]
    pooled_across_factor_versions: bool = False
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "as_of": self.as_of,
            "eligible_observation_count": self.eligible_observation_count,
            "excluded_observation_count": self.excluded_observation_count,
            "summaries": [item.__dict__ for item in self.summaries],
            "factor_versions": list(self.factor_versions),
            "pooled_across_factor_versions": False,
            "automatic_trading_permitted": False,
        }


def calibrate_factor_residuals(
    observations: Iterable[ResidualForecastObservation | Mapping[str, Any]],
    *,
    as_of: dt.datetime | str,
    minimum_samples: int = 20,
    recent_window: int = 10,
    minimum_recent_samples: int = 5,
    active_hit_rate: float = 0.55,
    decay_hit_rate: float = 0.48,
    quarantine_hit_rate: float = 0.38,
    maximum_rmse: float = 0.12,
) -> FactorResidualCalibrationResult:
    """Calibrate residual forecasts without ever pooling factor-model versions."""

    cutoff = _aware(as_of, "as_of")
    if minimum_samples < 5 or recent_window < 3 or minimum_recent_samples < 3:
        raise FactorCalibrationError("sample thresholds are too small")
    if minimum_recent_samples > recent_window:
        raise FactorCalibrationError("minimum_recent_samples exceeds recent_window")
    parsed: list[ResidualForecastObservation] = []
    excluded = 0
    seen_ids: set[str] = set()
    for raw in observations:
        item = raw if isinstance(raw, ResidualForecastObservation) else ResidualForecastObservation(**dict(raw))
        if item.observation_id in seen_ids:
            raise FactorCalibrationError("duplicate observation_id")
        seen_ids.add(item.observation_id)
        if not item.calibration_eligible or item.settled_at > cutoff:
            excluded += 1
            continue
        parsed.append(item)

    groups: dict[tuple[str, str, int, str], list[ResidualForecastObservation]] = {}
    for item in parsed:
        key = (
            item.signal_model_version,
            item.factor_model_version,
            item.horizon_sessions,
            item.market_regime,
        )
        groups.setdefault(key, []).append(item)

    summaries: list[FactorResidualCalibrationSummary] = []
    for key, rows in sorted(groups.items()):
        rows.sort(key=lambda item: (item.target_session, item.settled_at, item.observation_id))
        predicted = np.array([item.predicted_residual_return for item in rows], dtype=float)
        realized = np.array([item.realized_residual_return for item in rows], dtype=float)
        costs = np.array([item.implementation_cost_return for item in rows], dtype=float)
        net = realized - costs
        errors = predicted - realized
        signs = np.sign(predicted) * np.sign(net)
        directional_mask = (np.abs(predicted) > 1e-12) & (np.abs(net) > 1e-12)
        sign_hit = None if not directional_mask.any() else float(np.mean(signs[directional_mask] > 0))
        rank_ic = _correlation(_rank(predicted), _rank(net))
        recent_rows = rows[-recent_window:]
        recent_predicted = np.array([item.predicted_residual_return for item in recent_rows], dtype=float)
        recent_net = np.array(
            [item.realized_residual_return - item.implementation_cost_return for item in recent_rows],
            dtype=float,
        )
        recent_mask = (np.abs(recent_predicted) > 1e-12) & (np.abs(recent_net) > 1e-12)
        recent_hit = (
            None
            if not recent_mask.any()
            else float(np.mean((np.sign(recent_predicted[recent_mask]) * np.sign(recent_net[recent_mask])) > 0))
        )
        rmse = float(np.sqrt(np.mean(errors * errors)))
        reasons: list[str] = []
        sample_count = len(rows)
        recent_count = len(recent_rows)
        recent_mean = float(np.mean(recent_net)) if recent_rows else None

        if sample_count < minimum_samples or recent_count < minimum_recent_samples:
            state = "research_only"
            multiplier = 0.0
            reasons.append("minimum_samples_not_met")
        elif rmse > maximum_rmse:
            state = "quarantined"
            multiplier = 0.0
            reasons.append("forecast_error_excessive")
        elif recent_hit is not None and recent_hit < quarantine_hit_rate:
            state = "quarantined"
            multiplier = 0.0
            reasons.append("recent_direction_hit_rate_failed")
        elif recent_mean is not None and recent_mean < 0 and (recent_hit or 0.0) < decay_hit_rate:
            state = "quarantined"
            multiplier = 0.0
            reasons.append("recent_net_residual_negative")
        elif (recent_hit is not None and recent_hit < decay_hit_rate) or (sign_hit is not None and sign_hit < 0.50):
            state = "decayed"
            multiplier = 0.35
            reasons.append("directional_calibration_weak")
        elif (recent_hit is not None and recent_hit >= active_hit_rate) and (rank_ic is None or rank_ic >= 0.05):
            state = "active"
            multiplier = 1.0
            reasons.append("calibration_healthy")
        else:
            state = "decayed"
            multiplier = 0.60
            reasons.append("calibration_mixed")

        summaries.append(
            FactorResidualCalibrationSummary(
                signal_model_version=key[0],
                factor_model_version=key[1],
                horizon_sessions=key[2],
                market_regime=key[3],
                sample_count=sample_count,
                recent_sample_count=recent_count,
                mean_predicted_residual=round(float(np.mean(predicted)), 10),
                mean_realized_residual=round(float(np.mean(realized)), 10),
                mean_net_realized_residual=round(float(np.mean(net)), 10),
                mae=round(float(np.mean(np.abs(errors))), 10),
                rmse=round(rmse, 10),
                sign_hit_rate=None if sign_hit is None else round(sign_hit, 8),
                rank_ic=None if rank_ic is None else round(rank_ic, 8),
                recent_sign_hit_rate=None if recent_hit is None else round(recent_hit, 8),
                recent_mean_net_residual=None if recent_mean is None else round(recent_mean, 10),
                state=state,
                weight_multiplier=multiplier,
                reason_codes=tuple(reasons),
            )
        )

    return FactorResidualCalibrationResult(
        status="ok" if summaries else "no_data",
        as_of=cutoff.isoformat().replace("+00:00", "Z"),
        eligible_observation_count=len(parsed),
        excluded_observation_count=excluded,
        summaries=tuple(summaries),
        factor_versions=tuple(sorted({item.factor_model_version for item in parsed})),
    )


def select_calibration_summary(
    result: FactorResidualCalibrationResult,
    *,
    signal_model_version: str,
    factor_model_version: str,
    horizon_sessions: int,
    market_regime: str,
) -> FactorResidualCalibrationSummary | None:
    """Select one exact-version calibration record; never substitute another model."""

    key = (
        _safe_id(signal_model_version, "signal_model_version"),
        _safe_id(factor_model_version, "factor_model_version"),
        int(horizon_sessions),
        _safe_id(market_regime, "market_regime"),
    )
    for item in result.summaries:
        if (
            item.signal_model_version,
            item.factor_model_version,
            item.horizon_sessions,
            item.market_regime,
        ) == key:
            return item
    return None


__all__ = [
    "FactorCalibrationError",
    "FactorModelDescriptor",
    "FactorResidualCalibrationResult",
    "FactorResidualCalibrationSummary",
    "ResidualForecastObservation",
    "calibrate_factor_residuals",
    "select_calibration_summary",
]
