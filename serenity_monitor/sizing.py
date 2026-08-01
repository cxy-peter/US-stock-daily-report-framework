"""Risk manager and portfolio manager with deterministic position sizing."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from .data import Quote
from .regime import MarketRegime
from .rules import ResearchAction, ResearchRecommendation


class PortfolioAction(str, Enum):
    EXIT = "清仓候选"
    REVIEW = "暂停交易/核实"
    TRIM = "减仓候选"
    REBALANCE = "风险再平衡"
    ADD = "加仓候选"
    OPEN = "开仓候选"
    HOLD_NO_CHASE = "持有不追高"
    HOLD = "继续持有"
    WATCH = "继续观察"


ACTIONABLE = {
    PortfolioAction.EXIT,
    PortfolioAction.TRIM,
    PortfolioAction.REBALANCE,
    PortfolioAction.ADD,
    PortfolioAction.OPEN,
}


@dataclass
class PortfolioSettings:
    cash_usd: float | None = None
    buying_power_usd: float | None = None
    account_value_usd: float | None = None
    cash_reserve_pct: float = 0.10
    daily_turnover_limit_pct: float = 0.05
    rebalance_band_pct: float = 0.0025
    min_trade_usd: float = 0.0
    default_max_weights: dict[str, float] = field(
        default_factory=lambda: {"high": 0.20, "medium": 0.12, "low": 0.05}
    )
    add_step_weights: dict[str, float] = field(
        default_factory=lambda: {"high": 0.02, "medium": 0.01, "low": 0.005}
    )
    open_step_weight: float = 0.01
    max_liquidity_participation: float = 0.01
    risk_group_caps: dict[str, float] = field(default_factory=dict)
    cash_equivalent_tickers: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict | None) -> "PortfolioSettings":
        data = data or {}
        return cls(
            cash_usd=_optional_float(data.get("cash_usd")),
            buying_power_usd=_optional_float(data.get("buying_power_usd")),
            account_value_usd=_optional_float(data.get("account_value_usd")),
            cash_reserve_pct=float(data.get("cash_reserve_pct", 0.10)),
            daily_turnover_limit_pct=float(data.get("daily_turnover_limit_pct", 0.05)),
            rebalance_band_pct=float(data.get("rebalance_band_pct", 0.0025)),
            min_trade_usd=float(data.get("min_trade_usd", 0.0)),
            default_max_weights={
                str(key): float(value)
                for key, value in (
                    data.get("default_max_weights")
                    or {"high": 0.20, "medium": 0.12, "low": 0.05}
                ).items()
            },
            add_step_weights={
                str(key): float(value)
                for key, value in (
                    data.get("add_step_weights")
                    or {"high": 0.02, "medium": 0.01, "low": 0.005}
                ).items()
            },
            open_step_weight=float(data.get("open_step_weight", 0.01)),
            max_liquidity_participation=float(
                data.get("max_liquidity_participation", 0.01)
            ),
            risk_group_caps={
                str(key): float(value)
                for key, value in (data.get("risk_group_caps") or {}).items()
            },
            cash_equivalent_tickers=tuple(
                str(ticker).upper()
                for ticker in (data.get("cash_equivalent_tickers") or [])
            ),
        )

    def immediately_deployable_cash(self, equity: float) -> float | None:
        if self.cash_usd is None:
            return None
        gross_available = self.cash_usd
        if self.buying_power_usd is not None:
            gross_available = min(gross_available, self.buying_power_usd)
        reserve_base = self.account_value_usd or equity
        return max(0.0, gross_available - reserve_base * self.cash_reserve_pct)


@dataclass
class PositionPlan:
    ticker: str
    name: str
    research_action: ResearchAction
    action: PortfolioAction
    current_shares: float
    current_price: float
    current_value: float
    current_weight: float
    target_weight: float
    adjusted_max_weight: float
    model_delta_usd: float
    executable_delta_usd: float | None
    trade_shares: float | None
    avg_correlation: float | None
    volatility_multiplier: float
    correlation_multiplier: float
    regime_multiplier: float
    confidence: int
    asset_type: str = "unknown"
    price_source: str = ""
    price_as_of: str = ""
    entry_price: float | None = None
    entry_price_estimated: bool = False
    unrealized_pnl_usd: float | None = None
    unrealized_pnl_pct: float | None = None
    position_status: str = "active"
    risk_groups: tuple[str, ...] = ()
    scheduled_dca_usd: float = 0.0
    scheduled_dca_status: str = "not_configured"
    reasons: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        return self.action in ACTIONABLE and abs(self.model_delta_usd) >= 1.0


def _optional_float(value: object) -> float | None:
    if value in (None, "", "null", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _volatility_multiplier(annualized_vol: float) -> float:
    if annualized_vol != annualized_vol:
        return 0.75
    if annualized_vol >= 0.70:
        return 0.45
    if annualized_vol >= 0.50:
        return 0.60
    if annualized_vol >= 0.35:
        return 0.75
    if annualized_vol >= 0.25:
        return 0.90
    return 1.0


def _correlation_multiplier(avg_corr: float | None) -> float:
    if avg_corr is None or avg_corr != avg_corr:
        return 0.90
    if avg_corr >= 0.80:
        return 0.70
    if avg_corr >= 0.60:
        return 0.85
    return 1.0


def calculate_average_correlations(
    quotes: dict[str, Quote],
    active_tickers: list[str],
) -> dict[str, float | None]:
    returns = {}
    for ticker in active_tickers:
        quote = quotes.get(ticker)
        if quote is None:
            continue
        series = quote.closes.pct_change().dropna().tail(126)
        if len(series) >= 20:
            returns[ticker] = series
    if len(returns) < 2:
        return {ticker: None for ticker in active_tickers}
    frame = pd.DataFrame(returns).dropna(how="all")
    correlation = frame.corr(min_periods=20)
    output: dict[str, float | None] = {}
    for ticker in active_tickers:
        if ticker not in correlation.columns:
            output[ticker] = None
            continue
        peers = correlation.loc[ticker].drop(labels=[ticker], errors="ignore").dropna()
        output[ticker] = float(peers.mean()) if len(peers) else None
    return output


def _base_cap(row: dict, settings: PortfolioSettings) -> float:
    explicit = row.get("max_weight_pct")
    if explicit not in (None, ""):
        value = float(explicit)
        return value / 100 if value > 1 else value
    conviction = str(row.get("conviction", "medium")).lower()
    return float(
        settings.default_max_weights.get(
            conviction,
            settings.default_max_weights["medium"],
        )
    )


def _add_step(row: dict, settings: PortfolioSettings) -> float:
    explicit = row.get("add_step_pct")
    if explicit not in (None, ""):
        value = float(explicit)
        return value / 100 if value > 1 else value
    conviction = str(row.get("conviction", "medium")).lower()
    return float(
        settings.add_step_weights.get(
            conviction,
            settings.add_step_weights["medium"],
        )
    )


def _map_nontrade(research_action: ResearchAction) -> PortfolioAction:
    return {
        ResearchAction.REVIEW: PortfolioAction.REVIEW,
        ResearchAction.HOLD_NO_CHASE: PortfolioAction.HOLD_NO_CHASE,
        ResearchAction.HOLD: PortfolioAction.HOLD,
        ResearchAction.WATCH: PortfolioAction.WATCH,
    }.get(research_action, PortfolioAction.HOLD)


def calculate_risk_group_exposures(
    holdings: list[dict],
    values: dict[str, float],
    equity: float,
) -> dict[str, float]:
    exposures: dict[str, float] = {}
    if equity <= 0:
        return exposures
    for row in holdings:
        ticker = str(row.get("ticker", "")).upper()
        weight = values.get(ticker, 0.0) / equity
        for group in row.get("risk_groups") or []:
            key = str(group)
            exposures[key] = exposures.get(key, 0.0) + weight
    return exposures


def build_position_plans(
    holdings: list[dict],
    watchlist: list[dict],
    quotes: dict[str, Quote],
    recommendations: dict[str, ResearchRecommendation],
    regime: MarketRegime,
    settings: PortfolioSettings,
    recurring_investments: dict[str, float] | None = None,
) -> tuple[list[PositionPlan], float]:
    values: dict[str, float] = {}
    for row in holdings:
        ticker = str(row["ticker"]).upper()
        quote = quotes.get(ticker)
        shares = _optional_float(row.get("shares")) or 0.0
        if quote is not None and shares > 0:
            values[ticker] = shares * quote.price
    invested = sum(values.values())
    equity = invested + (settings.cash_usd or 0.0)
    if equity <= 0:
        equity = invested or 1.0
    active = [
        str(row["ticker"]).upper()
        for row in holdings
        if values.get(str(row["ticker"]).upper(), 0.0) > 0
    ]
    average_correlations = calculate_average_correlations(quotes, active)
    recurring = {
        str(ticker).upper(): float(amount)
        for ticker, amount in (recurring_investments or {}).items()
        if _optional_float(amount) is not None and float(amount) > 0
    }
    current_group_exposures = calculate_risk_group_exposures(holdings, values, equity)
    projected_group_exposures = dict(current_group_exposures)

    plans: list[PositionPlan] = []
    all_rows = [(row, False) for row in holdings] + [(row, True) for row in watchlist]
    for row, is_watch in all_rows:
        ticker = str(row["ticker"]).upper()
        recommendation = recommendations.get(ticker)
        quote = quotes.get(ticker)
        if recommendation is None or quote is None:
            continue
        shares = 0.0 if is_watch else (_optional_float(row.get("shares")) or 0.0)
        current_value = shares * quote.price
        current_weight = current_value / equity if equity else 0.0
        tracking = bool(row.get("tracking_position", False))
        position_status = "tracking" if tracking else ("watchlist" if is_watch else "active")
        risk_groups = tuple(str(group) for group in (row.get("risk_groups") or []))
        entry_price = _optional_float(row.get("entry_price"))
        unrealized_pnl_usd = (
            current_value - shares * entry_price
            if entry_price is not None and shares > 0
            else None
        )
        unrealized_pnl_pct = (
            quote.price / entry_price - 1.0
            if entry_price not in (None, 0)
            else None
        )
        base_cap = _base_cap(row, settings)
        annualized_vol = (
            recommendation.indicators.ann_vol_30d
            if recommendation.indicators
            else float("nan")
        )
        vol_multiplier = _volatility_multiplier(annualized_vol)
        average_correlation = average_correlations.get(ticker)
        correlation_multiplier = _correlation_multiplier(average_correlation)
        adjusted_cap = max(
            0.0025,
            base_cap * min(
                vol_multiplier,
                correlation_multiplier,
                regime.risk_multiplier,
            ),
        )
        target_weight = current_weight
        action = _map_nontrade(recommendation.action)
        constraints: list[str] = []
        reasons = list(recommendation.reasons)

        if recommendation.action == ResearchAction.EXIT:
            action, target_weight = PortfolioAction.EXIT, 0.0
        elif recommendation.action == ResearchAction.REVIEW:
            action, target_weight = PortfolioAction.REVIEW, current_weight
        elif current_weight > adjusted_cap + settings.rebalance_band_pct and not tracking:
            action, target_weight = PortfolioAction.REBALANCE, adjusted_cap
            constraints.append(
                f"当前权重 {current_weight:.1%} 超过风险调整上限 {adjusted_cap:.1%}。"
            )
        elif recommendation.action == ResearchAction.TRIM:
            if tracking:
                action, target_weight = PortfolioAction.HOLD, current_weight
                constraints.append("跟踪仓不因仓位较小或短期盈亏被机械再平衡。")
            else:
                action = PortfolioAction.TRIM
                target_weight = min(current_weight * 0.5, adjusted_cap)
        elif recommendation.action == ResearchAction.ADD:
            target_weight = min(
                adjusted_cap,
                current_weight + _add_step(row, settings),
            )
            if target_weight > current_weight + 1e-9:
                action = PortfolioAction.ADD
            else:
                action = PortfolioAction.HOLD
                constraints.append("研究层允许新增，但已达到风险调整后的仓位上限。")
        elif recommendation.action == ResearchAction.OPEN:
            target_weight = min(adjusted_cap, settings.open_step_weight)
            action = PortfolioAction.OPEN if target_weight > 0 else PortfolioAction.WATCH
        elif recommendation.action == ResearchAction.HOLD_NO_CHASE:
            action = PortfolioAction.HOLD_NO_CHASE
        elif is_watch:
            action = PortfolioAction.WATCH

        model_delta = (target_weight - current_weight) * equity
        if (
            model_delta > 0
            and action in {PortfolioAction.ADD, PortfolioAction.OPEN}
            and risk_groups
        ):
            capacities = []
            blocked_groups = []
            for group in risk_groups:
                cap = settings.risk_group_caps.get(group)
                if cap is None:
                    continue
                available_weight = cap - projected_group_exposures.get(group, 0.0)
                if available_weight <= 1e-12:
                    blocked_groups.append(group)
                capacities.append(max(0.0, available_weight * equity))
            if blocked_groups:
                action = PortfolioAction.HOLD if shares > 0 else PortfolioAction.WATCH
                target_weight = current_weight
                model_delta = 0.0
                constraints.append(
                    "风险组容量不足，禁止新增暴露：" + ", ".join(blocked_groups)
                )
            elif capacities:
                permitted = min(model_delta, min(capacities))
                if permitted < model_delta:
                    constraints.append("新增金额按最紧风险组的剩余容量缩放。")
                    model_delta = permitted
                    target_weight = current_weight + model_delta / equity

        if abs(model_delta) < max(1.0, equity * 0.0001):
            model_delta = 0.0
            if action in {
                PortfolioAction.ADD,
                PortfolioAction.OPEN,
                PortfolioAction.TRIM,
                PortfolioAction.REBALANCE,
            }:
                action = PortfolioAction.HOLD if not is_watch else PortfolioAction.WATCH

        if model_delta:
            for group in risk_groups:
                projected_group_exposures[group] = max(
                    0.0,
                    projected_group_exposures.get(group, 0.0) + model_delta / equity,
                )

        scheduled_dca = recurring.get(ticker, 0.0)
        dca_status = "not_configured"
        if scheduled_dca > 0:
            blocked_dca_groups = [
                group
                for group in risk_groups
                if group in settings.risk_group_caps
                and current_group_exposures.get(group, 0.0) >= settings.risk_group_caps[group]
            ]
            if blocked_dca_groups:
                dca_status = "manual_review_risk_cap"
                constraints.append(
                    f"工作日定投 ${scheduled_dca:.0f} 是券商外部计划；风险组已满，"
                    f"需人工复核：{', '.join(blocked_dca_groups)}。"
                )
            else:
                dca_status = "external_plan_not_executed"
                reasons.append(
                    f"已记录工作日定投 ${scheduled_dca:.0f}；本系统只监控，不提交订单。"
                )

        plans.append(
            PositionPlan(
                ticker=ticker,
                name=str(row.get("name", ticker)),
                research_action=recommendation.action,
                action=action,
                current_shares=shares,
                current_price=quote.price,
                current_value=current_value,
                current_weight=current_weight,
                target_weight=target_weight,
                adjusted_max_weight=adjusted_cap,
                model_delta_usd=model_delta,
                executable_delta_usd=model_delta if model_delta <= 0 else None,
                trade_shares=(
                    model_delta / quote.price
                    if model_delta <= 0 and quote.price > 0
                    else None
                ),
                avg_correlation=average_correlation,
                volatility_multiplier=vol_multiplier,
                correlation_multiplier=correlation_multiplier,
                regime_multiplier=regime.risk_multiplier,
                confidence=recommendation.confidence,
                asset_type=quote.asset_type,
                price_source=quote.source,
                price_as_of=quote.as_of,
                entry_price=entry_price,
                entry_price_estimated=bool(row.get("entry_price_estimated", False)),
                unrealized_pnl_usd=unrealized_pnl_usd,
                unrealized_pnl_pct=unrealized_pnl_pct,
                position_status=position_status,
                risk_groups=risk_groups,
                scheduled_dca_usd=scheduled_dca,
                scheduled_dca_status=dca_status,
                reasons=reasons,
                constraints=constraints,
            )
        )

    _apply_turnover_and_cash(plans, equity, settings)
    return plans, equity


def _apply_turnover_and_cash(
    plans: list[PositionPlan],
    equity: float,
    settings: PortfolioSettings,
) -> None:
    discretionary = [
        plan
        for plan in plans
        if plan.action
        in {
            PortfolioAction.TRIM,
            PortfolioAction.REBALANCE,
            PortfolioAction.ADD,
            PortfolioAction.OPEN,
        }
        and abs(plan.model_delta_usd) > 0
    ]
    gross = sum(abs(plan.model_delta_usd) for plan in discretionary)
    cap = max(0.0, equity * settings.daily_turnover_limit_pct)
    scale = min(1.0, cap / gross) if gross > 0 and cap >= 0 else 1.0
    if scale < 1.0:
        for plan in discretionary:
            plan.model_delta_usd *= scale
            plan.target_weight = plan.current_weight + plan.model_delta_usd / equity
            plan.constraints.append(
                f"受组合单日换手上限 {settings.daily_turnover_limit_pct:.1%} 约束，"
                f"按 {scale:.1%} 缩放。"
            )

    for plan in plans:
        if plan.model_delta_usd < 0:
            amount = max(plan.model_delta_usd, -plan.current_value)
            plan.executable_delta_usd = amount
            plan.trade_shares = amount / plan.current_price if plan.current_price > 0 else None
            if plan.trade_shares is not None:
                plan.trade_shares = -min(plan.current_shares, abs(plan.trade_shares))
        elif plan.model_delta_usd == 0:
            plan.executable_delta_usd = 0.0
            plan.trade_shares = 0.0

    if settings.cash_usd is None:
        for plan in plans:
            if plan.model_delta_usd > 0:
                plan.executable_delta_usd = None
                plan.trade_shares = None
                plan.constraints.append(
                    "未配置实时现金余额：仅保留模型金额，不生成可执行买入。"
                )
        return

    available = settings.immediately_deployable_cash(equity) or 0.0
    buys = [plan for plan in plans if plan.model_delta_usd > 0]
    requested = sum(plan.model_delta_usd for plan in buys)
    buy_scale = min(1.0, available / requested) if requested > 0 else 1.0
    for plan in buys:
        executable = plan.model_delta_usd * buy_scale
        plan.executable_delta_usd = executable
        plan.trade_shares = (
            executable / plan.current_price if plan.current_price > 0 else None
        )
        if buy_scale < 1.0:
            plan.constraints.append(
                f"受可用现金约束，买入金额按 {buy_scale:.1%} 缩放；"
                f"保留 {settings.cash_reserve_pct:.1%} 现金缓冲。"
            )
        if executable <= 0:
            plan.action = (
                PortfolioAction.HOLD
                if plan.current_shares > 0
                else PortfolioAction.WATCH
            )
        elif executable < settings.min_trade_usd:
            plan.executable_delta_usd = 0.0
            plan.trade_shares = 0.0
            plan.action = (
                PortfolioAction.HOLD
                if plan.current_shares > 0
                else PortfolioAction.WATCH
            )
            plan.constraints.append(
                f"可执行金额低于最小调仓金额 ${settings.min_trade_usd:,.0f}，"
                "本次不生成交易变化。"
            )


def round_trade_shares(
    value: float | None,
    fractional: bool = True,
) -> float | None:
    if value is None:
        return None
    if fractional:
        return round(value, 4)
    return float(math.trunc(value))
