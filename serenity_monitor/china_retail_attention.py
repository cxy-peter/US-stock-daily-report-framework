"""Offline, research-only aggregation for authorized retail-social records.

The module intentionally contains no HTTP client, browser automation, login, or
scraping code.  Callers may provide a user-owned CSV export or already loaded
structured records, together with an explicit rights attestation.  X, Reddit,
and Xiaohongshu are deliberately collapsed into one ``social_media`` evidence
group so cross-posts never masquerade as independent confirmation.
"""
from __future__ import annotations

import csv
import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


SOCIAL_MEDIA_INDEPENDENCE_GROUP = "social_media"
MAX_VALIDATED_MODEL_WEIGHT = 0.02

AUTHORIZED_RIGHTS_BASES = frozenset(
    {
        "user_owned_export",
        "platform_data_export",
        "licensed_dataset",
        "creator_consent",
        "authorized_api_export",
        "research_data_agreement",
    }
)
AUTHORIZED_PURPOSES = frozenset(
    {"research", "personal_research", "investment_research"}
)

_PLATFORM_ALIASES = {
    "xiaohongshu": "xiaohongshu",
    "xhs": "xiaohongshu",
    "小红书": "xiaohongshu",
    "red": "xiaohongshu",
    "x": "x",
    "twitter": "x",
    "reddit": "reddit",
}
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
_FORBIDDEN_COLLECTION_MARKERS = (
    "login_bypass",
    "bypass_login",
    "cookie_dump",
    "unauthorized_scrape",
    "绕过登录",
    "盗取cookie",
)
_AD_TERMS = (
    "#ad",
    "sponsored",
    "paid partnership",
    "affiliate link",
    "广告合作",
    "商业合作",
    "付费推广",
    "种草合作",
)


@dataclass(frozen=True)
class UsageAuthorization:
    """Caller attestation that the supplied records may be analysed."""

    has_right_to_use: bool
    basis: str
    declared_by: str
    purpose: str = "investment_research"

    @classmethod
    def from_dict(cls, data: Mapping[str, object] | None) -> "UsageAuthorization":
        data = data or {}
        return cls(
            has_right_to_use=_as_bool(data.get("has_right_to_use")),
            basis=str(data.get("basis") or ""),
            declared_by=str(data.get("declared_by") or ""),
            purpose=str(data.get("purpose") or "investment_research"),
        )


@dataclass(frozen=True)
class TopicRule:
    """Auditable mapping from text keywords to investment research targets."""

    topic: str
    keywords: tuple[str, ...]
    sector: str = ""
    etfs: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()
    base_confidence: float = 0.55

    def __post_init__(self) -> None:
        if not self.topic.strip():
            raise ValueError("topic must not be empty")
        if not self.keywords:
            raise ValueError("topic keywords must not be empty")
        if not 0.0 <= self.base_confidence <= 1.0:
            raise ValueError("base_confidence must be between 0 and 1")


DEFAULT_TOPIC_RULES: tuple[TopicRule, ...] = (
    TopicRule(
        topic="nasdaq_100",
        keywords=("纳斯达克100", "纳指100", "nasdaq 100"),
        sector="US large-cap growth",
        base_confidence=0.70,
    ),
    TopicRule(
        topic="sp_500",
        keywords=("标普500", "s&p 500", "sp500"),
        sector="US large-cap equity",
        base_confidence=0.70,
    ),
    TopicRule(
        topic="semiconductors",
        keywords=("半导体", "芯片", "hbm", "dram", "gpu", "semiconductor"),
        sector="Semiconductors",
        base_confidence=0.62,
    ),
    TopicRule(
        topic="dividend_equity",
        keywords=("红利", "高股息", "dividend"),
        sector="US dividend equity",
        base_confidence=0.65,
    ),
    TopicRule(
        topic="crypto_assets",
        keywords=("比特币", "bitcoin", "btc", "稳定币", "stablecoin"),
        sector="Digital assets",
        base_confidence=0.55,
    ),
)


