"""Production bridge for one buy-side daily research report.

This module wires the already-tested political, Trump-policy, Polymarket,
volatility, Barra/Kalman and manager-skill libraries into the daily research
path.  It also ingests private social/news-agent summaries through a
provenance-gated inbox.  Missing inputs remain visible; no component can place
an order or silently mutate a portfolio ledger.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
import requests

from .advanced_market_risk import (
    OptionChainSnapshot,
    OptionQuote,
    OptionTailRiskResult,
    OvernightRiskResult,
    OvernightSnapshot,
    VolatilitySurfaceResult,
    VolatilitySurfaceSnapshot,
    evaluate_option_tail_risk,
    evaluate_overnight_risk,
    evaluate_volatility_surface,
)
from .daily_research_enrichment import (
    DailyResearchEnrichment,
    build_daily_research_enrichment,
    fetch_research_price_history,
)
from .external_views import ExternalSettings, collect_external_views
from .political_collectors import WhiteHouseCollector
from .political_communications import (
    ActorProfile,
    PoliticalBriefResult,
    build_political_brief,
)
from .polymarket_live import (
    LivePolymarketResult,
    PolymarketDataError,
    PolymarketPublicClient,
    aggregate_live_markets,
    score_live_market,
)
from .pro_research.barra import BarraProxyResult, fit_barra_proxy
from .pro_research.kalman import KalmanExposureResult, kalman_dynamic_exposures
from .pro_research.manager_skill import (
    ManagerFragility,
    ManagerSkillResult,
    evaluate_manager_skill,
)
from .pro_research.policy import TrumpPolicyIndexResult, compute_trump_policy_index
from .pro_research.polymarket import PolymarketStudyResult, study_resolved_markets
from .research_opinion_inbox import (
    OpinionInboxResult,
    assess_opinion_inbox,
    parse_opinion_records,
)


_INPUT_ENV_KEYS = (
    "DAILY_RESEARCH_INPUTS_JSON_B64",
    "NEWS_AGENT_DIGEST_JSON_B64",
    "XHS_VIEWS_JSON_B64",
    "BROKER_RESEARCH_DIGEST_JSON_B64",
)
_INPUT_PATH_KEYS = (
    "DAILY_RESEARCH_INPUTS_PATH",
    "NEWS_AGENT_DIGEST_PATH",
    "XHS_VIEWS_PATH",
    "BROKER_RESEARCH_DIGEST_PATH",
)
_ASSET_PROXY = {"QQQM": "QQQ", "VOO": "SPY"}
_POLICY_TOPIC_MAP = {
    "trade_tariff": "trade_tariff",
    "ai_semiconductor": "ai_semiconductor",
    "energy_power": "energy",
    "defense_geopolitics": "defense_geopolitics",
    "immigration_labor": "immigration_labor",
    "healthcare": "healthcare",
    "fiscal_tax": "fiscal_tax",
    "financial_regulation": "financial_regulation",
    "monetary_rates": "fed_rates",
}
_POLICY_SOURCE_MAP = {
    "signed_official_action": "signed_official_action",
    "implemented_official_action": "implemented_official_action",
    "official_order": "official_action",
    "official_fact_sheet": "official_action",
    "official_speech": "official_statement",
    "official_press_briefing": "official_statement",
    "official_interview": "direct_quote_primary",
    "official_x": "direct_quote_primary",
    "official_social": "direct_quote_primary",
    "agency_statement": "official_statement",
    "company_official_statement": "official_statement",
    "media_direct_quote": "direct_quote_media",
    "media_analysis": "media_analysis",
    "commentary": "social_summary",
}
_POLICY_STAGE_MAP = {
    "implemented": "implemented",
    "signed": "signed",
    "directed": "formal_proposal",
    "formal_proposal": "formal_proposal",
    "negotiating": "official_statement",
    "announced_intent": "official_statement",
    "conditional_view": "campaign_or_interview",
    "general_view": "campaign_or_interview",
    "media_interpretation": "media_interpretation",
}
_POLICY_ASSET_SENSITIVITY = {
    "trade_tariff": {
        "MU": -0.70, "SMH": -0.65, "QQQM": -0.38, "VOO": -0.25,
        "XLE": -0.10, "GLDM": 0.12,
    },
    "ai_semiconductor": {
        "MU": 0.75, "SMH": 0.70, "QQQM": 0.35, "VOO": 0.12,
    },
    "energy": {"XLE": 0.75, "VOO": -0.08, "QQQM": -0.12, "GLDM": 0.08},
    "defense_geopolitics": {
        "VOO": -0.25, "QQQM": -0.22, "SMH": -0.18, "MU": -0.18,
        "XLE": 0.35, "GLDM": 0.30,
    },
    "fed_rates": {
        "QQQM": -0.55, "SMH": -0.40, "MU": -0.35, "VOO": -0.20,
        "SCHD": -0.05, "BOXX": 0.20,
    },
    "financial_regulation": {
        "JPM": -0.35, "GS": -0.35, "MS": -0.30, "SCHW": -0.30,
        "IBKR": -0.25, "VOO": -0.05,
    },
    "fiscal_tax": {"VOO": 0.15, "QQQM": 0.12, "SCHD": 0.08},
    "healthcare": {"VOO": -0.05},
    "immigration_labor": {"VOO": -0.08, "QQQM": -0.06},
}
_POLY_TOPICS = {
    "trade_tariff": (
        ("tariff", "trade war", "trade deal", "china trade"),
        _POLICY_ASSET_SENSITIVITY["trade_tariff"],
    ),
    "fed_rates": (
        ("fed", "interest rate", "rate cut", "rate hike", "inflation"),
        _POLICY_ASSET_SENSITIVITY["fed_rates"],
    ),
    "energy_geopolitics": (
        ("iran", "israel", "oil", "hormuz", "red sea", "opec"),
        _POLICY_ASSET_SENSITIVITY["defense_geopolitics"],
    ),
    "ai_semiconductor": (
        ("semiconductor", "chip", "artificial intelligence", "ai regulation"),
        _POLICY_ASSET_SENSITIVITY["ai_semiconductor"],
    ),
    "digital_assets": (
        ("bitcoin", "crypto", "stablecoin", "digital asset"),
        {"COIN": 0.70, "CRCL": 0.70, "VOO": 0.02},
    ),
}
_INSTITUTION_TARGETS = (
    {"ticker": "BLK", "name": "BlackRock", "social_aliases": ["iShares", "BlackRock"]},
    {"ticker": "IVZ", "name": "Invesco", "social_aliases": ["Invesco", "QQQ", "QQQM"]},
    {"ticker": "STT", "name": "State Street", "social_aliases": ["SPDR", "State Street"]},
    {"ticker": "SCHW", "name": "Charles Schwab", "social_aliases": ["Schwab", "SCHD"]},
    {"ticker": "JPM", "name": "JPMorgan", "social_aliases": ["JPMorgan Asset Management"]},
    {"ticker": "GS", "name": "Goldman Sachs", "social_aliases": ["Goldman Sachs Asset Management"]},
    {"ticker": "MS", "name": "Morgan Stanley", "social_aliases": ["Morgan Stanley Investment Management"]},
    {"ticker": "IBKR", "name": "Interactive Brokers", "social_aliases": ["Interactive Brokers"]},
    {"ticker": "CME", "name": "CME Group", "social_aliases": ["CME Group"]},
    {"ticker": "ICE", "name": "Intercontinental Exchange", "social_aliases": ["NYSE", "ICE"]},
    {"ticker": "NDAQ", "name": "Nasdaq", "social_aliases": ["Nasdaq"]},
)
_INSTITUTION_QUERIES = (
    "(site:blackrock.com OR site:vanguard.com OR site:ssga.com OR site:invesco.com) "
    "fund flow fee launch closure manager outlook",
    "(site:schwabassetmanagement.com OR site:vaneck.com OR site:pimco.com OR "
    "site:capitalgroup.com) ETF fund manager market outlook",
    "(site:jpmorganchase.com OR site:goldmansachs.com OR site:morganstanley.com) "
    "asset management markets outlook earnings",
    "(site:cmegroup.com OR site:ice.com OR site:nasdaq.com OR "
    "site:interactivebrokers.com) volume market structure margin liquidity",
)


def _clamp(value: float, lower: float, upper: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        return lower
    return min(max(number, lower), upper)


def _decode_json_b64(value: str) -> Any:
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    return json.loads(raw.decode("utf-8"))


def _load_json_path(value: str) -> Any:
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError("research input path is not a file")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _merge_input_payload(target: dict[str, Any], payload: Any, default_key: str) -> None:
    if payload in (None, ""):
        return
    if isinstance(payload, list):
        target.setdefault(default_key, []).extend(
            row for row in payload if isinstance(row, Mapping)
        )
        return
    if not isinstance(payload, Mapping):
        raise ValueError("research input must be an object or list")
    for key, value in payload.items():
        if isinstance(value, list):
            target.setdefault(str(key), []).extend(value)
        elif isinstance(value, Mapping):
            existing = target.setdefault(str(key), {})
            if isinstance(existing, dict):
                existing.update(value)
            else:
                target[str(key)] = dict(value)
        else:
            target[str(key)] = value


def load_private_research_inputs(
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...]]:
    """Load compact private inputs without committing raw social/research files."""

    env = dict(os.environ if environ is None else environ)
    merged: dict[str, Any] = {}
    health: list[Mapping[str, Any]] = []
    b64_defaults = {
        "DAILY_RESEARCH_INPUTS_JSON_B64": "opinion_records",
        "NEWS_AGENT_DIGEST_JSON_B64": "agent_digests",
        "XHS_VIEWS_JSON_B64": "xiaohongshu_views",
        "BROKER_RESEARCH_DIGEST_JSON_B64": "broker_research_digests",
    }
    path_defaults = {
        "DAILY_RESEARCH_INPUTS_PATH": "opinion_records",
        "NEWS_AGENT_DIGEST_PATH": "agent_digests",
        "XHS_VIEWS_PATH": "xiaohongshu_views",
        "BROKER_RESEARCH_DIGEST_PATH": "broker_research_digests",
    }
    for key in _INPUT_ENV_KEYS:
        value = str(env.get(key) or "").strip()
        if not value:
            health.append({"source": key, "status": "not_configured", "detail": "optional"})
            continue
        try:
            _merge_input_payload(merged, _decode_json_b64(value), b64_defaults[key])
            health.append({"source": key, "status": "healthy", "detail": "decoded privately"})
        except (ValueError, TypeError, json.JSONDecodeError):
            health.append({"source": key, "status": "error", "detail": "invalid private JSON"})
    for key in _INPUT_PATH_KEYS:
        value = str(env.get(key) or "").strip()
        if not value:
            continue
        try:
            _merge_input_payload(merged, _load_json_path(value), path_defaults[key])
            health.append({"source": key, "status": "healthy", "detail": "local private path"})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            health.append({"source": key, "status": "error", "detail": "unreadable private JSON"})
    return merged, tuple(health)


@dataclass(frozen=True)
class ResearchThesis:
    thesis_id: str
    title: str
    stance: str
    change: str
    consensus_and_variant: str
    evidence_chain: tuple[str, ...]
    catalysts: tuple[str, ...]
    horizon: str
    invalidation: tuple[str, ...]
    confidence: float
    affected_assets: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "evidence_chain": list(self.evidence_chain),
            "catalysts": list(self.catalysts),
            "invalidation": list(self.invalidation),
            "affected_assets": list(self.affected_assets),
        }


@dataclass(frozen=True)
class AdvancedDailyResearch:
    status: str
    generated_at: str
    base: DailyResearchEnrichment
    political_brief: PoliticalBriefResult
    trump_policy: TrumpPolicyIndexResult
    live_polymarket: LivePolymarketResult
    resolved_polymarket: PolymarketStudyResult
    volatility_surface: VolatilitySurfaceResult
    option_tail: OptionTailRiskResult | None
    overnight_risk: tuple[OvernightRiskResult, ...]
    barra: BarraProxyResult | None
    kalman: KalmanExposureResult | None
    manager_skill: Mapping[str, ManagerSkillResult]
    opinion_inbox: OpinionInboxResult
    institutional_news: tuple[Mapping[str, Any], ...]
    theses: tuple[ResearchThesis, ...]
    effective_risk_budget: float
    source_health: tuple[Mapping[str, Any], ...]
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at,
            "base": self.base.to_dict(),
            "political_brief": self.political_brief.to_dict(),
            "trump_policy": self.trump_policy.to_dict(),
            "live_polymarket": self.live_polymarket.to_dict(),
            "resolved_polymarket": self.resolved_polymarket.to_dict(),
            "volatility_surface": self.volatility_surface.to_dict(),
            "option_tail": None if self.option_tail is None else self.option_tail.to_dict(),
            "overnight_risk": [row.to_dict() for row in self.overnight_risk],
            "barra": None if self.barra is None else self.barra.to_dict(),
            "kalman": None if self.kalman is None else self.kalman.to_dict(),
            "manager_skill": {
                key: value.to_dict() for key, value in self.manager_skill.items()
            },
            "opinion_inbox": self.opinion_inbox.to_dict(),
            "institutional_news": [dict(row) for row in self.institutional_news],
            "theses": [row.to_dict() for row in self.theses],
            "effective_risk_budget": self.effective_risk_budget,
            "source_health": [dict(row) for row in self.source_health],
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def _actors() -> tuple[ActorProfile, ...]:
    return (
        ActorProfile(
            actor_id="white_house",
            display_name="White House / President",
            role="President of the United States",
            institution="U.S. Executive Branch",
            actor_type="executive",
            base_weight=1.0,
            policy_authority=1.0,
            monitored_topics=(),
            holdings_tags=(
                "trade_tariff", "ai_semiconductor", "energy_power",
                "monetary_rates", "financial_regulation", "digital_assets",
                "defense_geopolitics", "fiscal_tax",
            ),
        ),
    )


def _claim_policy_events(brief: PoliticalBriefResult) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for claim in brief.top_claims:
        topic = _POLICY_TOPIC_MAP.get(claim.topic)
        if not topic:
            continue
        events.append(
            {
                "event_id": claim.claim_id,
                "observed_at": claim.observed_at,
                "actor": "white_house",
                "source_tier": _POLICY_SOURCE_MAP.get(
                    claim.source_type, "media_analysis"
                ),
                "stage": _POLICY_STAGE_MAP.get(
                    claim.stage, "media_interpretation"
                ),
                "policy_topic": topic,
                "direction": claim.direction,
                "magnitude": claim.importance,
                "confidence": claim.confidence,
                "horizon_days": {
                    "intraday": 1,
                    "short": 5,
                    "medium": 60,
                    "long": 365,
                    "structural": 1095,
                }.get(claim.horizon, 60),
                "asset_impacts": _POLICY_ASSET_SENSITIVITY.get(topic, {}),
                "title": claim.compact_summary,
                "invalidation": "A later official action or implementation fact reverses the claim.",
            }
        )
    return events


def _build_policy(
    *,
    inputs: Mapping[str, Any],
    as_of: dt.datetime,
    network_enabled: bool,
    session: requests.Session | None,
    source_health: list[Mapping[str, Any]],
) -> tuple[PoliticalBriefResult, TrumpPolicyIndexResult]:
    documents: list[Any] = list(inputs.get("political_documents") or ())
    if network_enabled:
        try:
            collected = WhiteHouseCollector(session=session).collect(
                actor_id="white_house",
                since=as_of - dt.timedelta(days=10),
                per_listing_limit=6,
            )
            documents.extend(collected.documents)
            source_health.extend(
                {
                    "source": row.source_id,
                    "status": row.status,
                    "detail": f"{row.detail}; items={row.item_count}",
                }
                for row in collected.source_health
            )
        except (OSError, ValueError, requests.RequestException):
            source_health.append(
                {
                    "source": "white_house_public",
                    "status": "error",
                    "detail": "official collection failed",
                }
            )
    else:
        source_health.append(
            {
                "source": "white_house_public",
                "status": "disabled",
                "detail": "network disabled",
            }
        )
    brief = build_political_brief(
        _actors(),
        documents,
        previous_claims=tuple(inputs.get("previous_policy_claims") or ()),
        portfolio_tags=tuple(inputs.get("portfolio_tags") or ()),
        as_of=as_of,
    )
    raw_events = list(inputs.get("policy_events") or ())
    raw_events.extend(_claim_policy_events(brief))
    tpti = compute_trump_policy_index(raw_events, as_of=as_of)
    source_health.extend(
        [
            {
                "source": "political_claim_brief",
                "status": brief.status,
                "detail": f"claims={brief.accepted_claim_count}",
            },
            {
                "source": "trump_policy_transmission",
                "status": tpti.status,
                "detail": f"events={tpti.accepted_count}",
            },
        ]
    )
    return brief, tpti


def _topic_for_question(question: str) -> tuple[str, Mapping[str, float]] | None:
    lowered = question.casefold()
    best: tuple[int, str, Mapping[str, float]] | None = None
    for topic, (keywords, sensitivities) in _POLY_TOPICS.items():
        score = sum(1 for keyword in keywords if keyword in lowered)
        if score and (best is None or score > best[0]):
            best = (score, topic, sensitivities)
    return None if best is None else (best[1], best[2])


def _build_live_polymarket(
    *,
    inputs: Mapping[str, Any],
    network_enabled: bool,
    session: requests.Session | None,
    source_health: list[Mapping[str, Any]],
) -> LivePolymarketResult:
    if not network_enabled:
        source_health.append(
            {"source": "live_polymarket", "status": "disabled", "detail": "network disabled"}
        )
        return aggregate_live_markets(())
    client = PolymarketPublicClient(session=session)
    signals = []
    try:
        watchlist = list(inputs.get("polymarket_watchlist") or ())
        candidates: list[tuple[Mapping[str, Any], str, Mapping[str, float]]] = []
        if watchlist:
            for row in watchlist[:8]:
                if not isinstance(row, Mapping) or not row.get("slug"):
                    continue
                market = client.market_by_slug(str(row["slug"]))
                topic = str(row.get("topic") or "other").casefold()
                sensitivities = {
                    str(key).upper(): float(value)
                    for key, value in dict(row.get("asset_sensitivities") or {}).items()
                }
                candidates.append((market, topic, sensitivities))
        else:
            markets = client.list_markets(limit=150, active=True, closed=False)
            ranked = []
            for market in markets:
                matched = _topic_for_question(str(market.get("question") or ""))
                if matched is None:
                    continue
                topic, sensitivities = matched
                liquidity = float(market.get("liquidity") or 0.0)
                volume = float(market.get("volume") or 0.0)
                ranked.append((max(liquidity, volume * 0.10), market, topic, sensitivities))
            ranked.sort(key=lambda row: row[0], reverse=True)
            candidates = [
                (market, topic, sensitivities)
                for _rank, market, topic, sensitivities in ranked[:4]
            ]
        calibration = _clamp(
            float(inputs.get("polymarket_calibration_multiplier") or 0.35),
            0.0,
            1.0,
        )
        for market, topic, sensitivities in candidates:
            try:
                snapshot = client.snapshot(
                    market,
                    topic=topic,
                    asset_sensitivities=sensitivities,
                    history_days=14,
                )
                signals.append(
                    score_live_market(
                        snapshot,
                        calibration_multiplier=calibration,
                    )
                )
            except (PolymarketDataError, ValueError, requests.RequestException):
                continue
        result = aggregate_live_markets(signals)
        source_health.append(
            {
                "source": "live_polymarket",
                "status": result.status,
                "detail": f"markets={result.market_count}",
            }
        )
        return result
    except (PolymarketDataError, ValueError, requests.RequestException):
        source_health.append(
            {
                "source": "live_polymarket",
                "status": "error",
                "detail": "public Gamma/CLOB collection failed",
            }
        )
        return aggregate_live_markets(())


def _close_frame(raw: pd.DataFrame, requested: Sequence[str]) -> pd.DataFrame:
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" not in raw.columns.get_level_values(0):
            raise ValueError("close field unavailable")
        close = raw["Close"].copy()
    else:
        if "Close" not in raw:
            raise ValueError("close field unavailable")
        close = raw[["Close"]].rename(columns={"Close": requested[0]})
    close.columns = [str(value).upper() for value in close.columns]
    return close.apply(pd.to_numeric, errors="coerce").dropna(how="all")


def _build_volatility(
    *,
    inputs: Mapping[str, Any],
    as_of: dt.datetime,
    network_enabled: bool,
    source_health: list[Mapping[str, Any]],
) -> VolatilitySurfaceResult:
    supplied = inputs.get("volatility_surface")
    if isinstance(supplied, Mapping):
        snapshot = VolatilitySurfaceSnapshot(
            observed_at=as_of,
            **{
                key: supplied.get(key)
                for key in (
                    "vix1d", "vix9d", "vix", "vix3m", "vix6m", "vvix",
                    "skew", "realized_vol_20d", "put_call_volume_ratio",
                    "put_call_open_interest_ratio", "source_health",
                )
                if key in supplied
            },
        )
        result = evaluate_volatility_surface(snapshot)
        source_health.append(
            {"source": "volatility_surface", "status": result.status, "detail": "private/live input"}
        )
        return result
    if network_enabled:
        try:
            import yfinance as yf

            requested = [
                "^VIX1D", "^VIX9D", "^VIX", "^VIX3M", "^VIX6M",
                "^VVIX", "^SKEW", "SPY",
            ]
            raw = yf.download(
                requested,
                period="3mo",
                interval="1d",
                auto_adjust=False,
                actions=False,
                progress=False,
                threads=False,
                group_by="column",
            )
            close = _close_frame(raw, requested)
            def last(symbol: str) -> float | None:
                if symbol not in close or close[symbol].dropna().empty:
                    return None
                return float(close[symbol].dropna().iloc[-1])
            realized = None
            if "SPY" in close and len(close["SPY"].dropna()) >= 21:
                realized = float(
                    close["SPY"].pct_change(fill_method=None).rolling(20).std().iloc[-1]
                    * np.sqrt(252)
                    * 100.0
                )
            result = evaluate_volatility_surface(
                VolatilitySurfaceSnapshot(
                    observed_at=as_of,
                    vix1d=last("^VIX1D"),
                    vix9d=last("^VIX9D"),
                    vix=last("^VIX"),
                    vix3m=last("^VIX3M"),
                    vix6m=last("^VIX6M"),
                    vvix=last("^VVIX"),
                    skew=last("^SKEW"),
                    realized_vol_20d=realized,
                    source_health="healthy",
                )
            )
            source_health.append(
                {
                    "source": "volatility_surface",
                    "status": result.status,
                    "detail": "public close proxies",
                }
            )
            return result
        except (ImportError, OSError, ValueError, KeyError, TypeError):
            pass
    result = evaluate_volatility_surface(
        VolatilitySurfaceSnapshot(
            observed_at=as_of,
            vix=None,
            source_health="blocked",
        )
    )
    source_health.append(
        {
            "source": "volatility_surface",
            "status": "blocked",
            "detail": "current VIX family unavailable",
        }
    )
    return result


def _build_option_tail(
    inputs: Mapping[str, Any],
    as_of: dt.datetime,
    source_health: list[Mapping[str, Any]],
) -> OptionTailRiskResult | None:
    raw = inputs.get("option_chain")
    if not isinstance(raw, Mapping):
        source_health.append(
            {
                "source": "option_tail",
                "status": "not_configured",
                "detail": "optional point-in-time chain",
            }
        )
        return None
    try:
        quotes = tuple(
            OptionQuote(
                option_type=str(row.get("option_type") or ""),
                strike=float(row.get("strike")),
                implied_volatility=float(row.get("implied_volatility")),
                delta=None if row.get("delta") is None else float(row.get("delta")),
                bid=None if row.get("bid") is None else float(row.get("bid")),
                ask=None if row.get("ask") is None else float(row.get("ask")),
                volume=float(row.get("volume") or 0.0),
                open_interest=float(row.get("open_interest") or 0.0),
            )
            for row in raw.get("quotes") or ()
        )
        result = evaluate_option_tail_risk(
            OptionChainSnapshot(
                symbol=str(raw.get("symbol") or "SPY"),
                observed_at=as_of,
                spot=float(raw.get("spot")),
                days_to_expiry=float(raw.get("days_to_expiry")),
                quotes=quotes,
                risk_free_rate=float(raw.get("risk_free_rate") or 0.0),
                dividend_yield=float(raw.get("dividend_yield") or 0.0),
                source_health=str(raw.get("source_health") or "healthy"),
            )
        )
        source_health.append(
            {"source": "option_tail", "status": result.status, "detail": "point-in-time chain"}
        )
        return result
    except (TypeError, ValueError, KeyError):
        source_health.append(
            {"source": "option_tail", "status": "error", "detail": "invalid option chain"}
        )
        return None


def _build_overnight(
    inputs: Mapping[str, Any],
    as_of: dt.datetime,
    source_health: list[Mapping[str, Any]],
) -> tuple[OvernightRiskResult, ...]:
    rows = []
    for raw in inputs.get("overnight_snapshots") or ():
        if not isinstance(raw, Mapping):
            continue
        try:
            rows.append(
                evaluate_overnight_risk(
                    OvernightSnapshot(
                        symbol=str(raw.get("symbol") or ""),
                        observed_at=as_of,
                        previous_close=float(raw.get("previous_close")),
                        premarket_price=float(raw.get("premarket_price")),
                        overnight_high=raw.get("overnight_high"),
                        overnight_low=raw.get("overnight_low"),
                        historical_mean=float(raw.get("historical_mean") or 0.0),
                        historical_std=raw.get("historical_std"),
                        premarket_volume_ratio=raw.get("premarket_volume_ratio"),
                        es_return=raw.get("es_return"),
                        nq_return=raw.get("nq_return"),
                        rty_return=raw.get("rty_return"),
                        vix_change=raw.get("vix_change"),
                        credit_confirmation=raw.get("credit_confirmation"),
                        source_health=str(raw.get("source_health") or "healthy"),
                    )
                )
            )
        except (TypeError, ValueError, KeyError):
            continue
    source_health.append(
        {
            "source": "overnight_internal_cache",
            "status": "healthy" if rows else "not_configured",
            "detail": f"signals={len(rows)}; never a second user-facing report",
        }
    )
    return tuple(rows)


def _factor_return_frame(prices: pd.DataFrame) -> pd.DataFrame:
    close = prices.copy().sort_index().apply(pd.to_numeric, errors="coerce")
    close.columns = [str(value).upper() for value in close.columns]
    returns = close.pct_change(fill_method=None)
    if "SPY" not in returns:
        raise ValueError("SPY is required for public factor proxies")
    factors: dict[str, pd.Series] = {"market": returns["SPY"]}
    pairs = {
        "size": ("IWM", "SPY"),
        "semis": ("SMH", "SPY"),
        "memory": ("MU", "SMH"),
        "energy": ("XLE", "SPY"),
        "rates": ("TLT", "SPY"),
        "gold": ("GLD", "SPY"),
        "defensive": ("SCHD", "SPY"),
    }
    for name, (left, right) in pairs.items():
        if left in returns and right in returns:
            factors[name] = returns[left] - returns[right]
    return pd.DataFrame(factors).replace([np.inf, -np.inf], np.nan).dropna(how="all")


def _asset_return_frame(
    prices: pd.DataFrame,
    symbols: Sequence[str],
) -> pd.DataFrame:
    close = prices.copy().sort_index().apply(pd.to_numeric, errors="coerce")
    close.columns = [str(value).upper() for value in close.columns]
    result: dict[str, pd.Series] = {}
    for symbol in symbols:
        proxy = _ASSET_PROXY.get(symbol, symbol)
        if proxy in close:
            result[symbol] = close[proxy].pct_change(fill_method=None)
    if not result:
        raise ValueError("no portfolio asset return proxy is available")
    return pd.DataFrame(result).replace([np.inf, -np.inf], np.nan).dropna(how="all")


def _build_barra_kalman(
    *,
    prices: pd.DataFrame | None,
    symbols: Sequence[str],
    inputs: Mapping[str, Any],
    source_health: list[Mapping[str, Any]],
) -> tuple[BarraProxyResult | None, KalmanExposureResult | None, pd.DataFrame | None]:
    if prices is None:
        source_health.append(
            {"source": "barra_kalman_live", "status": "blocked", "detail": "price history missing"}
        )
        return None, None, None
    try:
        assets = _asset_return_frame(prices, symbols)
        factors = _factor_return_frame(prices)
        common = assets.index.intersection(factors.index)
        assets, factors = assets.loc[common], factors.loc[common]
        configured = {
            str(key).upper(): max(0.0, float(value))
            for key, value in dict(inputs.get("portfolio_weights") or {}).items()
        }
        available = [str(column).upper() for column in assets.columns]
        if configured and any(symbol in configured for symbol in available):
            weights = {symbol: configured.get(symbol, 0.0) for symbol in available}
            total = sum(weights.values())
            weights = (
                {symbol: value / total for symbol, value in weights.items()}
                if total > 0
                else {symbol: 1.0 / len(available) for symbol in available}
            )
        else:
            weights = {symbol: 1.0 / len(available) for symbol in available}
        barra = fit_barra_proxy(assets, factors, weights)
        vector = np.array([weights[str(column).upper()] for column in assets.columns])
        portfolio_return = assets.mul(vector, axis=1).sum(axis=1).rename("portfolio")
        kalman = kalman_dynamic_exposures(portfolio_return, factors)
        source_health.append(
            {
                "source": "barra_kalman_live",
                "status": "healthy",
                "detail": f"observations={len(common)}; factors={len(factors.columns)}",
            }
        )
        return barra, kalman, factors
    except (ValueError, KeyError, TypeError, np.linalg.LinAlgError):
        source_health.append(
            {
                "source": "barra_kalman_live",
                "status": "error",
                "detail": "public proxy fit failed closed",
            }
        )
        return None, None, None


def _series_from_rows(rows: Iterable[Mapping[str, Any]], value_key: str) -> pd.Series:
    values: dict[pd.Timestamp, float] = {}
    for row in rows:
        timestamp = pd.Timestamp(str(row.get("date") or row.get("session") or ""))
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert(None)
        values[timestamp] = float(row.get(value_key))
    return pd.Series(values).sort_index()


def _build_manager_skill(
    *,
    inputs: Mapping[str, Any],
    prices: pd.DataFrame | None,
    factors: pd.DataFrame | None,
    source_health: list[Mapping[str, Any]],
) -> Mapping[str, ManagerSkillResult]:
    results: dict[str, ManagerSkillResult] = {}
    for row in inputs.get("manager_research") or ():
        if not isinstance(row, Mapping):
            continue
        try:
            name = str(row.get("name") or row.get("manager") or "manager").strip()
            fund = _series_from_rows(row.get("returns") or (), "return")
            factor_rows = row.get("factor_returns") or ()
            factor_frame = pd.DataFrame(
                {
                    str(key): _series_from_rows(factor_rows, str(key))
                    for key in dict(row.get("factor_columns") or {}).keys()
                }
            )
            if factor_frame.empty and isinstance(row.get("factor_frame"), Mapping):
                factor_frame = pd.DataFrame(row["factor_frame"])
            fragility = ManagerFragility(**dict(row.get("fragility") or {}))
            results[name] = evaluate_manager_skill(
                fund,
                factor_frame,
                market_factor=str(row.get("market_factor") or "market"),
                fragility=fragility,
            )
        except (TypeError, ValueError, KeyError, np.linalg.LinAlgError):
            continue

    active_symbols = [
        str(value).upper()
        for value in inputs.get("active_fund_symbols") or ()
        if str(value).strip()
    ]
    if prices is not None and factors is not None:
        close = prices.copy()
        close.columns = [str(value).upper() for value in close.columns]
        for symbol in active_symbols:
            proxy = _ASSET_PROXY.get(symbol, symbol)
            if proxy not in close:
                continue
            try:
                fund = close[proxy].pct_change(fill_method=None).rename(symbol)
                results[f"strategy_proxy:{symbol}"] = evaluate_manager_skill(
                    fund,
                    factors,
                    market_factor="market",
                    fragility=None,
                    bootstrap_iterations=250,
                )
            except (ValueError, np.linalg.LinAlgError):
                continue
    source_health.append(
        {
            "source": "manager_skill_live",
            "status": "healthy" if results else "need_info",
            "detail": (
                f"evaluations={len(results)}"
                if results
                else "named-manager/fund return series not supplied"
            ),
        }
    )
    return results


def _collect_institutional_news(
    *,
    network_enabled: bool,
    session: requests.Session | None,
    source_health: list[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    if not network_enabled:
        source_health.append(
            {
                "source": "fund_and_financial_company_news",
                "status": "disabled",
                "detail": "network disabled",
            }
        )
        return ()
    settings = ExternalSettings(
        enabled=True,
        lookback_days=7,
        max_items_per_ticker=3,
        news_enabled=True,
        news_limit=3,
        stocktwits_enabled=False,
        reddit_enabled=False,
        x_enabled=False,
        x_discovery_enabled=False,
        sec_enabled=False,
        manual_kol_enabled=False,
        public_web_enabled=True,
        public_web_queries=_INSTITUTION_QUERIES,
        public_web_limit_per_query=5,
    )
    bundle = collect_external_views(
        (),
        _INSTITUTION_TARGETS,
        settings,
        session=session,
        network_enabled=True,
    )
    source_health.extend(
        {
            "source": f"institutional:{row.source}",
            "status": row.status,
            "detail": row.detail,
        }
        for row in bundle.statuses
    )
    items = []
    for ticker, view in bundle.by_ticker.items():
        for item in view.items:
            items.append(
                {
                    "ticker": ticker,
                    "source": item.source,
                    "title": item.title,
                    "url": item.url,
                    "published": item.published,
                    "source_class": (
                        "company_primary" if item.is_primary_source else "financial_news"
                    ),
                    "independence_group": item.independence_group,
                    "research_weight": item.research_weight,
                    "verification": (
                        "primary"
                        if item.is_primary_source
                        else "secondary_requires_original_check"
                    ),
                }
            )
    for item in bundle.global_items:
        items.append(
            {
                "ticker": item.ticker,
                "source": item.source,
                "title": item.title,
                "url": item.url,
                "published": item.published,
                "source_class": "public_search_context",
                "independence_group": item.independence_group,
                "research_weight": item.research_weight,
                "verification": "secondary_requires_original_check",
            }
        )
    unique = {}
    for item in items:
        key = (item["source"], item["title"], item["url"])
        unique[key] = item
    return tuple(list(unique.values())[:24])


def _opinion_payloads(inputs: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    payloads: list[Mapping[str, Any]] = []
    for key, platform in (
        ("opinion_records", None),
        ("agent_digests", "github_agent"),
        ("xiaohongshu_views", "xiaohongshu"),
        ("broker_research_digests", "broker_research"),
    ):
        for raw in inputs.get(key) or ():
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            if platform and not row.get("platform"):
                row["platform"] = platform
            payloads.append(row)
    return payloads


def _evidence_items(
    base: DailyResearchEnrichment,
    institutional_news: Sequence[Mapping[str, Any]],
    brief: PoliticalBriefResult,
) -> list[Any]:
    rows: list[Any] = list(base.global_narratives.observations)
    rows.extend(institutional_news)
    rows.extend(
        {
            "source_class": (
                "government_primary"
                if claim.source_type.startswith("official")
                or "official" in claim.source_type
                else "major_media"
            ),
            "independence_group": f"political:{claim.actor_id}",
            "source": claim.actor_name,
            "url": claim.source_url,
            "title": claim.evidence_sentence,
        }
        for claim in brief.top_claims
    )
    return rows


def _change_text(thesis_id: str, score: float, inputs: Mapping[str, Any]) -> str:
    previous = dict(inputs.get("previous_thesis_scores") or {}).get(thesis_id)
    if previous is None:
        return "首次记录/缺少昨日同口径分数"
    delta = score - float(previous)
    if delta > 0.08:
        return f"较昨日增强 {delta:+.2f}"
    if delta < -0.08:
        return f"较昨日减弱 {delta:+.2f}"
    return f"较昨日基本稳定 {delta:+.2f}"


def _build_theses(
    *,
    base: DailyResearchEnrichment,
    brief: PoliticalBriefResult,
    tpti: TrumpPolicyIndexResult,
    live_poly: LivePolymarketResult,
    vol: VolatilitySurfaceResult,
    barra: BarraProxyResult | None,
    kalman: KalmanExposureResult | None,
    managers: Mapping[str, ManagerSkillResult],
    opinion: OpinionInboxResult,
    institutional_news: Sequence[Mapping[str, Any]],
    inputs: Mapping[str, Any],
) -> tuple[ResearchThesis, ...]:
    topics = base.global_narratives.topic_scores
    factor = base.institutional_factor_research
    active = tuple(getattr(factor, "active_factors", ()) or ())
    quarantined = tuple(getattr(factor, "quarantined_factors", ()) or ())
    theses: list[ResearchThesis] = []

    memory_score = (
        topics.get("memory_hbm_demand", 0.0)
        - topics.get("memory_oversupply", 0.0)
        - 0.60 * topics.get("semiconductor_export_controls", 0.0)
        + 0.25 * tpti.asset_scores.get("MU", 0.0)
        + 0.20 * live_poly.asset_scores.get("MU", 0.0)
    )
    memory_stance = (
        "审慎看多/持有"
        if memory_score > 0.12
        else ("偏空/不追涨" if memory_score < -0.12 else "中性持有")
    )
    memory_variant = (
        "产业叙事偏正，但单因子尚未跨 1/5/20 日通过，不能把 HBM 故事直接等同于可交易 alpha。"
        if memory_score >= 0 and quarantined
        else "产业叙事与多周期因子方向一致，仍需一级来源和估值/仓位门禁。"
    )
    theses.append(
        ResearchThesis(
            thesis_id="memory_hbm",
            title="MU/SMH：HBM 景气、供给约束与政策尾部",
            stance=memory_stance,
            change=_change_text("memory_hbm", memory_score, inputs),
            consensus_and_variant=memory_variant,
            evidence_chain=tuple(
                row
                for row in (
                    f"全球叙事分：HBM={topics.get('memory_hbm_demand', 0.0):+.2f}，"
                    f"供给过剩={topics.get('memory_oversupply', 0.0):+.2f}。",
                    f"Trump/政策对 MU 的传导={tpti.asset_scores.get('MU', 0.0):+.2f}。",
                    (
                        "跨周期 active 因子：" + "、".join(active[:4])
                        if active
                        else "没有单因子通过跨周期 admission；正面故事不进入加仓权重。"
                    ),
                )
                if row
            ),
            catalysts=(
                "HBM 合同、产能与价格的公司一级披露",
                "出口许可、关税或韩国半导体扶持政策的正式落地",
                "存储价格和库存周期持续改善",
            ),
            horizon="1—2 个季度；事件风险按 1/5/20 个交易日复核",
            invalidation=(
                "DRAM/HBM 库存和价格转弱",
                "出口限制或关税明显收紧",
                "相对 SMH/市场的 5 日和 20 日 OOS 因子继续失效",
            ),
            confidence=round(_clamp(0.45 + 0.30 * abs(memory_score) - (0.15 if quarantined else 0.0), 0.15, 0.85), 4),
            affected_assets=("MU", "SMH", "QQQM"),
        )
    )

    macro_score = (
        -0.45 * topics.get("rates_inflation", 0.0)
        -0.35 * topics.get("oil_supply", 0.0)
        -0.25 * topics.get("middle_east_escalation", 0.0)
        +0.25 * live_poly.asset_scores.get("VOO", 0.0)
    )
    macro_stance = (
        "风险预算收紧"
        if vol.composite_stress >= 0.50 or macro_score < -0.12
        else "中性持有"
    )
    theses.append(
        ResearchThesis(
            thesis_id="macro_risk",
            title="QQQM/VOO：利率、油价、波动率与风险溢价",
            stance=macro_stance,
            change=_change_text("macro_risk", macro_score - vol.composite_stress, inputs),
            consensus_and_variant=(
                "指数趋势可能仍强，但尾部风险和实际利率决定是否能把趋势转换为新增仓位；"
                "波动率曲面属于一个相关证据组，不重复计票。"
            ),
            evidence_chain=(
                f"VIX 曲面状态={vol.regime}，压力分={vol.composite_stress:.2f}。",
                f"宏观叙事综合分={macro_score:+.2f}。",
                f"实时 Polymarket 风险预算乘数={live_poly.risk_budget_multiplier:.3f}。",
                (
                    f"Barra 风险预算乘数={barra.risk_budget_multiplier:.3f}。"
                    if barra is not None
                    else "Barra 公共代理未形成可用结果。"
                ),
                (
                    f"Kalman 动态暴露风险预算乘数={kalman.risk_budget_multiplier:.3f}。"
                    if kalman is not None
                    else "Kalman 动态暴露未形成可用结果。"
                ),
            ),
            catalysts=(
                "通胀、就业、FOMC 与实际收益率",
                "VIX 前端期限结构恢复顺价",
                "油价和信用利差同步回落",
            ),
            horizon="次日风险预算与 1—4 周战术仓位",
            invalidation=(
                "VIX/VVIX/SKEW 同组压力持续上升",
                "实际收益率和油价同时上行",
                "市场宽度与信用确认恶化",
            ),
            confidence=round(_clamp(0.55 + 0.25 * vol.composite_stress, 0.25, 0.90), 4),
            affected_assets=("QQQM", "VOO", "SCHD", "BOXX", "GLDM"),
        )
    )

    policy_score = brief.decision_score_contribution + tpti.decision_score_contribution
    theses.append(
        ResearchThesis(
            thesis_id="policy_transmission",
            title="政策传导：特朗普/白宫言论与正式落地系数",
            stance=(
                "仅作风险覆盖"
                if brief.accepted_claim_count == 0
                else ("负面风险覆盖" if policy_score < 0 else "中性/正面观察")
            ),
            change=_change_text("policy_transmission", policy_score, inputs),
            consensus_and_variant=(
                "市场常把发言热度当作信号；本系统只给完整政策句、权威层级、实施阶段、"
                "具体性、时效和资产敏感度计权，媒体不能替代原始来源。"
            ),
            evidence_chain=(
                f"政策完整句 claim 数={brief.accepted_claim_count}。",
                f"TPTI 综合方向={tpti.composite_score:+.2f}，置信度={tpti.confidence:.1%}。",
                f"政策风险预算乘数={min(brief.risk_budget_multiplier, tpti.risk_budget_multiplier):.3f}。",
            ),
            catalysts=(
                "行政命令、法案签署、监管实施日期",
                "关税/出口许可/能源与数字资产正式文件",
                "主流媒体对执行细节的独立核验",
            ),
            horizon="政策事件 1—60 日；结构性政策最长 1—3 年",
            invalidation=(
                "后续正式文件与原言论相反",
                "只有媒体转述而无原始完整句",
                "政策停留在一般观点且未进入实施阶段",
            ),
            confidence=round(_clamp(tpti.confidence, 0.10, 0.90), 4),
            affected_assets=tuple(sorted(tpti.asset_scores, key=lambda key: abs(tpti.asset_scores[key]), reverse=True)[:6]),
        )
    )

    if institutional_news or opinion.assessments or managers:
        manager_pass = [
            key for key, value in managers.items() if value.verdict == "PASS"
        ]
        theses.append(
            ResearchThesis(
                thesis_id="institution_quality",
                title="基金公司、金融机构与经理质量",
                stance="监控治理、资金流和收益来源可重复性",
                change=_change_text(
                    "institution_quality",
                    0.10 * len(manager_pass) - 0.05 * opinion.bearish_crowding_score,
                    inputs,
                ),
                consensus_and_variant=(
                    "高历史收益不等于能力；经理评价同时使用 alpha、bootstrap、TM/HM 择时、"
                    "上下行捕获、滚动持续性、风格漂移和杠杆/流动性脆弱度。"
                ),
                evidence_chain=(
                    f"基金/金融机构新闻条目={len(institutional_news)}。",
                    f"经理/策略 skill 评估={len(managers)}，PASS={len(manager_pass)}。",
                    f"外部观点已验证={opinion.verified_count}，仅线索={opinion.context_only_count}。",
                ),
                catalysts=(
                    "基金经理变更、产品费率/关闭/发行与资金流",
                    "管理规模、容量和组合风格变化",
                    "银行、券商、交易所的成交量和市场结构变化",
                ),
                horizon="月度经理/产品评审；重大事件当日复核",
                invalidation=(
                    "经理或团队变更导致历史业绩不可归属",
                    "alpha 在 bootstrap/滚动窗口中不再显著",
                    "杠杆、集中度、流动性或主经纪商脆弱度恶化",
                ),
                confidence=round(
                    _clamp(
                        0.30
                        + 0.10 * min(len(institutional_news), 4)
                        + 0.15 * min(len(managers), 2),
                        0.20,
                        0.80,
                    ),
                    4,
                ),
                affected_assets=tuple(
                    sorted(
                        {
                            str(row.get("ticker") or "")
                            for row in institutional_news
                            if str(row.get("ticker") or "")
                        }
                    )[:8]
                ),
            )
        )
    return tuple(theses[:5])


def build_daily_advanced_research(
    symbols: Iterable[str],
    *,
    as_of: dt.datetime | None = None,
    session: requests.Session | None = None,
    network_enabled: bool = True,
    price_history: pd.DataFrame | None = None,
    inputs: Mapping[str, Any] | None = None,
) -> AdvancedDailyResearch:
    now = as_of or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    now = now.astimezone(dt.timezone.utc)
    symbol_list = tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in symbols
            if str(value).strip()
        )
    )
    source_health: list[Mapping[str, Any]] = []
    if inputs is None:
        private_inputs, input_health = load_private_research_inputs()
        inputs = private_inputs
        source_health.extend(input_health)
    else:
        inputs = dict(inputs)

    prices = price_history
    if prices is None and network_enabled:
        prices, price_health = fetch_research_price_history(symbol_list)
        source_health.append(price_health)
    base = build_daily_research_enrichment(
        symbol_list,
        as_of=now,
        session=session,
        network_enabled=network_enabled,
        price_history=prices,
    )
    brief, tpti = _build_policy(
        inputs=inputs,
        as_of=now,
        network_enabled=network_enabled,
        session=session,
        source_health=source_health,
    )
    live_poly = _build_live_polymarket(
        inputs=inputs,
        network_enabled=network_enabled,
        session=session,
        source_health=source_health,
    )
    resolved_poly = study_resolved_markets(
        inputs.get("resolved_polymarket_events") or (),
        freeze_hours=int(inputs.get("polymarket_freeze_hours") or 24),
        min_samples=max(3, int(inputs.get("polymarket_min_samples") or 5)),
    )
    source_health.append(
        {
            "source": "resolved_polymarket_calibration",
            "status": resolved_poly.status,
            "detail": f"events={resolved_poly.accepted_count}",
        }
    )
    vol = _build_volatility(
        inputs=inputs,
        as_of=now,
        network_enabled=network_enabled,
        source_health=source_health,
    )
    option_tail = _build_option_tail(inputs, now, source_health)
    overnight = _build_overnight(inputs, now, source_health)
    barra, kalman, factor_returns = _build_barra_kalman(
        prices=prices,
        symbols=symbol_list,
        inputs=inputs,
        source_health=source_health,
    )
    managers = _build_manager_skill(
        inputs=inputs,
        prices=prices,
        factors=factor_returns,
        source_health=source_health,
    )
    institution_news = _collect_institutional_news(
        network_enabled=network_enabled,
        session=session,
        source_health=source_health,
    )
    opinion_records, rejected = parse_opinion_records(
        _opinion_payloads(inputs),
        as_of=now,
        lookback_days=int(inputs.get("opinion_lookback_days") or 14),
    )
    opinion = assess_opinion_inbox(
        opinion_records,
        evidence_items=_evidence_items(base, institution_news, brief),
        portfolio_tickers=symbol_list,
        rejected_count=rejected,
    )
    source_health.append(
        {
            "source": "opinion_inbox",
            "status": opinion.status,
            "detail": (
                f"accepted={opinion.accepted_count}; verified={opinion.verified_count}; "
                f"context={opinion.context_only_count}"
            ),
        }
    )

    multipliers = [
        base.global_narratives.risk_budget_multiplier,
        getattr(base.institutional_factor_research, "risk_budget_multiplier", 0.90),
        brief.risk_budget_multiplier,
        tpti.risk_budget_multiplier,
        live_poly.risk_budget_multiplier,
        resolved_poly.risk_budget_multiplier,
        vol.risk_budget_multiplier,
        opinion.risk_budget_multiplier,
    ]
    if option_tail is not None:
        multipliers.append(option_tail.risk_budget_multiplier)
    multipliers.extend(row.risk_budget_multiplier for row in overnight)
    if barra is not None:
        multipliers.append(barra.risk_budget_multiplier)
    if kalman is not None:
        multipliers.append(kalman.risk_budget_multiplier)
    effective = _clamp(float(np.prod(multipliers)), 0.40, 1.02)

    theses = _build_theses(
        base=base,
        brief=brief,
        tpti=tpti,
        live_poly=live_poly,
        vol=vol,
        barra=barra,
        kalman=kalman,
        managers=managers,
        opinion=opinion,
        institutional_news=institution_news,
        inputs=inputs,
    )
    all_health = list(base.source_health) + source_health
    severe = {
        str(row.get("status") or "").casefold()
        for row in all_health
        if str(row.get("source") or "") in {
            "research_price_history",
            "institutional_factor_validation",
            "volatility_surface",
            "barra_kalman_live",
        }
    }
    if "error" in severe or "blocked" in severe:
        status = "degraded"
    elif base.status == "completed":
        status = "completed"
    else:
        status = "partial"
    warnings = tuple(
        dict.fromkeys(
            [
                *base.warnings,
                *brief.warnings,
                *tpti.warnings,
                *live_poly.warnings,
                *resolved_poly.warnings,
                *vol.warnings,
                *opinion.warnings,
                "Advanced model libraries are now in the daily path, but missing live inputs remain explicit.",
                "No module in this bridge can submit, modify or cancel an order.",
            ]
        )
    )
    return AdvancedDailyResearch(
        status=status,
        generated_at=now.isoformat(),
        base=base,
        political_brief=brief,
        trump_policy=tpti,
        live_polymarket=live_poly,
        resolved_polymarket=resolved_poly,
        volatility_surface=vol,
        option_tail=option_tail,
        overnight_risk=overnight,
        barra=barra,
        kalman=kalman,
        manager_skill=managers,
        opinion_inbox=opinion,
        institutional_news=institution_news,
        theses=theses,
        effective_risk_budget=round(effective, 6),
        source_health=tuple(all_health),
        warnings=warnings,
    )


def render_buy_side_research_markdown(result: AdvancedDailyResearch) -> str:
    """Render a thesis-led memo rather than a source-by-source news dump."""

    lines = ["## 4. 买方研究结论", ""]
    for thesis in result.theses:
        assets = "、".join(thesis.affected_assets) or "组合层"
        lines += [
            f"### {thesis.title}",
            f"- **结论/动作含义：** {thesis.stance}",
            f"- **相对昨日：** {thesis.change}",
            f"- **共识与差异化判断：** {thesis.consensus_and_variant}",
            "- **证据链：** " + "；".join(thesis.evidence_chain),
            "- **催化剂：** " + "；".join(thesis.catalysts),
            f"- **时间尺度：** {thesis.horizon}",
            "- **反证/退出条件：** " + "；".join(thesis.invalidation),
            f"- **置信度：** {thesis.confidence:.0%}；影响标的：{assets}",
            "",
        ]

    factor = result.base.institutional_factor_research
    lines += [
        "## 5. 高级模型、因子与风险预算",
        "",
        f"- 有效组合风险预算：**{result.effective_risk_budget:.1%}**。",
        f"- 政治完整句：`{result.political_brief.status}` / "
        f"{result.political_brief.accepted_claim_count} 条；"
        f"TPTI={result.trump_policy.composite_score:+.3f}，"
        f"置信度={result.trump_policy.confidence:.1%}。",
        f"- 实时 Polymarket：`{result.live_polymarket.status}` / "
        f"{result.live_polymarket.market_count} 个市场；"
        f"已结算校准：`{result.resolved_polymarket.status}` / "
        f"{result.resolved_polymarket.accepted_count} 个事件。",
        f"- VIX/尾部风险：`{result.volatility_surface.status}` / "
        f"{result.volatility_surface.regime} / "
        f"压力={result.volatility_surface.composite_stress:.1%}。",
        f"- Barra 公共代理：`{'ok' if result.barra is not None else 'BLOCKED'}`；"
        f"Kalman 动态暴露：`{'ok' if result.kalman is not None else 'BLOCKED'}`。",
        f"- 经理/策略 skill：{len(result.manager_skill)} 个评估；"
        f"PASS={sum(row.verdict == 'PASS' for row in result.manager_skill.values())}。",
    ]
    if factor is None:
        lines.append("- 1/5/20 日因子：`BLOCKED`，不得推断为中性。")
    else:
        lines += [
            f"- 1/5/20 日因子总状态：`{factor.status}`；"
            f"active={len(factor.active_factors)}，"
            f"quarantined={len(factor.quarantined_factors)}。",
            "",
            "| Horizon | 状态 | OOS 样本 | 净年化 | Sharpe | PSR | 最大回撤 |",
            "|---:|---|---:|---:|---:|---:|---:|",
        ]
        for row in factor.horizon_summaries:
            sharpe = "UNKNOWN" if row.net_sharpe is None else f"{row.net_sharpe:.2f}"
            psr = (
                "UNKNOWN"
                if row.probabilistic_sharpe_ratio is None
                else f"{row.probabilistic_sharpe_ratio:.1%}"
            )
            lines.append(
                f"| {row.horizon_sessions} | {row.status} | {row.oos_observations} | "
                f"{row.net_annualized_return:+.2%} | {sharpe} | {psr} | "
                f"{row.max_drawdown:.2%} |"
            )

    lines += ["", "## 6. 基金公司、金融机构与市场观点", ""]
    if result.institutional_news:
        lines += [
            "| 标的/机构 | 来源 | 事实或观点 | 复核状态 |",
            "|---|---|---|---|",
        ]
        for row in result.institutional_news[:12]:
            lines.append(
                f"| {row.get('ticker') or 'GLOBAL'} | "
                f"{str(row.get('source') or '').replace('|', '/')} | "
                f"{str(row.get('title') or '').replace('|', '/')[:180]} | "
                f"{row.get('verification') or 'secondary'} |"
            )
    else:
        lines.append("- 基金公司/金融机构新闻源本次无可用新增；不视为中性。")
    lines += [
        "",
        f"- 外部观点 inbox：`{result.opinion_inbox.status}`；"
        f"已验证={result.opinion_inbox.verified_count}，"
        f"仅线索={result.opinion_inbox.context_only_count}，"
        f"看空拥挤度={result.opinion_inbox.bearish_crowding_score:.1%}。",
    ]
    if result.opinion_inbox.assessments:
        lines += [
            "",
            "| 平台 | 标的 | 方向 | 验证 | 观点 | 处理 |",
            "|---|---|---:|---|---|---|",
        ]
        for row in result.opinion_inbox.assessments[:12]:
            lines.append(
                f"| {row.platform} | {row.ticker or 'GLOBAL'} | "
                f"{row.direction:+.2f} | {row.verification_status} | "
                f"{row.claim.replace('|', '/')[:180]} | "
                f"{row.reason.replace('|', '/')[:140]} |"
            )
    lines += [
        "",
        "- Agent 摘要、Reddit、Quora、小红书均不能单独产生加仓；"
        "缺少原始链接或独立复核时只保留为线索。",
    ]
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "AdvancedDailyResearch",
    "ResearchThesis",
    "build_daily_advanced_research",
    "load_private_research_inputs",
    "render_buy_side_research_markdown",
]
