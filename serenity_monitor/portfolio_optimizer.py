"""Constrained, human-in-the-loop portfolio allocation research.

The optimizer is deliberately modest: it combines a shrunk covariance matrix,
optional expected-return estimates, turnover and implementation costs, box
constraints and group caps.  It returns an allocation proposal and diagnostics;
it has no broker, order, or confirmed-ledger mutation API.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np


_EPS = 1e-12


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _clip(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), lower), upper)


def _project_box_simplex(
    vector: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    *,
    total: float = 1.0,
    iterations: int = 100,
) -> np.ndarray:
    """Project onto a bounded simplex using monotone bisection."""

    if float(lower.sum()) > total + 1e-10 or float(upper.sum()) < total - 1e-10:
        raise ValueError("box constraints do not contain a feasible simplex")
    low = float(np.min(vector - upper)) - 1.0
    high = float(np.max(vector - lower)) + 1.0
    for _ in range(iterations):
        midpoint = 0.5 * (low + high)
        candidate = np.clip(vector - midpoint, lower, upper)
        if candidate.sum() > total:
            low = midpoint
        else:
            high = midpoint
    result = np.clip(vector - 0.5 * (low + high), lower, upper)
    residual = total - float(result.sum())
    if abs(residual) > 1e-10:
        if residual > 0:
            capacity = upper - result
        else:
            capacity = result - lower
        capacity_sum = float(capacity.sum())
        if capacity_sum <= _EPS:
            raise ValueError("projection failed to satisfy the simplex")
        result = result + residual * capacity / capacity_sum
    return np.clip(result, lower, upper)


def _apply_group_caps(
    weights: np.ndarray,
    groups: Mapping[str, Sequence[int]],
    caps: Mapping[str, float],
    lower: np.ndarray,
    upper: np.ndarray,
    *,
    max_rounds: int = 50,
) -> np.ndarray:
    """Enforce overlapping group caps with repeated projection and redistribution."""

    result = weights.copy()
    for _ in range(max_rounds):
        changed = False
        for group, indices_raw in groups.items():
            if group not in caps:
                continue
            indices = np.array(sorted(set(int(item) for item in indices_raw)), dtype=int)
            if len(indices) == 0:
                continue
            if (indices < 0).any() or (indices >= len(result)).any():
                raise ValueError(f"group {group} contains an invalid asset index")
            cap = _finite(caps[group], f"group_caps.{group}")
            if not 0.0 <= cap <= 1.0:
                raise ValueError(f"group cap for {group} must be between zero and one")
            group_total = float(result[indices].sum())
            if group_total <= cap + 1e-10:
                continue
            excess = group_total - cap
            removable = result[indices] - lower[indices]
            removable_total = float(removable.sum())
            if removable_total + 1e-10 < excess:
                raise ValueError(f"group cap for {group} conflicts with minimum weights")
            result[indices] -= excess * removable / max(removable_total, _EPS)
            outside = np.array([index for index in range(len(result)) if index not in set(indices)], dtype=int)
            if len(outside) == 0:
                raise ValueError(f"group cap for {group} leaves no redistribution asset")
            capacity = upper[outside] - result[outside]
            capacity_total = float(capacity.sum())
            if capacity_total + 1e-10 < excess:
                raise ValueError(f"group cap for {group} cannot be redistributed")
            result[outside] += excess * capacity / max(capacity_total, _EPS)
            result = _project_box_simplex(result, lower, upper)
            changed = True
        if not changed:
            return result
    raise ValueError("group-cap projection did not converge")


@dataclass(frozen=True)
class AllocationConstraints:
    min_weights: Mapping[str, float] = field(default_factory=dict)
    max_weights: Mapping[str, float] = field(default_factory=dict)
    group_members: Mapping[str, Sequence[str]] = field(default_factory=dict)
    group_caps: Mapping[str, float] = field(default_factory=dict)
    max_turnover: float | None = None


@dataclass(frozen=True)
class AllocationResult:
    status: str
    symbols: tuple[str, ...]
    current_weights: Mapping[str, float]
    proposed_weights: Mapping[str, float]
    expected_return: float | None
    expected_volatility: float
    expected_sharpe: float | None
    turnover: float
    estimated_implementation_cost: float
    marginal_risk_contributions: Mapping[str, float]
    percentage_risk_contributions: Mapping[str, float]
    objective_value: float
    binding_constraints: tuple[str, ...]
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "symbols": list(self.symbols),
            "current_weights": dict(self.current_weights),
            "proposed_weights": dict(self.proposed_weights),
            "expected_return": self.expected_return,
            "expected_volatility": self.expected_volatility,
            "expected_sharpe": self.expected_sharpe,
            "turnover": self.turnover,
            "estimated_implementation_cost": self.estimated_implementation_cost,
            "marginal_risk_contributions": dict(self.marginal_risk_contributions),
            "percentage_risk_contributions": dict(self.percentage_risk_contributions),
            "objective_value": self.objective_value,
            "binding_constraints": list(self.binding_constraints),
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def optimize_allocation(
    *,
    symbols: Sequence[str],
    covariance: Sequence[Sequence[float]] | np.ndarray,
    current_weights: Mapping[str, float],
    expected_returns: Mapping[str, float] | None = None,
    constraints: AllocationConstraints | None = None,
    risk_aversion: float = 4.0,
    turnover_penalty: float = 0.25,
    transaction_cost_bps: Mapping[str, float] | float = 10.0,
    covariance_shrinkage: float = 0.15,
    learning_rate: float = 0.05,
    iterations: int = 2500,
) -> AllocationResult:
    """Return a constrained allocation proposal using projected gradient descent.

    Expected returns are optional.  Without them the result is a constrained
    minimum-variance / turnover-aware proposal.  The result is research only and
    must pass owner, tax, liquidity and broker-confirmation checks before use.
    """

    names = tuple(str(symbol).strip().upper() for symbol in symbols)
    if not names or len(names) != len(set(names)):
        raise ValueError("symbols must be a non-empty unique sequence")
    size = len(names)
    matrix = np.asarray(covariance, dtype=float)
    if matrix.shape != (size, size) or not np.isfinite(matrix).all():
        raise ValueError("covariance must be a finite square matrix matching symbols")
    matrix = 0.5 * (matrix + matrix.T)
    diagonal = np.diag(np.diag(matrix))
    shrinkage = _finite(covariance_shrinkage, "covariance_shrinkage")
    if not 0.0 <= shrinkage <= 1.0:
        raise ValueError("covariance_shrinkage must be between zero and one")
    matrix = (1.0 - shrinkage) * matrix + shrinkage * diagonal
    eigenvalues = np.linalg.eigvalsh(matrix)
    if float(eigenvalues.min()) < -1e-8:
        matrix = matrix + np.eye(size) * (-float(eigenvalues.min()) + 1e-8)

    current = np.array([_finite(current_weights.get(name, 0.0), f"current.{name}") for name in names])
    if (current < -1e-10).any() or float(current.sum()) <= _EPS:
        raise ValueError("current weights must be non-negative with a positive total")
    current = current / current.sum()

    policy = constraints or AllocationConstraints()
    lower = np.array([_finite(policy.min_weights.get(name, 0.0), f"min.{name}") for name in names])
    upper = np.array([_finite(policy.max_weights.get(name, 1.0), f"max.{name}") for name in names])
    if (lower < 0).any() or (upper > 1).any() or (lower > upper).any():
        raise ValueError("weight bounds must satisfy 0 <= min <= max <= 1")
    current = _project_box_simplex(current, lower, upper)

    group_indices = {
        group: tuple(names.index(str(symbol).strip().upper()) for symbol in members)
        for group, members in policy.group_members.items()
    }
    current = _apply_group_caps(current, group_indices, policy.group_caps, lower, upper)

    if expected_returns is None:
        mu = np.zeros(size, dtype=float)
        has_expected_returns = False
    else:
        mu = np.array([_finite(expected_returns.get(name, 0.0), f"expected_return.{name}") for name in names])
        has_expected_returns = True

    risk_aversion_value = _finite(risk_aversion, "risk_aversion")
    turnover_penalty_value = _finite(turnover_penalty, "turnover_penalty")
    learning_rate_value = _finite(learning_rate, "learning_rate")
    if risk_aversion_value <= 0 or turnover_penalty_value < 0 or learning_rate_value <= 0:
        raise ValueError("risk_aversion and learning_rate must be positive; turnover_penalty non-negative")
    if iterations < 50 or iterations > 100_000:
        raise ValueError("iterations must be between 50 and 100000")

    if isinstance(transaction_cost_bps, Mapping):
        costs = np.array([
            _finite(transaction_cost_bps.get(name, 0.0), f"transaction_cost_bps.{name}") / 10_000.0
            for name in names
        ])
    else:
        cost = _finite(transaction_cost_bps, "transaction_cost_bps") / 10_000.0
        costs = np.full(size, cost, dtype=float)
    if (costs < 0).any():
        raise ValueError("transaction costs must be non-negative")

    weights = current.copy()
    smoothing = 1e-8
    for step in range(iterations):
        difference = weights - current
        gradient = (
            risk_aversion_value * (matrix @ weights)
            - mu
            + 2.0 * turnover_penalty_value * difference
            + costs * difference / np.sqrt(difference * difference + smoothing)
        )
        rate = learning_rate_value / math.sqrt(1.0 + step / 100.0)
        candidate = _project_box_simplex(weights - rate * gradient, lower, upper)
        candidate = _apply_group_caps(candidate, group_indices, policy.group_caps, lower, upper)
        if float(np.max(np.abs(candidate - weights))) < 1e-10:
            weights = candidate
            break
        weights = candidate

    turnover = 0.5 * float(np.abs(weights - current).sum())
    if policy.max_turnover is not None:
        max_turnover = _finite(policy.max_turnover, "max_turnover")
        if not 0.0 <= max_turnover <= 1.0:
            raise ValueError("max_turnover must be between zero and one")
        if turnover > max_turnover + 1e-10:
            scale = max_turnover / max(turnover, _EPS)
            weights = current + scale * (weights - current)
            weights = _project_box_simplex(weights, lower, upper)
            weights = _apply_group_caps(weights, group_indices, policy.group_caps, lower, upper)
            turnover = 0.5 * float(np.abs(weights - current).sum())

    portfolio_variance = max(float(weights @ matrix @ weights), 0.0)
    volatility = math.sqrt(portfolio_variance)
    expected_return = float(weights @ mu) if has_expected_returns else None
    expected_sharpe = None if expected_return is None or volatility <= _EPS else expected_return / volatility
    marginal = matrix @ weights
    raw_contributions = weights * marginal
    contribution_total = float(raw_contributions.sum())
    percentages = (
        np.zeros(size, dtype=float)
        if abs(contribution_total) <= _EPS
        else raw_contributions / contribution_total
    )
    difference = weights - current
    implementation_cost = float(np.sum(costs * np.abs(difference)))
    objective = (
        0.5 * risk_aversion_value * portfolio_variance
        - float(weights @ mu)
        + turnover_penalty_value * float(difference @ difference)
        + implementation_cost
    )

    binding: list[str] = []
    for index, name in enumerate(names):
        if abs(weights[index] - lower[index]) <= 1e-6 and lower[index] > 0:
            binding.append(f"min:{name}")
        if abs(weights[index] - upper[index]) <= 1e-6 and upper[index] < 1:
            binding.append(f"max:{name}")
    for group, indices in group_indices.items():
        if group in policy.group_caps:
            group_total = float(weights[list(indices)].sum())
            if abs(group_total - float(policy.group_caps[group])) <= 1e-6:
                binding.append(f"group_cap:{group}")
    if policy.max_turnover is not None and abs(turnover - float(policy.max_turnover)) <= 1e-6:
        binding.append("max_turnover")

    warnings = [
        "The optimizer proposes weights only; it cannot create or transmit an order.",
        "Expected-return estimates are fragile and require point-in-time out-of-sample validation.",
        "Tax lots, wash sales, liquidity, account restrictions and owner intent remain downstream gates.",
    ]
    if expected_returns is None:
        warnings.append("No expected returns were supplied; the proposal is risk/turnover driven.")
    if float(np.max(percentages)) > 0.35:
        warnings.append("One asset contributes more than 35% of modeled portfolio variance.")

    return AllocationResult(
        status="ok",
        symbols=names,
        current_weights={name: round(float(value), 10) for name, value in zip(names, current)},
        proposed_weights={name: round(float(value), 10) for name, value in zip(names, weights)},
        expected_return=None if expected_return is None else round(expected_return, 10),
        expected_volatility=round(volatility, 10),
        expected_sharpe=None if expected_sharpe is None else round(expected_sharpe, 10),
        turnover=round(turnover, 10),
        estimated_implementation_cost=round(implementation_cost, 10),
        marginal_risk_contributions={name: round(float(value), 10) for name, value in zip(names, marginal)},
        percentage_risk_contributions={name: round(float(value), 10) for name, value in zip(names, percentages)},
        objective_value=round(objective, 12),
        binding_constraints=tuple(sorted(binding)),
        warnings=tuple(warnings),
    )


__all__ = ["AllocationConstraints", "AllocationResult", "optimize_allocation"]
