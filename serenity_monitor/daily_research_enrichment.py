"""Daily global-source and factor-validation enrichment for one private report.

Public research may refresh every day while broker reconciliation runs on a
separate cadence. Missing research remains visible and never becomes a neutral
or bullish default. Adjusted history is research-only, not settlement-grade.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
import requests

from .external_views import (
    ExternalBundle,
    ExternalSettings,
    collect_external_views,
)
from .factor_backtest import FactorBacktestResult, walk_forward_factor_backtest
from .global_market_narratives import (
    GlobalNarrativeResult,
    score_global_narratives,
)


# external_views intentionally caps public-search queries at four. Keep every
# required category inside these four executed queries rather than documenting
# sources that would be silently truncated.
DEFAULT_GLOBAL_QUERIES = (
    "site:aljazeera.com oil crude OPEC Hormuz Red Sea shipping",
    "site:news.skhynix.com HBM DRAM memory semiconductor partnership",
    "(site:en.yna.co.kr OR site:koreaherald.com OR site:koreatimes.co.kr) "
    "SK hynix HBM memory semiconductor",
    "(site:quora.com Micron MU HBM semiconductor outlook) OR "
    '("Serenity" Micron MU HBM semiconductor investing)',
)

_RESEARCH_PROXIES = (
    "SPY",
    "IWM",
    "SMH",
    "MU",
    "XLE",
    "USO",
    "TLT",
    "GLD",
    "SCHD",
    "QQQ",
)

_ALIASES = {
    "MU": ["Micron", "Micron Technology", "HBM memory"],
    "SMH": ["VanEck Semiconductor ETF", "semiconductor ETF"],
    "QQQM": ["Invesco Nasdaq 100 ETF", "Nasdaq 100"],
    "VOO": ["Vanguard S&P 500 ETF", "S&P 500"],
    "SCHD": ["Schwab US Dividend Equity ETF", "dividend ETF"],
    "NVDA": ["NVIDIA", "Nvidia"],
}


@dataclass(frozen=True)
class DailyResearchEnrichment:
    status: str
    generated_at: str
    global_narratives: GlobalNarrativeResult
    factor_validation: FactorBacktestResult | None
    source_health: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at,
            "global_narratives": self.global_narratives.to_dict(),
            "factor_validation": (
                None
                if self.factor_validation is None
                else self.factor_validation.to_dict()
            ),
            "source_health": [dict(item) for item in self.source_health],
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def _rfc3339(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _targets(symbols: Iterable[str]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    targets: list[dict[str, Any]] = []
    for raw in symbols:
        ticker = str(raw).strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        aliases = _ALIASES.get(ticker, [])
        targets.append(
            {
                "ticker": ticker,
                "name": aliases[0] if aliases else ticker,
                "social_aliases": aliases,
                "asset_type": "stock" if ticker in {"MU", "NVDA"} else "etf",
            }
        )
    return targets


def default_global_source_settings(*, lookback_days: int = 7) -> ExternalSettings:
    return ExternalSettings(
        enabled=True,
        lookback_days=max(1, int(lookback_days)),
        max_items_per_ticker=15,
        news_enabled=True,
        news_limit=6,
        stocktwits_enabled=False,
        reddit_enabled=True,
        reddit_subreddits=(
            "stocks",
            "investing",
            "wallstreetbets",
            "semiconductors",
        ),
        reddit_limit_per_sub=3,
        x_enabled=False,
        x_discovery_enabled=False,
        x_handles=(),
        sec_enabled=False,
        manual_kol_enabled=False,
        public_web_enabled=True,
        public_web_queries=DEFAULT_GLOBAL_QUERIES,
        public_web_limit_per_query=5,
    )


def collect_daily_global_narratives(
    symbols: Iterable[str],
    *,
    session: requests.Session | None = None,
    settings: ExternalSettings | None = None,
    as_of: dt.datetime | None = None,
    network_enabled: bool = True,
) -> tuple[GlobalNarrativeResult, ExternalBundle]:
    now = as_of or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    symbol_list = [str(item).strip().upper() for item in symbols if str(item).strip()]
    source_settings = settings or default_global_source_settings()
    bundle = collect_external_views(
        (),
        _targets(symbol_list),
        source_settings,
        session=session,
        network_enabled=network_enabled,
    )
    items = list(bundle.global_items)
    for view in bundle.by_ticker.values():
        items.extend(view.items)
    return (
        score_global_narratives(
            items,
            as_of=now,
            lookback_days=source_settings.lookback_days,
            portfolio_tickers=symbol_list,
        ),
        bundle,
    )


def fetch_research_price_history(
    symbols: Iterable[str],
    *,
    period: str = "5y",
) -> tuple[pd.DataFrame | None, Mapping[str, Any]]:
    """Fetch adjusted history as a non-settlement research source."""

    requested = sorted(
        {
            str(item).strip().upper()
            for item in tuple(symbols) + _RESEARCH_PROXIES
            if str(item).strip()
        }
    )
    if not requested:
        return None, {
            "source": "research_price_history",
            "status": "blocked",
            "detail": "no symbols supplied",
        }
    try:
        import yfinance as yf

        raw = yf.download(
            requested,
            period=period,
            interval="1d",
            auto_adjust=True,
            actions=False,
            progress=False,
            threads=False,
            group_by="column",
        )
        if raw is None or raw.empty:
            raise ValueError("empty research history")
        if isinstance(raw.columns, pd.MultiIndex):
            if "Close" not in raw.columns.get_level_values(0):
                raise ValueError("close field unavailable")
            close = raw["Close"].copy()
        else:
            if "Close" not in raw:
                raise ValueError("close field unavailable")
            close = raw[["Close"]].rename(columns={"Close": requested[0]})
        close.columns = [str(column).upper() for column in close.columns]
        close = (
            close.apply(pd.to_numeric, errors="coerce")
            .sort_index()
            .dropna(axis=1, how="all")
            .dropna(how="all")
        )
        if len(close) < 280:
            raise ValueError("insufficient history")
        return close, {
            "source": "research_price_history",
            "status": "healthy",
            "detail": (
                f"yfinance adjusted research history; rows={len(close)}; "
                "not settlement-grade"
            ),
        }
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return None, {
            "source": "research_price_history",
            "status": "blocked",
            "detail": "adjusted public research history unavailable",
        }


def build_factor_dataset(
    prices: pd.DataFrame,
    portfolio_symbols: Iterable[str],
) -> tuple[pd.DataFrame, pd.Series]:
    """Build close-t signals and a t+1 onward portfolio-return target input."""

    close = prices.copy().sort_index().apply(pd.to_numeric, errors="coerce")
    close.columns = [str(column).upper() for column in close.columns]
    returns = close.pct_change(fill_method=None)
    symbols = [
        str(item).strip().upper()
        for item in portfolio_symbols
        if str(item).strip().upper() in returns.columns
    ]
    if not symbols:
        raise ValueError("portfolio symbols do not overlap research history")
    target = returns[symbols].mean(axis=1, skipna=True).rename("portfolio")

    def total_return(symbol: str, periods: int) -> pd.Series | None:
        if symbol not in close:
            return None
        return close[symbol] / close[symbol].shift(periods) - 1.0

    def relative(left: str, right: str, periods: int) -> pd.Series | None:
        lhs = total_return(left, periods)
        rhs = total_return(right, periods)
        return None if lhs is None or rhs is None else lhs - rhs

    signals: dict[str, pd.Series] = {}
    if "SPY" in returns:
        signals["market_momentum_21"] = total_return("SPY", 21)  # type: ignore[assignment]
        signals["market_momentum_63"] = total_return("SPY", 63)  # type: ignore[assignment]
        signals["market_volatility_21"] = -returns["SPY"].rolling(21).std()
    candidates = {
        "semis_relative_21": relative("SMH", "SPY", 21),
        "memory_relative_21": relative("MU", "SMH", 21),
        "oil_relative_21": relative("XLE", "SPY", 21),
        "rates_relative_21": relative("TLT", "SPY", 21),
        "gold_relative_21": relative("GLD", "SPY", 21),
        "breadth_relative_21": relative("IWM", "SPY", 21),
        "defensive_relative_21": relative("SCHD", "SPY", 21),
    }
    signals.update({name: series for name, series in candidates.items() if series is not None})
    if len(signals) < 2:
        raise ValueError("insufficient factor proxy coverage")
    return pd.DataFrame(signals).replace([np.inf, -np.inf], np.nan), target


def validate_daily_factors(
    prices: pd.DataFrame,
    portfolio_symbols: Iterable[str],
    *,
    feature_version: str = "daily_global_proxy_factors/v1.0.0",
    transaction_cost_bps: float = 5.0,
) -> FactorBacktestResult:
    signals, target = build_factor_dataset(prices, portfolio_symbols)
    return walk_forward_factor_backtest(
        signals,
        daily_returns=target,
        target_name="equal_weight_private_portfolio_proxy",
        feature_version=feature_version,
        horizon_sessions=5,
        train_size=252,
        test_size=63,
        step_size=63,
        expanding=True,
        ridge=5.0,
        transaction_cost_bps=transaction_cost_bps,
        minimum_oos_observations=30,
    )


def build_daily_research_enrichment(
    symbols: Iterable[str],
    *,
    as_of: dt.datetime | None = None,
    session: requests.Session | None = None,
    network_enabled: bool = True,
    price_history: pd.DataFrame | None = None,
) -> DailyResearchEnrichment:
    now = as_of or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    symbol_list = [str(item).strip().upper() for item in symbols if str(item).strip()]
    global_result, bundle = collect_daily_global_narratives(
        symbol_list,
        session=session,
        as_of=now,
        network_enabled=network_enabled,
    )
    health: list[Mapping[str, Any]] = [
        {"source": item.source, "status": item.status, "detail": item.detail}
        for item in bundle.statuses
    ]
    prices = price_history
    if prices is None and network_enabled:
        prices, price_health = fetch_research_price_history(symbol_list)
        health.append(price_health)
    elif prices is None:
        health.append(
            {
                "source": "research_price_history",
                "status": "disabled",
                "detail": "network collection disabled",
            }
        )

    factor_result: FactorBacktestResult | None = None
    warnings = list(global_result.warnings)
    if prices is not None:
        try:
            factor_result = validate_daily_factors(prices, symbol_list)
            health.append(
                {
                    "source": "factor_validation",
                    "status": factor_result.status,
                    "detail": (
                        f"oos={factor_result.oos_observations}; "
                        f"model={factor_result.model_version}"
                    ),
                }
            )
            warnings.extend(factor_result.warnings)
        except (ValueError, KeyError, np.linalg.LinAlgError):
            health.append(
                {
                    "source": "factor_validation",
                    "status": "blocked",
                    "detail": "walk-forward factor validation unavailable",
                }
            )
            warnings.append("Factor validation is blocked; no factor weight is inferred.")
    else:
        health.append(
            {
                "source": "factor_validation",
                "status": "blocked",
                "detail": "research price history missing",
            }
        )

    statuses = {str(item.get("status") or "") for item in health}
    if factor_result is not None and global_result.status in {"healthy", "research_only"}:
        status = "completed"
    elif "error" in statuses:
        status = "degraded"
    else:
        status = "partial"
    return DailyResearchEnrichment(
        status=status,
        generated_at=_rfc3339(now),
        global_narratives=global_result,
        factor_validation=factor_result,
        source_health=tuple(health),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _pct(value: float | None) -> str:
    return "UNKNOWN" if value is None else f"{value:.2%}"


def render_daily_research_markdown(result: DailyResearchEnrichment) -> str:
    narrative = result.global_narratives
    lines = [
        "## 全球市场与主观叙事因子",
        f"- 状态：`{narrative.status}`；有效观察：{narrative.accepted_count}；"
        f"独立加权来源组：{narrative.independent_groups}。",
        f"- 全球叙事风险预算乘数：{narrative.risk_budget_multiplier:.1%}；"
        f"有界研究分贡献：{narrative.decision_score_contribution:+.2%}。",
        f"- 社区情绪：{narrative.community_sentiment:+.3f}；"
        f"媒体分歧：{narrative.media_disagreement:.1%}；"
        f"拥挤惩罚：{narrative.crowding_penalty:.1%}。",
        "- Quora/搜索摘要只作线索，Reddit/社区只作一个相关证据组；"
        "均不能单独加仓或触发交易。",
    ]
    if narrative.topic_scores:
        topics = "；".join(
            f"{key}={value:+.2f}"
            for key, value in sorted(
                narrative.topic_scores.items(),
                key=lambda item: abs(item[1]),
                reverse=True,
            )[:8]
        )
        lines.append(f"- 主题状态：{topics}")
    if narrative.asset_scores:
        assets = "；".join(
            f"{key}={value:+.2f}"
            for key, value in sorted(
                narrative.asset_scores.items(),
                key=lambda item: abs(item[1]),
                reverse=True,
            )[:10]
        )
        lines.append(f"- 跨资产传导：{assets}")
    if narrative.observations:
        lines += [
            "",
            "| 来源 | 主题 | 方向 | 权重 | 状态 | 标题 |",
            "|---|---|---:|---:|---|---|",
        ]
        for item in narrative.observations[:10]:
            state = "context_only" if item.context_only else "weighted"
            lines.append(
                f"| {item.source} | {item.topic} | {item.direction:+.2f} | "
                f"{item.weight:.2f} | {state} | {item.title.replace('|', '/')} |"
            )

    factor = result.factor_validation
    lines += ["", "## 滚动回归与因子有效性"]
    if factor is None:
        lines.append("- `BLOCKED`：缺少足够的历史价格或因子代理；不推断因子有效。")
    else:
        sharpe = "UNKNOWN" if factor.net_sharpe is None else f"{factor.net_sharpe:.2f}"
        pred_ic = (
            "UNKNOWN"
            if factor.prediction_information_coefficient is None
            else f"{factor.prediction_information_coefficient:+.3f}"
        )
        lines += [
            f"- 状态：`{factor.status}`；OOS 观察：{factor.oos_observations}；"
            f"严格 walk-forward 模型：`{factor.model_version}`。",
            f"- OOS 净年化：{factor.net_annualized_return:+.2%}；"
            f"净年化波动：{factor.net_annualized_volatility:.2%}；Sharpe：{sharpe}。",
            f"- 命中率：{_pct(factor.hit_rate)}；预测 IC：{pred_ic}；"
            f"最大回撤：{factor.max_drawdown:.2%}。",
            f"- 平均换手：{factor.average_turnover:.2%}；"
            f"成本拖累：{factor.total_cost_drag:.4f}；"
            f"风险预算乘数：{factor.risk_budget_multiplier:.1%}。",
            "",
            "| 因子 | OOS IC | 方向校正 IC | 系数一致性 | 状态 | 有效权重 |",
            "|---|---:|---:|---:|---|---:|",
        ]
        for item in factor.factor_diagnostics:
            raw_ic = (
                "UNKNOWN"
                if item.oos_information_coefficient is None
                else f"{item.oos_information_coefficient:+.3f}"
            )
            directional_ic = (
                "UNKNOWN"
                if item.directional_information_coefficient is None
                else f"{item.directional_information_coefficient:+.3f}"
            )
            lines.append(
                f"| {item.factor} | {raw_ic} | {directional_ic} | "
                f"{item.coefficient_sign_consistency:.1%} | "
                f"{item.admission_status} | {item.effective_weight_multiplier:.1%} |"
            )

    lines += [
        "",
        "## 全球研究源健康",
        "| 来源 | 状态 | 说明 |",
        "|---|---|---|",
    ]
    for item in result.source_health:
        detail = str(item.get("detail") or "").replace("|", "/")
        lines.append(
            f"| {item.get('source', 'unknown')} | {item.get('status', 'unknown')} | {detail} |"
        )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "DEFAULT_GLOBAL_QUERIES",
    "DailyResearchEnrichment",
    "build_daily_research_enrichment",
    "build_factor_dataset",
    "collect_daily_global_narratives",
    "default_global_source_settings",
    "fetch_research_price_history",
    "render_daily_research_markdown",
    "validate_daily_factors",
]