def topic_rules_from_config(
    data: Sequence[Mapping[str, object]] | None,
) -> tuple[TopicRule, ...]:
    """Build auditable topic mappings from public or ignored private config.

    The public defaults deliberately stop at themes and sectors. A private
    runtime may add its own ETF/ticker mapping inline in its ignored portfolio
    configuration without exposing that strategy fingerprint in this module.
    """

    if data is None:
        return DEFAULT_TOPIC_RULES
    if isinstance(data, (str, bytes)) or not isinstance(data, Sequence):
        raise ValueError("china_retail_attention.topic_rules must be a list")

    allowed_keys = {
        "topic",
        "keywords",
        "sector",
        "etfs",
        "tickers",
        "base_confidence",
    }
    rules: list[TopicRule] = []
    for index, item in enumerate(data):
        if not isinstance(item, Mapping):
            raise ValueError(f"topic_rules[{index}] must be a mapping")
        unknown_keys = set(item) - allowed_keys
        if unknown_keys:
            unknown = ", ".join(sorted(str(key) for key in unknown_keys))
            raise ValueError(f"topic_rules[{index}] has unsupported keys: {unknown}")

        def values(name: str, *, required: bool = False) -> tuple[str, ...]:
            raw = item.get(name)
            if raw is None and not required:
                return ()
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                raise ValueError(f"topic_rules[{index}].{name} must be a list")
            cleaned = tuple(str(value).strip() for value in raw if str(value).strip())
            if required and not cleaned:
                raise ValueError(f"topic_rules[{index}].{name} must not be empty")
            return cleaned

        confidence = item.get("base_confidence", 0.55)
        rules.append(
            TopicRule(
                topic=str(item.get("topic") or "").strip(),
                keywords=values("keywords", required=True),
                sector=str(item.get("sector") or "").strip(),
                etfs=tuple(value.upper() for value in values("etfs")),
                tickers=tuple(value.upper() for value in values("tickers")),
                base_confidence=float(confidence),
            )
        )
    return tuple(rules)


@dataclass(frozen=True)
class ChinaRetailAttentionSettings:
    """Conservative signal settings.

    ``execution_weight`` is historical naming retained for configuration
    compatibility.  It is only a model-blending coefficient, never a portfolio
    weight or an order instruction.  It remains zero unless validation has been
    explicitly attested and can never exceed two percent.
    """

    freshness_half_life_hours: float = 72.0
    max_age_days: int = 30
    engagement_winsor_quantile: float = 0.95
    engagement_hard_cap: float = 1_000_000.0
    duplicate_burst_window_hours: float = 6.0
    max_records: int = 5_000
    execution_weight: float = 0.0
    validation_passed: bool = False
    candidate_weight_cap: float = MAX_VALIDATED_MODEL_WEIGHT
    manipulation_weight_block: float = 0.60
    allowed_authorization_bases: frozenset[str] = field(
        default_factory=lambda: AUTHORIZED_RIGHTS_BASES
    )

    def __post_init__(self) -> None:
        if self.freshness_half_life_hours <= 0:
            raise ValueError("freshness_half_life_hours must be positive")
        if self.max_age_days < 1:
            raise ValueError("max_age_days must be at least one")
        if not 0.0 < self.engagement_winsor_quantile <= 1.0:
            raise ValueError("engagement_winsor_quantile must be in (0, 1]")
        if self.engagement_hard_cap <= 0:
            raise ValueError("engagement_hard_cap must be positive")
        if self.duplicate_burst_window_hours <= 0:
            raise ValueError("duplicate_burst_window_hours must be positive")
        if self.max_records < 1:
            raise ValueError("max_records must be positive")
        if not 0.0 <= self.candidate_weight_cap <= MAX_VALIDATED_MODEL_WEIGHT:
            raise ValueError("candidate_weight_cap may not exceed 0.02")
        if not 0.0 <= self.execution_weight <= self.candidate_weight_cap:
            raise ValueError("execution_weight must be between zero and candidate_weight_cap")
        if not 0.0 <= self.manipulation_weight_block <= 1.0:
            raise ValueError("manipulation_weight_block must be between zero and one")

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, object] | None,
    ) -> "ChinaRetailAttentionSettings":
        data = data or {}
        return cls(
            freshness_half_life_hours=float(
                data.get("freshness_half_life_hours") or 72.0
            ),
            max_age_days=int(data.get("max_age_days") or 30),
            engagement_winsor_quantile=float(
                data.get("engagement_winsor_quantile") or 0.95
            ),
            engagement_hard_cap=float(
                data.get("engagement_hard_cap") or 1_000_000.0
            ),
            duplicate_burst_window_hours=float(
                data.get("duplicate_burst_window_hours") or 6.0
            ),
            max_records=int(data.get("max_records") or 5_000),
            execution_weight=float(data.get("execution_weight") or 0.0),
            validation_passed=_as_bool(data.get("validation_passed")),
            candidate_weight_cap=float(
                data.get("candidate_weight_cap") or MAX_VALIDATED_MODEL_WEIGHT
            ),
            manipulation_weight_block=float(
                data.get("manipulation_weight_block") or 0.60
            ),
        )

    @property
    def validated_model_weight(self) -> float:
        if not self.validation_passed:
            return 0.0
        return min(
            self.execution_weight,
            self.candidate_weight_cap,
            MAX_VALIDATED_MODEL_WEIGHT,
        )


