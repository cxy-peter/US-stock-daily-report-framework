"""Deterministic, offline aggregation of authorized social-attention records.

This module is intentionally a *model boundary*, not a collector.  It contains
no HTTP client, browser, login, cookie, scraping, or platform-specific access
code.  Callers must provide already-authorized records whose author/content
identifiers have been irreversibly transformed before they cross this boundary.

All score arithmetic uses :class:`~decimal.Decimal` under a fixed local
context.  Missing platforms are omitted and the remaining healthy platform
priors are re-normalized; absence is never interpreted as neutral sentiment.
Records that are advertisements, duplicates, or coordinated activity remain in
the manipulation audit but are isolated from attention and sentiment scores.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from typing import Iterable, Mapping, Sequence


ZERO = Decimal("0")
ONE = Decimal("1")
SOCIAL_DECISION_WEIGHT_CAP = Decimal("0.05")
XHS_INITIAL_EXECUTION_WEIGHT = ZERO
SOCIAL_EVIDENCE_GROUP = "social_media"

SOCIAL_TOPIC_TAXONOMY: tuple[str, ...] = (
    "broad_market",
    "crypto_assets",
    "dividend_equity",
    "nasdaq_100",
    "semiconductors",
    "sp_500",
)

DEFAULT_PLATFORM_PRIORS: tuple[tuple[str, Decimal], ...] = (
    ("xiaohongshu", Decimal("0.40")),
    ("x", Decimal("0.35")),
    ("reddit", Decimal("0.15")),
    ("other", Decimal("0.10")),
)

AUTHORIZED_RIGHTS_STATUSES = frozenset(
    {
        "authorized",
        "licensed",
        "user_owned",
        "platform_data_export",
        "creator_consent",
        "authorized_api_export",
        "research_data_agreement",
    }
)
SOURCE_HEALTH_STATUSES = frozenset(
    {"healthy", "degraded", "unavailable", "quarantined"}
)

_PLATFORM_ALIASES = {
    "xiaohongshu": "xiaohongshu",
    "xhs": "xiaohongshu",
    "小红书": "xiaohongshu",
    "x": "x",
    "twitter": "x",
    "reddit": "reddit",
    "other": "other",
}
_HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.^-]{0,31}$")
_TOPIC_TAXONOMY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_OUTPUT_QUANTUM = Decimal("0.000000000001")
_CALC_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_MAX_ENGAGEMENT_COUNT = Decimal("1000000000000000000")


class SocialHeatValidationError(ValueError):
    """Raised when an input cannot safely enter the model boundary."""


def _require_decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise SocialHeatValidationError(f"{field_name} must be Decimal")
    if not value.is_finite():
        raise SocialHeatValidationError(f"{field_name} must be finite")
    return value


def _require_non_negative_decimal(value: object, field_name: str) -> Decimal:
    result = _require_decimal(value, field_name)
    if result < ZERO:
        raise SocialHeatValidationError(f"{field_name} must be non-negative")
    return result


def _require_count_decimal(value: object, field_name: str) -> Decimal:
    result = _require_non_negative_decimal(value, field_name)
    if result != result.to_integral_value():
        raise SocialHeatValidationError(f"{field_name} must be an integral Decimal count")
    if result > _MAX_ENGAGEMENT_COUNT:
        raise SocialHeatValidationError(f"{field_name} exceeds the safe count bound")
    return result


def _require_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise SocialHeatValidationError(f"{field_name} must be bool")
    return value


def _require_aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise SocialHeatValidationError(f"{field_name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise SocialHeatValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _normalize_platform(value: object) -> str:
    text = str(value).strip().casefold()
    try:
        return _PLATFORM_ALIASES[text]
    except KeyError as exc:
        raise SocialHeatValidationError(
            "platform must be xiaohongshu, x, reddit, or explicit other"
        ) from exc


def _normalize_status(value: object, field_name: str) -> str:
    text = str(value).strip().casefold()
    if not text or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", text):
        raise SocialHeatValidationError(f"{field_name} is invalid")
    return text


def _normalize_hash(value: object, field_name: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    text = str(value).strip().casefold()
    if not _HASH_RE.fullmatch(text):
        raise SocialHeatValidationError(
            f"{field_name} must be an irreversible SHA-256 identifier"
        )
    return text.removeprefix("sha256:")


def _normalize_topic(value: object) -> str:
    raw = str(value)
    text = raw.strip()
    if raw != text:
        raise SocialHeatValidationError(
            "topic must be a lowercase ASCII taxonomy identifier"
        )
    if not _TOPIC_TAXONOMY_RE.fullmatch(text):
        raise SocialHeatValidationError(
            "topic must be a lowercase ASCII taxonomy identifier"
        )
    if text not in SOCIAL_TOPIC_TAXONOMY:
        raise SocialHeatValidationError("topic is outside the built-in closed taxonomy")
    return text


def _normalize_ticker(value: object) -> str:
    text = str(value).strip().upper()
    if not _TICKER_RE.fullmatch(text):
        raise SocialHeatValidationError("ticker is invalid")
    return text


def _seconds(value: timedelta) -> Decimal:
    """Return exact Decimal seconds without ``timedelta.total_seconds`` float."""

    return (
        Decimal(value.days) * Decimal("86400")
        + Decimal(value.seconds)
        + Decimal(value.microseconds) / Decimal("1000000")
    )


def _quantize(value: Decimal) -> Decimal:
    with localcontext(_CALC_CONTEXT):
        return value.quantize(_OUTPUT_QUANTUM)


def _ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    left = numerator if isinstance(numerator, Decimal) else Decimal(numerator)
    right = denominator if isinstance(denominator, Decimal) else Decimal(denominator)
    if right == ZERO:
        return ZERO
    with localcontext(_CALC_CONTEXT):
        return left / right


def _clamp(value: Decimal, minimum: Decimal = ZERO, maximum: Decimal = ONE) -> Decimal:
    return min(max(value, minimum), maximum)


def _ln1p(value: Decimal) -> Decimal:
    with localcontext(_CALC_CONTEXT):
        return (ONE + value).ln()


def _freshness_weight(age_seconds: Decimal, half_life_hours: Decimal) -> Decimal:
    if age_seconds <= ZERO:
        return ONE
    with localcontext(_CALC_CONTEXT):
        age_hours = age_seconds / Decimal("3600")
        exponent = -(Decimal("2").ln() * age_hours / half_life_hours)
        return exponent.exp()


def _decimal_text(value: Decimal) -> str:
    if value == ZERO:
        return "0"
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class EngagementBreakdown:
    """Auditable engagement components; all values must already be Decimal."""

    likes: Decimal
    comments: Decimal
    shares: Decimal
    saves: Decimal
    views: Decimal

    def __post_init__(self) -> None:
        for name in ("likes", "comments", "shares", "saves", "views"):
            object.__setattr__(
                self,
                name,
                _require_count_decimal(getattr(self, name), f"engagement.{name}"),
            )


@dataclass(frozen=True)
class SocialObservation:
    """One pre-authorized, text-free social observation.

    ``author_id_hash``, ``content_id_hash`` and an optional cluster identifier
    must be SHA-256/HMAC-SHA-256 style irreversible identifiers.  Raw handles,
    URLs, post bodies, and account names do not belong in this model.
    """

    platform: str
    rights_status: str
    source_health: str
    observed_at: datetime
    author_id_hash: str
    content_id_hash: str
    topic: str
    ticker: str
    sentiment: Decimal
    engagement: EngagementBreakdown
    is_ad: bool
    is_duplicate: bool
    is_coordinated: bool
    cross_platform_cluster_hash: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "platform", _normalize_platform(self.platform))
        object.__setattr__(
            self, "rights_status", _normalize_status(self.rights_status, "rights_status")
        )
        health = _normalize_status(self.source_health, "source_health")
        if health not in SOURCE_HEALTH_STATUSES:
            raise SocialHeatValidationError("source_health is unsupported")
        object.__setattr__(self, "source_health", health)
        object.__setattr__(
            self, "observed_at", _require_aware_datetime(self.observed_at, "observed_at")
        )
        object.__setattr__(
            self,
            "author_id_hash",
            _normalize_hash(self.author_id_hash, "author_id_hash"),
        )
        object.__setattr__(
            self,
            "content_id_hash",
            _normalize_hash(self.content_id_hash, "content_id_hash"),
        )
        object.__setattr__(self, "topic", _normalize_topic(self.topic))
        object.__setattr__(self, "ticker", _normalize_ticker(self.ticker))
        sentiment = _require_decimal(self.sentiment, "sentiment")
        if not Decimal("-1") <= sentiment <= ONE:
            raise SocialHeatValidationError("sentiment must be between -1 and 1")
        object.__setattr__(self, "sentiment", sentiment)
        if not isinstance(self.engagement, EngagementBreakdown):
            raise SocialHeatValidationError("engagement must be EngagementBreakdown")
        for name in ("is_ad", "is_duplicate", "is_coordinated"):
            object.__setattr__(self, name, _require_bool(getattr(self, name), name))
        object.__setattr__(
            self,
            "cross_platform_cluster_hash",
            _normalize_hash(
                self.cross_platform_cluster_hash,
                "cross_platform_cluster_hash",
                optional=True,
            ),
        )


@dataclass(frozen=True)
class SocialHeatSettings:
    """Conservative model settings with a hard five-percent decision cap."""

    platform_priors: tuple[tuple[str, Decimal], ...] = DEFAULT_PLATFORM_PRIORS
    current_window_hours: int = 24
    baseline_days: int = 30
    freshness_half_life_hours: Decimal = Decimal("72")
    engagement_hard_cap: Decimal = Decimal("1000000")
    quarantine_threshold: Decimal = Decimal("0.60")
    final_social_decision_weight: Decimal = SOCIAL_DECISION_WEIGHT_CAP
    xhs_execution_weight: Decimal = XHS_INITIAL_EXECUTION_WEIGHT
    max_records: int = 100_000
    allowed_topics: tuple[str, ...] = SOCIAL_TOPIC_TAXONOMY

    def __post_init__(self) -> None:
        if type(self.current_window_hours) is not int or self.current_window_hours < 1:
            raise SocialHeatValidationError("current_window_hours must be positive int")
        if type(self.baseline_days) is not int or self.baseline_days != 30:
            raise SocialHeatValidationError("baseline_days must remain exactly 30")
        if type(self.max_records) is not int or self.max_records < 1:
            raise SocialHeatValidationError("max_records must be positive int")
        half_life = _require_decimal(
            self.freshness_half_life_hours, "freshness_half_life_hours"
        )
        cap = _require_decimal(self.engagement_hard_cap, "engagement_hard_cap")
        threshold = _require_decimal(self.quarantine_threshold, "quarantine_threshold")
        final_weight = _require_decimal(
            self.final_social_decision_weight, "final_social_decision_weight"
        )
        xhs_weight = _require_decimal(self.xhs_execution_weight, "xhs_execution_weight")
        if not Decimal("0.000001") <= half_life <= Decimal("876000"):
            raise SocialHeatValidationError("freshness half life is outside safe bounds")
        if not ONE <= cap <= Decimal("1000000000000000000"):
            raise SocialHeatValidationError("engagement cap is outside safe bounds")
        if not ZERO < threshold <= Decimal("0.60"):
            raise SocialHeatValidationError(
                "quarantine_threshold must be in (0, 0.60]"
            )
        if not ZERO <= final_weight <= SOCIAL_DECISION_WEIGHT_CAP:
            raise SocialHeatValidationError(
                "final_social_decision_weight may not exceed 0.05"
            )
        if xhs_weight != ZERO:
            raise SocialHeatValidationError("xhs_execution_weight must remain zero")

        if isinstance(self.allowed_topics, (str, bytes)):
            raise SocialHeatValidationError("allowed_topics must be a closed topic sequence")
        try:
            allowed_topics = tuple(_normalize_topic(item) for item in self.allowed_topics)
        except TypeError as exc:
            raise SocialHeatValidationError(
                "allowed_topics must be a closed topic sequence"
            ) from exc
        if not allowed_topics:
            raise SocialHeatValidationError("allowed_topics may not be empty")
        if len(allowed_topics) != len(set(allowed_topics)):
            raise SocialHeatValidationError("allowed_topics may not contain duplicates")
        object.__setattr__(self, "allowed_topics", tuple(sorted(allowed_topics)))

        priors: list[tuple[str, Decimal]] = []
        seen: set[str] = set()
        for raw_platform, raw_weight in self.platform_priors:
            platform = _normalize_platform(raw_platform)
            if platform in seen:
                raise SocialHeatValidationError("platform_priors contain duplicates")
            seen.add(platform)
            weight = _require_decimal(raw_weight, f"platform_priors.{platform}")
            if weight < ZERO:
                raise SocialHeatValidationError("platform prior may not be negative")
            priors.append((platform, weight))
        if seen != {"xiaohongshu", "x", "reddit", "other"}:
            raise SocialHeatValidationError("platform_priors must cover all four platforms")
        if sum((weight for _, weight in priors), ZERO) != ONE:
            raise SocialHeatValidationError("platform_priors must sum exactly to one")
        object.__setattr__(self, "platform_priors", tuple(priors))

    @property
    def priors_by_platform(self) -> Mapping[str, Decimal]:
        return dict(self.platform_priors)


@dataclass(frozen=True)
class PlatformHeat:
    platform: str
    prior_weight: Decimal
    normalized_attention_weight: Decimal
    normalized_execution_weight: Decimal
    execution_multiplier: Decimal
    raw_current_count: int
    clean_current_count: int
    author_count: int
    author_entropy: Decimal
    independent_content_count: int
    baseline_growth_30d: Decimal | None
    log_engagement: Decimal
    sentiment_mean: Decimal | None
    sentiment_disagreement: Decimal | None
    topic_concentration: Decimal
    ad_rate: Decimal
    duplicate_rate: Decimal
    coordinated_rate: Decimal
    cross_platform_overlap: Decimal
    first_seen_at: datetime | None
    half_life_hours: Decimal
    attention_score: Decimal
    manipulation_risk: Decimal
    quarantine: bool


@dataclass(frozen=True)
class TopicHeat:
    topic: str
    ticker: str
    independent_content_count: int
    attention_share: Decimal
    sentiment_mean: Decimal | None
    sentiment_disagreement: Decimal | None
    first_seen_at: datetime
    half_life_hours: Decimal


@dataclass(frozen=True)
class SocialHeatResult:
    status: str
    as_of: datetime
    eligible_input_digest: str
    total_input_count: int
    authorized_healthy_count: int
    excluded_rights_count: int
    excluded_source_health_count: int
    current_count: int
    clean_current_count: int
    author_count: int
    author_entropy: Decimal
    independent_content_count: int
    baseline_growth_30d: Decimal | None
    log_engagement: Decimal
    sentiment_mean: Decimal | None
    sentiment_disagreement: Decimal | None
    topic_concentration: Decimal
    ad_rate: Decimal
    duplicate_rate: Decimal
    coordinated_rate: Decimal
    cross_platform_overlap: Decimal
    first_seen_at: datetime | None
    half_life_hours: Decimal
    attention_score: Decimal
    manipulation_risk: Decimal
    quarantine: bool
    coverage: Decimal
    platform_weights: tuple[tuple[str, Decimal], ...]
    execution_platform_weights: tuple[tuple[str, Decimal], ...]
    platforms: tuple[PlatformHeat, ...]
    topics: tuple[TopicHeat, ...]
    decision_weight_cap: Decimal
    decision_contribution: Decimal
    independence_group: str = field(default=SOCIAL_EVIDENCE_GROUP, init=False)
    research_only: bool = field(default=True, init=False)
    can_trigger_open: bool = field(default=False, init=False)
    can_trigger_add: bool = field(default=False, init=False)
    can_trigger_trim: bool = field(default=False, init=False)
    can_trigger_exit: bool = field(default=False, init=False)
    can_increase_dca: bool = field(default=False, init=False)


@dataclass(frozen=True)
class _Metrics:
    raw_count: int
    clean_count: int
    author_count: int
    author_entropy: Decimal
    independent_content_count: int
    baseline_growth: Decimal | None
    log_engagement: Decimal
    sentiment_mean: Decimal | None
    sentiment_disagreement: Decimal | None
    topic_concentration: Decimal
    ad_rate: Decimal
    duplicate_rate: Decimal
    coordinated_rate: Decimal
    cross_overlap: Decimal
    first_seen_at: datetime | None
    attention_score: Decimal
    manipulation_risk: Decimal


def _record_key(item: SocialObservation) -> tuple[object, ...]:
    return (
        item.observed_at,
        item.platform,
        item.author_id_hash,
        item.content_id_hash,
        item.topic,
        item.ticker,
        item.cross_platform_cluster_hash or "",
        item.rights_status,
        item.source_health,
        item.is_ad,
        item.is_duplicate,
        item.is_coordinated,
        _decimal_text(item.sentiment),
        _decimal_text(item.engagement.likes),
        _decimal_text(item.engagement.comments),
        _decimal_text(item.engagement.shares),
        _decimal_text(item.engagement.saves),
        _decimal_text(item.engagement.views),
    )


def _eligible_digest(records: Sequence[SocialObservation]) -> str:
    payload = [
        {
            "platform": item.platform,
            "rights_status": item.rights_status,
            "source_health": item.source_health,
            "observed_at": _rfc3339(item.observed_at),
            "author_id_hash": item.author_id_hash,
            "content_id_hash": item.content_id_hash,
            "topic": item.topic,
            "ticker": item.ticker,
            "sentiment": _decimal_text(item.sentiment),
            "engagement": {
                name: _decimal_text(getattr(item.engagement, name))
                for name in ("likes", "comments", "shares", "saves", "views")
            },
            "is_ad": item.is_ad,
            "is_duplicate": item.is_duplicate,
            "is_coordinated": item.is_coordinated,
            "cross_platform_cluster_hash": item.cross_platform_cluster_hash,
        }
        for item in sorted(records, key=_record_key)
    ]
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _weighted_engagement(item: SocialObservation, hard_cap: Decimal) -> Decimal:
    engagement = item.engagement
    with localcontext(_CALC_CONTEXT):
        raw = (
            engagement.likes
            + engagement.comments * Decimal("2")
            + engagement.shares * Decimal("3")
            + engagement.saves * Decimal("2")
            + engagement.views * Decimal("0.02")
        )
        return min(raw, hard_cap)


def _logical_content_id(item: SocialObservation) -> str:
    return item.cross_platform_cluster_hash or item.content_id_hash


def _risk_rates(
    raw_current: Sequence[SocialObservation],
    duplicate_content_ids: frozenset[str],
    overlap_clusters: frozenset[str],
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    count = len(raw_current)
    if count == 0:
        return ZERO, ZERO, ZERO, ZERO, ZERO
    ad_rate = _ratio(sum(item.is_ad for item in raw_current), count)
    duplicate_rate = _ratio(
        sum(
            item.is_duplicate or item.content_id_hash in duplicate_content_ids
            for item in raw_current
        ),
        count,
    )
    coordinated_rate = _ratio(sum(item.is_coordinated for item in raw_current), count)
    overlap_rate = _ratio(
        sum(
            item.cross_platform_cluster_hash in overlap_clusters
            for item in raw_current
            if item.cross_platform_cluster_hash is not None
        ),
        count,
    )
    with localcontext(_CALC_CONTEXT):
        blended = (
            ad_rate * Decimal("0.25")
            + duplicate_rate * Decimal("0.30")
            + coordinated_rate * Decimal("0.35")
            + overlap_rate * Decimal("0.10")
        )
    manipulation = max(ad_rate, duplicate_rate, coordinated_rate, overlap_rate, blended)
    return tuple(
        _quantize(value)
        for value in (ad_rate, duplicate_rate, coordinated_rate, overlap_rate, manipulation)
    )  # type: ignore[return-value]


def _clean_records(
    records: Sequence[SocialObservation], duplicate_content_ids: frozenset[str]
) -> tuple[SocialObservation, ...]:
    return tuple(
        item
        for item in records
        if not (
            item.is_ad
            or item.is_duplicate
            or item.is_coordinated
            or item.content_id_hash in duplicate_content_ids
        )
    )


def _content_multiplicity(records: Sequence[SocialObservation]) -> Mapping[str, int]:
    return Counter(_logical_content_id(item) for item in records)


def _record_mass(
    item: SocialObservation,
    *,
    as_of: datetime,
    settings: SocialHeatSettings,
    multiplicity: Mapping[str, int],
    decay: bool,
) -> Decimal:
    value = _ln1p(_weighted_engagement(item, settings.engagement_hard_cap))
    divisor = Decimal(multiplicity[_logical_content_id(item)])
    with localcontext(_CALC_CONTEXT):
        value /= divisor
        if decay:
            value *= _freshness_weight(
                _seconds(as_of - item.observed_at), settings.freshness_half_life_hours
            )
        return value


def _entropy(records: Sequence[SocialObservation]) -> Decimal:
    counts = Counter(item.author_id_hash for item in records)
    if len(counts) <= 1:
        return ZERO
    total = Decimal(len(records))
    with localcontext(_CALC_CONTEXT):
        entropy = ZERO
        for count in counts.values():
            probability = Decimal(count) / total
            entropy -= probability * probability.ln()
        return _quantize(entropy / Decimal(len(counts)).ln())


def _weighted_sentiment(
    records: Sequence[SocialObservation], masses: Mapping[int, Decimal]
) -> tuple[Decimal | None, Decimal | None]:
    if not records:
        return None, None
    weights = [masses[id(item)] for item in records]
    total = sum(weights, ZERO)
    if total == ZERO:
        weights = [ONE for _ in records]
        total = Decimal(len(records))
    with localcontext(_CALC_CONTEXT):
        mean = sum(
            (item.sentiment * weight for item, weight in zip(records, weights)), ZERO
        ) / total
        variance = sum(
            (
                weight * (item.sentiment - mean) * (item.sentiment - mean)
                for item, weight in zip(records, weights)
            ),
            ZERO,
        ) / total
        return _quantize(mean), _quantize(variance.sqrt())


def _topic_concentration(
    records: Sequence[SocialObservation], masses: Mapping[int, Decimal]
) -> Decimal:
    if not records:
        return ZERO
    totals: defaultdict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for item in records:
        totals[(item.topic, item.ticker)] += masses[id(item)]
    grand_total = sum(totals.values(), ZERO)
    if grand_total == ZERO:
        counts = Counter((item.topic, item.ticker) for item in records)
        grand_total = Decimal(len(records))
        totals = defaultdict(lambda: ZERO, {key: Decimal(value) for key, value in counts.items()})
    with localcontext(_CALC_CONTEXT):
        return _quantize(
            sum(((value / grand_total) ** 2 for value in totals.values()), ZERO)
        )


def _attention_score(
    *,
    decayed_mass: Decimal,
    independent_content_count: int,
    growth: Decimal | None,
    author_entropy: Decimal,
    manipulation_risk: Decimal,
) -> Decimal:
    if independent_content_count == 0:
        return ZERO
    with localcontext(_CALC_CONTEXT):
        engagement_component = decayed_mass / (decayed_mass + Decimal("10"))
        breadth = Decimal(independent_content_count) / (
            Decimal(independent_content_count) + Decimal("5")
        )
        if growth is None:
            growth_component = Decimal("0.5")
        else:
            ratio = max(ZERO, growth + ONE)
            growth_component = ZERO if ratio == ZERO else ratio / (ONE + ratio)
        raw = (
            engagement_component * Decimal("0.45")
            + breadth * Decimal("0.25")
            + growth_component * Decimal("0.15")
            + author_entropy * Decimal("0.15")
        )
        return _quantize(_clamp(raw * (ONE - manipulation_risk)))


def _calculate_metrics(
    *,
    raw_current: Sequence[SocialObservation],
    clean_current: Sequence[SocialObservation],
    clean_baseline: Sequence[SocialObservation],
    clean_history: Sequence[SocialObservation],
    as_of: datetime,
    settings: SocialHeatSettings,
    duplicate_content_ids: frozenset[str],
    overlap_clusters: frozenset[str],
) -> _Metrics:
    ad_rate, duplicate_rate, coordinated_rate, overlap_rate, manipulation = _risk_rates(
        raw_current, duplicate_content_ids, overlap_clusters
    )
    multiplicity = _content_multiplicity(clean_current)
    current_masses = {
        id(item): _record_mass(
            item,
            as_of=as_of,
            settings=settings,
            multiplicity=multiplicity,
            decay=False,
        )
        for item in clean_current
    }
    current_log_engagement = sum(current_masses.values(), ZERO)
    decayed_mass = sum(
        (
            _record_mass(
                item,
                as_of=as_of,
                settings=settings,
                multiplicity=multiplicity,
                decay=True,
            )
            for item in clean_current
        ),
        ZERO,
    )

    baseline_multiplicity = _content_multiplicity(clean_baseline)
    baseline_total = sum(
        (
            _record_mass(
                item,
                as_of=as_of,
                settings=settings,
                multiplicity=baseline_multiplicity,
                decay=False,
            )
            for item in clean_baseline
        ),
        ZERO,
    )
    baseline_growth: Decimal | None = None
    if baseline_total > ZERO:
        with localcontext(_CALC_CONTEXT):
            baseline_daily = baseline_total / Decimal(settings.baseline_days)
            baseline_growth = _quantize(current_log_engagement / baseline_daily - ONE)

    author_entropy = _entropy(clean_current)
    sentiment_mean, disagreement = _weighted_sentiment(clean_current, current_masses)
    concentration = _topic_concentration(clean_current, current_masses)
    independent_content = len({_logical_content_id(item) for item in clean_current})
    attention = _attention_score(
        decayed_mass=decayed_mass,
        independent_content_count=independent_content,
        growth=baseline_growth,
        author_entropy=author_entropy,
        manipulation_risk=manipulation,
    )
    first_seen = min((item.observed_at for item in clean_history), default=None)
    return _Metrics(
        raw_count=len(raw_current),
        clean_count=len(clean_current),
        author_count=len({item.author_id_hash for item in clean_current}),
        author_entropy=author_entropy,
        independent_content_count=independent_content,
        baseline_growth=baseline_growth,
        log_engagement=_quantize(current_log_engagement),
        sentiment_mean=sentiment_mean,
        sentiment_disagreement=disagreement,
        topic_concentration=concentration,
        ad_rate=ad_rate,
        duplicate_rate=duplicate_rate,
        coordinated_rate=coordinated_rate,
        cross_overlap=overlap_rate,
        first_seen_at=first_seen,
        attention_score=attention,
        manipulation_risk=manipulation,
    )


def _validate_cluster_consistency(records: Sequence[SocialObservation]) -> None:
    meanings: dict[str, tuple[str, str]] = {}
    for item in records:
        cluster = item.cross_platform_cluster_hash
        if cluster is None:
            continue
        meaning = (item.topic, item.ticker)
        prior = meanings.setdefault(cluster, meaning)
        if prior != meaning:
            raise SocialHeatValidationError(
                "cross-platform cluster has inconsistent topic or ticker"
            )


def _build_topic_results(
    records: Sequence[SocialObservation],
    history: Sequence[SocialObservation],
    as_of: datetime,
    settings: SocialHeatSettings,
) -> tuple[TopicHeat, ...]:
    if not records:
        return ()
    multiplicity = _content_multiplicity(records)
    masses = {
        id(item): _record_mass(
            item,
            as_of=as_of,
            settings=settings,
            multiplicity=multiplicity,
            decay=True,
        )
        for item in records
    }
    total_mass = sum(masses.values(), ZERO)
    if total_mass == ZERO:
        masses = {id(item): ONE / Decimal(multiplicity[_logical_content_id(item)]) for item in records}
        total_mass = sum(masses.values(), ZERO)
    grouped: defaultdict[tuple[str, str], list[SocialObservation]] = defaultdict(list)
    for item in records:
        grouped[(item.topic, item.ticker)].append(item)
    results: list[TopicHeat] = []
    for (topic, ticker), items in sorted(grouped.items()):
        sentiment, disagreement = _weighted_sentiment(items, masses)
        first_seen = min(
            item.observed_at
            for item in history
            if item.topic == topic and item.ticker == ticker
        )
        share = sum((masses[id(item)] for item in items), ZERO) / total_mass
        results.append(
            TopicHeat(
                topic=topic,
                ticker=ticker,
                independent_content_count=len(
                    {_logical_content_id(item) for item in items}
                ),
                attention_share=_quantize(share),
                sentiment_mean=sentiment,
                sentiment_disagreement=disagreement,
                first_seen_at=first_seen,
                half_life_hours=settings.freshness_half_life_hours,
            )
        )
    return tuple(results)


def _combine_platform_sentiment(
    platforms: Sequence[PlatformHeat],
) -> tuple[Decimal | None, Decimal | None]:
    usable = tuple(
        item
        for item in platforms
        if not item.quarantine
        and item.normalized_attention_weight > ZERO
        and item.sentiment_mean is not None
    )
    if not usable:
        return None, None
    total_weight = sum((item.normalized_attention_weight for item in usable), ZERO)
    with localcontext(_CALC_CONTEXT):
        mean = sum(
            (
                item.normalized_attention_weight * item.sentiment_mean
                for item in usable
                if item.sentiment_mean is not None
            ),
            ZERO,
        ) / total_weight
        variance = ZERO
        for item in usable:
            assert item.sentiment_mean is not None
            within_variance = (
                ZERO
                if item.sentiment_disagreement is None
                else item.sentiment_disagreement * item.sentiment_disagreement
            )
            variance += item.normalized_attention_weight * (
                within_variance
                + (item.sentiment_mean - mean) * (item.sentiment_mean - mean)
            )
        variance /= total_weight
        return _quantize(mean), _quantize(variance.sqrt())


def _analyze_social_heat(
    observations: Iterable[SocialObservation],
    *,
    as_of: datetime,
    settings: SocialHeatSettings | None = None,
) -> SocialHeatResult:
    """Analyze an authorized offline snapshot without performing I/O.

    Unknown/unauthorized rights and non-healthy source rows are counted for the
    audit boundary but never enter the score.  A future timestamp, malformed
    record, inconsistent cross-platform cluster, or float-valued model input is
    rejected fail-closed.
    """

    active_settings = settings or SocialHeatSettings()
    if not isinstance(active_settings, SocialHeatSettings):
        raise SocialHeatValidationError("settings must be SocialHeatSettings")
    cutoff = _require_aware_datetime(as_of, "as_of")
    try:
        records = tuple(observations)
    except TypeError as exc:
        raise SocialHeatValidationError("observations must be iterable") from exc
    if len(records) > active_settings.max_records:
        raise SocialHeatValidationError("observation limit exceeded")
    if any(not isinstance(item, SocialObservation) for item in records):
        raise SocialHeatValidationError("observations must contain SocialObservation")
    records = tuple(sorted(records, key=_record_key))
    if any(item.topic not in active_settings.allowed_topics for item in records):
        raise SocialHeatValidationError("topic is not allowed by the configured taxonomy")
    if any(item.observed_at > cutoff for item in records):
        raise SocialHeatValidationError("future observation is not allowed")

    excluded_rights = sum(
        item.rights_status not in AUTHORIZED_RIGHTS_STATUSES for item in records
    )
    rights_eligible = tuple(
        item for item in records if item.rights_status in AUTHORIZED_RIGHTS_STATUSES
    )
    excluded_health = sum(item.source_health != "healthy" for item in rights_eligible)
    eligible = tuple(item for item in rights_eligible if item.source_health == "healthy")
    _validate_cluster_consistency(eligible)

    content_counts = Counter(item.content_id_hash for item in eligible)
    duplicate_ids = frozenset(key for key, value in content_counts.items() if value > 1)
    cluster_platforms: defaultdict[str, set[str]] = defaultdict(set)
    for item in eligible:
        if item.cross_platform_cluster_hash is not None:
            cluster_platforms[item.cross_platform_cluster_hash].add(item.platform)
    overlap_clusters = frozenset(
        cluster for cluster, platforms in cluster_platforms.items() if len(platforms) > 1
    )

    current_start = cutoff - timedelta(hours=active_settings.current_window_hours)
    baseline_start = current_start - timedelta(days=active_settings.baseline_days)
    current = tuple(item for item in eligible if current_start <= item.observed_at <= cutoff)
    baseline = tuple(
        item for item in eligible if baseline_start <= item.observed_at < current_start
    )
    clean_all = _clean_records(eligible, duplicate_ids)
    clean_current_all = _clean_records(current, duplicate_ids)
    clean_baseline_all = _clean_records(baseline, duplicate_ids)

    priors = active_settings.priors_by_platform
    platforms: list[PlatformHeat] = []
    for platform in ("xiaohongshu", "x", "reddit", "other"):
        raw_current = tuple(item for item in current if item.platform == platform)
        if not raw_current:
            continue
        clean_current = tuple(
            item for item in clean_current_all if item.platform == platform
        )
        clean_baseline = tuple(
            item for item in clean_baseline_all if item.platform == platform
        )
        clean_history = tuple(item for item in clean_all if item.platform == platform)
        metrics = _calculate_metrics(
            raw_current=raw_current,
            clean_current=clean_current,
            clean_baseline=clean_baseline,
            clean_history=clean_history,
            as_of=cutoff,
            settings=active_settings,
            duplicate_content_ids=duplicate_ids,
            overlap_clusters=overlap_clusters,
        )
        quarantine = (
            metrics.clean_count == 0
            or metrics.manipulation_risk >= active_settings.quarantine_threshold
        )
        platforms.append(
            PlatformHeat(
                platform=platform,
                prior_weight=priors[platform],
                normalized_attention_weight=ZERO,
                normalized_execution_weight=ZERO,
                execution_multiplier=(
                    active_settings.xhs_execution_weight
                    if platform == "xiaohongshu"
                    else ONE
                ),
                raw_current_count=metrics.raw_count,
                clean_current_count=metrics.clean_count,
                author_count=metrics.author_count,
                author_entropy=metrics.author_entropy,
                independent_content_count=metrics.independent_content_count,
                baseline_growth_30d=metrics.baseline_growth,
                log_engagement=metrics.log_engagement,
                sentiment_mean=metrics.sentiment_mean,
                sentiment_disagreement=metrics.sentiment_disagreement,
                topic_concentration=metrics.topic_concentration,
                ad_rate=metrics.ad_rate,
                duplicate_rate=metrics.duplicate_rate,
                coordinated_rate=metrics.coordinated_rate,
                cross_platform_overlap=metrics.cross_overlap,
                first_seen_at=metrics.first_seen_at,
                half_life_hours=active_settings.freshness_half_life_hours,
                attention_score=metrics.attention_score,
                manipulation_risk=metrics.manipulation_risk,
                quarantine=quarantine,
            )
        )

    coverage = sum(
        (item.prior_weight for item in platforms if not item.quarantine), ZERO
    )
    if coverage > ZERO:
        platforms = [
            replace(
                item,
                normalized_attention_weight=(
                    _quantize(item.prior_weight / coverage)
                    if not item.quarantine
                    else ZERO
                ),
            )
            for item in platforms
        ]

    execution_coverage = sum(
        (
            item.prior_weight
            for item in platforms
            if not item.quarantine and item.execution_multiplier > ZERO
        ),
        ZERO,
    )
    if execution_coverage > ZERO:
        platforms = [
            replace(
                item,
                normalized_execution_weight=(
                    _quantize(item.prior_weight / execution_coverage)
                    if not item.quarantine and item.execution_multiplier > ZERO
                    else ZERO
                ),
            )
            for item in platforms
        ]

    usable_platforms = {item.platform for item in platforms if not item.quarantine}
    aggregate_current = tuple(
        item for item in current if item.platform in usable_platforms
    )
    aggregate_clean_current = tuple(
        item for item in clean_current_all if item.platform in usable_platforms
    )
    aggregate_clean_baseline = tuple(
        item for item in clean_baseline_all if item.platform in usable_platforms
    )
    aggregate_clean_history = tuple(
        item for item in clean_all if item.platform in usable_platforms
    )
    aggregate = _calculate_metrics(
        raw_current=aggregate_current,
        clean_current=aggregate_clean_current,
        clean_baseline=aggregate_clean_baseline,
        clean_history=aggregate_clean_history,
        as_of=cutoff,
        settings=active_settings,
        duplicate_content_ids=duplicate_ids,
        overlap_clusters=overlap_clusters,
    )

    attention = ZERO
    direction = ZERO
    for platform in platforms:
        if platform.quarantine:
            continue
        attention_weight = platform.normalized_attention_weight
        attention += attention_weight * platform.attention_score
        if platform.sentiment_mean is not None:
            direction += (
                platform.normalized_execution_weight
                * platform.attention_score
                * platform.sentiment_mean
            )
    attention = _quantize(attention)
    active_prior = sum((item.prior_weight for item in platforms), ZERO)
    manipulation = (
        ZERO
        if active_prior == ZERO
        else _quantize(
            sum(
                (item.prior_weight * item.manipulation_risk for item in platforms),
                ZERO,
            )
            / active_prior
        )
    )
    combined_sentiment, combined_disagreement = _combine_platform_sentiment(platforms)
    ad_rate, duplicate_rate, coordinated_rate, overlap_rate, _ = _risk_rates(
        current, duplicate_ids, overlap_clusters
    )
    with localcontext(_CALC_CONTEXT):
        contribution = _clamp(
            direction * active_settings.final_social_decision_weight,
            -SOCIAL_DECISION_WEIGHT_CAP,
            SOCIAL_DECISION_WEIGHT_CAP,
        )
    contribution = _quantize(contribution)
    topics = _build_topic_results(
        aggregate_clean_current,
        aggregate_clean_history,
        cutoff,
        active_settings,
    )

    if not eligible:
        status = "no_eligible_data"
    elif not current:
        status = "no_current_data"
    elif coverage == ZERO:
        status = "quarantined"
    else:
        status = "ok"
    return SocialHeatResult(
        status=status,
        as_of=cutoff,
        eligible_input_digest=_eligible_digest(eligible),
        total_input_count=len(records),
        authorized_healthy_count=len(eligible),
        excluded_rights_count=excluded_rights,
        excluded_source_health_count=excluded_health,
        current_count=len(current),
        clean_current_count=aggregate.clean_count,
        author_count=aggregate.author_count,
        author_entropy=aggregate.author_entropy,
        independent_content_count=aggregate.independent_content_count,
        baseline_growth_30d=aggregate.baseline_growth,
        log_engagement=aggregate.log_engagement,
        sentiment_mean=combined_sentiment,
        sentiment_disagreement=combined_disagreement,
        topic_concentration=aggregate.topic_concentration,
        ad_rate=ad_rate,
        duplicate_rate=duplicate_rate,
        coordinated_rate=coordinated_rate,
        cross_platform_overlap=overlap_rate,
        first_seen_at=aggregate.first_seen_at,
        half_life_hours=active_settings.freshness_half_life_hours,
        attention_score=attention,
        manipulation_risk=manipulation,
        quarantine=coverage == ZERO,
        coverage=_quantize(coverage),
        platform_weights=tuple(
            (item.platform, item.normalized_attention_weight) for item in platforms
        ),
        execution_platform_weights=tuple(
            (item.platform, item.normalized_execution_weight) for item in platforms
        ),
        platforms=tuple(platforms),
        topics=topics,
        decision_weight_cap=SOCIAL_DECISION_WEIGHT_CAP,
        decision_contribution=contribution,
    )


def analyze_social_heat(
    observations: Iterable[SocialObservation],
    *,
    as_of: datetime,
    settings: SocialHeatSettings | None = None,
) -> SocialHeatResult:
    """Analyze an authorized offline snapshot under a fixed Decimal context.

    Unknown/unauthorized rights and non-healthy source rows are counted for the
    audit boundary but never enter the score.  A future timestamp, malformed
    record, inconsistent cross-platform cluster, or float-valued model input is
    rejected fail-closed.  The caller's ambient Decimal context cannot change
    the result.
    """

    with localcontext(_CALC_CONTEXT):
        return _analyze_social_heat(observations, as_of=as_of, settings=settings)


__all__ = [
    "AUTHORIZED_RIGHTS_STATUSES",
    "DEFAULT_PLATFORM_PRIORS",
    "EngagementBreakdown",
    "PlatformHeat",
    "SOCIAL_DECISION_WEIGHT_CAP",
    "SOCIAL_EVIDENCE_GROUP",
    "SOCIAL_TOPIC_TAXONOMY",
    "SocialHeatResult",
    "SocialHeatSettings",
    "SocialHeatValidationError",
    "SocialObservation",
    "TopicHeat",
    "XHS_INITIAL_EXECUTION_WEIGHT",
    "analyze_social_heat",
]
