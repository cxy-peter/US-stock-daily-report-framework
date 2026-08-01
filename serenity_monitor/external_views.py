"""External evidence collectors with explicit source-health reporting."""
from __future__ import annotations

import hashlib
import html
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus

import requests
import yaml

from .credibility import Claim, MarketContext, assess_opinion
from .sec_edgar import collect_sec_filings, company_security_targets
from .source_profiles import load_source_profiles


@dataclass
class ExternalItem:
    item_id: str
    source: str
    source_kind: str  # news | kol | social | community
    title: str
    text: str = ""
    url: str = ""
    published: str = ""
    author: str = ""
    ticker: str = ""
    source_reference: str = ""
    credibility: float = 0.5
    engagement: int = 0
    source_id: str = "anonymous_social"
    is_primary_source: bool = False
    claim_direction: str = ""
    claim_horizon_days: int | None = None
    invalidation_condition: str = ""
    primary_evidence_count: int = 0
    position_disclosed: bool | None = None
    conflict_disclosed: bool | None = None
    sponsored: bool = False
    estimated_position_usd: float | None = None
    source_score: float = 0.0
    claim_score: float = 0.0
    manager_fragility_score: float = 0.0
    manipulation_risk_score: float = 0.0
    research_weight: float = 0.0
    independence_group: str = "unverified_social"
    copy_trade_allowed: bool = False
    can_inform_research: bool = False
    red_flags: list[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return f"{self.title} {self.text}".strip()


@dataclass
class ExternalView:
    ticker: str
    items: list[ExternalItem] = field(default_factory=list)


@dataclass
class SourceStatus:
    source: str
    status: str  # ok | partial | disabled | blocked | error
    detail: str


@dataclass
class ExternalBundle:
    by_ticker: dict[str, ExternalView] = field(default_factory=dict)
    global_items: list[ExternalItem] = field(default_factory=list)
    statuses: list[SourceStatus] = field(default_factory=list)

    def view(self, ticker: str) -> ExternalView:
        return self.by_ticker.get(ticker.upper(), ExternalView(ticker.upper()))


@dataclass
class ExternalSettings:
    enabled: bool = True
    lookback_days: int = 7
    max_items_per_ticker: int = 10
    news_enabled: bool = True
    news_limit: int = 6
    stocktwits_enabled: bool = False
    stocktwits_limit: int = 20
    reddit_enabled: bool = False
    reddit_subreddits: tuple[str, ...] = ("stocks", "investing", "wallstreetbets")
    reddit_limit_per_sub: int = 3
    x_enabled: bool = True
    x_bearer_token_env: str = "X_BEARER_TOKEN"
    x_max_posts_per_handle: int = 100
    x_handles: tuple[dict, ...] = ()
    x_discovery_enabled: bool = True
    x_discovery_max_tickers: int = 10
    x_discovery_results_per_ticker: int = 10
    source_profiles_path: str = "config/source_profiles.private.yaml"
    sec_enabled: bool = True
    sec_lookback_days: int = 120
    sec_max_filings_per_ticker: int = 5
    sec_user_agent_env: str = "SEC_USER_AGENT"
    manual_kol_enabled: bool = True
    manual_kol_path: str = "config/manual_external_views.private.yaml"
    public_web_enabled: bool = True
    public_web_queries: tuple[str, ...] = ()
    public_web_limit_per_query: int = 5

    @classmethod
    def from_dict(cls, data: dict | None) -> "ExternalSettings":
        data = data or {}
        news = data.get("news", {}) or {}
        stocktwits = data.get("stocktwits", {}) or {}
        reddit = data.get("reddit", {}) or {}
        x_cfg = data.get("x", {}) or {}
        sec_cfg = data.get("sec", {}) or {}
        manual_kol_cfg = data.get("manual_kol", {}) or {}
        public_web_cfg = data.get("public_web", {}) or {}
        return cls(
            enabled=bool(data.get("enabled", True)),
            lookback_days=max(1, int(data.get("lookback_days", 7))),
            max_items_per_ticker=max(1, int(data.get("max_items_per_ticker", 10))),
            news_enabled=bool(news.get("enabled", True)),
            news_limit=max(1, int(news.get("limit", 6))),
            stocktwits_enabled=bool(stocktwits.get("enabled", False)),
            stocktwits_limit=max(1, int(stocktwits.get("limit", 20))),
            reddit_enabled=bool(reddit.get("enabled", False)),
            reddit_subreddits=tuple(reddit.get("subreddits", cls.reddit_subreddits)),
            reddit_limit_per_sub=max(1, int(reddit.get("limit_per_sub", 3))),
            x_enabled=bool(x_cfg.get("enabled", True)),
            x_bearer_token_env=str(x_cfg.get("bearer_token_env", "X_BEARER_TOKEN")),
            x_max_posts_per_handle=max(5, min(100, int(x_cfg.get("max_posts_per_handle", 100)))),
            x_handles=tuple(x_cfg.get("handles", []) or []),
            x_discovery_enabled=bool(x_cfg.get("discovery_enabled", True)),
            x_discovery_max_tickers=max(
                1, min(20, int(x_cfg.get("discovery_max_tickers", 10)))
            ),
            x_discovery_results_per_ticker=max(
                10, min(100, int(x_cfg.get("discovery_results_per_ticker", 10)))
            ),
            source_profiles_path=str(
                data.get("source_profiles_path", "config/source_profiles.private.yaml")
            ),
            sec_enabled=bool(sec_cfg.get("enabled", True)),
            sec_lookback_days=max(1, int(sec_cfg.get("lookback_days", 120))),
            sec_max_filings_per_ticker=max(1, int(sec_cfg.get("max_filings_per_ticker", 5))),
            sec_user_agent_env=str(sec_cfg.get("user_agent_env", "SEC_USER_AGENT")),
            manual_kol_enabled=bool(manual_kol_cfg.get("enabled", True)),
            manual_kol_path=str(
                manual_kol_cfg.get("path", "config/manual_external_views.private.yaml")
            ),
            public_web_enabled=bool(public_web_cfg.get("enabled", True)),
            public_web_queries=tuple(public_web_cfg.get("queries", []) or []),
            public_web_limit_per_query=max(
                1, min(10, int(public_web_cfg.get("limit_per_query", 5)))
            ),
        )


def _item_id(source: str, native_id: str, text: str) -> str:
    raw = f"{source}|{native_id}|{text}".encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:20]