@dataclass(frozen=True)
class TopicMapping:
    topic: str
    sector: str
    etfs: tuple[str, ...]
    tickers: tuple[str, ...]
    matched_keywords: tuple[str, ...]
    reason: str
    confidence: float


@dataclass(frozen=True)
class RecordSignal:
    record_id: str
    platform: str
    author_hash: str
    published_at: str
    observed_at: str
    text_hash: str
    normalized_text_hash: str
    freshness_weight: float
    raw_engagement: float
    capped_engagement: float
    engagement_weight: float
    sponsored_or_ad: bool
    topic_mappings: tuple[TopicMapping, ...]
    independence_group: str = SOCIAL_MEDIA_INDEPENDENCE_GROUP
    research_only: bool = True
    can_trigger_trade: bool = False


@dataclass(frozen=True)
class TopicAttention:
    topic: str
    sector: str
    etfs: tuple[str, ...]
    tickers: tuple[str, ...]
    reason: str
    confidence: float
    attention_score: float
    sample_count: int
    model_weight_contribution: float
    independence_group: str = SOCIAL_MEDIA_INDEPENDENCE_GROUP
    research_only: bool = True
    can_trigger_trade: bool = False


@dataclass(frozen=True)
class ChinaRetailAttentionResult:
    status: str
    detail: str
    authorization_basis: str
    input_count: int
    accepted_count: int
    unique_count: int
    rejected_count: int
    exact_duplicate_count: int
    normalized_duplicate_count: int
    engagement_winsor_cap: float
    ad_ratio: float
    duplicate_burst_score: float
    source_concentration: float
    manipulation_penalty: float
    execution_weight: float
    records: tuple[RecordSignal, ...] = ()
    topics: tuple[TopicAttention, ...] = ()
    warnings: tuple[str, ...] = ()
    independence_group: str = SOCIAL_MEDIA_INDEPENDENCE_GROUP
    research_only: bool = True
    can_trigger_trade: bool = False
    weight_semantics: str = "model_blend_only_not_position_or_order"


@dataclass
class _PreparedRecord:
    record_id: str
    platform: str
    author_id: str
    text: str
    exact_key: str
    normalized_key: str
    published_at: datetime
    engagement: float
    sponsored_or_ad: bool
    mappings: tuple[TopicMapping, ...] = ()


def analyze_authorized_csv(
    path: str | Path,
    authorization: UsageAuthorization | None,
    settings: ChinaRetailAttentionSettings | None = None,
    topic_rules: Sequence[TopicRule] = DEFAULT_TOPIC_RULES,
    now: datetime | None = None,
) -> ChinaRetailAttentionResult:
    """Read and analyse an explicitly authorized CSV export.

    Expected columns are ``platform``, ``author_id``, ``text`` and an ISO-8601
    ``published_at`` with timezone.  Engagement can be supplied as one
    ``engagement`` value or as likes/comments/shares/saves/reposts/views.
    """

    settings = settings or ChinaRetailAttentionSettings()
    auth_error = _authorization_error(authorization, settings)
    if auth_error:
        return _blocked(auth_error, authorization)
    target = Path(path)
    if not target.is_file():
        return _blocked(f"Authorized CSV file is missing: {target}", authorization)
    if target.suffix.casefold() != ".csv":
        return _blocked("Only CSV files or in-memory structured records are accepted", authorization)
    try:
        with target.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = {str(name or "").strip() for name in (reader.fieldnames or [])}
            required = {"platform", "author_id", "text", "published_at"}
            if not required.issubset(fields):
                missing = ", ".join(sorted(required - fields))
                return _blocked(f"CSV is missing required columns: {missing}", authorization)
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        return _blocked(f"CSV could not be read safely: {type(exc).__name__}: {exc}", authorization)
    return analyze_authorized_records(
        rows,
        authorization,
        settings=settings,
        topic_rules=topic_rules,
        now=now,
        source_label=str(target),
    )


