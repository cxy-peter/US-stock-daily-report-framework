"""Auditable objective-market overlays used to tighten, never loosen, risk.

The daily agent already classifies the SPY trend.  This module adds independent
confirmation groups (volatility, credit and breadth) without turning a single
index into a trading signal.  China-related prices are recorded as context for
the ``china_retail_attention`` research overlay, but do not alter sizing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .data import Quote
from .regime import MarketRegime


DEFAULT_SYMBOLS = {
    "vix": "^VIX",
    "vix3m": "^VIX3M",
    "spy": "SPY",
    "rsp": "RSP",
    "iwm": "IWM",
    "hyg": "HYG",
    "lqd": "LQD",
    "hxc": "^HXC",
    "kweb": "KWEB",
    "cnh": "CNH=X",
}


@dataclass(frozen=True)
class ObjectiveSignalSettings:
    enabled: bool = True
    provider: str = "hybrid"
    providers: dict[str, str] = field(
        default_factory=lambda: {"vix": "cboe", "vix3m": "cboe"}
    )
    period: str = "1y"
    symbols: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SYMBOLS))
    group_weights: dict[str, float] = field(
        default_factory=lambda: {
            "volatility": 0.45,
            "credit": 0.30,
            "breadth": 0.25,
        }
    )
    min_healthy_groups: int = 2
    min_confirming_groups: int = 2
    max_staleness_days: int = 7
    confirmation_threshold: float = 0.50
    max_risk_budget_reduction: float = 0.30
    allow_downside_risk_tightening: bool = True

    @classmethod
    def from_dict(cls, data: dict | None) -> "ObjectiveSignalSettings":
        data = data or {}
        symbols = dict(DEFAULT_SYMBOLS)
        symbols.update(
            {
                str(key): str(value)
                for key, value in (data.get("symbols") or {}).items()
                if str(value).strip()
            }
        )
        weights = {
            str(key): max(0.0, float(value))
            for key, value in (
                data.get("group_weights")
                or {"volatility": 0.45, "credit": 0.30, "breadth": 0.25}
            ).items()
        }
        providers = {"vix": "cboe", "vix3m": "cboe"}
        providers.update(
            {
                str(key): str(value)
                for key, value in (data.get("providers") or {}).items()
                if str(value).strip()
            }
        )
        return cls(
            enabled=bool(data.get("enabled", True)),
            provider=str(data.get("provider", "hybrid")),
            providers=providers,
            period=str(data.get("period", "1y")),
            symbols=symbols,
            group_weights=weights,
            min_healthy_groups=max(1, int(data.get("min_healthy_groups", 2))),
            min_confirming_groups=max(1, int(data.get("min_confirming_groups", 2))),
            max_staleness_days=max(0, int(data.get("max_staleness_days", 7))),
            confirmation_threshold=max(
                0.0, min(1.0, float(data.get("confirmation_threshold", 0.50)))
            ),
            max_risk_budget_reduction=max(
                0.0, min(0.50, float(data.get("max_risk_budget_reduction", 0.30)))
            ),
            allow_downside_risk_tightening=bool(
                data.get("allow_downside_risk_tightening", True)
            ),
        )


@dataclass(frozen=True)
class ObjectiveComponent:
    name: str
    group: str
    status: str
    value: float | None
    stress_score: float | None
    as_of: str
    source: str
    detail: str


@dataclass(frozen=True)
class ChinaCrossAssetContext:
    status: str
    hxc_return_1m: float | None = None
    china_equity_proxy: str = ""
    china_equity_return_1m: float | None = None
    usd_cnh_return_1m: float | None = None
    detail: str = ""
    can_trigger_trade: bool = False


@dataclass(frozen=True)
class ObjectiveMarketSnapshot:
    status: str
    stress_score: float | None
    risk_budget_multiplier: float
    healthy_groups: int
    confirming_groups: int
    group_scores: dict[str, float]
    group_weights: dict[str, float]
    components: tuple[ObjectiveComponent, ...]
    china_context: ChinaCrossAssetContext
    can_tighten_risk: bool
    can_increase_risk: bool = False
    provisional: bool = True
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _return(quote: Quote | None, periods: int = 21) -> float | None:
    if quote is None:
        return None
    closes = quote.closes.dropna().astype(float)
    if len(closes) <= periods or float(closes.iloc[-1 - periods]) <= 0:
        return None
    return float(closes.iloc[-1] / closes.iloc[-1 - periods] - 1.0)


def _component(
    name: str,
    group: str,
    value: float | None,
    score: float | None,
    quote: Quote | None,
    detail: str,
) -> ObjectiveComponent:
    return ObjectiveComponent(
        name=name,
        group=group,
        status="ok" if value is not None and score is not None else "unavailable",
        value=None if value is None else round(float(value), 6),
        stress_score=None if score is None else round(_clip(float(score)), 4),
        as_of=quote.as_of if quote is not None else "",
        source=quote.source if quote is not None else "",
        detail=detail,
    )


def _volatility_components(quotes: dict[str, Quote]) -> tuple[list[ObjectiveComponent], float | None]:
    vix, vix3m = quotes.get("vix"), quotes.get("vix3m")
    level = float(vix.price) if vix is not None and vix.price > 0 else None
    level_score = None if level is None else _clip((level - 15.0) / 20.0)
    ratio = (
        float(vix.price / vix3m.price)
        if vix is not None and vix3m is not None and vix.price > 0 and vix3m.price > 0
        else None
    )
    term_score = None if ratio is None else _clip((ratio - 0.90) / 0.20)
    components = [
        _component(
            "VIX level",
            "volatility",
            level,
            level_score,
            vix,
            "15 or below is calm; 35 or above maps to maximum stress.",
        ),
        _component(
            "VIX/VIX3M term ratio",
            "volatility",
            ratio,
            term_score,
            vix,
            "Backwardation raises stress; this ratio is not a standalone trade signal.",
        ),
    ]
    scores = [item.stress_score for item in components if item.stress_score is not None]
    return components, (sum(scores) / len(scores) if scores else None)


def _relative_group(
    quotes: dict[str, Quote],
    left_key: str,
    right_key: str,
    group: str,
    name: str,
    full_stress_gap: float,
) -> tuple[ObjectiveComponent, float | None]:
    left, right = quotes.get(left_key), quotes.get(right_key)
    left_return, right_return = _return(left), _return(right)
    gap = (
        left_return - right_return
        if left_return is not None and right_return is not None
        else None
    )
    score = None if gap is None else _clip(gap / full_stress_gap)
    component = _component(
        name,
        group,
        gap,
        score,
        left,
        f"21-day relative return; {full_stress_gap:.1%} or worse maps to maximum stress.",
    )
    return component, component.stress_score


def _china_context(quotes: dict[str, Quote]) -> ChinaCrossAssetContext:
    hxc_return = _return(quotes.get("hxc"))
    kweb_return = _return(quotes.get("kweb"))
    equity_return = hxc_return if hxc_return is not None else kweb_return
    equity_proxy = "HXC" if hxc_return is not None else ("KWEB ETF proxy" if kweb_return is not None else "")
    cnh_return = _return(quotes.get("cnh"))
    available = sum(value is not None for value in (equity_return, cnh_return))
    if available == 0:
        return ChinaCrossAssetContext(
            status="unavailable",
            detail="HXC/KWEB and USD/CNH are unavailable; XHS themes have no cross-asset confirmation.",
        )
    return ChinaCrossAssetContext(
        status="ok" if available == 2 else "partial",
        hxc_return_1m=None if hxc_return is None else round(hxc_return, 6),
        china_equity_proxy=equity_proxy,
        china_equity_return_1m=(
            None if equity_return is None else round(equity_return, 6)
        ),
        usd_cnh_return_1m=None if cnh_return is None else round(cnh_return, 6),
        detail=(
            "HXC is preferred; KWEB is an explicit tradable proxy when HXC is unavailable. "
            "Context only for China/ADR themes; it cannot create or reverse a portfolio action."
        ),
    )


def build_objective_market_snapshot(
    quotes: dict[str, Quote],
    settings: ObjectiveSignalSettings,
) -> ObjectiveMarketSnapshot:
    if not settings.enabled:
        return ObjectiveMarketSnapshot(
            status="disabled",
            stress_score=None,
            risk_budget_multiplier=1.0,
            healthy_groups=0,
            confirming_groups=0,
            group_scores={},
            group_weights=dict(settings.group_weights),
            components=(),
            china_context=ChinaCrossAssetContext("disabled", detail="Disabled by config."),
            can_tighten_risk=False,
            detail="Objective-market overlay disabled by config.",
        )

    mock_input_keys = tuple(
        sorted(
            key
            for key, quote in quotes.items()
            if str(quote.source or "").strip().casefold() == "mock"
        )
    )
    # Mock legs are removed before any pair, group or weighted score is built.
    # This prevents a live left leg from hiding a mocked right leg in a spread.
    decision_quotes = {
        key: quote for key, quote in quotes.items() if key not in mock_input_keys
    }

    volatility_components, volatility_score = _volatility_components(decision_quotes)
    credit_component, credit_score = _relative_group(
        decision_quotes, "hyg", "lqd", "credit", "HYG minus LQD", -0.03
    )
    rsp_component, rsp_score = _relative_group(
        decision_quotes, "rsp", "spy", "breadth", "RSP minus SPY", -0.04
    )
    iwm_component, iwm_score = _relative_group(
        decision_quotes, "iwm", "spy", "breadth", "IWM minus SPY", -0.05
    )
    breadth_scores = [score for score in (rsp_score, iwm_score) if score is not None]
    breadth_score = sum(breadth_scores) / len(breadth_scores) if breadth_scores else None
    group_scores_raw = {
        "volatility": volatility_score,
        "credit": credit_score,
        "breadth": breadth_score,
    }
    group_scores = {
        key: round(float(value), 4)
        for key, value in group_scores_raw.items()
        if value is not None
    }
    healthy_groups = len(group_scores)
    confirming_groups = sum(
        value >= settings.confirmation_threshold for value in group_scores.values()
    )
    available_weights = {
        group: max(0.0, settings.group_weights.get(group, 0.0))
        for group in group_scores
    }
    weight_sum = sum(available_weights.values())
    stress_score = (
        sum(group_scores[group] * weight for group, weight in available_weights.items())
        / weight_sum
        if weight_sum > 0
        else None
    )
    components = tuple(
        volatility_components + [credit_component, rsp_component, iwm_component]
    )
    enough_data = healthy_groups >= settings.min_healthy_groups
    confirmed = confirming_groups >= settings.min_confirming_groups
    can_tighten = bool(
        settings.allow_downside_risk_tightening
        and enough_data
        and confirmed
        and stress_score is not None
    )
    reduction = (
        settings.max_risk_budget_reduction * float(stress_score)
        if can_tighten and stress_score is not None
        else 0.0
    )
    multiplier = 1.0 - reduction
    status = "ok" if healthy_groups == 3 else ("partial" if healthy_groups else "blocked")
    if mock_input_keys:
        status = "mixed_mock" if healthy_groups else "mock"
    detail_parts = [
        f"{healthy_groups}/3 objective groups healthy",
        f"{confirming_groups} confirm stress",
        "downside-only overlay",
    ]
    if mock_input_keys:
        detail_parts.append(
            "mock inputs excluded before scoring: " + ", ".join(mock_input_keys)
        )
    if not enough_data:
        detail_parts.append("insufficient independent groups")
    elif not confirmed:
        detail_parts.append("stress not independently confirmed")
    return ObjectiveMarketSnapshot(
        status=status,
        stress_score=None if stress_score is None else round(float(stress_score), 4),
        risk_budget_multiplier=round(_clip(multiplier, 0.5, 1.0), 4),
        healthy_groups=healthy_groups,
        confirming_groups=confirming_groups,
        group_scores=group_scores,
        group_weights=dict(settings.group_weights),
        components=components,
        china_context=_china_context(decision_quotes),
        can_tighten_risk=can_tighten,
        detail="; ".join(detail_parts),
    )


def apply_objective_overlay(
    regime: MarketRegime,
    snapshot: ObjectiveMarketSnapshot,
) -> MarketRegime:
    """Tighten a base regime when independent objective groups confirm stress.

    The overlay deliberately cannot raise a risk budget or turn risk-off into
    risk-on.  Social or China-context readings are not inputs to this function.
    """
    if not snapshot.can_tighten_risk:
        return regime
    multiplier = min(regime.risk_multiplier, snapshot.risk_budget_multiplier)
    label = regime.label
    if snapshot.stress_score is not None and snapshot.stress_score >= 0.65:
        label = "risk_off"
    elif label == "risk_on" and snapshot.stress_score is not None and snapshot.stress_score >= 0.45:
        label = "neutral"
    reasons = regime.reasons + (
        "Objective stress overlay: "
        f"score={snapshot.stress_score:.2f}, groups={snapshot.confirming_groups}, "
        f"risk budget capped at {multiplier:.0%}.",
    )
    return MarketRegime(label, multiplier, regime.score, reasons)
