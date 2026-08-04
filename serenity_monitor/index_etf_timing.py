"""Simple, staged timing for long-term index-ETF investors.

The model is intentionally narrower than the broader research framework.  It
protects a strategic core, uses timing only for a bounded tactical sleeve, and
never turns one indicator into an all-in/all-out decision.  It is designed for
liquid broad-index ETFs and cash-reserve products; single stocks, thematic
ETFs, options, leverage and shorting receive no new-capital signal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import math
import numpy as np
import pandas as pd


_PROXY = {
    "VOO": "SPY",
    "QQQM": "QQQ",
}
_DEFAULT_CORE = ("VOO", "QQQM", "DIA", "SCHD")
_DEFAULT_RESERVE = ("BOXX",)
_DEFAULT_DIVERSIFIER = ("GLDM",)


def _clamp(value: float, lower: float, upper: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        return lower
    return min(max(number, lower), upper)


def _rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    average_gain = gains.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    average_loss = losses.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = average_gain / average_loss.replace(0.0, np.nan)
    value = 100.0 - 100.0 / (1.0 + rs)
    return value.fillna(50.0)


def _percentile_of_last(series: pd.Series, window: int = 504) -> float | None:
    values = pd.to_numeric(series.dropna().tail(window), errors="coerce").dropna()
    if len(values) < 60:
        return None
    latest = float(values.iloc[-1])
    return float((values <= latest).mean())


@dataclass(frozen=True)
class IndexTimingSignal:
    symbol: str
    role: str
    status: str
    regime: str
    action: str
    close: float | None
    ma50: float | None
    ma200: float | None
    momentum_63: float | None
    drawdown_63: float | None
    rsi14: float | None
    realized_vol_20: float | None
    volatility_percentile: float | None
    current_weight: float | None
    target_weight: float | None
    tactical_add_nav_fraction: float
    tactical_trim_nav_fraction: float
    score: float
    rationale: str
    invalidation: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class IndexTimingResult:
    status: str
    risk_budget: float
    core_symbols: tuple[str, ...]
    reserve_symbols: tuple[str, ...]
    diversifier_symbols: tuple[str, ...]
    signals: tuple[IndexTimingSignal, ...]
    portfolio_action: str
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "risk_budget": self.risk_budget,
            "core_symbols": list(self.core_symbols),
            "reserve_symbols": list(self.reserve_symbols),
            "diversifier_symbols": list(self.diversifier_symbols),
            "signals": [row.to_dict() for row in self.signals],
            "portfolio_action": self.portfolio_action,
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def _role(
    symbol: str,
    *,
    core: set[str],
    reserve: set[str],
    diversifier: set[str],
) -> str:
    if symbol in core:
        return "core_index"
    if symbol in reserve:
        return "cash_reserve"
    if symbol in diversifier:
        return "diversifier"
    return "legacy_or_satellite"


def _unavailable_signal(
    symbol: str,
    role: str,
    current_weight: float | None,
    target_weight: float | None,
    reason: str,
) -> IndexTimingSignal:
    return IndexTimingSignal(
        symbol=symbol,
        role=role,
        status="blocked",
        regime="unknown",
        action="NO_NEW_CAPITAL" if role == "legacy_or_satellite" else "PAUSE_ADD",
        close=None,
        ma50=None,
        ma200=None,
        momentum_63=None,
        drawdown_63=None,
        rsi14=None,
        realized_vol_20=None,
        volatility_percentile=None,
        current_weight=current_weight,
        target_weight=target_weight,
        tactical_add_nav_fraction=0.0,
        tactical_trim_nav_fraction=0.0,
        score=0.0,
        rationale=reason,
        invalidation="Obtain at least 252 clean adjusted daily closes under the same symbol definition.",
    )


def analyze_index_etf_timing(
    prices: pd.DataFrame,
    symbols: Iterable[str],
    *,
    risk_budget: float = 1.0,
    current_weights: Mapping[str, float] | None = None,
    target_weights: Mapping[str, float] | None = None,
    core_symbols: Iterable[str] = _DEFAULT_CORE,
    reserve_symbols: Iterable[str] = _DEFAULT_RESERVE,
    diversifier_symbols: Iterable[str] = _DEFAULT_DIVERSIFIER,
    tactical_sleeve_nav: float = 0.15,
    tranche_nav: float = 0.025,
) -> IndexTimingResult:
    """Return staged index timing without selling the strategic core.

    `TACTICAL_ADD_1/2` means an owner review for one or two bounded tranches.
    `TRIM_TACTICAL_ONLY` may occur only when a target weight is explicitly
    supplied; the model never infers a strategic target or liquidates the core.
    """

    budget = _clamp(risk_budget, 0.40, 1.05)
    sleeve = _clamp(tactical_sleeve_nav, 0.0, 0.30)
    tranche = _clamp(tranche_nav, 0.0, max(sleeve, 1e-12))
    current = {str(k).upper(): max(0.0, float(v)) for k, v in dict(current_weights or {}).items()}
    targets = {str(k).upper(): max(0.0, float(v)) for k, v in dict(target_weights or {}).items()}
    core = {str(value).strip().upper() for value in core_symbols if str(value).strip()}
    reserve = {str(value).strip().upper() for value in reserve_symbols if str(value).strip()}
    diversifier = {str(value).strip().upper() for value in diversifier_symbols if str(value).strip()}
    requested = tuple(dict.fromkeys(str(value).strip().upper() for value in symbols if str(value).strip()))

    close = prices.copy().sort_index().apply(pd.to_numeric, errors="coerce")
    close.columns = [str(value).upper() for value in close.columns]
    signals: list[IndexTimingSignal] = []

    for symbol in requested:
        role = _role(symbol, core=core, reserve=reserve, diversifier=diversifier)
        proxy = _PROXY.get(symbol, symbol)
        current_weight = current.get(symbol)
        target_weight = targets.get(symbol)
        if proxy not in close:
            signals.append(
                _unavailable_signal(
                    symbol,
                    role,
                    current_weight,
                    target_weight,
                    f"No adjusted history is available for {proxy}; missing data is not a neutral timing signal.",
                )
            )
            continue
        series = close[proxy].dropna()
        if len(series) < 252:
            signals.append(
                _unavailable_signal(
                    symbol,
                    role,
                    current_weight,
                    target_weight,
                    f"Only {len(series)} sessions are available; at least 252 are required.",
                )
            )
            continue

        returns = series.pct_change(fill_method=None)
        ma50_series = series.rolling(50).mean()
        ma200_series = series.rolling(200).mean()
        vol20_series = returns.rolling(20).std() * math.sqrt(252.0)
        rsi_series = _rsi(series)
        latest = float(series.iloc[-1])
        ma50 = float(ma50_series.iloc[-1])
        ma200 = float(ma200_series.iloc[-1])
        momentum63 = float(series.iloc[-1] / series.iloc[-64] - 1.0)
        high63 = float(series.tail(63).max())
        drawdown63 = latest / high63 - 1.0
        rsi14 = float(rsi_series.iloc[-1])
        vol20 = float(vol20_series.iloc[-1])
        vol_percentile = _percentile_of_last(vol20_series)
        extension50 = latest / ma50 - 1.0

        if latest >= ma200 and ma50 >= ma200:
            regime = "uptrend"
            regime_score = 0.35
        elif latest >= ma200:
            regime = "recovery"
            regime_score = 0.15
        elif latest < ma200 and ma50 < ma200:
            regime = "downtrend"
            regime_score = -0.35
        else:
            regime = "weakening"
            regime_score = -0.15

        pullback_score = 0.0
        if regime in {"uptrend", "recovery"}:
            pullback_score = _clamp((-drawdown63 - 0.02) / 0.10, 0.0, 1.0) * 0.35
        reversion_score = _clamp((45.0 - rsi14) / 20.0, -1.0, 1.0) * 0.15
        risk_score = _clamp((budget - 0.75) / 0.25, -1.0, 1.0) * 0.15
        volatility_penalty = 0.0 if vol_percentile is None else max(0.0, vol_percentile - 0.75) * 0.40
        score = _clamp(regime_score + pullback_score + reversion_score + risk_score - volatility_penalty, -1.0, 1.0)

        action = "CORE_HOLD"
        add_fraction = 0.0
        trim_fraction = 0.0
        rationale = "Strategic exposure remains invested; no bounded tactical timing threshold is met."
        invalidation = "A confirmed trend break, materially lower risk budget, or an explicit target-weight breach changes the review state."

        if role == "legacy_or_satellite":
            action = "NO_NEW_CAPITAL"
            rationale = "The owner mandate is index-ETF timing; single-name or thematic exposure receives no new capital."
            invalidation = "Only an explicit owner-approved allowlist change can make this symbol eligible for new capital."
        elif role == "cash_reserve":
            action = "RESERVE_HOLD"
            rationale = "The reserve sleeve is dry powder for staged index purchases, not a momentum trade."
            invalidation = "Use reserve cash only after an approved index ETF passes timing, cash and transaction-cost gates."
        elif role == "diversifier":
            action = "DIVERSIFIER_HOLD"
            rationale = "The diversifier is monitored separately from equity timing and receives no automatic add signal."
            invalidation = "An explicit strategic target and rebalancing rule are required before changing this sleeve."
        elif budget < 0.72 or regime == "downtrend" or (vol_percentile is not None and vol_percentile >= 0.95):
            action = "PAUSE_ADD"
            rationale = (
                f"Risk budget={budget:.1%}, regime={regime}, volatility percentile="
                f"{'UNKNOWN' if vol_percentile is None else f'{vol_percentile:.1%}'}; do not average down into an unconfirmed break."
            )
            invalidation = "Price reclaims the 200-day trend, volatility normalizes, and the combined risk budget recovers."
        elif (
            regime in {"uptrend", "recovery"}
            and drawdown63 <= -0.08
            and rsi14 <= 38.0
            and budget >= 0.82
            and (vol_percentile is None or vol_percentile < 0.90)
        ):
            action = "TACTICAL_ADD_2"
            add_fraction = min(sleeve, 2.0 * tranche)
            rationale = "A deep pullback occurs inside a non-broken long-term trend with an acceptable risk budget; review two staged tranches, never one all-in order."
            invalidation = "Cancel the second tranche if the 200-day trend breaks, breadth/risk deteriorates, or the first tranche cannot cover expected costs."
        elif (
            regime in {"uptrend", "recovery"}
            and drawdown63 <= -0.04
            and rsi14 <= 45.0
            and budget >= 0.85
            and (vol_percentile is None or vol_percentile < 0.90)
        ):
            action = "TACTICAL_ADD_1"
            add_fraction = min(sleeve, tranche)
            rationale = "A moderate pullback inside an intact/recovering trend supports one small tactical tranche after cost review."
            invalidation = "Do not add if price loses the 200-day trend or the combined risk budget falls below 85%."
        elif (
            target_weight is not None
            and current_weight is not None
            and current_weight > target_weight + 0.05
            and rsi14 >= 75.0
            and extension50 >= 0.10
        ):
            action = "TRIM_TACTICAL_ONLY"
            trim_fraction = min(current_weight - target_weight, sleeve)
            rationale = "The position is explicitly above target and unusually extended; review trimming only the tactical excess, never the strategic core."
            invalidation = "No trim if lot-level tax/transaction costs exceed the expected benefit or the position is not actually above its approved target."

        signals.append(
            IndexTimingSignal(
                symbol=symbol,
                role=role,
                status="ok",
                regime=regime,
                action=action,
                close=round(latest, 8),
                ma50=round(ma50, 8),
                ma200=round(ma200, 8),
                momentum_63=round(momentum63, 8),
                drawdown_63=round(drawdown63, 8),
                rsi14=round(rsi14, 6),
                realized_vol_20=round(vol20, 8),
                volatility_percentile=None if vol_percentile is None else round(vol_percentile, 6),
                current_weight=current_weight,
                target_weight=target_weight,
                tactical_add_nav_fraction=round(add_fraction, 6),
                tactical_trim_nav_fraction=round(trim_fraction, 6),
                score=round(score, 6),
                rationale=rationale,
                invalidation=invalidation,
            )
        )

    actions = {row.action for row in signals}
    if "TRIM_TACTICAL_ONLY" in actions:
        portfolio_action = "TRIM_TACTICAL_REVIEW"
    elif actions & {"TACTICAL_ADD_1", "TACTICAL_ADD_2"}:
        portfolio_action = "STAGED_INDEX_ADD_REVIEW"
    elif "PAUSE_ADD" in actions:
        portfolio_action = "HOLD_CORE_PAUSE_ADD"
    else:
        portfolio_action = "HOLD_CORE"
    if not signals:
        status = "blocked"
    elif any(row.status == "ok" for row in signals):
        status = "ok" if all(row.status == "ok" for row in signals) else "partial"
    else:
        status = "blocked"
    warnings = (
        "The strategic index core is never sold solely because of an overbought/oversold indicator.",
        "Timing applies only to a bounded tactical sleeve and uses staged tranches with hysteresis.",
        "RSI, moving averages, drawdown and volatility are one price-derived evidence family, not four independent votes.",
        "Single stocks, thematic ETFs, options, leverage and shorting receive no new-capital signal.",
        "A target weight must be explicitly supplied before any tactical trim can be proposed.",
    )
    return IndexTimingResult(
        status=status,
        risk_budget=round(budget, 6),
        core_symbols=tuple(sorted(core)),
        reserve_symbols=tuple(sorted(reserve)),
        diversifier_symbols=tuple(sorted(diversifier)),
        signals=tuple(signals),
        portfolio_action=portfolio_action,
        warnings=warnings,
    )


def fetch_index_etf_history(
    symbols: Iterable[str],
    *,
    period: str = "5y",
) -> tuple[pd.DataFrame | None, Mapping[str, Any]]:
    requested = sorted(
        {
            _PROXY.get(str(value).strip().upper(), str(value).strip().upper())
            for value in symbols
            if str(value).strip()
        }
    )
    if not requested:
        return None, {"source": "index_timing_history", "status": "blocked", "detail": "no symbols"}
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
            raise ValueError("empty history")
        if isinstance(raw.columns, pd.MultiIndex):
            if "Close" not in raw.columns.get_level_values(0):
                raise ValueError("close unavailable")
            close = raw["Close"].copy()
        else:
            if "Close" not in raw:
                raise ValueError("close unavailable")
            close = raw[["Close"]].rename(columns={"Close": requested[0]})
        close.columns = [str(value).upper() for value in close.columns]
        close = close.apply(pd.to_numeric, errors="coerce").sort_index().dropna(how="all")
        if len(close) < 252:
            raise ValueError("insufficient history")
        return close, {
            "source": "index_timing_history",
            "status": "healthy",
            "detail": f"adjusted public history; rows={len(close)}; research-only",
        }
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return None, {
            "source": "index_timing_history",
            "status": "blocked",
            "detail": "adjusted index history unavailable",
        }


def render_index_timing_markdown(result: IndexTimingResult) -> str:
    lines = [
        "## 指数 ETF 择时",
        f"- 组合动作：**{result.portfolio_action}**；综合风险预算：{result.risk_budget:.1%}。",
        "- 核心仓长期持有；只有战术仓按回撤、趋势和风险预算分批低买/高位收回。",
        "",
        "| 标的 | 角色 | 趋势 | 动作 | 63日回撤 | RSI14 | 波动分位 | 战术NAV | 理由 |",
        "|---|---|---|---|---:|---:|---:|---:|---|",
    ]
    for row in result.signals:
        drawdown = "UNKNOWN" if row.drawdown_63 is None else f"{row.drawdown_63:.1%}"
        rsi = "UNKNOWN" if row.rsi14 is None else f"{row.rsi14:.1f}"
        vol = "UNKNOWN" if row.volatility_percentile is None else f"{row.volatility_percentile:.1%}"
        nav = row.tactical_add_nav_fraction or row.tactical_trim_nav_fraction
        lines.append(
            f"| {row.symbol} | {row.role} | {row.regime} | **{row.action}** | "
            f"{drawdown} | {rsi} | {vol} | {nav:.1%} | {row.rationale.replace('|', '/')[:220]} |"
        )
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "IndexTimingResult",
    "IndexTimingSignal",
    "analyze_index_etf_timing",
    "fetch_index_etf_history",
    "render_index_timing_markdown",
]