def analyze_authorized_records(
    records: Iterable[Mapping[str, object]],
    authorization: UsageAuthorization | None,
    settings: ChinaRetailAttentionSettings | None = None,
    topic_rules: Sequence[TopicRule] = DEFAULT_TOPIC_RULES,
    now: datetime | None = None,
    source_label: str = "authorized structured records",
) -> ChinaRetailAttentionResult:
    """Aggregate authorized structured records without performing network I/O."""

    settings = settings or ChinaRetailAttentionSettings()
    auth_error = _authorization_error(authorization, settings)
    if auth_error:
        return _blocked(auth_error, authorization)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("now must include a timezone")
    current_time = current_time.astimezone(timezone.utc)

    raw_rows = list(records)
    input_count = len(raw_rows)
    truncated = input_count > settings.max_records
    prepared: list[_PreparedRecord] = []
    rejection_reasons: Counter[str] = Counter()
    for index, row in enumerate(raw_rows[: settings.max_records]):
        record, rejection = _prepare_record(
            row,
            index=index,
            now=current_time,
            settings=settings,
        )
        if record is None:
            rejection_reasons[rejection] += 1
        else:
            prepared.append(record)
    if truncated:
        rejection_reasons["record limit exceeded"] += input_count - settings.max_records

    rejected_count = sum(rejection_reasons.values())
    if not prepared:
        reasons = _format_rejections(rejection_reasons)
        detail = f"No usable authorized social records from {source_label}"
        if reasons:
            detail += f"; {reasons}"
        return _blocked(
            detail,
            authorization,
            input_count=input_count,
            rejected_count=rejected_count,
        )

    unique, exact_duplicates, normalized_duplicates = _deduplicate(prepared)
    for record in unique:
        record.mappings = _map_topics(record.text, topic_rules)

    engagement_cap = min(
        settings.engagement_hard_cap,
        _quantile(
            [record.engagement for record in unique],
            settings.engagement_winsor_quantile,
        ),
    )
    engagement_cap = max(0.0, engagement_cap)
    record_signals = tuple(
        _record_signal(record, engagement_cap, current_time, settings)
        for record in unique
    )

    ad_ratio = sum(record.sponsored_or_ad for record in prepared) / len(prepared)
    burst_score = _duplicate_burst_score(prepared, settings)
    concentration = _source_concentration(prepared)
    duplicate_ratio = (exact_duplicates + normalized_duplicates) / len(prepared)
    ad_penalty = 0.35 * ad_ratio
    duplicate_penalty = min(0.40, 0.30 * duplicate_ratio + 0.20 * burst_score)
    concentration_penalty = max(0.0, (concentration - 0.25) / 0.75) * 0.30
    manipulation_penalty = min(
        0.85,
        ad_penalty + duplicate_penalty + concentration_penalty,
    )

    execution_weight = settings.validated_model_weight
    warnings = [
        "Social-media output is research-only and cannot trigger a trade.",
        "execution_weight is a model-blending coefficient, not a portfolio weight or order.",
        "X, Reddit and Xiaohongshu share one social_media independence group.",
    ]
    if not settings.validation_passed:
        warnings.append("Model validation is not attested; execution_weight remains zero.")
    if manipulation_penalty >= settings.manipulation_weight_block:
        execution_weight = 0.0
        warnings.append("Manipulation-risk threshold reached; model weight forced to zero.")
    if rejection_reasons:
        warnings.append(f"Rejected records: {_format_rejections(rejection_reasons)}")

    topics = _aggregate_topics(
        record_signals,
        manipulation_penalty=manipulation_penalty,
        execution_weight=execution_weight,
    )
    if not topics:
        execution_weight = 0.0
        warnings.append("No topic rule matched; model weight forced to zero.")

    status = "partial" if rejection_reasons else "ok"
    detail = (
        f"{len(unique)} unique authorized record(s) analysed from {source_label}; "
        f"{exact_duplicates} exact and {normalized_duplicates} normalized duplicate(s) removed"
    )
    return ChinaRetailAttentionResult(
        status=status,
        detail=detail,
        authorization_basis=authorization.basis,
        input_count=input_count,
        accepted_count=len(prepared),
        unique_count=len(unique),
        rejected_count=rejected_count,
        exact_duplicate_count=exact_duplicates,
        normalized_duplicate_count=normalized_duplicates,
        engagement_winsor_cap=round(engagement_cap, 3),
        ad_ratio=round(ad_ratio, 4),
        duplicate_burst_score=round(burst_score, 4),
        source_concentration=round(concentration, 4),
        manipulation_penalty=round(manipulation_penalty, 4),
        execution_weight=round(execution_weight, 4),
        records=record_signals,
        topics=topics,
        warnings=tuple(warnings),
    )


