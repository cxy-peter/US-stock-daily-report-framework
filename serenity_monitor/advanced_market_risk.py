"""Advanced market-risk features for one post-close report.

The module separates three related but non-independent evidence groups:

1. the SPX option-implied volatility surface;
2. single-name/index option-chain tail pricing;
3. overnight and premarket price discovery.

All outputs are research/risk controls. They may tighten risk or request review,
but cannot independently create or execute a trade.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence


_NORMAL = NormalDist()


def _clamp(value: float, lower: float, upper: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("value must be finite")
    return min(max(value, lower), upper)


def _finite_optional(value: Any, *, positive: bool = False) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return number


def _ratio(left: float | None, right: float | None) -> float | None:
    if left is None or right is None or abs(right) <= 1e-12:
        return None
    return left / right


@dataclass(frozen=True)
class VolatilitySurfaceSnapshot:
    observed_at: dt.datetime
    vix1d: float | None = None
    vix9d: float | None = None
    vix: float | None = None
    vix3m: float | None = None
    vix6m: float | None = None
    vvix: float | None = None
    skew: float | None = None
    realized_vol_20d: float | None = None
    put_call_volume_ratio: float | None = None
    put_call_open_interest_ratio: float | None = None
    source_health: str = "healthy"

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        for name in (
            "vix1d", "vix9d", "vix", "vix3m", "vix6m", "vvix", "skew",
            "realized_vol_20d", "put_call_volume_ratio", "put_call_open_interest_ratio",
        ):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(float(value)) or float(value) < 0):
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True)
class VolatilitySurfaceResult:
    status: str
    regime: str
    level_score: float
    backwardation_score: float
    vol_of_vol_score: float
    tail_skew_score: float
    variance_risk_premium_score: float
    put_call_score: float
    composite_stress: float
    risk_budget_multiplier: float
    ratios: Mapping[str, float | None]
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "regime": self.regime,
            "level_score": self.level_score,
            "backwardation_score": self.backwardation_score,
            "vol_of_vol_score": self.vol_of_vol_score,
            "tail_skew_score": self.tail_skew_score,
            "variance_risk_premium_score": self.variance_risk_premium_score,
            "put_call_score": self.put_call_score,
            "composite_stress": self.composite_stress,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "ratios": dict(self.ratios),
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def evaluate_volatility_surface(snapshot: VolatilitySurfaceSnapshot) -> VolatilitySurfaceResult:
    """Evaluate front-end stress, term structure, VVIX and tail skew jointly."""

    vix = _finite_optional(snapshot.vix, positive=True)
    ratios = {
        "vix1d_vix9d": _ratio(snapshot.vix1d, snapshot.vix9d),
        "vix9d_vix": _ratio(snapshot.vix9d, vix),
        "vix_vix3m": _ratio(vix, snapshot.vix3m),
        "vix3m_vix6m": _ratio(snapshot.vix3m, snapshot.vix6m),
    }
    available = sum(value is not None for value in (
        snapshot.vix1d, snapshot.vix9d, snapshot.vix, snapshot.vix3m,
        snapshot.vix6m, snapshot.vvix, snapshot.skew,
    ))
    if vix is None or snapshot.source_health not in {"healthy", "degraded"}:
        return VolatilitySurfaceResult(
            status="blocked",
            regime="unknown",
            level_score=0.0,
            backwardation_score=0.0,
            vol_of_vol_score=0.0,
            tail_skew_score=0.0,
            variance_risk_premium_score=0.0,
            put_call_score=0.0,
            composite_stress=0.0,
            risk_budget_multiplier=0.85,
            ratios=ratios,
            warnings=("A current VIX observation is required; calm is never inferred from missing data.",),
        )

    level_score = _clamp((vix - 16.0) / 24.0, 0.0, 1.0)
    backwardation_components: list[float] = []
    for key in ("vix1d_vix9d", "vix9d_vix", "vix_vix3m"):
        ratio = ratios[key]
        if ratio is not None:
            backwardation_components.append(_clamp((ratio - 0.95) / 0.25, 0.0, 1.0))
    backwardation = max(backwardation_components, default=0.0)
    vvix = _finite_optional(snapshot.vvix)
    vol_of_vol = 0.0 if vvix is None else _clamp((vvix - 90.0) / 80.0, 0.0, 1.0)
    skew = _finite_optional(snapshot.skew)
    tail_skew = 0.0 if skew is None else _clamp((skew - 120.0) / 45.0, 0.0, 1.0)

    realized = _finite_optional(snapshot.realized_vol_20d)
    variance_risk_premium = 0.0
    if realized is not None:
        # Inputs may be supplied in percentage points (e.g. 18) or decimals (0.18).
        realized_points = realized * 100.0 if realized <= 2.0 else realized
        variance_risk_premium = _clamp((vix - realized_points - 2.0) / 14.0, 0.0, 1.0)

    put_call_values = [
        value
        for value in (
            _finite_optional(snapshot.put_call_volume_ratio),
            _finite_optional(snapshot.put_call_open_interest_ratio),
        )
        if value is not None
    ]
    put_call = 0.0 if not put_call_values else _clamp((sum(put_call_values) / len(put_call_values) - 0.85) / 1.15, 0.0, 1.0)

    # These variables share an SPX-options lineage and therefore form one group,
    # not six independent confirmations.
    composite = _clamp(
        0.28 * level_score
        + 0.25 * backwardation
        + 0.17 * vol_of_vol
        + 0.15 * tail_skew
        + 0.10 * variance_risk_premium
        + 0.05 * put_call,
        0.0,
        1.0,
    )
    if composite >= 0.75:
        regime = "stress"
    elif composite >= 0.50:
        regime = "elevated"
    elif composite >= 0.25:
        regime = "normal"
    else:
        regime = "calm"
    risk_multiplier = _clamp(1.0 - 0.32 * composite, 0.68, 1.0)
    warnings = [
        "VIX maturities, VVIX and SKEW share an option-surface lineage and count as one evidence group.",
        "The volatility overlay is downside-only and cannot independently create a position.",
    ]
    if available < 5:
        warnings.append("The volatility surface is partial; the result carries reduced interpretation coverage.")
    return VolatilitySurfaceResult(
        status="ok" if available >= 5 else "partial",
        regime=regime,
        level_score=round(level_score, 6),
        backwardation_score=round(backwardation, 6),
        vol_of_vol_score=round(vol_of_vol, 6),
        tail_skew_score=round(tail_skew, 6),
        variance_risk_premium_score=round(variance_risk_premium, 6),
        put_call_score=round(put_call, 6),
        composite_stress=round(composite, 6),
        risk_budget_multiplier=round(risk_multiplier, 6),
        ratios={key: None if value is None else round(value, 6) for key, value in ratios.items()},
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class OptionQuote:
    option_type: str
    strike: float
    implied_volatility: float
    delta: float | None
    bid: float | None
    ask: float | None
    volume: float = 0.0
    open_interest: float = 0.0

    def __post_init__(self) -> None:
        option_type = self.option_type.strip().casefold()
        if option_type not in {"call", "put"}:
            raise ValueError("option_type must be call or put")
        object.__setattr__(self, "option_type", option_type)
        if self.strike <= 0 or self.implied_volatility <= 0:
            raise ValueError("strike and implied_volatility must be positive")
        if self.delta is not None and not -1.0 <= self.delta <= 1.0:
            raise ValueError("delta must be between -1 and 1")


@dataclass(frozen=True)
class OptionChainSnapshot:
    symbol: str
    observed_at: dt.datetime
    spot: float
    days_to_expiry: float
    quotes: tuple[OptionQuote, ...]
    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    contract_multiplier: float = 100.0
    source_health: str = "healthy"


@dataclass(frozen=True)
class OptionTailRiskResult:
    status: str
    atm_iv: float | None
    put_25d_iv: float | None
    call_25d_iv: float | None
    put_10d_iv: float | None
    downside_skew: float | None
    wing_convexity: float | None
    expected_move: float | None
    put_call_volume_ratio: float | None
    put_call_open_interest_ratio: float | None
    estimated_net_gamma_notional: float | None
    tail_risk_score: float
    liquidity_score: float
    risk_budget_multiplier: float
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "warnings": list(self.warnings)}


def _nearest(quotes: Sequence[OptionQuote], target: float, selector) -> OptionQuote | None:
    candidates = [quote for quote in quotes if selector(quote)]
    if not candidates:
        return None
    return min(candidates, key=lambda quote: abs((quote.delta if quote.delta is not None else 0.0) - target))


def _atm(quotes: Sequence[OptionQuote], spot: float) -> list[OptionQuote]:
    if not quotes:
        return []
    distance = min(abs(quote.strike - spot) for quote in quotes)
    return [quote for quote in quotes if abs(abs(quote.strike - spot) - distance) <= 1e-9]


def _normal_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def _black_scholes_gamma(
    spot: float,
    strike: float,
    annual_vol: float,
    time_years: float,
    risk_free_rate: float,
    dividend_yield: float,
) -> float:
    if time_years <= 0 or annual_vol <= 0:
        return 0.0
    d1 = (
        math.log(spot / strike)
        + (risk_free_rate - dividend_yield + 0.5 * annual_vol * annual_vol) * time_years
    ) / (annual_vol * math.sqrt(time_years))
    return math.exp(-dividend_yield * time_years) * _normal_pdf(d1) / (
        spot * annual_vol * math.sqrt(time_years)
    )


def evaluate_option_tail_risk(snapshot: OptionChainSnapshot) -> OptionTailRiskResult:
    """Extract skew, convexity, expected move and an approximate gamma profile."""

    if snapshot.observed_at.tzinfo is None or snapshot.observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if snapshot.spot <= 0 or snapshot.days_to_expiry <= 0:
        raise ValueError("spot and days_to_expiry must be positive")
    if snapshot.source_health not in {"healthy", "degraded"} or not snapshot.quotes:
        return OptionTailRiskResult(
            status="blocked", atm_iv=None, put_25d_iv=None, call_25d_iv=None,
            put_10d_iv=None, downside_skew=None, wing_convexity=None,
            expected_move=None, put_call_volume_ratio=None,
            put_call_open_interest_ratio=None, estimated_net_gamma_notional=None,
            tail_risk_score=0.0, liquidity_score=0.0, risk_budget_multiplier=0.85,
            warnings=("A current liquid option chain is required; missing options do not imply low tail risk.",),
        )

    quotes = snapshot.quotes
    atm_quotes = _atm(quotes, snapshot.spot)
    atm_iv = None if not atm_quotes else sum(quote.implied_volatility for quote in atm_quotes) / len(atm_quotes)
    put25 = _nearest(quotes, -0.25, lambda quote: quote.option_type == "put" and quote.delta is not None)
    call25 = _nearest(quotes, 0.25, lambda quote: quote.option_type == "call" and quote.delta is not None)
    put10 = _nearest(quotes, -0.10, lambda quote: quote.option_type == "put" and quote.delta is not None)
    put25_iv = None if put25 is None else put25.implied_volatility
    call25_iv = None if call25 is None else call25.implied_volatility
    put10_iv = None if put10 is None else put10.implied_volatility
    downside_skew = None if put25_iv is None or call25_iv is None else put25_iv - call25_iv
    wing_convexity = None if put10_iv is None or put25_iv is None else put10_iv - put25_iv
    time_years = snapshot.days_to_expiry / 365.0
    expected_move = None if atm_iv is None else snapshot.spot * atm_iv * math.sqrt(time_years)

    call_volume = sum(max(quote.volume, 0.0) for quote in quotes if quote.option_type == "call")
    put_volume = sum(max(quote.volume, 0.0) for quote in quotes if quote.option_type == "put")
    call_oi = sum(max(quote.open_interest, 0.0) for quote in quotes if quote.option_type == "call")
    put_oi = sum(max(quote.open_interest, 0.0) for quote in quotes if quote.option_type == "put")
    put_call_volume = None if call_volume <= 0 else put_volume / call_volume
    put_call_oi = None if call_oi <= 0 else put_oi / call_oi

    spread_scores: list[float] = []
    gamma_notional = 0.0
    gamma_available = False
    for quote in quotes:
        if quote.bid is not None and quote.ask is not None and quote.ask >= quote.bid >= 0:
            midpoint = 0.5 * (quote.bid + quote.ask)
            if midpoint > 0:
                spread_scores.append(_clamp(1.0 - (quote.ask - quote.bid) / midpoint / 0.50, 0.0, 1.0))
        if quote.open_interest > 0:
            gamma = _black_scholes_gamma(
                snapshot.spot, quote.strike, quote.implied_volatility, time_years,
                snapshot.risk_free_rate, snapshot.dividend_yield,
            )
            sign = -1.0 if quote.option_type == "put" else 1.0
            gamma_notional += sign * gamma * quote.open_interest * snapshot.contract_multiplier * snapshot.spot * snapshot.spot
            gamma_available = True
    liquidity_score = 0.0 if not spread_scores else sum(spread_scores) / len(spread_scores)

    skew_score = 0.0 if downside_skew is None else _clamp((downside_skew - 0.02) / 0.25, 0.0, 1.0)
    convexity_score = 0.0 if wing_convexity is None else _clamp((wing_convexity - 0.01) / 0.20, 0.0, 1.0)
    put_call_score = 0.0
    ratios = [value for value in (put_call_volume, put_call_oi) if value is not None]
    if ratios:
        put_call_score = _clamp((sum(ratios) / len(ratios) - 0.9) / 1.2, 0.0, 1.0)
    gamma_score = 0.0
    if gamma_available:
        # Large negative signed gamma is treated as a potential amplification risk.
        scaled_gamma = -gamma_notional / max(snapshot.spot * snapshot.spot * 1_000_000.0, 1.0)
        gamma_score = _clamp(scaled_gamma, 0.0, 1.0)
    tail_score = _clamp(
        (0.34 * skew_score + 0.24 * convexity_score + 0.20 * put_call_score + 0.22 * gamma_score)
        * (0.65 + 0.35 * liquidity_score),
        0.0,
        1.0,
    )
    multiplier = _clamp(1.0 - 0.22 * tail_score, 0.78, 1.0)
    warnings = [
        "Option-implied tail metrics depend on strike coverage, quote quality and dealer-position assumptions.",
        "Signed gamma is an approximate open-interest proxy, not an observed dealer inventory.",
        "The option-chain result is one correlated options evidence group and cannot independently trade.",
    ]
    if liquidity_score < 0.40:
        warnings.append("Option quotes are relatively wide; tail metrics receive a liquidity haircut.")
    return OptionTailRiskResult(
        status="ok" if liquidity_score >= 0.40 else "partial",
        atm_iv=None if atm_iv is None else round(atm_iv, 8),
        put_25d_iv=None if put25_iv is None else round(put25_iv, 8),
        call_25d_iv=None if call25_iv is None else round(call25_iv, 8),
        put_10d_iv=None if put10_iv is None else round(put10_iv, 8),
        downside_skew=None if downside_skew is None else round(downside_skew, 8),
        wing_convexity=None if wing_convexity is None else round(wing_convexity, 8),
        expected_move=None if expected_move is None else round(expected_move, 8),
        put_call_volume_ratio=None if put_call_volume is None else round(put_call_volume, 8),
        put_call_open_interest_ratio=None if put_call_oi is None else round(put_call_oi, 8),
        estimated_net_gamma_notional=None if not gamma_available else round(gamma_notional, 4),
        tail_risk_score=round(tail_score, 6),
        liquidity_score=round(liquidity_score, 6),
        risk_budget_multiplier=round(multiplier, 6),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class OvernightSnapshot:
    symbol: str
    observed_at: dt.datetime
    previous_close: float
    premarket_price: float
    overnight_high: float | None = None
    overnight_low: float | None = None
    historical_mean: float = 0.0
    historical_std: float | None = None
    premarket_volume_ratio: float | None = None
    es_return: float | None = None
    nq_return: float | None = None
    rty_return: float | None = None
    vix_change: float | None = None
    credit_confirmation: float | None = None
    source_health: str = "healthy"


@dataclass(frozen=True)
class OvernightRiskResult:
    status: str
    symbol: str
    overnight_return: float | None
    overnight_z_score: float | None
    range_extension: float | None
    futures_confirmation: float | None
    liquidity_quality: float
    classification: str
    risk_score: float
    risk_budget_multiplier: float
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "warnings": list(self.warnings)}


def evaluate_overnight_risk(snapshot: OvernightSnapshot) -> OvernightRiskResult:
    """Classify a premarket gap relative to its own history and cross-asset context."""

    if snapshot.observed_at.tzinfo is None or snapshot.observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if snapshot.previous_close <= 0 or snapshot.premarket_price <= 0:
        raise ValueError("previous_close and premarket_price must be positive")
    if snapshot.source_health not in {"healthy", "degraded"}:
        return OvernightRiskResult(
            status="blocked", symbol=snapshot.symbol.upper(), overnight_return=None,
            overnight_z_score=None, range_extension=None, futures_confirmation=None,
            liquidity_quality=0.0, classification="data_unavailable", risk_score=0.0,
            risk_budget_multiplier=0.85,
            warnings=("Premarket data is unavailable; the model does not infer a normal open.",),
        )
    overnight_return = snapshot.premarket_price / snapshot.previous_close - 1.0
    z_score = None
    if snapshot.historical_std is not None and snapshot.historical_std > 1e-8:
        z_score = (overnight_return - snapshot.historical_mean) / snapshot.historical_std
    range_extension = None
    if snapshot.overnight_high is not None and snapshot.overnight_low is not None:
        range_extension = (snapshot.overnight_high - snapshot.overnight_low) / snapshot.previous_close

    futures = [value for value in (snapshot.es_return, snapshot.nq_return, snapshot.rty_return) if value is not None]
    futures_confirmation = None if not futures else sum(futures) / len(futures)
    volume_ratio = snapshot.premarket_volume_ratio
    liquidity_quality = 0.50 if volume_ratio is None else _clamp(volume_ratio / 1.5, 0.0, 1.0)
    z_magnitude = 0.0 if z_score is None else _clamp((abs(z_score) - 1.0) / 3.0, 0.0, 1.0)
    sign = 1.0 if overnight_return >= 0 else -1.0
    confirmation = 0.0
    if futures_confirmation is not None:
        confirmation += 0.45 if futures_confirmation * sign > 0 else -0.20
    if snapshot.vix_change is not None:
        # VIX should normally confirm downside, not upside.
        confirmation += 0.35 if (sign < 0 and snapshot.vix_change > 0) else 0.0
    if snapshot.credit_confirmation is not None:
        confirmation += 0.20 if snapshot.credit_confirmation * sign > 0 else 0.0
    confirmation = _clamp(confirmation, -0.25, 1.0)

    if z_score is None:
        classification = "unscaled_gap"
    elif abs(z_score) < 1.5:
        classification = "normal_range"
    elif liquidity_quality < 0.25 and abs(z_score) >= 2.0:
        classification = "thin_liquidity_stretch"
    elif confirmation >= 0.45 and sign < 0:
        classification = "confirmed_gap_down"
    elif confirmation >= 0.45 and sign > 0:
        classification = "confirmed_gap_up"
    else:
        classification = "unconfirmed_anomaly"

    downside = 1.0 if sign < 0 else 0.25
    risk_score = _clamp(z_magnitude * (0.55 + 0.45 * max(confirmation, 0.0)) * downside, 0.0, 1.0)
    multiplier = _clamp(1.0 - 0.18 * risk_score, 0.82, 1.0)
    warnings = [
        "Premarket prices are less liquid than regular-session closes and require volume/futures confirmation.",
        "An overnight anomaly changes opening caution, not the strategic portfolio by itself.",
        "This signal belongs inside the single daily report; it does not create a second user-facing premarket report.",
    ]
    return OvernightRiskResult(
        status="ok" if z_score is not None else "partial",
        symbol=snapshot.symbol.upper(),
        overnight_return=round(overnight_return, 8),
        overnight_z_score=None if z_score is None else round(z_score, 6),
        range_extension=None if range_extension is None else round(range_extension, 8),
        futures_confirmation=None if futures_confirmation is None else round(futures_confirmation, 8),
        liquidity_quality=round(liquidity_quality, 6),
        classification=classification,
        risk_score=round(risk_score, 6),
        risk_budget_multiplier=round(multiplier, 6),
        warnings=tuple(warnings),
    )


__all__ = [
    "OptionChainSnapshot",
    "OptionQuote",
    "OptionTailRiskResult",
    "OvernightRiskResult",
    "OvernightSnapshot",
    "VolatilitySurfaceResult",
    "VolatilitySurfaceSnapshot",
    "evaluate_option_tail_risk",
    "evaluate_overnight_risk",
    "evaluate_volatility_surface",
]
