"""Multi-horizon factor research inspired by academic and practitioner controls.

The module combines purged walk-forward tests at 1/5/20-session horizons. It
separates daily monitoring from factor-definition changes, requires costs and
multiple-testing control, and aggregates only out-of-sample evidence.
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

import pandas as pd

from .factor_backtest import FactorBacktestResult, walk_forward_factor_backtest


_DEFAULT_RATIONALES = {
    "market_momentum_21": "short-horizon trend / behavioral underreaction",
    "market_momentum_63": "medium-horizon trend / behavioral underreaction",
    "market_volatility_21": "volatility-managed risk scaling",
    "semis_relative_21": "semiconductor industry leadership",
    "memory_relative_21": "memory/HBM cycle relative strength",
    "oil_relative_21": "energy and inflation transmission",
    "rates_relative_21": "duration and discount-rate transmission",
    "gold_relative_21": "safe-haven and real-rate transmission",
    "breadth_relative_21": "market breadth and small-cap participation",
    "defensive_relative_21": "quality/dividend defensive leadership",
}


@dataclass(frozen=True)
class HorizonSummary:
    horizon_sessions: int
    status: str
    model_version: str
    oos_observations: int
    net_annualized_return: float
    net_sharpe: float | None
    probabilistic_sharpe_ratio: float | None
    prediction_information_coefficient: float | None
    max_drawdown: float
    average_turnover: float
    total_cost_drag: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class InstitutionalFactorDiagnostic:
    factor: str
    economic_rationale: str
    horizons_tested: tuple[int, ...]
    active_horizons: tuple[int, ...]
    watch_horizons: tuple[int, ...]
    quarantined_horizons: tuple[int, ...]
    median_directional_ic: float | None
    best_multiple_testing_q: float | None
    median_robustness_score: float
    horizon_sign_consistency: float
    admission_status: str
    effective_weight_multiplier: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        payload = self.__dict__.copy()
        payload["horizons_tested"] = list(self.horizons_tested)
        payload["active_horizons"] = list(self.active_horizons)
        payload["watch_horizons"] = list(self.watch_horizons)
        payload["quarantined_horizons"] = list(self.quarantined_horizons)
        return payload


@dataclass(frozen=True)
class InstitutionalFactorResearchResult:
    status: str
    as_of: str
    feature_version: str
    horizon_summaries: tuple[HorizonSummary, ...]
    factor_diagnostics: tuple[InstitutionalFactorDiagnostic, ...]
    active_factors: tuple[str, ...]
    watch_factors: tuple[str, ...]
    quarantined_factors: tuple[str, ...]
    risk_budget_multiplier: float
    daily_monitoring: bool
    factor_definition_review_cadence: str
    model_refit_policy: str
    warnings: tuple[str, ...]
    horizon_results: tuple[FactorBacktestResult, ...] = field(repr=False)
    automatic_trading_permitted: bool = False

    @property
    def primary_result(self) -> FactorBacktestResult | None:
        for result in self.horizon_results:
            if result.horizon_sessions == 5:
                return result
        return self.horizon_results[0] if self.horizon_results else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "as_of": self.as_of,
            "feature_version": self.feature_version,
            "horizon_summaries": [item.to_dict() for item in self.horizon_summaries],
            "factor_diagnostics": [item.to_dict() for item in self.factor_diagnostics],
            "active_factors": list(self.active_factors),
            "watch_factors": list(self.watch_factors),
            "quarantined_factors": list(self.quarantined_factors),
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "daily_monitoring": self.daily_monitoring,
            "factor_definition_review_cadence": self.factor_definition_review_cadence,
            "model_refit_policy": self.model_refit_policy,
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def _median(values: Iterable[float | None]) -> float | None:
    valid = [float(value) for value in values if value is not None]
    return None if not valid else float(statistics.median(valid))


def run_institutional_factor_research(
    signals: pd.DataFrame,
    daily_returns: pd.Series,
    *,
    as_of: dt.date | None = None,
    feature_version: str = "institutional_factor_library/v1.0.0",
    horizons: tuple[int, ...] = (1, 5, 20),
    train_size: int = 504,
    test_size: int = 63,
    transaction_cost_bps: float = 5.0,
    economic_rationales: Mapping[str, str] | None = None,
) -> InstitutionalFactorResearchResult:
    """Run the same versioned factor library across multiple OOS horizons."""

    if not horizons or any(int(value) < 1 for value in horizons):
        raise ValueError("horizons must contain positive sessions")
    rationales = {**_DEFAULT_RATIONALES, **dict(economic_rationales or {})}
    results: list[FactorBacktestResult] = []
    failures: list[str] = []
    for horizon in sorted(set(int(value) for value in horizons)):
        try:
            results.append(
                walk_forward_factor_backtest(
                    signals,
                    daily_returns=daily_returns,
                    target_name="private_portfolio_proxy",
                    feature_version=f"{feature_version}/h{horizon}",
                    horizon_sessions=horizon,
                    train_size=train_size,
                    test_size=max(test_size, horizon * 3),
                    step_size=max(test_size, horizon * 3),
                    expanding=True,
                    ridge=5.0,
                    transaction_cost_bps=transaction_cost_bps,
                    minimum_oos_observations=24,
                    purge_sessions=horizon,
                    embargo_sessions=horizon,
                    multiple_testing_alpha=0.10,
                )
            )
        except ValueError as exc:
            failures.append(f"h{horizon}:{type(exc).__name__}")
    if not results:
        raise ValueError("all institutional factor horizons failed")

    factor_names = sorted(
        {item.factor for result in results for item in result.factor_diagnostics}
    )
    diagnostics: list[InstitutionalFactorDiagnostic] = []
    for factor in factor_names:
        rows = [
            (result.horizon_sessions, item)
            for result in results
            for item in result.factor_diagnostics
            if item.factor == factor
        ]
        active = tuple(horizon for horizon, item in rows if item.admission_status == "active")
        watch = tuple(horizon for horizon, item in rows if item.admission_status == "watch")
        quarantined = tuple(
            horizon
            for horizon, item in rows
            if item.admission_status in {"quarantined", "blocked"}
        )
        directional_ics = [item.directional_information_coefficient for _, item in rows]
        median_ic = _median(directional_ics)
        q_values = [item.multiple_testing_q_value for _, item in rows]
        best_q_values = [value for value in q_values if value is not None]
        best_q = min(best_q_values) if best_q_values else None
        median_robustness = _median(item.robustness_score for _, item in rows) or 0.0
        signs = [1 if value and value > 0 else -1 if value and value < 0 else 0 for value in directional_ics]
        nonzero_signs = [value for value in signs if value]
        sign_consistency = (
            max(nonzero_signs.count(1), nonzero_signs.count(-1)) / len(nonzero_signs)
            if nonzero_signs
            else 0.0
        )
        weights = [item.effective_weight_multiplier for _, item in rows if item.admission_status in {"active", "watch"}]
        coverage = len(active) / len(rows) if rows else 0.0
        effective_weight = (statistics.median(weights) * (0.5 + 0.5 * coverage)) if weights else 0.0

        if len(active) >= 2 and sign_consistency >= 2 / 3 and (best_q is not None and best_q <= 0.10):
            status = "active"
            reason = "effective_across_multiple_horizons"
        elif len(active) + len(watch) >= 2 and median_ic is not None and median_ic > 0:
            status = "watch"
            reason = "positive_but_not_multi_horizon_strong"
            effective_weight = min(0.50, effective_weight)
        else:
            status = "quarantined"
            reason = "insufficient_cross_horizon_evidence"
            effective_weight = 0.0
        diagnostics.append(
            InstitutionalFactorDiagnostic(
                factor=factor,
                economic_rationale=rationales.get(factor, "portfolio-specific proxy; rationale review required"),
                horizons_tested=tuple(horizon for horizon, _ in rows),
                active_horizons=active,
                watch_horizons=watch,
                quarantined_horizons=quarantined,
                median_directional_ic=None if median_ic is None else round(median_ic, 6),
                best_multiple_testing_q=None if best_q is None else round(best_q, 6),
                median_robustness_score=round(median_robustness, 6),
                horizon_sign_consistency=round(sign_consistency, 6),
                admission_status=status,
                effective_weight_multiplier=round(max(0.0, min(1.0, effective_weight)), 6),
                reason=reason,
            )
        )

    active_factors = tuple(item.factor for item in diagnostics if item.admission_status == "active")
    watch_factors = tuple(item.factor for item in diagnostics if item.admission_status == "watch")
    quarantined_factors = tuple(
        item.factor for item in diagnostics if item.admission_status == "quarantined"
    )
    active_horizon_models = sum(result.status == "active" for result in results)
    if len(active_factors) >= 2 and active_horizon_models >= 2:
        status, risk_multiplier = "active", 1.0
    elif active_factors or watch_factors:
        status, risk_multiplier = "research_only", 1.0
    else:
        status, risk_multiplier = "quarantined", 0.95

    summaries = tuple(
        HorizonSummary(
            horizon_sessions=result.horizon_sessions,
            status=result.status,
            model_version=result.model_version,
            oos_observations=result.oos_observations,
            net_annualized_return=result.net_annualized_return,
            net_sharpe=result.net_sharpe,
            probabilistic_sharpe_ratio=result.probabilistic_sharpe_ratio,
            prediction_information_coefficient=result.prediction_information_coefficient,
            max_drawdown=result.max_drawdown,
            average_turnover=result.average_turnover,
            total_cost_drag=result.total_cost_drag,
        )
        for result in results
    )
    warnings = [
        "Daily runs append new evidence; factor definitions are not changed by daily noise.",
        "Admission requires purged OOS evidence, costs, FDR control and cross-horizon stability.",
        "Capacity, liquidity, taxes and portfolio fit remain downstream owner gates.",
    ]
    if failures:
        warnings.append("Unavailable horizons: " + ", ".join(failures))
    return InstitutionalFactorResearchResult(
        status=status,
        as_of=(as_of or dt.date.today()).isoformat(),
        feature_version=feature_version,
        horizon_summaries=summaries,
        factor_diagnostics=tuple(diagnostics),
        active_factors=active_factors,
        watch_factors=watch_factors,
        quarantined_factors=quarantined_factors,
        risk_budget_multiplier=risk_multiplier,
        daily_monitoring=True,
        factor_definition_review_cadence="monthly_or_on_data_definition_change",
        model_refit_policy="daily_append_with_versioned_purged_walk_forward",
        warnings=tuple(warnings),
        horizon_results=tuple(results),
    )


__all__ = [
    "HorizonSummary",
    "InstitutionalFactorDiagnostic",
    "InstitutionalFactorResearchResult",
    "run_institutional_factor_research",
]