def _authorization_error(
    authorization: UsageAuthorization | None,
    settings: ChinaRetailAttentionSettings,
) -> str:
    if authorization is None:
        return "Permission basis is missing"
    if not authorization.has_right_to_use:
        return "Caller has not attested a right to use the supplied records"
    basis = authorization.basis.strip().casefold()
    if basis not in {value.casefold() for value in settings.allowed_authorization_bases}:
        return f"Permission basis is unclear or unsupported: {authorization.basis or '<empty>'}"
    if not authorization.declared_by.strip():
        return "Permission declaration must identify the declaring user or organization"
    if authorization.purpose.strip().casefold() not in AUTHORIZED_PURPOSES:
        return "Authorization purpose must explicitly permit research use"
    return ""


def _blocked(
    detail: str,
    authorization: UsageAuthorization | None,
    input_count: int = 0,
    rejected_count: int = 0,
) -> ChinaRetailAttentionResult:
    return ChinaRetailAttentionResult(
        status="blocked",
        detail=detail,
        authorization_basis=(authorization.basis if authorization else ""),
        input_count=input_count,
        accepted_count=0,
        unique_count=0,
        rejected_count=rejected_count,
        exact_duplicate_count=0,
        normalized_duplicate_count=0,
        engagement_winsor_cap=0.0,
        ad_ratio=0.0,
        duplicate_burst_score=0.0,
        source_concentration=0.0,
        manipulation_penalty=0.0,
        execution_weight=0.0,
        warnings=(
            "No social signal was produced.",
            "Social-media evidence is research-only and cannot trigger a trade.",
        ),
    )


def _prepare_record(
    row: Mapping[str, object],
    index: int,
    now: datetime,
    settings: ChinaRetailAttentionSettings,
) -> tuple[_PreparedRecord | None, str]:
    if not isinstance(row, Mapping):
        return None, "record is not structured"
    method = str(row.get("collection_method") or "").strip().casefold()
    if any(marker in method for marker in _FORBIDDEN_COLLECTION_MARKERS):
        return None, "disallowed collection method"
    platform_raw = str(row.get("platform") or "").strip().casefold()
    platform = _PLATFORM_ALIASES.get(platform_raw)
    if not platform:
        return None, "unsupported or missing platform"
    title = str(row.get("title") or "").strip()
    body = str(row.get("text") or "").strip()
    text = _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", f"{title} {body}".strip()))
    if len(text) < 3:
        return None, "missing or too-short text"
    published_at = _parse_datetime(
        row.get("published_at") or row.get("published") or row.get("created_at")
    )
    if published_at is None:
        return None, "published_at is missing, invalid, or lacks timezone"
    if published_at > now and (published_at - now).total_seconds() > 300:
        return None, "published_at is implausibly in the future"
    age_days = max(0.0, (now - published_at).total_seconds()) / 86_400.0
    if age_days > settings.max_age_days:
        return None, "record is outside max_age_days"
    author_id = str(
        row.get("author_id") or row.get("source_id") or row.get("author") or "unknown"
    ).strip()
    exact = _exact_text(text)
    normalized = _normalized_text(text)
    if len(normalized) < 3:
        return None, "text is empty after normalization"
    native_id = str(row.get("record_id") or row.get("id") or "").strip()
    safe_record_id = (
        _hash(f"{platform}|native|{native_id}")
        if native_id
        else _hash(f"{platform}|{author_id}|{published_at.isoformat()}|{exact}")
    )
    sponsored = _as_bool(row.get("sponsored")) or any(
        term in text.casefold() for term in _AD_TERMS
    )
    return (
        _PreparedRecord(
            record_id=safe_record_id,
            platform=platform,
            author_id=author_id or "unknown",
            text=text,
            exact_key=_hash(exact),
            normalized_key=_hash(normalized),
            published_at=published_at,
            engagement=_engagement(row),
            sponsored_or_ad=sponsored,
        ),
        "",
    )


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _exact_text(text: str) -> str:
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip().casefold()


