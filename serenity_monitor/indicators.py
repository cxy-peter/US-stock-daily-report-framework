"""Deterministic price, liquidity and trend indicators."""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

W_1W, W_1M, W_3M, W_1Y = 5, 21, 63, 252


@dataclass
class Indicators:
    price: float
    drawdown_from_peak: float
    peak_price: float
    ret_1w: float
    ret_1m: float
    ret_3m: float
    ann_vol_30d: float
    volume_ratio: float
    last_day_change: float
    max_1d_drop_1m: float
    avg_dollar_vol_20d: float
    ma50: float
    ma200: float
    rsi14: float


def _ret(closes: pd.Series, days: int) -> float:
    if len(closes) <= days:
        return float("nan")
    return float(closes.iloc[-1] / closes.iloc[-1 - days] - 1)


def _rsi(closes: pd.Series, window: int = 14) -> float:
    diff = closes.diff().dropna()
    if len(diff) < window:
        return float("nan")
    gains = diff.clip(lower=0).tail(window).mean()
    losses = -diff.clip(upper=0).tail(window).mean()
    if losses == 0:
        return 100.0
    return float(100 - 100 / (1 + gains / losses))


def compute(closes: pd.Series, volumes: pd.Series, peak_window: int = W_1Y) -> Indicators:
    closes = closes.dropna().astype(float)
    volumes = volumes.reindex(closes.index).fillna(0).astype(float)
    if closes.empty:
        raise ValueError("closes must not be empty")
    price = float(closes.iloc[-1])
    peak = float(closes.tail(peak_window).max())
    daily = closes.pct_change().dropna()
    volume_ratio = float("nan")
    if len(volumes) >= 20 and volumes.tail(20).mean() > 0:
        volume_ratio = float(volumes.iloc[-1] / volumes.tail(20).mean())
    return Indicators(
        price=price,
        drawdown_from_peak=price / peak - 1 if peak else float("nan"),
        peak_price=peak,
        ret_1w=_ret(closes, W_1W),
        ret_1m=_ret(closes, W_1M),
        ret_3m=_ret(closes, W_3M),
        ann_vol_30d=(
            float(daily.tail(30).std() * math.sqrt(252)) if len(daily) >= 30 else float("nan")
        ),
        volume_ratio=volume_ratio,
        last_day_change=float(daily.iloc[-1]) if len(daily) else float("nan"),
        max_1d_drop_1m=float(daily.tail(W_1M).min()) if len(daily) else float("nan"),
        avg_dollar_vol_20d=float((closes.tail(20) * volumes.tail(20)).mean()),
        ma50=float(closes.tail(50).mean()),
        ma200=float(closes.tail(200).mean()),
        rsi14=_rsi(closes),
    )