def _clip(text: str, limit: int = 500) -> str:
    cleaned = html.unescape(re.sub(r"\s+", " ", text or "")).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3] + "..."


def _target_rows(rows: Iterable[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        ticker = str(row.get("ticker", "")).strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            out.append(row)
    return out


def _matches_row(text: str, row: dict) -> bool:
    if not text:
        return False
    ticker = str(row.get("ticker", "")).strip().upper()
    if len(ticker) <= 3:
        if re.search(rf"(?i)(?<![A-Z0-9])\${re.escape(ticker)}(?![A-Z0-9])", text):
            return True
    elif re.search(rf"(?i)(?<![A-Z0-9])\$?{re.escape(ticker)}(?![A-Z0-9])", text):
        return True
    aliases: list[str] = []
    name = str(row.get("name", "")).strip()
    if len(name) >= 4:
        aliases.append(name)
    aliases.extend(str(x).strip() for x in (row.get("social_aliases") or []) if str(x).strip())
    lowered = text.casefold()
    return any(alias.casefold() in lowered for alias in aliases if len(alias) >= 4)


def _dedupe(items: list[ExternalItem]) -> list[ExternalItem]:
    seen: set[str] = set()
    out: list[ExternalItem] = []
    for item in items:
        if item.item_id not in seen:
            seen.add(item.item_id)
            out.append(item)
    return out


def collect_external_views(
    holdings: Iterable[dict],
    watchlist: Iterable[dict],
    settings: ExternalSettings,
    session: requests.Session | None = None,
    market_contexts: dict[str, MarketContext] | None = None,
    network_enabled: bool = True,
) -> ExternalBundle:
    targets = _target_rows(list(holdings) + list(watchlist))
    bundle = ExternalBundle(
        by_ticker={str(row["ticker"]).upper(): ExternalView(str(row["ticker"]).upper()) for row in targets}
    )
    if not settings.enabled:
        bundle.statuses.append(SourceStatus("external", "disabled", "External evidence disabled by config"))
        return bundle
    http = session or requests.Session()
    http.headers.update({"User-Agent": "daily-research-agent/2.0"})
    _collect_manual_kol(targets, settings, bundle)

    if network_enabled:
        _collect_per_ticker_source(
            "Nasdaq News",
            settings.news_enabled,
            targets,
            bundle,
            lambda ticker: _fetch_nasdaq_news(http, ticker, settings.news_limit),
            "Disabled by config",
        )
        _collect_per_ticker_source(
            "StockTwits",
            settings.stocktwits_enabled,
            targets,
            bundle,
            lambda ticker: _fetch_stocktwits(http, ticker, settings.stocktwits_limit),
            "Low-signal feed disabled",
        )
        _collect_per_ticker_source(
            "Reddit",
            settings.reddit_enabled,
            targets,
            bundle,
            lambda ticker: _fetch_reddit(
                http,
                next(row for row in targets if str(row["ticker"]).upper() == ticker),
                settings,
            ),
            "Disabled by config",
        )
        _collect_sec(http, targets, settings, bundle)
        _collect_x_kols(http, targets, settings, bundle)
        _collect_x_discovery(http, targets, settings, bundle)
        _collect_public_web_kols(http, targets, settings, bundle)
    else:
        bundle.statuses.extend(
            [
                SourceStatus("Nasdaq News", "disabled", "Network collection disabled for this run"),
                SourceStatus("SEC EDGAR", "disabled", "Network collection disabled for this run"),
            ]
        )
        if settings.stocktwits_enabled:
            bundle.statuses.append(SourceStatus("StockTwits", "disabled", "Network collection disabled"))
        if settings.reddit_enabled:
            bundle.statuses.append(SourceStatus("Reddit", "disabled", "Network collection disabled"))
        _record_x_offline_status(settings, bundle)
        if settings.x_discovery_enabled:
            bundle.statuses.append(
                SourceStatus(
                    "X Discovery",
                    "blocked" if not os.getenv(settings.x_bearer_token_env, "").strip() else "disabled",
                    (
                        f"Missing GitHub secret/environment variable {settings.x_bearer_token_env}"
                        if not os.getenv(settings.x_bearer_token_env, "").strip()
                        else "Network collection disabled for this run"
                    ),
                )
            )
        if settings.public_web_enabled:
            bundle.statuses.append(
                SourceStatus(
                    "Public Web KOL Discovery",
                    "disabled",
                    "Network collection disabled for this run",
                )
            )

    for view in bundle.by_ticker.values():
        view.items = sorted(
            _dedupe(view.items), key=lambda item: (item.published, item.engagement), reverse=True
        )[: settings.max_items_per_ticker]
    bundle.global_items = sorted(
        _dedupe(bundle.global_items), key=lambda item: (item.published, item.engagement), reverse=True
    )[:10]
    _score_bundle(bundle, settings, market_contexts or {})
    return bundle


def _record_x_offline_status(settings: ExternalSettings, bundle: ExternalBundle) -> None:
    if not settings.x_enabled:
        bundle.statuses.append(SourceStatus("X KOL", "disabled", "Disabled by config"))
        return
    token = os.getenv(settings.x_bearer_token_env, "").strip()
    if not token:
        bundle.statuses.append(
            SourceStatus(
                "X KOL",
                "blocked",
                f"Missing GitHub secret/environment variable {settings.x_bearer_token_env}",
            )
        )
    else:
        bundle.statuses.append(SourceStatus("X KOL", "disabled", "Network collection disabled for this run"))


def _collect_sec(
    session: requests.Session,
    targets: list[dict],
    settings: ExternalSettings,
    bundle: ExternalBundle,
) -> None:
    if not settings.sec_enabled:
        bundle.statuses.append(SourceStatus("SEC EDGAR", "disabled", "Disabled by config"))
        return
    sec_targets = company_security_targets(targets)
    if not sec_targets:
        bundle.statuses.append(
            SourceStatus("SEC EDGAR", "ok", "No company-security targets configured")
        )
        return
    user_agent = os.getenv(settings.sec_user_agent_env, "").strip()
    if not user_agent:
        bundle.statuses.append(
            SourceStatus(
                "SEC EDGAR",
                "blocked",
                f"Missing required environment variable {settings.sec_user_agent_env}",
            )
        )
        return
    result = collect_sec_filings(
        sec_targets,
        session=session,
        user_agent=user_agent,
        lookback_days=settings.sec_lookback_days,
        max_filings_per_ticker=settings.sec_max_filings_per_ticker,
    )
    bundle.statuses.append(SourceStatus("SEC EDGAR", result.status, result.detail))
    for filing in result.filings:
        item = ExternalItem(
            item_id=_item_id("sec", filing.accession_number, filing.title),
            source="SEC EDGAR",
            source_kind="filing",
            source_id="official_company_filing",
            title=filing.title,
            text=f"Official {filing.form} filing for {filing.ticker}.",
            url=filing.url,
            published=filing.filing_date,
            ticker=filing.ticker,
            credibility=1.0,
            is_primary_source=True,
            claim_direction="neutral",
            claim_horizon_days=90,
            invalidation_condition="Reconcile filing facts with the documented thesis.",
            primary_evidence_count=1,
            position_disclosed=True,
            conflict_disclosed=True,
        )
        if filing.ticker in bundle.by_ticker:
            bundle.by_ticker[filing.ticker].items.append(item)


def _infer_direction(text: str) -> str:
    lowered = text.casefold()
    bearish = ("guidance cut", "downgrade", "fraud", "default", "下调", "风险", "减持")
    bullish = ("raise guidance", "beat expectations", "record revenue", "上调", "增长", "需求强劲")
    if any(term in lowered for term in bearish):
        return "bearish"
    if any(term in lowered for term in bullish):
        return "bullish"
    return "neutral"


def _score_bundle(
    bundle: ExternalBundle,
    settings: ExternalSettings,
    market_contexts: dict[str, MarketContext],
) -> None:
    profiles = load_source_profiles(Path(settings.source_profiles_path))
    for view in bundle.by_ticker.values():
        for item in view.items:
            _score_item(item, profiles, market_contexts.get(view.ticker, MarketContext()))
    for item in bundle.global_items:
        _score_item(item, profiles, MarketContext())


def _score_item(item: ExternalItem, profiles: dict, market: MarketContext) -> None:
    profile = profiles.get(item.source_id) or profiles["anonymous_social"]
    claim = Claim(
        claim_id=item.item_id,
        source_id=profile.source_id,
        ticker=item.ticker,
        text=item.full_text,
        direction=item.claim_direction or _infer_direction(item.full_text),
        horizon_days=item.claim_horizon_days,
        invalidation_condition=item.invalidation_condition or None,
        primary_evidence_count=max(
            int(item.primary_evidence_count or 0),
            1 if item.is_primary_source else 0,
        ),
        position_disclosed=item.position_disclosed,
        conflict_disclosed=item.conflict_disclosed,
        sponsored=item.sponsored,
        estimated_position_usd=item.estimated_position_usd,
    )
    assessment = assess_opinion(profile, claim, market)
    item.source_score = assessment.source_score
    item.claim_score = assessment.claim_score
    item.manager_fragility_score = assessment.manager_fragility_score
    item.manipulation_risk_score = assessment.manipulation_risk_score
    item.research_weight = assessment.research_weight
    item.credibility = assessment.research_weight
    item.independence_group = assessment.independence_group
    item.copy_trade_allowed = (
        assessment.copy_trade_allowed if item.source_kind == "kol" else False
    )
    item.can_inform_research = assessment.can_inform_research
    readable_flags: list[str] = []
    if assessment.source_score < 55:
        readable_flags.append("来源身份、历史业绩或披露信息不足")
    if assessment.claim_score < 55:
        readable_flags.append("论点缺少期限、失效条件或一级材料")
    if assessment.manager_fragility_score >= 35:
        readable_flags.append("管理人杠杆、集中度或流动性脆弱")
    if assessment.manipulation_risk_score >= 35:
        readable_flags.append("标的流动性、涨幅或量能显示操纵/拥挤风险")
    if not item.copy_trade_allowed:
        readable_flags.append("禁止复制交易")
    item.red_flags = readable_flags


def _collect_manual_kol(
    targets: list[dict],
    settings: ExternalSettings,
    bundle: ExternalBundle,
) -> None:
    """Load user-supplied KOL notes without scraping a gated social platform."""
    if not settings.manual_kol_enabled:
        bundle.statuses.append(SourceStatus("Manual KOL", "disabled", "Disabled by config"))
        return
    path = Path(settings.manual_kol_path)
    if not path.exists():
        bundle.statuses.append(
            SourceStatus("Manual KOL", "disabled", f"No local evidence file: {path}")
        )
        return
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rows = payload.get("items") or []
        if not isinstance(rows, list):
            raise ValueError("items must be a list")
    except Exception as exc:
        bundle.statuses.append(
            SourceStatus("Manual KOL", "error", f"{type(exc).__name__}: {exc}")
        )
        return

    accepted = 0
    unmatched = 0
    target_by_ticker = {
        str(row.get("ticker", "")).upper(): row
        for row in targets
        if str(row.get("ticker", "")).strip()
    }
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        title = _clip(str(row.get("title", "")), 300)
        text = _clip(str(row.get("text", "")), 1200)
        if not title and not text:
            continue
        source_id = str(row.get("source_id") or "anonymous_social")
        author = str(row.get("author") or "")
        platform = str(row.get("platform") or "manual")
        native_id = str(row.get("id") or f"row-{index + 1}")
        explicit_tickers = {
            str(ticker).upper()
            for ticker in (row.get("tickers") or [])
            if str(ticker).strip()
        }
        matched_tickers = explicit_tickers & set(target_by_ticker)
        if not matched_tickers:
            matched_tickers = {
                ticker
                for ticker, target in target_by_ticker.items()
                if _matches_row(f"{title} {text}", target)
            }
        source_reference = str(
            row.get("source_reference")
            or row.get("source_url_or_file")
            or "user-supplied structured note"
        )
        base = ExternalItem(
            item_id=_item_id(f"manual:{platform}", native_id, f"{title} {text}"),
            source=str(row.get("source_label") or f"{platform}/{author or source_id}"),
            source_kind="kol",
            title=title or text[:180],
            text=text,
            url=str(row.get("url") or ""),
            published=str(row.get("published") or ""),
            author=author,
            source_reference=source_reference,
            engagement=int(row.get("engagement", 0) or 0),
            source_id=source_id,
            claim_direction=str(row.get("direction") or ""),
            claim_horizon_days=_optional_int(row.get("horizon_days")),
            invalidation_condition=str(row.get("invalidation_condition") or ""),
            primary_evidence_count=int(row.get("primary_evidence_count", 0) or 0),
            position_disclosed=_optional_bool(row.get("position_disclosed")),
            conflict_disclosed=_optional_bool(row.get("conflict_disclosed")),
            sponsored=bool(row.get("sponsored", False)),
            estimated_position_usd=_optional_float(row.get("estimated_position_usd")),
        )
        if matched_tickers:
            for ticker in sorted(matched_tickers):
                bundle.by_ticker[ticker].items.append(
                    ExternalItem(**{**base.__dict__, "ticker": ticker})
                )
                accepted += 1
        else:
            bundle.global_items.append(base)
            unmatched += 1
    bundle.statuses.append(
        SourceStatus(
            "Manual KOL",
            "ok",
            f"{accepted} ticker-linked item(s); {unmatched} global item(s); source={path}",
        )
    )


def _collect_public_web_kols(
    session: requests.Session,
    targets: list[dict],
    settings: ExternalSettings,
    bundle: ExternalBundle,
) -> None:
    """Discover public KOL pages through low-frequency search RSS snippets."""
    if not settings.public_web_enabled:
        bundle.statuses.append(
            SourceStatus("Public Web KOL Discovery", "disabled", "Disabled by config")
        )
        return
    if not settings.public_web_queries:
        bundle.statuses.append(
            SourceStatus("Public Web KOL Discovery", "disabled", "No search queries configured")
        )
        return
    accepted = 0
    failures: list[str] = []
    namespace: dict[str, str] = {}
    for query in settings.public_web_queries[:4]:
        try:
            response = session.get(
                "https://www.bing.com/search",
                params={"format": "rss", "q": query},
                timeout=15,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except Exception as exc:
            failures.append(f"{query[:40]}: {type(exc).__name__}")
            continue
        for node in root.findall(".//item")[: settings.public_web_limit_per_query]:
            title = _clip(node.findtext("title", default="", namespaces=namespace), 300)
            text = _clip(node.findtext("description", default="", namespaces=namespace), 700)
            url = str(node.findtext("link", default="", namespaces=namespace) or "")
            published = str(node.findtext("pubDate", default="", namespaces=namespace) or "")
            if not title and not text:
                continue
            source_id = (
                "xiaohongshu_public_search"
                if "xiaohongshu.com" in url.casefold() or "小红书" in f"{title} {text}"
                else "anonymous_social"
            )
            base = ExternalItem(
                item_id=_item_id("public-web", url or title, f"{title} {text}"),
                source="Public web search snippet",
                source_kind="kol",
                title=title or text[:180],
                text=text,
                url=url,
                published=published,
                source_reference=url or f"search query: {query}",
                source_id=source_id,
            )
            matched = False
            for target in targets:
                if _matches_row(base.full_text, target):
                    ticker = str(target["ticker"]).upper()
                    bundle.by_ticker[ticker].items.append(
                        ExternalItem(**{**base.__dict__, "ticker": ticker})
                    )
                    accepted += 1
                    matched = True
            if not matched:
                bundle.global_items.append(base)
    if failures and not accepted:
        status = "error"
    elif failures:
        status = "partial"
    else:
        status = "ok"
    detail = f"{accepted} ticker-linked public snippet(s)"
    if failures:
        detail += "; " + "; ".join(failures)
    bundle.statuses.append(SourceStatus("Public Web KOL Discovery", status, detail))


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: object) -> bool | None:
    if value in (None, "", "unknown"):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "disclosed"}:
        return True
    if text in {"0", "false", "no", "not_disclosed"}:
        return False
    return None


def _collect_per_ticker_source(
    name: str,
    enabled: bool,
    targets: list[dict],
    bundle: ExternalBundle,
    fetcher,
    disabled_detail: str,
) -> None:
    if not enabled:
        bundle.statuses.append(SourceStatus(name, "disabled", disabled_detail))
        return
    success, failures, items = 0, 0, 0
    for row in targets:
        ticker = str(row["ticker"]).upper()
        try:
            fetched = fetcher(ticker)
            bundle.by_ticker[ticker].items.extend(fetched)
            success += 1
            items += len(fetched)
        except Exception:
            failures += 1
    status = "ok" if failures == 0 else ("partial" if success else "error")
    bundle.statuses.append(
        SourceStatus(name, status, f"{success} tickers ok; {failures} failed; {items} items")
    )


def _fetch_nasdaq_news(session: requests.Session, ticker: str, limit: int) -> list[ExternalItem]:
    response = session.get(
        "https://api.nasdaq.com/api/news/topic/articlebysymbol",
        params={"q": f"{ticker}|stocks", "limit": str(limit)},
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/",
        },
        timeout=15,
    )
    response.raise_for_status()
    rows = ((response.json().get("data") or {}).get("rows") or [])
    out: list[ExternalItem] = []
    for row in rows[:limit]:
        title = _clip(row.get("title", ""), 240)
        if not title:
            continue
        primary = str(row.get("primarysymbol", "")).upper()
        related = {str(x).split("|", 1)[0].upper() for x in (row.get("related_symbols") or [])}
        if primary != ticker and ticker not in related and not re.search(
            rf"(?i)(?<![A-Z0-9])\$?{re.escape(ticker)}(?![A-Z0-9])", title
        ):
            continue
        link = str(row.get("url", ""))
        if link.startswith("/"):
            link = "https://www.nasdaq.com" + link
        out.append(
            ExternalItem(
                item_id=_item_id("nasdaq", link or title, title),
                source=f"Nasdaq/{row.get('publisher', 'publisher')}",
                source_kind="news",
                title=title,
                text=_clip(row.get("summary", ""), 500),
                url=link,
                published=str(row.get("created", ""))[:25],
                ticker=ticker,
                credibility=0.75,
                source_id="financial_news",
            )
        )
    return out


