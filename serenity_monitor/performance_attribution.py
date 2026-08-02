"""Single- and multi-period performance attribution.

Brinson-Fachler decomposes active return into allocation, selection and
interaction. Carino links period contributions through compounding so linked
contributions reconcile to cumulative portfolio return minus cumulative
benchmark return.

The module is accounting/research only and has no order or portfolio-mutation
API.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


TOLERANCE = 1e-10


def _finite(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _normalized_weights(
    values: Mapping[str, float],
    groups: Sequence[str],
    *,
    name: str,
    tolerance: float = 1e-8,
) -> dict[str, float]:
    result = {group: _finite(values.get(group, 0.0), f"{name}.{group}") for group in groups}
    if any(value < -tolerance for value in result.values()):
        raise ValueError(f"{name} must be non-negative")
    total = sum(result.values())
    if total <= tolerance:
        raise ValueError(f"{name} must contain a positive weight")
    if abs(total - 1.0) > tolerance:
        # Explicit normalization allows small source rounding while preserving
        # the relative group exposure. Large leverage belongs in a different
        # attribution contract and is rejected below.
        if not 0.95 <= total <= 1.05:
            raise ValueError(f"{name} must sum approximately to one")
        result = {group: max(value, 0.0) / total for group, value in result.items()}
    return result


@dataclass(frozen=True)
class BrinsonGroupAttribution:
    group: str
    portfolio_weight: float
    benchmark_weight: float
    portfolio_return: float
    benchmark_return: float
    allocation: float
    selection: float
    interaction: float
    active_contribution: float


@dataclass(frozen=True)
class BrinsonPeriodResult:
    portfolio_return: float
    benchmark_return: float
    active_return: float
    allocation: float
    selection: float
    interaction: float
    residual: float
    groups: tuple[BrinsonGroupAttribution, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio_return": self.portfolio_return,
            "benchmark_return": self.benchmark_return,
            "active_return": self.active_return,
            "allocation": self.allocation,
            "selection": self.selection,
            "interaction": self.interaction,
            "residual": self.residual,
            "groups": [item.__dict__ for item in self.groups],
        }


def brinson_fachler(
    portfolio_weights: Mapping[str, float],
    benchmark_weights: Mapping[str, float],
    portfolio_returns: Mapping[str, float],
    benchmark_returns: Mapping[str, float],
) -> BrinsonPeriodResult:
    """Compute Brinson-Fachler attribution with an explicit interaction term."""

    groups = tuple(
        sorted(
            set(portfolio_weights)
            | set(benchmark_weights)
            | set(portfolio_returns)
            | set(benchmark_returns)
        )
    )
    if not groups:
        raise ValueError("at least one attribution group is required")
    wp = _normalized_weights(portfolio_weights, groups, name="portfolio_weights")
    wb = _normalized_weights(benchmark_weights, groups, name="benchmark_weights")
    rp = {group: _finite(portfolio_returns.get(group, 0.0), f"portfolio_returns.{group}") for group in groups}
    rb = {group: _finite(benchmark_returns.get(group, 0.0), f"benchmark_returns.{group}") for group in groups}

    portfolio_total = sum(wp[group] * rp[group] for group in groups)
    benchmark_total = sum(wb[group] * rb[group] for group in groups)
    rows: list[BrinsonGroupAttribution] = []
    for group in groups:
        allocation = (wp[group] - wb[group]) * (rb[group] - benchmark_total)
        selection = wb[group] * (rp[group] - rb[group])
        interaction = (wp[group] - wb[group]) * (rp[group] - rb[group])
        active_contribution = allocation + selection + interaction
        rows.append(
            BrinsonGroupAttribution(
                group=group,
                portfolio_weight=round(wp[group], 12),
                benchmark_weight=round(wb[group], 12),
                portfolio_return=round(rp[group], 12),
                benchmark_return=round(rb[group], 12),
                allocation=round(allocation, 12),
                selection=round(selection, 12),
                interaction=round(interaction, 12),
                active_contribution=round(active_contribution, 12),
            )
        )
    allocation_total = sum(row.allocation for row in rows)
    selection_total = sum(row.selection for row in rows)
    interaction_total = sum(row.interaction for row in rows)
    active_return = portfolio_total - benchmark_total
    residual = active_return - allocation_total - selection_total - interaction_total
    if abs(residual) > 1e-8:
        raise ArithmeticError("Brinson-Fachler attribution failed to reconcile")
    return BrinsonPeriodResult(
        portfolio_return=round(portfolio_total, 12),
        benchmark_return=round(benchmark_total, 12),
        active_return=round(active_return, 12),
        allocation=round(allocation_total, 12),
        selection=round(selection_total, 12),
        interaction=round(interaction_total, 12),
        residual=round(residual, 12),
        groups=tuple(rows),
    )


@dataclass(frozen=True)
class CarinoPeriodInput:
    period_id: str
    portfolio_return: float
    benchmark_return: float
    contributions: Mapping[str, float]


@dataclass(frozen=True)
class CarinoPeriodLink:
    period_id: str
    portfolio_return: float
    benchmark_return: float
    active_return: float
    k_factor: float
    linking_multiplier: float
    contributions: Mapping[str, float]
    linked_contributions: Mapping[str, float]


@dataclass(frozen=True)
class CarinoLinkResult:
    cumulative_portfolio_return: float
    cumulative_benchmark_return: float
    cumulative_active_return: float
    total_k_factor: float
    linked_contributions: Mapping[str, float]
    residual: float
    periods: tuple[CarinoPeriodLink, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cumulative_portfolio_return": self.cumulative_portfolio_return,
            "cumulative_benchmark_return": self.cumulative_benchmark_return,
            "cumulative_active_return": self.cumulative_active_return,
            "total_k_factor": self.total_k_factor,
            "linked_contributions": dict(self.linked_contributions),
            "residual": self.residual,
            "periods": [
                {
                    **{
                        key: value
                        for key, value in item.__dict__.items()
                        if key not in {"contributions", "linked_contributions"}
                    },
                    "contributions": dict(item.contributions),
                    "linked_contributions": dict(item.linked_contributions),
                }
                for item in self.periods
            ],
        }


def _carino_k(portfolio_return: float, benchmark_return: float) -> float:
    if portfolio_return <= -1.0 or benchmark_return <= -1.0:
        raise ValueError("period returns must be greater than -100%")
    active = portfolio_return - benchmark_return
    if abs(active) <= 1e-14:
        return 1.0 / (1.0 + portfolio_return)
    return (
        math.log1p(portfolio_return) - math.log1p(benchmark_return)
    ) / active


def carino_link(periods: Iterable[CarinoPeriodInput | Mapping[str, Any]]) -> CarinoLinkResult:
    """Link active contributions through time using the Carino method."""

    normalized: list[CarinoPeriodInput] = []
    for index, raw in enumerate(periods):
        if isinstance(raw, CarinoPeriodInput):
            item = raw
        else:
            item = CarinoPeriodInput(
                period_id=str(raw.get("period_id") or index),
                portfolio_return=_finite(raw.get("portfolio_return"), "portfolio_return"),
                benchmark_return=_finite(raw.get("benchmark_return"), "benchmark_return"),
                contributions={
                    str(key): _finite(value, f"contributions.{key}")
                    for key, value in dict(raw.get("contributions") or {}).items()
                },
            )
        portfolio_return = _finite(item.portfolio_return, "portfolio_return")
        benchmark_return = _finite(item.benchmark_return, "benchmark_return")
        contributions = {
            str(key): _finite(value, f"contributions.{key}")
            for key, value in item.contributions.items()
        }
        active = portfolio_return - benchmark_return
        contribution_sum = sum(contributions.values())
        if abs(contribution_sum - active) > 1e-8:
            raise ValueError(
                f"period {item.period_id} contributions do not reconcile to active return"
            )
        normalized.append(
            CarinoPeriodInput(
                period_id=item.period_id,
                portfolio_return=portfolio_return,
                benchmark_return=benchmark_return,
                contributions=contributions,
            )
        )
    if not normalized:
        raise ValueError("at least one period is required")

    cumulative_portfolio = math.prod(1.0 + item.portfolio_return for item in normalized) - 1.0
    cumulative_benchmark = math.prod(1.0 + item.benchmark_return for item in normalized) - 1.0
    cumulative_active = cumulative_portfolio - cumulative_benchmark
    total_k = _carino_k(cumulative_portfolio, cumulative_benchmark)
    if abs(total_k) <= 1e-14:
        raise ArithmeticError("Carino total k-factor is zero")

    total_contributions: dict[str, float] = {}
    links: list[CarinoPeriodLink] = []
    for item in normalized:
        k_factor = _carino_k(item.portfolio_return, item.benchmark_return)
        multiplier = k_factor / total_k
        linked = {
            key: value * multiplier for key, value in item.contributions.items()
        }
        for key, value in linked.items():
            total_contributions[key] = total_contributions.get(key, 0.0) + value
        links.append(
            CarinoPeriodLink(
                period_id=item.period_id,
                portfolio_return=round(item.portfolio_return, 12),
                benchmark_return=round(item.benchmark_return, 12),
                active_return=round(item.portfolio_return - item.benchmark_return, 12),
                k_factor=round(k_factor, 12),
                linking_multiplier=round(multiplier, 12),
                contributions={key: round(value, 12) for key, value in item.contributions.items()},
                linked_contributions={key: round(value, 12) for key, value in linked.items()},
            )
        )
    residual = cumulative_active - sum(total_contributions.values())
    if abs(residual) > 1e-8:
        raise ArithmeticError("Carino-linked contributions failed to reconcile")
    return CarinoLinkResult(
        cumulative_portfolio_return=round(cumulative_portfolio, 12),
        cumulative_benchmark_return=round(cumulative_benchmark, 12),
        cumulative_active_return=round(cumulative_active, 12),
        total_k_factor=round(total_k, 12),
        linked_contributions={key: round(value, 12) for key, value in sorted(total_contributions.items())},
        residual=round(residual, 12),
        periods=tuple(links),
    )


def carino_link_brinson(periods: Sequence[tuple[str, BrinsonPeriodResult]]) -> CarinoLinkResult:
    """Convenience wrapper linking allocation/selection/interaction totals."""

    return carino_link(
        CarinoPeriodInput(
            period_id=period_id,
            portfolio_return=result.portfolio_return,
            benchmark_return=result.benchmark_return,
            contributions={
                "allocation": result.allocation,
                "selection": result.selection,
                "interaction": result.interaction,
                "residual": result.residual,
            },
        )
        for period_id, result in periods
    )


__all__ = [
    "BrinsonGroupAttribution",
    "BrinsonPeriodResult",
    "CarinoLinkResult",
    "CarinoPeriodInput",
    "CarinoPeriodLink",
    "brinson_fachler",
    "carino_link",
    "carino_link_brinson",
]