def _normalized_text(text: str) -> str:
    value = _URL_RE.sub("", _exact_text(text))
    return "".join(character for character in value if character.isalnum())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "是"}


def _safe_float(value: object) -> float:
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, number)


def _engagement(row: Mapping[str, object]) -> float:
    if row.get("engagement") not in (None, ""):
        return _safe_float(row.get("engagement"))
    return (
        _safe_float(row.get("likes"))
        + 2.0 * _safe_float(row.get("comments"))
        + 3.0 * _safe_float(row.get("shares"))
        + 2.0 * _safe_float(row.get("saves"))
        + 3.0 * _safe_float(row.get("reposts"))
        + 0.01 * _safe_float(row.get("views"))
    )


def _deduplicate(
    records: Sequence[_PreparedRecord],
) -> tuple[list[_PreparedRecord], int, int]:
    exact_groups: dict[str, list[_PreparedRecord]] = defaultdict(list)
    for record in records:
        exact_groups[record.exact_key].append(record)
    exact_unique = [_best_record(group) for group in exact_groups.values()]
    exact_duplicate_count = len(records) - len(exact_unique)

    normalized_groups: dict[str, list[_PreparedRecord]] = defaultdict(list)
    for record in exact_unique:
        normalized_groups[record.normalized_key].append(record)
    normalized_unique = [_best_record(group) for group in normalized_groups.values()]
    normalized_duplicate_count = len(exact_unique) - len(normalized_unique)
    normalized_unique.sort(key=lambda item: item.published_at, reverse=True)
    return normalized_unique, exact_duplicate_count, normalized_duplicate_count


def _best_record(records: Sequence[_PreparedRecord]) -> _PreparedRecord:
    return max(records, key=lambda item: (item.engagement, item.published_at))


def _contains_keyword(text: str, keyword: str) -> bool:
    candidate = keyword.strip().casefold()
    if not candidate:
        return False
    lowered = text.casefold()
    if re.fullmatch(r"\$?[a-z0-9_-]{1,5}", candidate):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])",
            lowered,
        ) is not None
    return candidate in lowered


def _map_topics(text: str, rules: Sequence[TopicRule]) -> tuple[TopicMapping, ...]:
    mappings: list[TopicMapping] = []
    for rule in rules:
        matched = tuple(
            keyword for keyword in rule.keywords if _contains_keyword(text, keyword)
        )
        if not matched:
            continue
        confidence = min(0.95, rule.base_confidence + 0.05 * (len(matched) - 1))
        targets = []
        if rule.sector:
            targets.append(f"sector={rule.sector}")
        if rule.etfs:
            targets.append(f"ETF={','.join(rule.etfs)}")
        if rule.tickers:
            targets.append(f"ticker={','.join(rule.tickers)}")
        reason = (
            f"Matched {', '.join(matched)}; mapped first by topic rule "
            f"{rule.topic} to {'; '.join(targets) or 'research topic only'}"
        )
        mappings.append(
            TopicMapping(
                topic=rule.topic,
                sector=rule.sector,
                etfs=tuple(dict.fromkeys(ticker.upper() for ticker in rule.etfs)),
                tickers=tuple(dict.fromkeys(ticker.upper() for ticker in rule.tickers)),
                matched_keywords=matched,
                reason=reason,
                confidence=round(confidence, 4),
            )
        )
    return tuple(mappings)


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _record_signal(
    record: _PreparedRecord,
    engagement_cap: float,
    now: datetime,
    settings: ChinaRetailAttentionSettings,
) -> RecordSignal:
    age_hours = max(0.0, (now - record.published_at).total_seconds()) / 3_600.0
    freshness = math.exp(
        -math.log(2.0) * age_hours / settings.freshness_half_life_hours
    )
    capped = min(record.engagement, engagement_cap)
    if engagement_cap > 0:
        engagement_weight = math.log1p(capped) / math.log1p(engagement_cap)
    else:
        engagement_weight = 0.0
    return RecordSignal(
        record_id=record.record_id,
        platform=record.platform,
        author_hash=_hash(record.author_id.casefold()),
        published_at=record.published_at.isoformat(),
        observed_at=now.isoformat(),
        text_hash=record.exact_key,
        normalized_text_hash=record.normalized_key,
        freshness_weight=round(freshness, 4),
        raw_engagement=round(record.engagement, 3),
        capped_engagement=round(capped, 3),
        engagement_weight=round(engagement_weight, 4),
        sponsored_or_ad=record.sponsored_or_ad,
        topic_mappings=record.mappings,
    )


