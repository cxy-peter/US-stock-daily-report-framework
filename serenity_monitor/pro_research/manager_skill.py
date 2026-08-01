"""Fund-manager skill, timing, persistence and fragility diagnostics."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from .common import aligned_frames, annualized_return, annualized_volatility, clamp, ridge_regression, stable_sigmoid


@dataclass(frozen=True)
class ManagerFragility:
    gross_leverage: float | None = None
    top10_concentration: float | None = None
    liquidity_days: float | None = None
    prime_broker_concentration: float | None = None
    tenure_months: int | None = None
    fund_age_months: int | None = None


@dataclass(frozen=True)
class ManagerSkillResult:
    status: str
    observations: int
    annualized_alpha: float | None
    alpha_t_stat: float | None
    factor_betas: Mapping[str, float]
    r_squared: float | None
    bootstrap_skill_probability: float | None
    treynor_mazuy_timing: float | None
    henriksson_merton_timing: float | None
    up_capture: float | None
    down_capture: float | None
    rolling_alpha_positive_share: float | None
    annualized_tracking_error: float | None
    skill_score: float | None
    fragility_score: float
    copy_trade_allowed: bool
    verdict: str
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "observations": self.observations,
            "annualized_alpha": self.annualized_alpha,
            "alpha_t_stat": self.alpha_t_stat,
            "factor_betas": dict(self.factor_betas),
            "r_squared": self.r_squared,
            "bootstrap_skill_probability": self.bootstrap_skill_probability,
            "treynor_mazuy_timing": self.treynor_mazuy_timing,
            "henriksson_merton_timing": self.henriksson_merton_timing,
            "up_capture": self.up_capture,
            "down_capture": self.down_capture,
            "rolling_alpha_positive_share": self.rolling_alpha_positive_share,
            "annualized_tracking_error": self.annualized_tracking_error,
            "skill_score": self.skill_score,
            "fragility_score": self.fragility_score,
            "copy_trade_allowed": self.copy_trade_allowed,
            "verdict": self.verdict,
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def _capture_ratio(fund: np.ndarray, market: np.ndarray, positive: bool) -> float | None:
    mask = market > 0 if positive else market < 0
    if not mask.any():
        return None
    denominator = float(np.mean(market[mask]))
    if abs(denominator) <= 1e-12:
        return None
    return float(np.mean(fund[mask]) / denominator)


def _rolling_alpha_share(
    fund: pd.Series,
    factors: pd.DataFrame,
    *,
    window: int,
    ridge: float,
) -> float | None:
    if len(fund) < window + 20:
        return None
    positive = 0
    total = 0
    for end in range(window, len(fund) + 1, max(5, window // 8)):
        start = end - window
        try:
            fit = ridge_regression(
                fund.iloc[start:end].to_numpy(dtype=float),
                factors.iloc[start:end].to_numpy(dtype=float),
                ridge=ridge,
            )
        except ValueError:
            continue
        total += 1
        positive += int(fit.intercept > 0)
    return None if total == 0 else positive / total


def _fragility_score(fragility: ManagerFragility | None) -> tuple[float, list[str]]:
    if fragility is None:
        return 0.50, ["Manager fragility inputs are incomplete; copy-trade permission remains conservative."]
    components: list[float] = []
    warnings: list[str] = []
    if fragility.gross_leverage is not None:
        components.append(clamp((fragility.gross_leverage - 1.0) / 2.0, 0.0, 1.0))
        if fragility.gross_leverage >= 3.0:
            warnings.append("Gross leverage is at or above 3x.")
    else:
        components.append(0.50)
    if fragility.top10_concentration is not None:
        components.append(clamp((fragility.top10_concentration - 0.40) / 0.50, 0.0, 1.0))
        if fragility.top10_concentration >= 0.80:
            warnings.append("Top-10 concentration is at or above 80%.")
    else:
        components.append(0.50)
    if fragility.liquidity_days is not None:
        components.append(clamp((fragility.liquidity_days - 3.0) / 17.0, 0.0, 1.0))
        if fragility.liquidity_days >= 10.0:
            warnings.append("Estimated liquidation time exceeds ten trading days.")
    else:
        components.append(0.50)
    if fragility.prime_broker_concentration is not None:
        components.append(clamp((fragility.prime_broker_concentration - 0.40) / 0.60, 0.0, 1.0))
        if fragility.prime_broker_concentration >= 0.80:
            warnings.append("Prime-broker concentration is high.")
    else:
        components.append(0.50)
    if fragility.tenure_months is not None:
        components.append(clamp((24.0 - fragility.tenure_months) / 24.0, 0.0, 1.0))
    else:
        components.append(0.50)
    if fragility.fund_age_months is not None:
        components.append(clamp((24.0 - fragility.fund_age_months) / 24.0, 0.0, 1.0))
    else:
        components.append(0.50)
    direct = float(np.mean(components[:4]))
    maturity = float(np.mean(components[4:]))
    return 0.80 * direct + 0.20 * maturity, warnings


def evaluate_manager_skill(
    fund_returns: pd.Series,
    factor_returns: pd.DataFrame,
    *,
    risk_free_returns: pd.Series | None = None,
    market_factor: str | None = None,
    periods_per_year: int = 252,
    ridge: float = 1e-6,
    bootstrap_iterations: int = 500,
    bootstrap_seed: int = 42,
    rolling_window: int = 126,
    fragility: ManagerFragility | None = None,
) -> ManagerSkillResult:
    """Separate repeatable return evidence from manager/fund fragility."""

    fund_frame = fund_returns.rename("fund").to_frame()
    fund_aligned, factors = aligned_frames(
        fund_frame,
        factor_returns,
        min_observations=60,
    )
    fund = fund_aligned.iloc[:, 0]
    if risk_free_returns is not None:
        common = fund.index.intersection(risk_free_returns.index)
        common = common.intersection(factors.index)
        fund = fund.loc[common]
        factors = factors.loc[common]
        risk_free = pd.to_numeric(risk_free_returns.loc[common], errors="coerce")
        valid = ~(fund.isna() | factors.isna().any(axis=1) | risk_free.isna())
        fund, factors, risk_free = fund.loc[valid], factors.loc[valid], risk_free.loc[valid]
        excess_fund = fund - risk_free
        excess_factors = factors.copy()
    else:
        risk_free = None
        excess_fund = fund
        excess_factors = factors

    if len(excess_fund) < 60:
        raise ValueError("at least 60 aligned manager observations are required")
    fit = ridge_regression(
        excess_fund.to_numpy(dtype=float),
        excess_factors.to_numpy(dtype=float),
        ridge=ridge,
    )
    annual_alpha = annualized_return(fit.intercept, periods_per_year)
    alpha_t = (
        None
        if fit.intercept_standard_error <= 1e-12
        else fit.intercept / fit.intercept_standard_error
    )
    betas = {
        str(name): round(float(value), 8)
        for name, value in zip(excess_factors.columns, fit.coefficients)
    }
    tracking_error = annualized_volatility(
        float(np.std(fit.residuals, ddof=max(1, len(fit.coefficients) + 1))),
        periods_per_year,
    )

    factor_names = [str(column) for column in excess_factors.columns]
    if market_factor and market_factor in factor_names:
        market_name = market_factor
    else:
        candidates = [
            name for name in factor_names if name.casefold() in {"market", "mkt", "mkt_rf", "spy"}
        ]
        market_name = candidates[0] if candidates else factor_names[0]
    market = excess_factors[market_name].to_numpy(dtype=float)
    fund_values = excess_fund.to_numpy(dtype=float)

    tm_design = np.column_stack([market, market**2])
    tm_fit = ridge_regression(fund_values, tm_design, ridge=ridge)
    tm_timing = float(tm_fit.coefficients[1])
    hm_design = np.column_stack([market, np.maximum(market, 0.0)])
    hm_fit = ridge_regression(fund_values, hm_design, ridge=ridge)
    hm_timing = float(hm_fit.coefficients[1])

    up_capture = _capture_ratio(fund_values, market, True)
    down_capture = _capture_ratio(fund_values, market, False)
    rolling_share = _rolling_alpha_share(
        excess_fund,
        excess_factors,
        window=min(rolling_window, max(40, len(excess_fund) // 2)),
        ridge=ridge,
    )

    bootstrap_probability: float | None
    if bootstrap_iterations < 50:
        bootstrap_probability = None
    else:
        rng = np.random.default_rng(bootstrap_seed)
        x = excess_factors.to_numpy(dtype=float)
        null_fitted = x @ np.asarray(fit.coefficients, dtype=float)
        bootstrap_alphas = np.empty(bootstrap_iterations, dtype=float)
        centered_residuals = fit.residuals - float(np.mean(fit.residuals))
        for index in range(bootstrap_iterations):
            pseudo = null_fitted + rng.choice(centered_residuals, size=len(centered_residuals), replace=True)
            pseudo_fit = ridge_regression(pseudo, x, ridge=ridge)
            bootstrap_alphas[index] = pseudo_fit.intercept
        bootstrap_probability = float(np.mean(bootstrap_alphas <= fit.intercept))

    fragility_score, fragility_warnings = _fragility_score(fragility)
    evidence_score = stable_sigmoid((annual_alpha / max(tracking_error, 1e-6)) * 2.0)
    t_score = stable_sigmoid((alpha_t or 0.0) - 1.0)
    bootstrap_score = bootstrap_probability if bootstrap_probability is not None else 0.50
    persistence_score = rolling_share if rolling_share is not None else 0.50
    timing_score = stable_sigmoid(5.0 * (tm_timing + hm_timing))
    skill_score = clamp(
        0.30 * evidence_score
        + 0.20 * t_score
        + 0.25 * bootstrap_score
        + 0.15 * persistence_score
        + 0.10 * timing_score,
        0.0,
        1.0,
    )

    copy_trade_allowed = (
        len(excess_fund) >= 252
        and skill_score >= 0.65
        and fragility_score < 0.45
        and (bootstrap_probability or 0.0) >= 0.70
    )
    if len(excess_fund) < 126:
        verdict = "NEED_INFO"
    elif skill_score >= 0.70 and fragility_score < 0.45:
        verdict = "PASS"
    elif skill_score >= 0.50 and fragility_score < 0.70:
        verdict = "WATCH"
    else:
        verdict = "REJECT"

    warnings = [
        "Manager skill is attributed only to the supplied observation period.",
        "A credible research record does not make a leveraged or illiquid portfolio copyable.",
        "This module never converts manager research into automatic execution.",
        *fragility_warnings,
    ]
    if len(excess_fund) < 252:
        warnings.append("The sample is shorter than one trading year; copy-trade permission is disabled.")
    if risk_free is None:
        warnings.append("Risk-free returns were not supplied; alpha is calculated against raw factor returns.")

    return ManagerSkillResult(
        status="ok",
        observations=len(excess_fund),
        annualized_alpha=round(annual_alpha, 8),
        alpha_t_stat=None if alpha_t is None else round(float(alpha_t), 8),
        factor_betas=betas,
        r_squared=round(fit.r_squared, 8),
        bootstrap_skill_probability=(
            None if bootstrap_probability is None else round(bootstrap_probability, 8)
        ),
        treynor_mazuy_timing=round(tm_timing, 8),
        henriksson_merton_timing=round(hm_timing, 8),
        up_capture=None if up_capture is None else round(up_capture, 8),
        down_capture=None if down_capture is None else round(down_capture, 8),
        rolling_alpha_positive_share=(
            None if rolling_share is None else round(rolling_share, 8)
        ),
        annualized_tracking_error=round(tracking_error, 8),
        skill_score=round(skill_score, 8),
        fragility_score=round(fragility_score, 8),
        copy_trade_allowed=copy_trade_allowed,
        verdict=verdict,
        warnings=tuple(warnings),
    )
