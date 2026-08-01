"""Shared numerical helpers for the optional Pro research suite.

The module intentionally depends only on NumPy/Pandas and contains no network,
broker, file-system, or delivery side effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


EPS = 1e-12


def clamp(value: float, lower: float, upper: float) -> float:
    """Clamp a finite float to a closed interval."""

    number = float(value)
    if not np.isfinite(number):
        raise ValueError("value must be finite")
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return float(min(max(number, lower), upper))


def safe_float(value: object, default: float | None = None) -> float | None:
    """Parse one finite float without converting missing values to zero."""

    if value in (None, ""):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if np.isfinite(number) else default


def aligned_series(*series: pd.Series, min_observations: int = 2) -> tuple[pd.Series, ...]:
    """Inner-align numeric series and remove rows containing any missing value."""

    if not series:
        raise ValueError("at least one series is required")
    frame = pd.concat(
        [pd.to_numeric(item, errors="coerce").rename(str(index)) for index, item in enumerate(series)],
        axis=1,
        join="inner",
    ).dropna()
    if len(frame) < min_observations:
        raise ValueError(f"at least {min_observations} aligned observations are required")
    return tuple(frame.iloc[:, index] for index in range(frame.shape[1]))


def aligned_frames(
    asset_returns: pd.DataFrame,
    factor_returns: pd.DataFrame,
    *,
    min_observations: int = 20,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Align two return matrices on date and retain numeric columns only."""

    assets = asset_returns.apply(pd.to_numeric, errors="coerce")
    factors = factor_returns.apply(pd.to_numeric, errors="coerce")
    common = assets.index.intersection(factors.index)
    assets = assets.loc[common]
    factors = factors.loc[common]
    valid = ~(assets.isna().any(axis=1) | factors.isna().any(axis=1))
    assets, factors = assets.loc[valid], factors.loc[valid]
    if len(assets) < min_observations:
        raise ValueError(f"at least {min_observations} aligned rows are required")
    if assets.empty or factors.empty:
        raise ValueError("asset and factor matrices must not be empty")
    return assets, factors


@dataclass(frozen=True)
class RegressionResult:
    intercept: float
    coefficients: tuple[float, ...]
    residuals: np.ndarray
    fitted: np.ndarray
    r_squared: float
    standard_errors: tuple[float, ...]
    intercept_standard_error: float


def ridge_regression(
    y: Sequence[float] | np.ndarray,
    x: Sequence[Sequence[float]] | np.ndarray,
    *,
    ridge: float = 1e-6,
    penalize_intercept: bool = False,
) -> RegressionResult:
    """Fit a small deterministic ridge regression with an explicit intercept."""

    y_array = np.asarray(y, dtype=float).reshape(-1)
    x_array = np.asarray(x, dtype=float)
    if x_array.ndim == 1:
        x_array = x_array.reshape(-1, 1)
    if len(y_array) != x_array.shape[0]:
        raise ValueError("x and y lengths differ")
    if len(y_array) <= x_array.shape[1] + 2:
        raise ValueError("insufficient observations for regression")
    if not np.isfinite(y_array).all() or not np.isfinite(x_array).all():
        raise ValueError("regression inputs must be finite")
    if ridge < 0:
        raise ValueError("ridge must be non-negative")

    design = np.column_stack([np.ones(len(y_array)), x_array])
    penalty = np.eye(design.shape[1]) * float(ridge)
    if not penalize_intercept:
        penalty[0, 0] = 0.0
    gram = design.T @ design + penalty
    inverse = np.linalg.pinv(gram)
    beta = inverse @ design.T @ y_array
    fitted = design @ beta
    residuals = y_array - fitted
    ss_res = float(residuals @ residuals)
    centered = y_array - y_array.mean()
    ss_tot = float(centered @ centered)
    r_squared = 0.0 if ss_tot <= EPS else 1.0 - ss_res / ss_tot

    dof = max(1, len(y_array) - design.shape[1])
    residual_variance = ss_res / dof
    covariance = inverse @ (design.T @ design) @ inverse * residual_variance
    standard_errors = np.sqrt(np.maximum(np.diag(covariance), 0.0))
    return RegressionResult(
        intercept=float(beta[0]),
        coefficients=tuple(float(value) for value in beta[1:]),
        residuals=residuals,
        fitted=fitted,
        r_squared=float(r_squared),
        standard_errors=tuple(float(value) for value in standard_errors[1:]),
        intercept_standard_error=float(standard_errors[0]),
    )


def normalized_weights(
    weights: Mapping[str, float] | Iterable[tuple[str, float]],
    columns: Sequence[str],
) -> np.ndarray:
    """Return non-negative normalized weights in the requested column order."""

    mapping = dict(weights)
    vector = np.array([float(mapping.get(column, 0.0)) for column in columns], dtype=float)
    if not np.isfinite(vector).all() or (vector < 0).any():
        raise ValueError("weights must be finite and non-negative")
    total = float(vector.sum())
    if total <= EPS:
        raise ValueError("at least one positive portfolio weight is required")
    return vector / total


def annualized_return(daily_mean: float, periods_per_year: int = 252) -> float:
    return float(daily_mean) * int(periods_per_year)


def annualized_volatility(daily_std: float, periods_per_year: int = 252) -> float:
    return float(daily_std) * float(np.sqrt(periods_per_year))


def stable_sigmoid(value: float) -> float:
    number = float(value)
    if number >= 0:
        z = np.exp(-number)
        return float(1.0 / (1.0 + z))
    z = np.exp(number)
    return float(z / (1.0 + z))