def _fetch_stocktwits(session: requests.Session, ticker: str, limit: int) -> list[ExternalItem]:
    response = session.get(
        f"https://api.stocktwits.com/api/2/streams/symbol/{quote_plus(ticker)}.json",
        timeout=12,
    )
    response.raise_for_status()
    out: list[ExternalItem] = []
    for message in (response.json().get("messages") or [])[:limit]:
        body = _clip(message.get("body", ""), 350)
        if not body:
            continue
        user = str((message.get("user") or {}).get("username", ""))
        native_id = str(message.get("id", ""))
        out.append(
            ExternalItem(
                item_id=_item_id("stocktwits", native_id, body),
                source="StockTwits",
                source_kind="social",
                title=body,
                url=(f"https://stocktwits.com/{user}/message/{native_id}" if user and native_id else ""),
                published=str(message.get("created_at", ""))[:25],
                author=user,
                ticker=ticker,
                credibility=0.20,
                engagement=int((message.get("likes") or {}).get("total", 0) or 0),
                source_id="anonymous_social",
            )
        )
    return out


def _fetch_reddit(
    session: requests.Session,
    target: dict,
    settings: ExternalSettings,
) -> list[ExternalItem]:
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    out: list[ExternalItem] = []
    ticker = str(target["ticker"]).upper()
    aliases = [
        str(alias).strip()
        for alias in (target.get("social_aliases") or [])
        if len(str(alias).strip()) >= 4
    ][:2]
    ticker_term = f"${ticker}" if len(ticker) <= 3 else ticker
    query = " OR ".join([ticker_term] + [f'"{alias}"' for alias in aliases])
    for subreddit in settings.reddit_subreddits:
        response = session.get(
            f"https://www.reddit.com/r/{subreddit}/search.rss",
            params={
                "q": query,
                "restrict_sr": "on",
                "sort": "new",
                "t": "week",
                "limit": settings.reddit_limit_per_sub,
            },
            timeout=12,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for entry in root.findall("atom:entry", namespace)[: settings.reddit_limit_per_sub]:
            title_node = entry.find("atom:title", namespace)
            id_node = entry.find("atom:id", namespace)
            published_node = entry.find("atom:published", namespace)
            title = _clip(title_node.text if title_node is not None else "", 260)
            if not title or not _matches_row(title, target):
                continue
            link = ""
            for node in entry.findall("atom:link", namespace):
                if node.attrib.get("href"):
                    link = node.attrib["href"]
                    break
            out.append(
                ExternalItem(
                    item_id=_item_id("reddit", id_node.text if id_node is not None else link, title),
                    source=f"Reddit r/{subreddit}",
                    source_kind="community",
                    title=title,
                    url=link,
                    published=(published_node.text if published_node is not None else "")[:25],
                    ticker=ticker,
                    credibility=0.30,
                    source_id="anonymous_social",
                )
            )
    return out


def _collect_x_discovery(
    session: requests.Session,
    targets: list[dict],
    settings: ExternalSettings,
    bundle: ExternalBundle,
) -> None:
    """Search a bounded ticker/alias set through the official X recent-search API."""
    if not settings.x_discovery_enabled:
        bundle.statuses.append(SourceStatus("X Discovery", "disabled", "Disabled by config"))
        return
    token = os.getenv(settings.x_bearer_token_env, "").strip()
    if not token:
        bundle.statuses.append(
            SourceStatus(
                "X Discovery",
                "blocked",
                f"Missing GitHub secret/environment variable {settings.x_bearer_token_env}",
            )
        )
        return
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "daily-research-agent/2.2",
    }
    known_profiles = {
        str(row.get("username", "")).strip().lstrip("@").casefold(): str(
            row.get("profile_id") or "anonymous_social"
        )
        for row in settings.x_handles
    }
    success = 0
    failures: list[str] = []
    item_count = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.lookback_days)
    for target in targets[: settings.x_discovery_max_tickers]:
        ticker = str(target["ticker"]).upper()
        ticker_term = f"${ticker}" if len(ticker) <= 3 else ticker
        aliases = [
            str(alias).strip()
            for alias in (target.get("social_aliases") or [])
            if len(str(alias).strip()) >= 4
        ][:2]
        terms = [ticker_term] + [f'"{alias}"' for alias in aliases]
        query = f"({' OR '.join(terms)}) -is:retweet"
        try:
            response = session.get(
                "https://api.x.com/2/tweets/search/recent",
                headers=headers,
                params={
                    "query": query,
                    "max_results": settings.x_discovery_results_per_ticker,
                    "start_time": cutoff.isoformat().replace("+00:00", "Z"),
                    "tweet.fields": "created_at,public_metrics,author_id,lang",
                    "expansions": "author_id",
                    "user.fields": "username,name",
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            success += 1
        except Exception as exc:
            failures.append(f"{ticker}: {type(exc).__name__}")
            continue
        users = {
            str(user.get("id", "")): user
            for user in ((payload.get("includes") or {}).get("users") or [])
        }
        for post in payload.get("data") or []:
            text = _clip(post.get("text", ""), 700)
            if not text or not _matches_row(text, target):
                continue
            user = users.get(str(post.get("author_id", "")), {})
            username = str(user.get("username", "")).strip()
            profile_id = known_profiles.get(username.casefold(), "anonymous_social")
            metrics = post.get("public_metrics") or {}
            engagement = sum(
                int(metrics.get(key, 0) or 0)
                for key in ("like_count", "retweet_count", "reply_count", "quote_count")
            )
            native_id = str(post.get("id", ""))
            bundle.by_ticker[ticker].items.append(
                ExternalItem(
                    item_id=_item_id(
                        f"x:{username or 'unknown'}",
                        native_id,
                        text,
                    ),
                    source=f"X discovery/@{username or 'unknown'}",
                    source_kind="kol",
                    title=text,
                    url=(
                        f"https://x.com/{username}/status/{native_id}"
                        if username and native_id
                        else ""
                    ),
                    published=str(post.get("created_at", ""))[:25],
                    author=f"@{username}" if username else "",
                    ticker=ticker,
                    source_id=profile_id,
                    engagement=engagement,
                )
            )
            item_count += 1
    status = "ok" if not failures else ("partial" if success else "error")
    detail = f"{success} ticker search(es) ok; {item_count} matched post(s)"
    if failures:
        detail += "; " + "; ".join(failures)
    bundle.statuses.append(SourceStatus("X Discovery", status, detail))


def _collect_x_kols(
    session: requests.Session,
    targets: list[dict],
    settings: ExternalSettings,
    bundle: ExternalBundle,
) -> None:
    if not settings.x_enabled:
        bundle.statuses.append(SourceStatus("X KOL", "disabled", "Disabled by config"))
        return
    if not settings.x_handles:
        bundle.statuses.append(SourceStatus("X KOL", "blocked", "No KOL handles configured"))
        return
    token = os.getenv(settings.x_bearer_token_env, "").strip()
    if not token:
        bundle.statuses.append(
            SourceStatus(
                "X KOL",
                "blocked",
                f"Missing GitHub secret/environment variable {settings.x_bearer_token_env}",
            )
        )
        return

    headers = {"Authorization": f"Bearer {token}", "User-Agent": "daily-research-agent/2.0"}
    success, failures, post_count = 0, [], 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.lookback_days)
    for handle_cfg in settings.x_handles:
        username = str(handle_cfg.get("username", "")).strip().lstrip("@")
        if not username:
            continue
        label = str(handle_cfg.get("label", username))
        profile_id = str(handle_cfg.get("profile_id") or "anonymous_social")
        try:
            user_response = session.get(
                f"https://api.x.com/2/users/by/username/{username}", headers=headers, timeout=15
            )
            user_response.raise_for_status()
            user_id = str((user_response.json().get("data") or {}).get("id", ""))
            if not user_id:
                raise RuntimeError("username lookup returned no id")
            timeline_response = session.get(
                f"https://api.x.com/2/users/{user_id}/tweets",
                headers=headers,
                params={
                    "max_results": settings.x_max_posts_per_handle,
                    "exclude": "retweets",
                    "tweet.fields": "created_at,public_metrics,lang",
                },
                timeout=20,
            )
            timeline_response.raise_for_status()
            posts = timeline_response.json().get("data", []) or []
            success += 1
        except Exception as exc:
            failures.append(f"@{username}: {type(exc).__name__}")
            continue

        for post in posts:
            text = _clip(post.get("text", ""), 700)
            created_at = str(post.get("created_at", ""))
            try:
                created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created_dt < cutoff:
                    continue
            except ValueError:
                pass
            metrics = post.get("public_metrics") or {}
            engagement = sum(
                int(metrics.get(key, 0) or 0)
                for key in ("like_count", "retweet_count", "reply_count", "quote_count")
            )
            native_id = str(post.get("id", ""))
            base = ExternalItem(
                item_id=_item_id(f"x:{username}", native_id, text),
                source=f"X/{label} (@{username})",
                source_kind="kol",
                title=text,
                url=f"https://x.com/{username}/status/{native_id}" if native_id else "",
                published=created_at[:25],
                author=f"@{username}",
                source_id=profile_id,
                engagement=engagement,
            )
            post_count += 1
            matched = False
            for row in targets:
                if _matches_row(text, row):
                    ticker = str(row["ticker"]).upper()
                    bundle.by_ticker[ticker].items.append(
                        ExternalItem(**{**base.__dict__, "ticker": ticker})
                    )
                    matched = True
            if not matched:
                bundle.global_items.append(base)

    if success and not failures:
        status, detail = "ok", f"Fetched {success} KOL timeline(s); {post_count} recent posts"
    elif success:
        status, detail = "partial", f"{success} ok; {post_count} posts; " + "; ".join(failures)
    else:
        status, detail = "error", "; ".join(failures) or "No valid handles"
    bundle.statuses.append(SourceStatus("X KOL", status, detail))
