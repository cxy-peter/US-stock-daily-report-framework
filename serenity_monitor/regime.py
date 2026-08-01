"""Market-regime classification used by the risk manager."""
from __future__ import annotations

from dataclasses import dataclass

from .indicators import Indicators


@dataclass(frozen=True)
class MarketRegime:
    label: str  # risk_on | neutral | risk_off
    risk_multiplier: float
    score: int
    reasons: tuple[str, ...]


def classify_regime(indicators: Indicators | None) -> MarketRegime:
    """Classify the broad market conservatively from a benchmark such as SPY.

    The regime never creates a buy/sell signal on its own. It only changes the
    maximum risk budget used by the portfolio manager.
    """
    if indicators is None:
        return MarketRegime(
            label="neutral",
            risk_multiplier=0.85,
            score=0,
            reasons=("基准行情缺失，按中性偏保守处理。",),
        )

    score = 0
    reasons: list[str] = []
    if indicators.price >= indicators.ma200:
        score += 1
        reasons.append("基准位于200日均线上方")
    else:
        score -= 1
        reasons.append("基准跌破200日均线")
    if indicators.ma50 >= indicators.ma200:
        score += 1
        reasons.append("50日均线不弱于200日均线")
    else:
        score -= 1
        reasons.append("50日均线低于200日均线")
    if indicators.ret_1m >= 0.02:
        score += 1
        reasons.append(f"近1月回报 {indicators.ret_1m:+.1%}")
    elif indicators.ret_1m <= -0.05:
        score -= 1
        reasons.append(f"近1月回报 {indicators.ret_1m:+.1%}")
    if indicators.ann_vol_30d == indicators.ann_vol_30d and indicators.ann_vol_30d >= 0.30:
        score -= 1
        reasons.append(f"30日年化波动升至 {indicators.ann_vol_30d:.1%}")

    if score >= 2:
        return MarketRegime("risk_on", 1.0, score, tuple(reasons))
    if score <= -2:
        return MarketRegime("risk_off", 0.65, score, tuple(reasons))
    return MarketRegime("neutral", 0.85, score, tuple(reasons))
