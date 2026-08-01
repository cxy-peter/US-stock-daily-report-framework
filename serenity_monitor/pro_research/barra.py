"""Barra-inspired public factor-risk proxy.

This is not MSCI Barra data or a replica of a commercial model. It provides an
auditable public-data proxy: ridge factor exposures, shrunk factor covariance,
specific risk, portfolio factor contributions and marginal asset risk.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .common import aligned_frames, annualized_volatility, clamp, normalized_weights, ridge_regression


@dataclass(frozen=True)
class AssetFactorFit:
    asset: str
    intercept_annualized: float
    factor_exposures: Mapping[str, float]
    r_squared: float
    specific_volatility: float


@dataclass(frozen=True)
class BarraProxyResult:
    status: str
    observations: int
    assets: tuple[str, ...]
    factors: tuple[str, ...]
    portfolio_exposures: Mapping[str, float]
    factor_risk_contributions: Mapping[str, float]
    asset_risk_contributions: Mapping[str, float]
    systematic_risk_share: float
    specific_risk_share: float
    annualized_volatility: float
    effective_factor_count: float
    top_factor: str | None
    top_factor_risk_share: float
    risk_budget_multiplier: float
    fits: tuple[AssetFactorFit, ...]
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observations": self.observations,
            "assets": list(self.assets),
            "factors": list(self.factors),
            "portfolio_exposures": dict(self.portfolio_exposures),
            "factor_risk_contributions": dict(self.factor_risk_contributions),
            "asset_risk_contributions": dict(self.asset_risk_contributions),
            "systematic_risk_share": self.systematic_risk_share,
            "specific_risk_share": self.specific_risk_share,
            "annualized_volatility": self.annualized_volatility,
            "effective_factor_count": self.effective_factor_count,
            "top_factor": self.top_factor,
            "top_factor_risk_share": self.top_factor_risk_share,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "fits": [
                {
                    "asset": item.asset,
                    "intercept_annualized": item.intercept_annualized,
                    "factor_exposures": dict(item.factor_exposures),
                    "r_squared": item.r_squared,
                    "specific_volatility": item.specific_volatility,
                }
                for item in self.fits
            ],
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def _shrink_covariance(matrix: np.ndarray, shrinkage: float) -> np.ndarray:
    covariance = np.cov(matrix, rowvar=False, ddof=1)
    if covariance.ndim == 0:
        covariance = np.array([[float(covariance)]], dtype=float)
    diagonal = np.diag(np.diag(covariance))
    return (1.0 - shrinkage) * covariance + shrinkage * diagonal


def fit_barra_proxy(
    asset_returns: pd.DataFrame,
    factor_returns: pd.DataFrame,
    portfolio_weights: Mapping[str, float],
    *,
    ridge: float = 1e-4,
    covariance_shrinkage: float = 0.25,
    periods_per_year: int = 252,
    min_observations: int = 60,
) -> BarraProxyResult:
    """Fit asset factor exposures and decompose portfolio risk."""

    if not 0.0 <= covariance_shrinkage <= 1.0:
        raise ValueError("covariance_shrinkage must be between zero and one")
    if periods_per_year < 1:
        raise ValueError("periods_per_year must be positive")
    assets, factors = aligned_frames(
        asset_returns,
        factor_returns,
        min_observations=min_observations,
    )
    asset_names = tuple(str(column) for column in assets.columns)
    factor_names = tuple(str(column) for column in factors.columns)
    weights = normalized_weights(portfolio_weights, asset_names)

    factor_matrix = factors.to_numpy(dtype=float)
    exposure_matrix = np.zeros((len(asset_names), len(factor_names)), dtype=float)
    specific_variances = np.zeros(len(asset_names), dtype=float)
    fits: list[AssetFactorFit] = []
    for index, asset in enumerate(asset_names):
        result = ridge_regression(
            assets[asset].to_numpy(dtype=float),
            factor_matrix,
            ridge=ridge,
        )
        exposure_matrix[index] = np.asarray(result.coefficients, dtype=float)
        residual_std = float(np.std(result.residuals, ddof=max(1, len(result.coefficients) + 1)))
        specific_variances[index] = residual_std**2 * periods_per_year
        fits.append(
            AssetFactorFit(
                asset=asset,
                intercept_annualized=round(result.intercept * periods_per_year, 8),
                factor_exposures={
                    factor: round(float(value), 8)
                    for factor, value in zip(factor_names, result.coefficients)
                },
                r_squared=round(result.r_squared, 8),
                specific_volatility=round(annualized_volatility(residual_std, periods_per_year), 8),
            )
        )

    factor_covariance = _shrink_covariance(factor_matrix, covariance_shrinkage) * periods_per_year
    portfolio_exposure = exposure_matrix.T @ weights
    systematic_variance = float(portfolio_exposure @ factor_covariance @ portfolio_exposure)
    specific_variance = float(np.sum((weights**2) * specific_variances))
    total_variance = max(systematic_variance + specific_variance, 0.0)
    annual_volatility = float(np.sqrt(total_variance))

    factor_marginal = factor_covariance @ portfolio_exposure
    raw_factor_contrib = portfolio_exposure * factor_marginal
    factor_denominator = float(np.sum(np.abs(raw_factor_contrib)))
    if factor_denominator <= 1e-12:
        factor_contributions = np.zeros(len(factor_names), dtype=float)
    else:
        factor_contributions = raw_factor_contrib / factor_denominator

    asset_covariance = exposure_matrix @ factor_covariance @ exposure_matrix.T + np.diag(specific_variances)
    portfolio_covariance_vector = asset_covariance @ weights
    raw_asset_contrib = weights * portfolio_covariance_vector
    asset_denominator = float(np.sum(raw_asset_contrib))
    if abs(asset_denominator) <= 1e-12:
        asset_contributions = np.zeros(len(asset_names), dtype=float)
    else:
        asset_contributions = raw_asset_contrib / asset_denominator

    systematic_share = 0.0 if total_variance <= 1e-12 else systematic_variance / total_variance
    specific_share = 0.0 if total_variance <= 1e-12 else specific_variance / total_variance
    absolute_factor_shares = np.abs(factor_contributions)
    share_sum = float(absolute_factor_shares.sum())
    normalized_factor_shares = (
        absolute_factor_shares / share_sum if share_sum > 1e-12 else absolute_factor_shares
    )
    effective_factor_count = (
        0.0
        if share_sum <= 1e-12
        else float(1.0 / max(float(np.sum(normalized_factor_shares**2)), 1e-12))
    )
    if len(factor_names):
        top_index = int(np.argmax(absolute_factor_shares))
        top_factor = factor_names[top_index]
        top_share = float(absolute_factor_shares[top_index])
    else:  # pragma: no cover - aligned_frames prevents this
        top_factor, top_share = None, 0.0

    concentration_penalty = clamp((top_share - 0.35) / 0.45, 0.0, 1.0)
    volatility_penalty = clamp((annual_volatility - 0.20) / 0.25, 0.0, 1.0)
    effective_penalty = 0.5 * concentration_penalty + 0.5 * volatility_penalty
    risk_multiplier = clamp(1.0 - 0.25 * effective_penalty, 0.75, 1.0)

    warnings = [
        "This is a Barra-inspired public proxy, not commercial MSCI Barra output.",
        "Factor contributions are model-dependent and do not create trades by themselves.",
    ]
    if len(assets) < 126:
        warnings.append("The estimation window is shorter than six months of trading sessions.")
    if top_share > 0.50:
        warnings.append("One factor explains more than half of absolute modeled factor risk.")

    return BarraProxyResult(
        status="ok",
        observations=len(assets),
        assets=asset_names,
        factors=factor_names,
        portfolio_exposures={
            factor: round(float(value), 8)
            for factor, value in zip(factor_names, portfolio_exposure)
        },
        factor_risk_contributions={
            factor: round(float(value), 8)
            for factor, value in zip(factor_names, factor_contributions)
        },
        asset_risk_contributions={
            asset: round(float(value), 8)
            for asset, value in zip(asset_names, asset_contributions)
        },
        systematic_risk_share=round(systematic_share, 8),
        specific_risk_share=round(specific_share, 8),
        annualized_volatility=round(annual_volatility, 8),
        effective_factor_count=round(effective_factor_count, 8),
        top_factor=top_factor,
        top_factor_risk_share=round(top_share, 8),
        risk_budget_multiplier=round(risk_multiplier, 8),
        fits=tuple(fits),
        warnings=tuple(warnings),
    )
