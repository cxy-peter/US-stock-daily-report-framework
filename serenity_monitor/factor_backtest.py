"""Point-in-time walk-forward regression and factor admission.

The engine is designed for daily monitoring, not daily factor invention. It
uses purged train/test windows, non-overlapping forward returns, transaction
costs, multiple-testing control and version isolation. Results are research
calibration only and never create an order.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd
from scipy.stats import norm, spearmanr


@dataclass(frozen=True)
class WalkForwardFold:
    fold_id: str
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_observations: int
    test_observations: int
    coefficients: Mapping[str, float]
    intercept: float
    target_scale: float
    purge_sessions: int = 0
    embargo_sessions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "train_observations": self.train_observations,
            "test_observations": self.test_observations,
            "coefficients": dict(self.coefficients),
            "intercept": self.intercept,
            "target_scale": self.target_scale,
            "purge_sessions": self.purge_sessions,
            "embargo_sessions": self.embargo_sessions,
        }


@dataclass(frozen=True)
class FactorDiagnostic:
    factor: str
    observations: int
    mean_standardized_coefficient: float
    coefficient_sign_consistency: float
    oos_information_coefficient: float | None
    directional_information_coefficient: float | None
    admission_status: str
    effective_weight_multiplier: float
    reason: str
    oos_p_value: float | None = None
    multiple_testing_q_value: float | None = None
    robustness_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class FactorBacktestResult:
    status: str
    feature_version: str
    model_version: str
    target_name: str
    horizon_sessions: int
    train_size: int
    test_size: int
    step_size: int
    transaction_cost_bps: float
    folds: tuple[WalkForwardFold, ...]
    factor_diagnostics: tuple[FactorDiagnostic, ...]
    oos_observations: int
    gross_mean_return: float
    net_mean_return: float
    gross_annualized_return: float
    net_annualized_return: float
    net_annualized_volatility: float
    net_sharpe: float | None
    hit_rate: float | None
    prediction_information_coefficient: float | None
    oos_r2: float | None
    max_drawdown: float
    average_turnover: float
    total_cost_drag: float
    risk_budget_multiplier: float
    warnings: tuple[str, ...]
    oos_records: tuple[Mapping[str, Any], ...]
    purge_sessions: int = 0
    embargo_sessions: int = 0
    probabilistic_sharpe_ratio: float | None = None
    multiple_testing_alpha: float = 0.10
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "feature_version": self.feature_version,
            "model_version": self.model_version,
            "target_name": self.target_name,
            "horizon_sessions": self.horizon_sessions,
            "train_size": self.train_size,
            "test_size": self.test_size,
            "step_size": self.step_size,
            "transaction_cost_bps": self.transaction_cost_bps,
            "purge_sessions": self.purge_sessions,
            "embargo_sessions": self.embargo_sessions,
            "multiple_testing_alpha": self.multiple_testing_alpha,
            "folds": [fold.to_dict() for fold in self.folds],
            "factor_diagnostics": [item.to_dict() for item in self.factor_diagnostics],
            "oos_observations": self.oos_observations,
            "gross_mean_return": self.gross_mean_return,
            "net_mean_return": self.net_mean_return,
            "gross_annualized_return": self.gross_annualized_return,
            "net_annualized_return": self.net_annualized_return,
            "net_annualized_volatility": self.net_annualized_volatility,
            "net_sharpe": self.net_sharpe,
            "probabilistic_sharpe_ratio": self.probabilistic_sharpe_ratio,
            "hit_rate": self.hit_rate,
            "prediction_information_coefficient": self.prediction_information_coefficient,
            "oos_r2": self.oos_r2,
            "max_drawdown": self.max_drawdown,
            "average_turnover": self.average_turnover,
            "total_cost_drag": self.total_cost_drag,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "warnings": list(self.warnings),
            "oos_records": [dict(item) for item in self.oos_records],
            "automatic_trading_permitted": False,
        }


def make_forward_returns(daily_returns: pd.Series, horizon_sessions: int = 1) -> pd.Series:
    """Return compounded t+1..t+h returns indexed by decision date t."""

    horizon = int(horizon_sessions)
    if horizon < 1:
        raise ValueError("horizon_sessions must be positive")
    series = pd.to_numeric(daily_returns, errors="coerce").sort_index()
    compounded = (1.0 + series).rolling(horizon).apply(np.prod, raw=True) - 1.0
    return compounded.shift(-horizon).rename(f"forward_{horizon}")


def _date(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _safe_corr(left: pd.Series, right: pd.Series) -> tuple[float | None, float | None]:
    aligned = pd.concat([left, right], axis=1).dropna()
    if len(aligned) < 3:
        return None, None
    if aligned.iloc[:, 0].nunique() < 2 or aligned.iloc[:, 1].nunique() < 2:
        return None, None
    result = spearmanr(
        aligned.iloc[:, 0].to_numpy(dtype=float),
        aligned.iloc[:, 1].to_numpy(dtype=float),
    )
    coefficient = float(result.statistic)
    p_value = float(result.pvalue)
    if not math.isfinite(coefficient) or not math.isfinite(p_value):
        return None, None
    return coefficient, p_value


def _bh_q_values(p_values: Mapping[str, float | None]) -> dict[str, float | None]:
    valid = sorted(
        ((name, float(value)) for name, value in p_values.items() if value is not None),
        key=lambda item: item[1],
    )
    if not valid:
        return {name: None for name in p_values}
    count = len(valid)
    adjusted: dict[str, float] = {}
    running = 1.0
    for rank_from_end, (name, value) in enumerate(reversed(valid), start=1):
        rank = count - rank_from_end + 1
        running = min(running, value * count / rank)
        adjusted[name] = min(1.0, running)
    return {name: adjusted.get(name) for name in p_values}


def _max_drawdown(returns: np.ndarray) -> float:
    if returns.size == 0:
        return 0.0
    wealth = np.cumprod(1.0 + returns)
    peaks = np.maximum.accumulate(wealth)
    return float(np.min(wealth / peaks - 1.0))


def _annualized_return(returns: np.ndarray, periods_per_year: float) -> float:
    if returns.size == 0:
        return 0.0
    wealth = float(np.prod(1.0 + returns))
    if wealth <= 0:
        return -1.0
    return float(wealth ** (periods_per_year / returns.size) - 1.0)


def _probabilistic_sharpe(returns: np.ndarray, benchmark_sharpe: float = 0.0) -> float | None:
    """Bailey–López de Prado probabilistic Sharpe approximation."""

    if returns.size < 8:
        return None
    standard_deviation = float(np.std(returns, ddof=1))
    if standard_deviation <= 1e-12:
        return None
    centered = returns - float(np.mean(returns))
    skewness = float(np.mean(centered**3) / standard_deviation**3)
    kurtosis = float(np.mean(centered**4) / standard_deviation**4)
    sample_sharpe = float(np.mean(returns) / standard_deviation)
    denominator_term = (
        1.0
        - skewness * sample_sharpe
        + ((kurtosis - 1.0) / 4.0) * sample_sharpe**2
    )
    if denominator_term <= 0:
        return None
    statistic = (
        (sample_sharpe - float(benchmark_sharpe))
        * math.sqrt(returns.size - 1)
        / math.sqrt(denominator_term)
    )
    return float(norm.cdf(statistic))


def _fit_ridge(
    x_train: pd.DataFrame,
    y_train: pd.Series,
    ridge: float,
) -> tuple[np.ndarray, float, pd.Series, pd.Series, float]:
    means = x_train.mean(axis=0)
    scales = x_train.std(axis=0, ddof=0).replace(0.0, np.nan)
    usable = scales.dropna().index
    if len(usable) == 0:
        raise ValueError("all factors are constant in training data")
    means = means.loc[usable]
    scales = scales.loc[usable]
    standardized = (x_train.loc[:, usable] - means) / scales
    y_mean = float(y_train.mean())
    centered_y = y_train.to_numpy(dtype=float) - y_mean
    matrix = standardized.to_numpy(dtype=float)
    gram = matrix.T @ matrix
    penalty = max(0.0, float(ridge)) * np.eye(gram.shape[0])
    coefficients = np.linalg.solve(gram + penalty, matrix.T @ centered_y)
    target_scale = float(y_train.std(ddof=0))
    if not math.isfinite(target_scale) or target_scale <= 1e-12:
        target_scale = 1.0
    return coefficients, y_mean, means, scales, target_scale


def _model_version(
    *,
    features: list[str],
    feature_version: str,
    horizon: int,
    train_size: int,
    test_size: int,
    step_size: int,
    purge_sessions: int,
    embargo_sessions: int,
    ridge: float,
    cost_bps: float,
    expanding: bool,
) -> str:
    payload = {
        "features": features,
        "feature_version": feature_version,
        "horizon": horizon,
        "train_size": train_size,
        "test_size": test_size,
        "step_size": step_size,
        "purge_sessions": purge_sessions,
        "embargo_sessions": embargo_sessions,
        "ridge": float(ridge),
        "cost_bps": float(cost_bps),
        "expanding": bool(expanding),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"walk_forward_ridge:{digest[:16]}"


def walk_forward_factor_backtest(
    signals: pd.DataFrame,
    *,
    daily_returns: pd.Series | None = None,
    forward_returns: pd.Series | None = None,
    target_name: str = "portfolio",
    feature_version: str,
    horizon_sessions: int = 1,
    train_size: int = 252,
    test_size: int = 21,
    step_size: int | None = None,
    expanding: bool = True,
    ridge: float = 1.0,
    transaction_cost_bps: float = 5.0,
    minimum_oos_observations: int = 30,
    purge_sessions: int | None = None,
    embargo_sessions: int = 0,
    multiple_testing_alpha: float = 0.10,
) -> FactorBacktestResult:
    """Run purged walk-forward regression and non-overlapping OOS backtest."""

    if not str(feature_version).strip():
        raise ValueError("feature_version is required")
    if (daily_returns is None) == (forward_returns is None):
        raise ValueError("supply exactly one of daily_returns or forward_returns")
    horizon = int(horizon_sessions)
    train_n = int(train_size)
    test_n = int(test_size)
    step_n = int(step_size if step_size is not None else test_size)
    purge_n = horizon if purge_sessions is None else int(purge_sessions)
    embargo_n = int(embargo_sessions)
    if min(horizon, test_n, step_n) < 1 or train_n < 20:
        raise ValueError("invalid walk-forward window")
    if min(purge_n, embargo_n) < 0:
        raise ValueError("purge and embargo must be non-negative")
    if transaction_cost_bps < 0:
        raise ValueError("transaction_cost_bps must be non-negative")
    if not 0 < multiple_testing_alpha < 1:
        raise ValueError("multiple_testing_alpha must be between zero and one")
    if signals.empty or signals.shape[1] < 1:
        raise ValueError("signals must contain at least one factor")

    x = signals.copy().sort_index()
    x.columns = [str(column).strip() for column in x.columns]
    if any(not column for column in x.columns) or len(set(x.columns)) != len(x.columns):
        raise ValueError("factor names must be unique and non-empty")
    x = x.apply(pd.to_numeric, errors="coerce")
    y = (
        make_forward_returns(daily_returns, horizon)
        if daily_returns is not None
        else pd.to_numeric(forward_returns, errors="coerce").sort_index()
    ).rename("target")
    frame = x.join(y, how="inner").replace([np.inf, -np.inf], np.nan).dropna()
    if len(frame) < train_n + purge_n + max(horizon, 3):
        raise ValueError("insufficient aligned observations for walk-forward test")

    feature_names = list(x.columns)
    model_version = _model_version(
        features=feature_names,
        feature_version=str(feature_version),
        horizon=horizon,
        train_size=train_n,
        test_size=test_n,
        step_size=step_n,
        purge_sessions=purge_n,
        embargo_sessions=embargo_n,
        ridge=ridge,
        cost_bps=transaction_cost_bps,
        expanding=expanding,
    )

    folds: list[WalkForwardFold] = []
    oos_rows: list[dict[str, Any]] = []
    coefficient_history = {name: [] for name in feature_names}
    last_position = 0.0
    fold_index = 0
    test_start = train_n + purge_n

    while test_start < len(frame):
        test_stop = min(len(frame), test_start + test_n)
        train_stop = test_start - purge_n
        train_start = 0 if expanding else max(0, train_stop - train_n)
        train = frame.iloc[train_start:train_stop]
        test = frame.iloc[test_start:test_stop].iloc[::horizon]
        if len(train) < train_n or test.empty:
            test_start += step_n + embargo_n
            continue
        try:
            coefficients, intercept, means, scales, target_scale = _fit_ridge(
                train[feature_names], train["target"], ridge
            )
        except (ValueError, np.linalg.LinAlgError):
            test_start += step_n + embargo_n
            continue
        usable = list(means.index)
        predictions = intercept + ((test[usable] - means) / scales).to_numpy(dtype=float) @ coefficients
        coefficient_map = {name: 0.0 for name in feature_names}
        for name, value in zip(usable, coefficients):
            coefficient_map[name] = float(value)
            coefficient_history[name].append(float(value))
        for name in set(feature_names) - set(usable):
            coefficient_history[name].append(0.0)

        fold_index += 1
        fold_id = f"fold-{fold_index:03d}"
        folds.append(
            WalkForwardFold(
                fold_id=fold_id,
                train_start=_date(train.index[0]),
                train_end=_date(train.index[-1]),
                test_start=_date(test.index[0]),
                test_end=_date(test.index[-1]),
                train_observations=len(train),
                test_observations=len(test),
                coefficients={key: round(value, 10) for key, value in coefficient_map.items()},
                intercept=round(float(intercept), 10),
                target_scale=round(target_scale, 10),
                purge_sessions=purge_n,
                embargo_sessions=embargo_n,
            )
        )
        for row_offset, (index, row) in enumerate(test.iterrows()):
            prediction = float(predictions[row_offset])
            position = float(np.clip(prediction / target_scale, -1.0, 1.0))
            turnover = abs(position - last_position)
            cost = turnover * float(transaction_cost_bps) / 10000.0
            realized = float(row["target"])
            gross = position * realized
            oos_rows.append(
                {
                    "date": _date(index),
                    "fold_id": fold_id,
                    "prediction": prediction,
                    "position": position,
                    "realized_forward_return": realized,
                    "gross_return": gross,
                    "turnover": turnover,
                    "cost": cost,
                    "net_return": gross - cost,
                    **{f"factor__{name}": float(row[name]) for name in feature_names},
                }
            )
            last_position = position
        test_start += step_n + embargo_n

    if not oos_rows:
        raise ValueError("walk-forward windows produced no OOS observations")

    oos = pd.DataFrame(oos_rows)
    gross = oos["gross_return"].to_numpy(dtype=float)
    net = oos["net_return"].to_numpy(dtype=float)
    predictions = oos["prediction"]
    realized = oos["realized_forward_return"]
    annual_periods = 252.0 / horizon
    net_std = float(np.std(net, ddof=1)) if len(net) > 1 else 0.0
    sharpe = (
        float(np.mean(net) / net_std * math.sqrt(annual_periods))
        if net_std > 1e-12
        else None
    )
    psr = _probabilistic_sharpe(net)
    hit_rate = float(np.mean(np.sign(predictions) == np.sign(realized)))
    prediction_ic, _prediction_p = _safe_corr(predictions, realized)
    target_variance = float(np.sum((realized - realized.mean()) ** 2))
    oos_r2 = (
        float(1.0 - np.sum((realized - predictions) ** 2) / target_variance)
        if target_variance > 1e-18
        else None
    )

    provisional: dict[str, dict[str, Any]] = {}
    p_values: dict[str, float | None] = {}
    for factor in feature_names:
        coefficients = np.asarray(coefficient_history.get(factor) or [], dtype=float)
        factor_series = oos[f"factor__{factor}"]
        raw_ic, p_value = _safe_corr(factor_series, realized)
        nonzero = coefficients[np.abs(coefficients) > 1e-12]
        mean_coefficient = float(np.mean(coefficients)) if coefficients.size else 0.0
        if nonzero.size:
            positive_share = float(np.mean(nonzero > 0))
            consistency = max(positive_share, 1.0 - positive_share)
        else:
            consistency = 0.0
        direction = 1.0 if mean_coefficient >= 0 else -1.0
        directional_ic = None if raw_ic is None else direction * raw_ic
        observations = int(factor_series.notna().sum())
        provisional[factor] = {
            "observations": observations,
            "mean_coefficient": mean_coefficient,
            "consistency": consistency,
            "raw_ic": raw_ic,
            "directional_ic": directional_ic,
            "p_value": p_value,
        }
        p_values[factor] = p_value
    q_values = _bh_q_values(p_values)

    diagnostics: list[FactorDiagnostic] = []
    for factor in feature_names:
        values = provisional[factor]
        observations = values["observations"]
        directional_ic = values["directional_ic"]
        consistency = values["consistency"]
        q_value = q_values[factor]
        sample_score = math.sqrt(observations / (observations + 100.0))
        ic_score = 0.0 if directional_ic is None else min(1.0, max(0.0, directional_ic) / 0.10)
        significance_score = 0.0 if q_value is None else max(0.0, 1.0 - q_value / 0.25)
        robustness = (
            0.35 * ic_score
            + 0.25 * consistency
            + 0.20 * significance_score
            + 0.20 * sample_score
        )
        if observations < minimum_oos_observations:
            status, weight, reason = "blocked", 0.0, "insufficient_oos_observations"
        elif directional_ic is None or q_value is None:
            status, weight, reason = "blocked", 0.0, "information_coefficient_unavailable"
        elif directional_ic >= 0.03 and consistency >= 0.60 and q_value <= multiple_testing_alpha:
            status = "active"
            weight = min(1.0, robustness)
            reason = "positive_oos_ic_stable_and_fdr_passed"
        elif directional_ic > 0 and consistency >= 0.50 and q_value <= 0.25:
            status = "watch"
            weight = min(0.50, robustness * 0.50)
            reason = "positive_but_weak_or_multiple_testing_marginal"
        else:
            status, weight, reason = "quarantined", 0.0, "negative_unstable_or_fdr_failed"
        diagnostics.append(
            FactorDiagnostic(
                factor=factor,
                observations=observations,
                mean_standardized_coefficient=round(values["mean_coefficient"], 10),
                coefficient_sign_consistency=round(consistency, 6),
                oos_information_coefficient=(
                    None if values["raw_ic"] is None else round(values["raw_ic"], 6)
                ),
                directional_information_coefficient=(
                    None if directional_ic is None else round(directional_ic, 6)
                ),
                admission_status=status,
                effective_weight_multiplier=round(max(0.0, min(1.0, weight)), 6),
                reason=reason,
                oos_p_value=(None if values["p_value"] is None else round(values["p_value"], 6)),
                multiple_testing_q_value=(None if q_value is None else round(q_value, 6)),
                robustness_score=round(robustness, 6),
            )
        )

    warnings = [
        f"Purged {purge_n} session(s) between every training and test block.",
        f"Applied a {embargo_n}-session fold embargo and horizon-spaced OOS records.",
        "Training-only scaling, turnover costs and Benjamini-Hochberg factor admission are enforced.",
        "Factor results are isolated by exact feature_version and model_version.",
        "Probabilistic Sharpe is descriptive and does not replace capacity, liquidity or tax review.",
    ]
    if len(oos) < minimum_oos_observations:
        status, risk_multiplier = "blocked", 0.95
        warnings.append("Insufficient OOS history for production weighting.")
    elif float(np.mean(net)) > 0 and psr is not None and psr >= 0.60:
        status, risk_multiplier = "active", 1.0
    elif float(np.mean(net)) > 0:
        status, risk_multiplier = "research_only", 1.0
    else:
        status, risk_multiplier = "quarantined", 0.95
        warnings.append("Net OOS performance is non-positive; the ensemble is quarantined.")

    return FactorBacktestResult(
        status=status,
        feature_version=str(feature_version),
        model_version=model_version,
        target_name=str(target_name),
        horizon_sessions=horizon,
        train_size=train_n,
        test_size=test_n,
        step_size=step_n,
        transaction_cost_bps=round(float(transaction_cost_bps), 6),
        folds=tuple(folds),
        factor_diagnostics=tuple(diagnostics),
        oos_observations=len(oos),
        gross_mean_return=round(float(np.mean(gross)), 10),
        net_mean_return=round(float(np.mean(net)), 10),
        gross_annualized_return=round(_annualized_return(gross, annual_periods), 10),
        net_annualized_return=round(_annualized_return(net, annual_periods), 10),
        net_annualized_volatility=round(net_std * math.sqrt(annual_periods), 10),
        net_sharpe=None if sharpe is None else round(sharpe, 6),
        hit_rate=round(hit_rate, 6),
        prediction_information_coefficient=(
            None if prediction_ic is None else round(prediction_ic, 6)
        ),
        oos_r2=None if oos_r2 is None else round(oos_r2, 6),
        max_drawdown=round(_max_drawdown(net), 10),
        average_turnover=round(float(oos["turnover"].mean()), 10),
        total_cost_drag=round(float(oos["cost"].sum()), 10),
        risk_budget_multiplier=round(risk_multiplier, 6),
        warnings=tuple(warnings),
        oos_records=tuple(
            {
                "date": row["date"],
                "fold_id": row["fold_id"],
                "prediction": round(float(row["prediction"]), 10),
                "position": round(float(row["position"]), 10),
                "realized_forward_return": round(float(row["realized_forward_return"]), 10),
                "gross_return": round(float(row["gross_return"]), 10),
                "turnover": round(float(row["turnover"]), 10),
                "cost": round(float(row["cost"]), 10),
                "net_return": round(float(row["net_return"]), 10),
            }
            for row in oos_rows
        ),
        purge_sessions=purge_n,
        embargo_sessions=embargo_n,
        probabilistic_sharpe_ratio=None if psr is None else round(psr, 6),
        multiple_testing_alpha=round(float(multiple_testing_alpha), 6),
    )


__all__ = [
    "FactorBacktestResult",
    "FactorDiagnostic",
    "WalkForwardFold",
    "make_forward_returns",
    "walk_forward_factor_backtest",
]
