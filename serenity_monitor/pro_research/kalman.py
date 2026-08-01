"""Kalman-filtered dynamic beta and factor exposure estimation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .common import aligned_frames, clamp, ridge_regression


@dataclass(frozen=True)
class KalmanExposureResult:
    status: str
    observations: int
    factor_names: tuple[str, ...]
    latest_exposures: Mapping[str, float]
    latest_standard_errors: Mapping[str, float]
    initial_exposures: Mapping[str, float]
    exposure_changes: Mapping[str, float]
    observation_variance: float
    process_variance: float
    risk_budget_multiplier: float
    path: pd.DataFrame
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self, *, include_path: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "observations": self.observations,
            "factor_names": list(self.factor_names),
            "latest_exposures": dict(self.latest_exposures),
            "latest_standard_errors": dict(self.latest_standard_errors),
            "initial_exposures": dict(self.initial_exposures),
            "exposure_changes": dict(self.exposure_changes),
            "observation_variance": self.observation_variance,
            "process_variance": self.process_variance,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }
        if include_path:
            payload["path"] = [
                {
                    "date": str(index),
                    **{column: float(value) for column, value in row.items()},
                }
                for index, row in self.path.iterrows()
            ]
        return payload


def kalman_dynamic_exposures(
    asset_returns: pd.Series,
    factor_returns: pd.DataFrame,
    *,
    include_intercept: bool = True,
    process_variance: float = 1e-5,
    observation_variance: float | None = None,
    initial_window: int = 40,
    ridge: float = 1e-5,
) -> KalmanExposureResult:
    """Estimate time-varying linear exposures with a random-walk state model."""

    if process_variance <= 0 or not np.isfinite(process_variance):
        raise ValueError("process_variance must be finite and positive")
    factors_frame = factor_returns.copy()
    assets_frame = asset_returns.rename("asset").to_frame()
    assets, factors = aligned_frames(
        assets_frame,
        factors_frame,
        min_observations=max(20, initial_window),
    )
    y = assets.iloc[:, 0].to_numpy(dtype=float)
    x = factors.to_numpy(dtype=float)
    factor_names = tuple(str(column) for column in factors.columns)
    state_names = (("alpha",) + factor_names) if include_intercept else factor_names
    if include_intercept:
        design = np.column_stack([np.ones(len(x)), x])
    else:
        design = x

    init_count = min(max(initial_window, design.shape[1] + 5), len(y))
    initial_fit = ridge_regression(y[:init_count], x[:init_count], ridge=ridge)
    if include_intercept:
        state = np.array([initial_fit.intercept, *initial_fit.coefficients], dtype=float)
    else:
        state = np.array(initial_fit.coefficients, dtype=float)
    initial_state = state.copy()
    dimension = len(state)
    covariance = np.eye(dimension, dtype=float) * 0.10
    process_covariance = np.eye(dimension, dtype=float) * float(process_variance)

    if observation_variance is None:
        residual_variance = float(np.var(initial_fit.residuals, ddof=1))
        observation_variance = max(residual_variance, 1e-8)
    if observation_variance <= 0 or not np.isfinite(observation_variance):
        raise ValueError("observation_variance must be finite and positive")

    states: list[np.ndarray] = []
    standard_errors: list[np.ndarray] = []
    identity = np.eye(dimension, dtype=float)
    for row, observation in zip(design, y):
        predicted_state = state
        predicted_covariance = covariance + process_covariance
        row_vector = row.reshape(1, -1)
        innovation_variance = float(
            (row_vector @ predicted_covariance @ row_vector.T).item()
            + observation_variance
        )
        gain = (predicted_covariance @ row_vector.T).reshape(-1) / max(
            innovation_variance, 1e-12
        )
        innovation = float(observation - row @ predicted_state)
        state = predicted_state + gain * innovation
        covariance = (identity - np.outer(gain, row)) @ predicted_covariance
        covariance = 0.5 * (covariance + covariance.T)
        states.append(state.copy())
        standard_errors.append(np.sqrt(np.maximum(np.diag(covariance), 0.0)))

    path = pd.DataFrame(states, index=assets.index, columns=state_names)
    se_path = pd.DataFrame(
        standard_errors,
        index=assets.index,
        columns=[f"{name}_se" for name in state_names],
    )
    combined = pd.concat([path, se_path], axis=1)

    latest = path.iloc[-1]
    latest_se = se_path.iloc[-1]
    initial_mapping = {
        name: round(float(value), 8) for name, value in zip(state_names, initial_state)
    }
    latest_mapping = {name: round(float(latest[name]), 8) for name in state_names}
    latest_se_mapping = {
        name: round(float(latest_se[f"{name}_se"]), 8) for name in state_names
    }
    changes = {
        name: round(latest_mapping[name] - initial_mapping[name], 8)
        for name in state_names
    }

    market_candidates = [
        name for name in factor_names if name.casefold() in {"market", "spy", "mkt", "mkt_rf"}
    ]
    if market_candidates:
        market_name = market_candidates[0]
        market_beta = latest_mapping[market_name]
        market_shift = abs(changes[market_name])
    else:
        market_beta = max((abs(latest_mapping[name]) for name in factor_names), default=0.0)
        market_shift = max((abs(changes[name]) for name in factor_names), default=0.0)
    beta_penalty = clamp((abs(market_beta) - 1.0) / 1.0, 0.0, 1.0)
    shift_penalty = clamp((market_shift - 0.20) / 0.60, 0.0, 1.0)
    risk_multiplier = clamp(1.0 - 0.10 * beta_penalty - 0.10 * shift_penalty, 0.80, 1.0)

    warnings = [
        "Kalman exposures are return-inferred estimates, not disclosed holdings.",
        "The filter is a monitoring signal and cannot independently create a trade.",
    ]
    if len(y) < 126:
        warnings.append("The dynamic-exposure history is shorter than six trading months.")
    if market_shift > 0.50:
        warnings.append("The estimated market exposure changed materially from its initial state.")

    return KalmanExposureResult(
        status="ok",
        observations=len(y),
        factor_names=state_names,
        latest_exposures=latest_mapping,
        latest_standard_errors=latest_se_mapping,
        initial_exposures=initial_mapping,
        exposure_changes=changes,
        observation_variance=round(float(observation_variance), 12),
        process_variance=round(float(process_variance), 12),
        risk_budget_multiplier=round(risk_multiplier, 8),
        path=combined,
        warnings=tuple(warnings),
    )
