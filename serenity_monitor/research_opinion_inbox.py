"""Private opinion/agent digest ingestion with provenance and re-verification.

The module accepts compact, privacy-minimized summaries from Reddit, Quora,
Xiaohongshu exports or external news agents.  Agent-generated prose is never
treated as an original source.  A view receives non-zero research weight only
when the underlying claim is independently corroborated; Xiaohongshu and other
social sources retain zero direct add/open weight even after corroboration.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse


_PRIMARY_CLASSES = {
    "issuer_primary",
    "company_primary",
    "government_primary",
    "regulatory_primary",
}
_INSTITUTIONAL_CLASSES = {
    "major_media",
    "regional_media",
    "industry_media",
    "financial_news",
    "independent_research",
}
_SOCIAL_PLATFORMS = {
    "reddit",
    "quora",
    "xiaohongshu",
    "xhs",
    "x",
    "twitter",
    "stocktwits",
    "forum",
}
_AGENT_PLATFORMS = {
    "agent",
    "news_agent",
    "github_agent",
    "llm_digest",
}
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,}", re.I)


def _clamp(value: float, lower: float, upper: float) -> float:
    number = float(value)
    if not math.isfinite(number):
        return lower
    return min(max(number, lower), upper)


def _aware(value: Any, fallback: dt.datetime) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return fallback
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _text(value: Any, limit: int = 1000) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    return normalized if len(normalized) <= limit else normalized[: limit - 3] + "..."


def _tokens(value: str) -> frozenset[str]:
    return frozenset(token.casefold() for token in _TOKEN_RE.findall(value or ""))


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return ""


def _get(item: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(item, Mapping) and name in item:
            return item[name]
        if hasattr(item, name):
            return getattr(item, name)
    return default


@dataclass(frozen=True)
class OpinionRecord:
    record_id: str
    platform: str
    observed_at: dt.datetime
    claim: str
    ticker: str = ""
    topic: str = ""
    direction: float = 0.0
    horizon_days: int = 20
    author: str = ""
    source_url: str = ""
    origin_urls: tuple[str, ...] = ()
    summary_agent: str = ""
    engagement: float = 0.0
    position_disclosed: bool | None = None
    conflict_disclosed: bool | None = None
    sponsored: bool = False
    invalidation: str = ""

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        as_of: dt.datetime,
    ) -> "OpinionRecord":
        platform = str(data.get("platform") or data.get("source") or "unknown").strip().casefold()
        claim = _text(data.get("claim") or data.get("summary") or data.get("text"), 1200)
        if not claim:
            raise ValueError("opinion claim is required")
        ticker = str(data.get("ticker") or "").strip().upper()
        if ticker and not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,10}", ticker):
            raise ValueError("ticker is invalid")
        observed_at = _aware(data.get("observed_at") or data.get("published"), as_of)
        if observed_at > as_of + dt.timedelta(minutes=5):
            raise ValueError("opinion is future-dated")
        raw_urls = data.get("origin_urls") or data.get("evidence_urls") or ()
        if isinstance(raw_urls, str):
            raw_urls = [raw_urls]
        origin_urls = tuple(
            str(value).strip()
            for value in raw_urls
            if str(value).strip().startswith("https://")
        )
        source_url = str(data.get("source_url") or data.get("url") or "").strip()
        direction = _clamp(float(data.get("direction") or 0.0), -1.0, 1.0)
        horizon_days = max(1, min(int(data.get("horizon_days") or 20), 3650))
        material = "|".join(
            [
                platform,
                observed_at.isoformat(),
                ticker,
                claim,
                source_url,
                ",".join(origin_urls),
            ]
        )
        record_id = str(data.get("record_id") or data.get("item_id") or "").strip()
        if not record_id:
            record_id = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return cls(
            record_id=record_id,
            platform=platform,
            observed_at=observed_at,
            claim=claim,
            ticker=ticker,
            topic=str(data.get("topic") or "").strip().casefold(),
            direction=direction,
            horizon_days=horizon_days,
            author=_text(data.get("author"), 120),
            source_url=source_url,
            origin_urls=origin_urls,
            summary_agent=_text(data.get("summary_agent") or data.get("agent"), 120),
            engagement=max(0.0, float(data.get("engagement") or 0.0)),
            position_disclosed=(
                None
                if data.get("position_disclosed") is None
                else bool(data.get("position_disclosed"))
            ),
            conflict_disclosed=(
                None
                if data.get("conflict_disclosed") is None
                else bool(data.get("conflict_disclosed"))
            ),
            sponsored=bool(data.get("sponsored", False)),
            invalidation=_text(data.get("invalidation"), 400),
        )


@dataclass(frozen=True)
class OpinionAssessment:
    record_id: str
    platform: str
    ticker: str
    claim: str
    direction: float
    verification_status: str
    primary_corroboration: int
    independent_corroboration_groups: int
    direct_decision_weight: float
    downside_overlay_weight: float
    source_url: str
    origin_urls: tuple[str, ...]
    reason: str
    invalidation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "origin_urls": list(self.origin_urls),
        }


@dataclass(frozen=True)
class OpinionInboxResult:
    status: str
    record_count: int
    accepted_count: int
    rejected_count: int
    verified_count: int
    context_only_count: int
    bearish_crowding_score: float
    risk_budget_multiplier: float
    assessments: tuple[OpinionAssessment, ...]
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "record_count": self.record_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "verified_count": self.verified_count,
            "context_only_count": self.context_only_count,
            "bearish_crowding_score": self.bearish_crowding_score,
            "risk_budget_multiplier": self.risk_budget_multiplier,
            "assessments": [item.to_dict() for item in self.assessments],
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def parse_opinion_records(
    payloads: Iterable[Mapping[str, Any]],
    *,
    as_of: dt.datetime | None = None,
    lookback_days: int = 14,
) -> tuple[tuple[OpinionRecord, ...], int]:
    now = as_of or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    now = now.astimezone(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=max(1, int(lookback_days)))
    records: list[OpinionRecord] = []
    rejected = 0
    seen: set[str] = set()
    for payload in payloads:
        try:
            record = OpinionRecord.from_mapping(payload, as_of=now)
            if record.observed_at < cutoff:
                continue
            if record.record_id in seen:
                continue
            seen.add(record.record_id)
            records.append(record)
        except (TypeError, ValueError, OverflowError):
            rejected += 1
    records.sort(key=lambda item: item.observed_at, reverse=True)
    return tuple(records), rejected


def _evidence_material(item: Any) -> tuple[str, str, str, str, str, frozenset[str]]:
    source_class = str(_get(item, "source_class", "source_kind", default="unknown")).casefold()
    group = str(_get(item, "independence_group", default="") or "").casefold()
    source = str(_get(item, "source", "outlet", default="") or "")
    url = str(_get(item, "url", "source_url", default="") or "")
    title = _text(
        f"{_get(item, 'title', default='')} {_get(item, 'text', 'body', default='')}",
        1400,
    )
    if not group:
        group = _domain(url) or re.sub(r"[^a-z0-9]+", "_", source.casefold()).strip("_")
    return source_class, group or "unknown", source, url, title, _tokens(title)


def assess_opinion_inbox(
    records: Sequence[OpinionRecord],
    *,
    evidence_items: Iterable[Any] = (),
    portfolio_tickers: Iterable[str] = (),
    rejected_count: int = 0,
) -> OpinionInboxResult:
    portfolio = {str(value).upper() for value in portfolio_tickers if str(value).strip()}
    evidence = [_evidence_material(item) for item in evidence_items]
    assessments: list[OpinionAssessment] = []
    bearish_values: list[float] = []
    verified_count = 0
    context_count = 0

    for record in records:
        claim_tokens = _tokens(record.claim)
        primary_groups: set[str] = set()
        independent_groups: set[str] = set()
        origin_domains = {_domain(url) for url in record.origin_urls if _domain(url)}
        for source_class, group, _source, url, text, evidence_tokens in evidence:
            ticker_match = bool(
                record.ticker
                and (
                    record.ticker.casefold() in text.casefold()
                    or f"${record.ticker}".casefold() in text.casefold()
                )
            )
            overlap = (
                len(claim_tokens & evidence_tokens) / max(len(claim_tokens), 1)
                if claim_tokens
                else 0.0
            )
            origin_match = _domain(url) in origin_domains if origin_domains else False
            if not (ticker_match or overlap >= 0.18 or origin_match):
                continue
            if source_class in _PRIMARY_CLASSES:
                primary_groups.add(group)
            if source_class in _PRIMARY_CLASSES | _INSTITUTIONAL_CLASSES:
                independent_groups.add(group)

        primary_count = len(primary_groups)
        independent_count = len(independent_groups)
        verified = primary_count >= 1 or independent_count >= 2
        platform = record.platform
        agent_generated = platform in _AGENT_PLATFORMS or bool(record.summary_agent)
        social = platform in _SOCIAL_PLATFORMS
        has_origin = bool(record.origin_urls or record.source_url)
        if verified and has_origin:
            verification_status = "verified"
            verified_count += 1
        elif has_origin:
            verification_status = "unverified_lead"
            context_count += 1
        else:
            verification_status = "context_only_no_origin"
            context_count += 1

        direct_weight = 0.0
        if verification_status == "verified" and not agent_generated and not social:
            direct_weight = 0.10
        # Social and agent summaries can tighten risk through crowding/manipulation
        # but cannot independently create ADD/OPEN.
        downside_weight = 0.0
        if record.direction < 0:
            credibility = 1.0 if verification_status == "verified" else 0.35
            disclosure = 1.0
            if record.sponsored:
                disclosure *= 0.35
            if record.position_disclosed is False or record.conflict_disclosed is False:
                disclosure *= 0.65
            engagement_scale = min(1.0, math.log1p(record.engagement) / 12.0)
            downside_weight = min(
                0.05,
                abs(record.direction)
                * credibility
                * disclosure
                * (0.50 + 0.50 * engagement_scale)
                * 0.05,
            )
            bearish_values.append(downside_weight)

        if agent_generated:
            reason = (
                "Agent summary is secondary synthesis; original links were "
                + ("independently corroborated." if verified else "not sufficiently corroborated.")
            )
        elif social:
            reason = (
                "Social view is retained as a crowding/claim lead; direct add/open weight is zero."
            )
        else:
            reason = (
                "Claim is independently corroborated."
                if verified
                else "Claim remains a lead pending primary or two-group corroboration."
            )
        if record.ticker and portfolio and record.ticker not in portfolio:
            reason += " The ticker is outside the current portfolio and remains watchlist context."

        assessments.append(
            OpinionAssessment(
                record_id=record.record_id,
                platform=platform,
                ticker=record.ticker,
                claim=record.claim,
                direction=round(record.direction, 6),
                verification_status=verification_status,
                primary_corroboration=primary_count,
                independent_corroboration_groups=independent_count,
                direct_decision_weight=round(direct_weight, 6),
                downside_overlay_weight=round(downside_weight, 6),
                source_url=record.source_url,
                origin_urls=record.origin_urls,
                reason=reason,
                invalidation=record.invalidation,
            )
        )

    crowding = min(1.0, sum(bearish_values) / 0.10) if bearish_values else 0.0
    multiplier = _clamp(1.0 - min(0.05, 0.05 * crowding), 0.95, 1.0)
    if not records:
        status = "no_data"
    elif verified_count:
        status = "ok" if rejected_count == 0 else "partial"
    else:
        status = "context_only"
    warnings = (
        "Agent-generated summaries are never original evidence.",
        "Xiaohongshu, Reddit, Quora and other social views have zero direct ADD/OPEN weight.",
        "A view is verified only by a primary source or at least two independent institutional groups.",
        "Missing community data is reported as missing, not neutral.",
    )
    return OpinionInboxResult(
        status=status,
        record_count=len(records) + int(rejected_count),
        accepted_count=len(records),
        rejected_count=int(rejected_count),
        verified_count=verified_count,
        context_only_count=context_count,
        bearish_crowding_score=round(crowding, 6),
        risk_budget_multiplier=round(multiplier, 6),
        assessments=tuple(assessments),
        warnings=warnings,
    )


__all__ = [
    "OpinionAssessment",
    "OpinionInboxResult",
    "OpinionRecord",
    "assess_opinion_inbox",
    "parse_opinion_records",
]