def _duplicate_burst_score(
    records: Sequence[_PreparedRecord],
    settings: ChinaRetailAttentionSettings,
) -> float:
    groups: dict[str, list[_PreparedRecord]] = defaultdict(list)
    for record in records:
        groups[record.normalized_key].append(record)
    burst_records = 0
    for group in groups.values():
        if len(group) < 3:
            continue
        times = [record.published_at for record in group]
        span_hours = (max(times) - min(times)).total_seconds() / 3_600.0
        if span_hours <= settings.duplicate_burst_window_hours:
            burst_records += len(group)
    return min(1.0, burst_records / len(records)) if records else 0.0


def _source_concentration(records: Sequence[_PreparedRecord]) -> float:
    if not records:
        return 0.0
    # The export's author_id is the caller's canonical identity.  Reposting the
    # same campaign across platforms must not dilute source concentration.
    counts = Counter(record.author_id.casefold() for record in records)
    total = len(records)
    return sum((count / total) ** 2 for count in counts.values())


def _aggregate_topics(
    records: Sequence[RecordSignal],
    manipulation_penalty: float,
    execution_weight: float,
) -> tuple[TopicAttention, ...]:
    grouped: dict[str, list[tuple[RecordSignal, TopicMapping]]] = defaultdict(list)
    for record in records:
        for mapping in record.topic_mappings:
            grouped[mapping.topic].append((record, mapping))
    output: list[TopicAttention] = []
    for topic, rows in grouped.items():
        scores = []
        mapping_confidences = []
        sectors: list[str] = []
        etfs: list[str] = []
        tickers: list[str] = []
        reasons: list[str] = []
        for record, mapping in rows:
            engagement_component = 0.20 + 0.80 * record.engagement_weight
            ad_multiplier = 0.55 if record.sponsored_or_ad else 1.0
            scores.append(record.freshness_weight * engagement_component * ad_multiplier)
            mapping_confidences.append(mapping.confidence * record.freshness_weight)
            if mapping.sector:
                sectors.append(mapping.sector)
            etfs.extend(mapping.etfs)
            tickers.extend(mapping.tickers)
            reasons.append(mapping.reason)
        sample_reliability = len(rows) / (len(rows) + 5.0)
        raw_attention = sum(scores) / len(scores)
        attention = 100.0 * raw_attention * (0.5 + 0.5 * sample_reliability)
        attention *= 1.0 - manipulation_penalty
        confidence = (
            sum(mapping_confidences) / len(mapping_confidences)
            * sample_reliability
            * (1.0 - manipulation_penalty)
        )
        contribution = execution_weight * (attention / 100.0) * confidence
        output.append(
            TopicAttention(
                topic=topic,
                sector=sectors[0] if sectors else "",
                etfs=tuple(dict.fromkeys(etfs)),
                tickers=tuple(dict.fromkeys(tickers)),
                reason=" | ".join(list(dict.fromkeys(reasons))[:3]),
                confidence=round(max(0.0, min(1.0, confidence)), 4),
                attention_score=round(max(0.0, min(100.0, attention)), 2),
                sample_count=len(rows),
                model_weight_contribution=round(
                    max(0.0, min(MAX_VALIDATED_MODEL_WEIGHT, contribution)),
                    6,
                ),
            )
        )
    return tuple(sorted(output, key=lambda item: item.attention_score, reverse=True))


def _format_rejections(reasons: Counter[str]) -> str:
    return "; ".join(f"{reason}={count}" for reason, count in sorted(reasons.items()))
