"""Daily global-source and institutional factor enrichment.

Public research may refresh every day while broker reconciliation runs on a
separate cadence. Missing research remains visible and adjusted price history is
research-only, never a settlement-grade close.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
import requests

from .external_views import ExternalBundle, ExternalSettings, collect_external_views
from .factor_backtest import FactorBacktestResult, walk_forward_factor_backtest
from .global_market_narratives import GlobalNarrativeResult, score_global_narratives
from .institutional_factor_research import (
    InstitutionalFactorResearchResult,
    run_institutional_factor_research,
)


# external_views executes at most four public-search queries. These four groups
# cover every requested category rather than silently truncating the list.
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

_ASSET_PROXY = {
    "QQQM": "QQQ",
    "VOO": "SPY",
}


@dataclass(frozen=True)
class DailyResearchEnrichment:
    status: str
    generated_at: str
    global_narratives: GlobalNarrativeResult
    factor_validation: FactorBacktestResult | None
    institutional_factor_research: InstitutionalFactorResearchResult | None
    source_health: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at,
            "global_narratives": self.global_narratives.to_dict(),
            "factor_validation": (
                None if self.factor_validation is None else self.factor_validation.to_dict()
            ),
            "institutional_factor_research": (
                None
                if self.institutional_factor_research is None
                else self.institutional_factor_research.to_dict()
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
        reddit_subreddits=("stocks", "investing", "wallstreetbets", "semiconductors"),
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
    result = score_global_narratives(
        items,
        as_of=now,
        lookback_days=source_settings.lookback_days,
        portfolio_tickers=symbol_list,
    )
    return result, bundle


def fetch_research_price_history(
    symbols: Iterable[str],
    *,
    period: str = "5y",
) -> tuple[pd.DataFrame | None, Mapping[str, Any]]:
    """Fetch adjusted public history as a non-settlement research source."""

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
        if len(close) < 650:
            raise ValueError("insufficient history for multi-horizon validation")
        return close, {
            "source": "research_price_history",
            "status": "healthy",
            "detail": (
                f"adjusted public research history; rows={len(close)}; "
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
    """Build close-t signals and the daily portfolio-proxy return series."""

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
        momentum_21 = total_return("SPY", 21)
        momentum_63 = total_return("SPY", 63)
        if momentum_21 is not None:
            signals["market_momentum_21"] = momentum_21
        if momentum_63 is not None:
            signals["market_momentum_63"] = momentum_63
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
    feature_version: str = "daily_global_proxy_factors/v2.0.0",
    transaction_cost_bps: float = 5.0,
) -> FactorBacktestResult:
    """Compatibility 5-session result used by existing consumers."""

    signals, target = build_factor_dataset(prices, portfolio_symbols)
    return walk_forward_factor_backtest(
        signals,
        daily_returns=target,
        target_name="equal_weight_private_portfolio_proxy",
        feature_version=feature_version,
        horizon_sessions=5,
        train_size=504,
        test_size=63,
        step_size=63,
        expanding=True,
        ridge=5.0,
        transaction_cost_bps=transaction_cost_bps,
        minimum_oos_observations=24,
        purge_sessions=5,
        embargo_sessions=5,
        multiple_testing_alpha=0.10,
    )


def validate_institutional_factors(
    prices: pd.DataFrame,
    portfolio_symbols: Iterable[str],
    *,
    as_of: dt.date | None = None,
    transaction_cost_bps: float = 5.0,
) -> InstitutionalFactorResearchResult:
    signals, target = build_factor_dataset(prices, portfolio_symbols)
    return run_institutional_factor_research(
        signals,
        target,
        as_of=as_of,
        feature_version="institutional_factor_library/v1.0.0",
        horizons=(1, 5, 20),
        train_size=504,
        test_size=63,
        transaction_cost_bps=transaction_cost_bps,
    )


def asset_narrative_score(result: DailyResearchEnrichment, ticker: str) -> float:
    symbol = str(ticker).strip().upper()
    proxy = _ASSET_PROXY.get(symbol, symbol)
    return float(
        result.global_narratives.asset_scores.get(
            symbol,
            result.global_narratives.asset_scores.get(proxy, 0.0),
        )
    )


def build_research_theses(result: DailyResearchEnrichment) -> tuple[str, ...]:
    """Return direct, falsifiable daily theses rather than process narration."""

    topics = result.global_narratives.topic_scores
    theses: list[str] = []
    if topics.get("memory_hbm_demand", 0.0) > 0.10:
        theses.append(
            "HBM/存储需求仍支持 MU 与 SMH；若 memory_oversupply 或出口限制转强，该论点失效。"
        )
    if topics.get("memory_oversupply", 0.0) > 0.10:
        theses.append(
            "存储供给过剩风险正在上升，MU/SMH 不宜追涨；库存和价格重新收紧才解除。"
        )
    if topics.get("oil_supply", 0.0) > 0.10 or topics.get("middle_east_escalation", 0.0) > 0.10:
        theses.append(
            "石油供应或中东冲突提高通胀与风险溢价，压制长久期科技；停火和运输恢复是反证。"
        )
    if topics.get("semiconductor_export_controls", 0.0) > 0.10:
        theses.append(
            "半导体出口限制是 MU/SMH/Nasdaq 的负面尾部因子；正式豁免或许可落地可推翻。"
        )
    if topics.get("rates_inflation", 0.0) > 0.10:
        theses.append(
            "利率与通胀压力仍不利于高久期资产；实际收益率回落与通胀降温是反证。"
        )
    institutional = result.institutional_factor_research
    if institutional:
        if institutional.active_factors:
            theses.append(
                "多周期 OOS 仍有效的因子："
                + "、".join(institutional.active_factors[:4])
                + "；只有跨期限、扣费后和 FDR 通过才保留权重。"
            )
        if institutional.quarantined_factors:
            theses.append(
                "今日隔离的因子："
                + "、".join(institutional.quarantined_factors[:4])
                + "；其信号不进入加仓判断。"
            )
    if not theses:
        theses.append("当前没有形成两组以上独立证据支持的新方向，基准结论是持有而非猜测。")
    return tuple(theses[:6])


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

    institutional: InstitutionalFactorResearchResult | None = None
    primary_factor: FactorBacktestResult | None = None
    warnings = list(global_result.warnings)
    if prices is not None:
        try:
            institutional = validate_institutional_factors(
                prices,
                symbol_list,
                as_of=now.date(),
            )
            primary_factor = institutional.primary_result
            health.append(
                {
                    "source": "institutional_factor_validation",
                    "status": institutional.status,
                    "detail": (
                        f"horizons=1/5/20; active={len(institutional.active_factors)}; "
                        f"quarantined={len(institutional.quarantined_factors)}"
                    ),
                }
            )
            warnings.extend(institutional.warnings)
        except (ValueError, KeyError, np.linalg.LinAlgError):
            health.append(
                {
                    "source": "institutional_factor_validation",
                    "status": "blocked",
                    "detail": "purged multi-horizon factor validation unavailable",
                }
            )
            warnings.append("Institutional factor validation is blocked; no factor weight is inferred.")
    else:
        health.append(
            {
                "source": "institutional_factor_validation",
                "status": "blocked",
                "detail": "research price history missing",
            }
        )

    statuses = {str(item.get("status") or "") for item in health}
    if institutional is not None and global_result.status in {"healthy", "research_only"}:
        status = "completed"
    elif "error" in statuses:
        status = "degraded"
    else:
        status = "partial"
    return DailyResearchEnrichment(
        status=status,
        generated_at=_rfc3339(now),
        global_narratives=global_result,
        factor_validation=primary_factor,
        institutional_factor_research=institutional,
        source_health=tuple(health),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def render_daily_research_markdown(result: DailyResearchEnrichment) -> str:
    """Render compact thesis-first research; detailed health is collapsible."""

    lines = ["## 5. 今日核心论点"]
    lines.extend(f"- **论点：** {thesis}" for thesis in build_research_theses(result))

    institutional = result.institutional_factor_research
    lines += ["", "## 6. 因子有效性"]
    if institutional is None:
        lines.append("- `BLOCKED`：无法完成 1/5/20 日 purged OOS 验证，因子权重为 0。")
    else:
        lines += [
            f"- 总状态：`{institutional.status}`；风险预算乘数："
            f"{institutional.risk_budget_multiplier:.1%}。",
            "- 每日追加新样本并重跑版本化 walk-forward；因子定义只在月度评审或数据定义变化时修改。",
            "",
            "| 因子 | 多周期状态 | 中位方向IC | 最优q值 | 稳健度 | 有效权重 |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for item in sorted(
            institutional.factor_diagnostics,
            key=lambda row: row.effective_weight_multiplier,
            reverse=True,
        ):
            ic = "UNKNOWN" if item.median_directional_ic is None else f"{item.median_directional_ic:+.3f}"
            q_value = "UNKNOWN" if item.best_multiple_testing_q is None else f"{item.best_multiple_testing_q:.3f}"
            lines.append(
                f"| {item.factor} | {item.admission_status} | {ic} | {q_value} | "
                f"{item.median_robustness_score:.1%} | {item.effective_weight_multiplier:.1%} |"
            )
        lines += ["", "| Horizon | 状态 | OOS样本 | 净年化 | Sharpe | PSR | 最大回撤 | 成本拖累 |", "|---:|---|---:|---:|---:|---:|---:|---:|"]
        for row in institutional.horizon_summaries:
            sharpe = "UNKNOWN" if row.net_sharpe is None else f"{row.net_sharpe:.2f}"
            psr = "UNKNOWN" if row.probabilistic_sharpe_ratio is None else f"{row.probabilistic_sharpe_ratio:.1%}"
            lines.append(
                f"| {row.horizon_sessions} | {row.status} | {row.oos_observations} | "
                f"{row.net_annualized_return:+.2%} | {sharpe} | {psr} | "
                f"{row.max_drawdown:.2%} | {row.total_cost_drag:.4f} |"
            )

    narrative = result.global_narratives
    lines += [
        "",
        "## 7. 事件与跨资产传导",
        f"- 独立来源组：{narrative.independent_groups}；叙事风险预算乘数："
        f"{narrative.risk_budget_multiplier:.1%}；社区拥挤惩罚："
        f"{narrative.crowding_penalty:.1%}。",
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
        lines.append(f"- 主题：{topics}")
    if narrative.asset_scores:
        assets = "；".join(
            f"{key}={value:+.2f}"
            for key, value in sorted(
                narrative.asset_scores.items(),
                key=lambda item: abs(item[1]),
                reverse=True,
            )[:10]
        )
        lines.append(f"- 资产传导：{assets}")
    if narrative.observations:
        lines += ["", "| 来源 | 主题 | 方向 | 权重 | 论据 |", "|---|---|---:|---:|---|"]
        for item in narrative.observations[:8]:
            state = "零权重线索" if item.context_only or item.weight < 0.01 else "加权"
            lines.append(
                f"| {item.source} | {item.topic} | {item.direction:+.2f} | "
                f"{item.weight:.2f} ({state}) | {item.title.replace('|', '/')} |"
            )

    lines += ["", "<details><summary>数据源健康与方法边界</summary>", "", "| 来源 | 状态 | 说明 |", "|---|---|---|"]
    for item in result.source_health:
        lines.append(
            f"| {item.get('source', 'unknown')} | {item.get('status', 'unknown')} | "
            f"{str(item.get('detail') or '').replace('|', '/')} |"
        )
    lines += ["", "- Quora/搜索摘要直接权重为 0；Reddit 是一个相关社区组。", "- 所有价格历史仅用于研究，不能替代 accepted close 或券商成交。", "- 因子、媒体、优化器均无下单权。", "", "</details>"]
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "DEFAULT_GLOBAL_QUERIES",
    "DailyResearchEnrichment",
    "asset_narrative_score",
    "build_daily_research_enrichment",
    "build_factor_dataset",
    "build_research_theses",
    "collect_daily_global_narratives",
    "default_global_source_settings",
    "fetch_research_price_history",
    "render_daily_research_markdown",
    "validate_daily_factors",
    "validate_institutional_factors",
]
