"""Offline, deterministic monitoring cadence for U.S. funds and ETPs.

The module is deliberately a research boundary rather than a data collector or
trading adapter.  Callers provide already-normalized source, evidence, and
metric records.  Legal/economic structure is checked before performance or
portfolio-fit evidence; product quality and portfolio fit are assessed
separately; and missing information is never converted to a numeric zero.

Only controlled identifiers and enums cross the boundary.  Raw documents,
URLs, social handles, account identifiers, query strings, secrets, and free
text do not belong here.  All numeric values use ``Decimal`` and all dates are
timezone-aware.  Social observations can open a research question but cannot
satisfy a required evidence category or strengthen a conclusion.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo


STATUSES = frozenset({"PASS", "WATCH", "REJECT", "NEED_INFO", "NOT_DUE"})
CADENCES = ("daily", "event", "monthly", "quarterly", "annual")
EVIDENCE_TYPES = frozenset(
    {"FACT", "CALCULATION", "INFERENCE", "JUDGMENT", "SOCIAL_SIGNAL"}
)
SOURCE_TIERS = frozenset({"primary", "calculated", "secondary", "social"})
SOURCE_HEALTH = frozenset({"healthy", "degraded", "unavailable", "quarantined"})
ASSESSMENTS = frozenset({"supports", "watch", "reject", "unknown", "not_applicable", "lead"})
DIMENSIONS = frozenset({"product_quality", "portfolio_fit", "both"})
METRIC_UNITS = frozenset(
    {
        "ratio",
        "percent",
        "usd",
        "days",
        "years",
        "count",
        "basis_points",
        "return",
        "risk",
        "score",
        "not_applicable",
    }
)
METRIC_VALUE_STATUSES = frozenset({"known", "unknown", "not_applicable"})
REVIEW_TIMEZONES = frozenset({"Asia/Shanghai", "America/New_York", "UTC"})
# Inclusive point-in-time windows.  Category evidence uses its native cadence
# even during an annual full review (monthly exposures stay monthly-current),
# while source-health observations always use the two-day daily window.
FRESHNESS_DAYS: Mapping[str, int] = MappingProxyType(
    {
        "daily": 2,
        "event": 7,
        "monthly": 45,
        "quarterly": 120,
        "annual": 400,
    }
)

LEGAL_STRUCTURES = frozenset(
    {
        "open_end_fund",
        "unit_investment_trust",
        "grantor_trust",
        "exchange_traded_note",
        "closed_end_fund",
        "business_development_company",
        "commodity_pool",
        "unknown",
    }
)
ECONOMIC_STRUCTURES = frozenset(
    {
        "physical",
        "synthetic",
        "fund_of_funds",
        "derivatives_overlay",
        "active_security_selection",
        "index_tracking",
        "leveraged_reset",
        "inverse_reset",
        "buffered_outcome",
        "unknown",
    }
)
PORTFOLIO_ROLES = frozenset(
    {
        "core_equity",
        "satellite_equity",
        "income",
        "diversifier",
        "liquidity_reserve",
        "hedge",
        "tactical",
        "unknown",
    }
)

EVENT_CATEGORIES = frozenset(
    {
        "source_health",
        "manager",
        "fees",
        "index_methodology",
        "prospectus",
        "legal_structure",
        "economic_structure",
        "structure",
    }
)

_PRODUCT_REQUIREMENTS: Mapping[str, frozenset[str]] = {
    "daily": frozenset(
        {
            "legal_structure",
            "economic_structure",
            "source_health",
            "manager",
            "fees",
            "prospectus",
        }
    ),
    "monthly": frozenset({"exposure", "style", "factor", "liquidity", "capacity"}),
    "quarterly": frozenset({"holdings", "attribution", "thesis", "manager_skill"}),
    "annual": frozenset(
        {
            "legal_structure",
            "economic_structure",
            "source_health",
            "manager",
            "fees",
            "prospectus",
            "exposure",
            "style",
            "factor",
            "liquidity",
            "capacity",
            "holdings",
            "attribution",
            "thesis",
            "manager_skill",
            "full_review",
        }
    ),
}
_FIT_REQUIREMENTS: Mapping[str, frozenset[str]] = {
    "monthly": frozenset({"portfolio_role", "overlap", "marginal_contribution", "liquidity"}),
    "quarterly": frozenset({"thesis", "risk_budget", "implementation"}),
    "annual": frozenset(
        {
            "portfolio_role",
            "overlap",
            "marginal_contribution",
            "liquidity",
            "thesis",
            "risk_budget",
            "implementation",
            "full_review",
            "tax",
            "costs",
        }
    ),
}

_CATEGORY_CADENCE: Mapping[str, str] = {
    **{category: "daily" for category in _PRODUCT_REQUIREMENTS["daily"]},
    "index_methodology": "daily",
    "structure": "daily",
    **{category: "monthly" for category in _PRODUCT_REQUIREMENTS["monthly"]},
    **{category: "monthly" for category in _FIT_REQUIREMENTS["monthly"]},
    "issuer_credit": "monthly",
    "call_terms": "monthly",
    "tax_structure": "monthly",
    "collateral_custody": "monthly",
    "creation_redemption": "monthly",
    "tracking": "monthly",
    "discount_premium": "monthly",
    "leverage": "monthly",
    "derivatives_structure": "monthly",
    "reset_terms": "monthly",
    "path_dependency": "monthly",
    "cap_buffer": "monthly",
    "look_through": "monthly",
    "fee_stack": "monthly",
    **{category: "quarterly" for category in _PRODUCT_REQUIREMENTS["quarterly"]},
    **{category: "quarterly" for category in _FIT_REQUIREMENTS["quarterly"]},
    "full_review": "annual",
    "tax": "annual",
    "costs": "annual",
    "portfolio_impact": "event",
}

# Public closed vocabularies for downstream aggregate-report adapters.  These
# are deliberately enumerated here, where the monitor derives the values,
# rather than re-accepting arbitrary identifier-shaped strings at transport
# boundaries.
FUND_MONITOR_CATEGORIES = frozenset(_CATEGORY_CADENCE)
FUND_MONITOR_SCOPED_CATEGORIES = frozenset(
    f"{dimension}.{category}"
    for dimension in ("product_quality", "portfolio_fit")
    for category in FUND_MONITOR_CATEGORIES
)
FUND_MONITOR_REASON_CODES = frozenset(
    {
        "unknown_legal_structure",
        "unknown_economic_structure",
        "unknown_portfolio_role",
        "nonconfirming_risk_observation",
        "judgment_inference_risk_observation",
        "coverage_ineligible_fact_calculation_risk_observation",
        "structure_hard_gate_blocked",
        "verified_structural_hard_reject",
        "confirmed_reject_signal",
        "confirmed_watch_signal",
        "material_event_under_review",
        "scheduled_review_complete",
        "review_not_due",
        "fund_monitor.overall.partial_not_due",
        *(f"fund_monitor.overall.{status.lower()}" for status in STATUSES),
        *(
            f"{prefix}{category}"
            for prefix in (
                "missing_required_",
                "unknown_required_",
                "source_not_healthy_",
                "stale_required_",
                "social_signal_unconfirmed_",
            )
            for category in FUND_MONITOR_CATEGORIES
        ),
    }
)

_PRIMARY_REQUIRED = frozenset(
    {
        "legal_structure",
        "economic_structure",
        "source_health",
        "manager",
        "fees",
        "index_methodology",
        "prospectus",
        "holdings",
        "liquidity",
        "capacity",
        "issuer_credit",
        "call_terms",
        "tax_structure",
        "collateral_custody",
        "creation_redemption",
        "tracking",
        "discount_premium",
        "leverage",
        "derivatives_structure",
        "reset_terms",
        "path_dependency",
        "cap_buffer",
        "look_through",
        "fee_stack",
    }
)
_FACT_ONLY_CATEGORIES = frozenset(
    {"legal_structure", "economic_structure", "structure"}
)

_LEGAL_BRANCH_REQUIREMENTS: Mapping[str, frozenset[str]] = {
    "exchange_traded_note": frozenset({"issuer_credit", "call_terms"}),
    "commodity_pool": frozenset({"tax_structure", "collateral_custody"}),
    "grantor_trust": frozenset({"tax_structure", "collateral_custody"}),
    "unit_investment_trust": frozenset({"creation_redemption", "tracking"}),
    "closed_end_fund": frozenset({"discount_premium", "leverage"}),
    "business_development_company": frozenset({"leverage", "liquidity"}),
}
_ECONOMIC_BRANCH_REQUIREMENTS: Mapping[str, frozenset[str]] = {
    "synthetic": frozenset({"derivatives_structure", "collateral_custody"}),
    "derivatives_overlay": frozenset({"derivatives_structure", "leverage"}),
    "fund_of_funds": frozenset({"look_through", "fee_stack"}),
    "index_tracking": frozenset({"index_methodology", "tracking"}),
    "leveraged_reset": frozenset({"index_methodology", "reset_terms", "path_dependency", "leverage"}),
    "inverse_reset": frozenset({"index_methodology", "reset_terms", "path_dependency", "leverage"}),
    "buffered_outcome": frozenset({"index_methodology", "cap_buffer", "path_dependency"}),
}

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_FUND_KEY_RE = re.compile(r"^(?=[A-Z0-9._-]*[A-Z])[A-Z0-9][A-Z0-9._-]{0,31}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_URI_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
_DOMAIN_SHAPE_RE = re.compile(
    r"(?:^|[._-])www\.|\.[a-z]{2,63}$",
    re.IGNORECASE,
)
_IPV4_SHAPE_RE = re.compile(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$")
_SECRETISH_RE = re.compile(
    r"(?:^|[_.:-])(account|authorization|cookie|handle|password|passwd|query|secret|token|url|username)(?:$|[_.:-])",
    re.IGNORECASE,
)
_SECRET_PREFIX_RE = re.compile(r"^(?:gh[opusr]_|sk-|bearer[_.:-]|basic[_.:-])", re.IGNORECASE)
_CALC_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_RATIO_QUANTUM = Decimal("0.000000000001")


class FundMonitorValidationError(ValueError):
    """Raised when an input cannot safely enter the monitoring boundary."""


def _identifier(value: object, field_name: str, *, fund: bool = False) -> str:
    if not isinstance(value, str):
        raise FundMonitorValidationError(f"{field_name} must be a controlled identifier")
    text = value.strip()
    if text != value or not text:
        raise FundMonitorValidationError(f"{field_name} must be a controlled identifier")
    if (
        "/" in text
        or "\\" in text
        or _URI_SCHEME_RE.search(text)
        or _DOMAIN_SHAPE_RE.search(text)
        or _IPV4_SHAPE_RE.fullmatch(text)
    ):
        raise FundMonitorValidationError(
            f"{field_name} may not contain a URI, domain, or path"
        )
    if fund:
        if not _FUND_KEY_RE.fullmatch(text):
            raise FundMonitorValidationError(f"{field_name} must be a controlled fund key")
    elif not _KEY_RE.fullmatch(text):
        raise FundMonitorValidationError(f"{field_name} must be a lowercase controlled identifier")
    if _SECRETISH_RE.search(text) or _SECRET_PREFIX_RE.search(text):
        raise FundMonitorValidationError(f"{field_name} may not contain private routing or secret material")
    return text


def _enum(value: object, allowed: frozenset[str], field_name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise FundMonitorValidationError(f"{field_name} is unsupported")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FundMonitorValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _strict_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise FundMonitorValidationError(f"{field_name} must be bool")
    return value


@dataclass(frozen=True)
class FundSource:
    """One privacy-minimized source-health observation."""

    source_key: str
    source_tier: str
    health: str
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_key", _identifier(self.source_key, "source_key"))
        object.__setattr__(self, "source_tier", _enum(self.source_tier, SOURCE_TIERS, "source_tier"))
        object.__setattr__(self, "health", _enum(self.health, SOURCE_HEALTH, "health"))
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "source.observed_at"))


@dataclass(frozen=True)
class FundEvidence:
    """Structured evidence with no raw text or location identifiers."""

    evidence_key: str
    source_key: str
    category: str
    evidence_type: str
    dimension: str
    assessment: str
    observed_at: datetime
    material_change: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_key", _identifier(self.evidence_key, "evidence_key"))
        object.__setattr__(self, "source_key", _identifier(self.source_key, "evidence.source_key"))
        object.__setattr__(self, "category", _identifier(self.category, "evidence.category"))
        evidence_type = _enum(self.evidence_type, EVIDENCE_TYPES, "evidence_type")
        object.__setattr__(self, "evidence_type", evidence_type)
        object.__setattr__(self, "dimension", _enum(self.dimension, DIMENSIONS, "evidence.dimension"))
        assessment = _enum(self.assessment, ASSESSMENTS, "evidence.assessment")
        object.__setattr__(self, "assessment", assessment)
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "evidence.observed_at"))
        material = _strict_bool(self.material_change, "evidence.material_change")
        object.__setattr__(self, "material_change", material)
        if material and self.category not in EVENT_CATEGORIES:
            raise FundMonitorValidationError("material_change is allowed only for controlled event categories")
        if evidence_type == "SOCIAL_SIGNAL":
            if assessment not in {"lead", "unknown"}:
                raise FundMonitorValidationError("social evidence must remain a weak lead")
        elif assessment == "lead":
            raise FundMonitorValidationError("lead assessment is reserved for social evidence")


def compute_event_acknowledgement_key(
    fund_key: str,
    evidence: FundEvidence,
) -> str:
    """Return the immutable SHA-256 identity of one structured event record."""

    normalized_fund_key = _identifier(fund_key, "fund_key", fund=True)
    if not isinstance(evidence, FundEvidence):
        raise FundMonitorValidationError("evidence must be FundEvidence")
    payload = {
        "assessment": evidence.assessment,
        "category": evidence.category,
        "dimension": evidence.dimension,
        "evidence_key": evidence.evidence_key,
        "evidence_type": evidence.evidence_type,
        "fund_key": normalized_fund_key,
        "material_change": evidence.material_change,
        "observed_at": evidence.observed_at.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z"),
        "source_key": evidence.source_key,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class FundMetric:
    """A point-in-time Decimal metric or an explicit UNKNOWN/NA marker."""

    metric_key: str
    source_key: str
    category: str
    evidence_type: str
    dimension: str
    observed_at: datetime
    value_status: str
    value: Decimal | None
    unit: str
    assessment: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_key", _identifier(self.metric_key, "metric_key"))
        object.__setattr__(self, "source_key", _identifier(self.source_key, "metric.source_key"))
        object.__setattr__(self, "category", _identifier(self.category, "metric.category"))
        evidence_type = _enum(
            self.evidence_type,
            frozenset({"FACT", "CALCULATION"}),
            "metric.evidence_type",
        )
        object.__setattr__(self, "evidence_type", evidence_type)
        object.__setattr__(self, "dimension", _enum(self.dimension, DIMENSIONS, "metric.dimension"))
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "metric.observed_at"))
        value_status = _enum(self.value_status, METRIC_VALUE_STATUSES, "metric.value_status")
        object.__setattr__(self, "value_status", value_status)
        object.__setattr__(self, "unit", _enum(self.unit, METRIC_UNITS, "metric.unit"))
        assessment = _enum(
            self.assessment,
            frozenset({"supports", "watch", "reject", "unknown", "not_applicable"}),
            "metric.assessment",
        )
        object.__setattr__(self, "assessment", assessment)
        if self.value is not None and not isinstance(self.value, Decimal):
            raise FundMonitorValidationError("metric.value must be Decimal or None")
        if isinstance(self.value, Decimal) and not self.value.is_finite():
            raise FundMonitorValidationError("metric.value must be finite")
        if value_status == "known":
            if self.value is None:
                raise FundMonitorValidationError("known metric requires a Decimal value")
            if assessment in {"unknown", "not_applicable"}:
                raise FundMonitorValidationError("known metric requires a substantive assessment")
        else:
            if self.value is not None:
                raise FundMonitorValidationError("UNKNOWN/NA metric must use None, never zero")
            expected = "unknown" if value_status == "unknown" else "not_applicable"
            if assessment != expected:
                raise FundMonitorValidationError("metric status and assessment disagree")


@dataclass(frozen=True)
class LastCompleted:
    daily: datetime | None
    monthly: datetime | None
    quarterly: datetime | None
    annual: datetime | None

    def __post_init__(self) -> None:
        for name in ("daily", "monthly", "quarterly", "annual"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, f"last_completed.{name}"))


@dataclass(frozen=True)
class FundMonitorRequest:
    fund_key: str
    as_of: datetime
    legal_structure: str
    economic_structure: str
    portfolio_role: str
    last_completed: LastCompleted
    sources: tuple[FundSource, ...]
    evidence: tuple[FundEvidence, ...]
    metrics: tuple[FundMetric, ...]
    review_timezone: str = "Asia/Shanghai"
    acknowledged_event_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fund_key", _identifier(self.fund_key, "fund_key", fund=True))
        object.__setattr__(self, "as_of", _aware(self.as_of, "as_of"))
        object.__setattr__(self, "legal_structure", _enum(self.legal_structure, LEGAL_STRUCTURES, "legal_structure"))
        object.__setattr__(self, "economic_structure", _enum(self.economic_structure, ECONOMIC_STRUCTURES, "economic_structure"))
        object.__setattr__(self, "portfolio_role", _enum(self.portfolio_role, PORTFOLIO_ROLES, "portfolio_role"))
        if not isinstance(self.last_completed, LastCompleted):
            raise FundMonitorValidationError("last_completed must be LastCompleted")
        for name in ("sources", "evidence", "metrics"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                raise FundMonitorValidationError(f"{name} must be a tuple")
        if self.review_timezone not in REVIEW_TIMEZONES:
            raise FundMonitorValidationError("review_timezone is unsupported")
        if not isinstance(self.acknowledged_event_keys, tuple):
            raise FundMonitorValidationError("acknowledged_event_keys must be a tuple")
        acknowledged: list[str] = []
        for item in self.acknowledged_event_keys:
            if not isinstance(item, str) or not _SHA256_RE.fullmatch(item):
                raise FundMonitorValidationError(
                    "acknowledged_event_keys must contain lowercase SHA-256 digests"
                )
            acknowledged.append(item)
        acknowledged_tuple = tuple(sorted(acknowledged))
        if len(acknowledged_tuple) != len(set(acknowledged_tuple)):
            raise FundMonitorValidationError("acknowledged_event_keys contains duplicates")
        object.__setattr__(self, "acknowledged_event_keys", acknowledged_tuple)


@dataclass(frozen=True)
class CadenceState:
    cadence: str
    due: bool
    triggered: bool
    next_due: datetime | None


@dataclass(frozen=True)
class SourceCoverage:
    required_categories: tuple[str, ...]
    category_cutoffs: tuple[tuple[str, datetime], ...]
    source_health_cutoff: datetime
    covered_categories: tuple[str, ...]
    missing_categories: tuple[str, ...]
    unknown_categories: tuple[str, ...]
    degraded_categories: tuple[str, ...]
    stale_categories: tuple[str, ...]
    social_only_categories: tuple[str, ...]
    ratio: Decimal | None
    healthy_primary_source_count: int
    healthy_calculated_source_count: int


@dataclass(frozen=True)
class DimensionAssessment:
    status: str
    summary_code: str
    summary_zh: str
    reason_codes: tuple[str, ...]
    coverage: SourceCoverage
    verified_structural_hard_reject: bool


@dataclass(frozen=True)
class FundMonitorResult:
    fund_key: str
    as_of: datetime
    review_timezone: str
    status: str
    summary_code: str
    summary_zh: str
    product_quality_status: str
    portfolio_fit_status: str
    product_quality: DimensionAssessment
    portfolio_fit: DimensionAssessment
    triggered_cadences: tuple[str, ...]
    triggered_event_keys: tuple[str, ...]
    schedules: tuple[CadenceState, ...]
    next_due: datetime
    source_coverage: SourceCoverage
    research_only: bool = field(default=True, init=False)
    broker_access: bool = field(default=False, init=False)
    can_trade: bool = field(default=False, init=False)
    can_submit_order: bool = field(default=False, init=False)
    can_change_position: bool = field(default=False, init=False)
    can_change_dca: bool = field(default=False, init=False)
    automatic_execution: bool = field(default=False, init=False)


_SUMMARY_ZH: Mapping[str, Mapping[str, str]] = {
    "overall": {
        "PASS": "本期基金监控通过；产品与组合适配证据未显示需要升级处理的问题。",
        "WATCH": "本期基金监控进入观察；存在已确认但仍需持续复核的变化或风险。",
        "REJECT": "本期基金监控不通过；可靠证据支持否决性结论。",
        "NEED_INFO": "本期基金监控信息不足；关键一级来源或指标缺失，暂不下结论。",
        "NOT_DUE": "本期尚未到复核时点，且没有重大事件触发。",
        "PARTIAL_NOT_DUE": "本期仅完成一个研究维度，另一维度未到复核时点；不得视为完整投资通过。",
    },
    "product_quality": {
        "PASS": "产品质量监控通过；结构和来源证据未显示需要升级处理的问题。",
        "WATCH": "产品质量进入观察；存在已确认但仍需持续复核的变化或风险。",
        "REJECT": "产品质量不通过；可靠证据支持否决性结论。",
        "NEED_INFO": "产品质量信息不足；关键一级来源或指标缺失，暂不下结论。",
        "NOT_DUE": "产品质量本期未到复核时点，且无重大事件触发。",
    },
    "portfolio_fit": {
        "PASS": "组合适配监控通过；边际贡献与实施约束未显示需要升级处理的问题。",
        "WATCH": "组合适配进入观察；现有角色或实施约束需要持续复核。",
        "REJECT": "组合适配不通过；可靠证据支持否决性结论。",
        "NEED_INFO": "组合适配信息不足；关键约束或边际贡献证据缺失，暂不下结论。",
        "NOT_DUE": "组合适配本期未到复核时点，且无重大事件触发。",
    },
}


def _next_month(value: datetime, zone: ZoneInfo) -> datetime:
    year, month = value.year, value.month
    if month == 12:
        return datetime(year + 1, 1, 1, tzinfo=zone)
    return datetime(year, month + 1, 1, tzinfo=zone)


def _next_quarter(value: datetime, zone: ZoneInfo) -> datetime:
    quarter = (value.month - 1) // 3
    next_month = quarter * 3 + 4
    if next_month > 12:
        return datetime(value.year + 1, 1, 1, tzinfo=zone)
    return datetime(value.year, next_month, 1, tzinfo=zone)


def _scheduled_boundary(cadence: str, completed: datetime, zone: ZoneInfo) -> datetime:
    local_completed = completed.astimezone(zone)
    if cadence == "daily":
        midnight = datetime(
            local_completed.year,
            local_completed.month,
            local_completed.day,
            tzinfo=zone,
        )
        return (midnight + timedelta(days=1)).astimezone(timezone.utc)
    if cadence == "monthly":
        return _next_month(local_completed, zone).astimezone(timezone.utc)
    if cadence == "quarterly":
        return _next_quarter(local_completed, zone).astimezone(timezone.utc)
    if cadence == "annual":
        return datetime(
            local_completed.year + 1, 1, 1, tzinfo=zone
        ).astimezone(timezone.utc)
    raise AssertionError(cadence)


def _schedules(request: FundMonitorRequest, material_event: bool) -> tuple[CadenceState, ...]:
    result: list[CadenceState] = []
    zone = ZoneInfo(request.review_timezone)
    for cadence in ("daily", "monthly", "quarterly", "annual"):
        completed = getattr(request.last_completed, cadence)
        if completed is not None and completed > request.as_of:
            raise FundMonitorValidationError(f"last_completed.{cadence} may not be in the future")
        boundary = (
            None
            if completed is None
            else _scheduled_boundary(cadence, completed, zone)
        )
        due = boundary is None or request.as_of >= boundary
        result.append(
            CadenceState(
                cadence=cadence,
                due=due,
                triggered=due,
                next_due=request.as_of if due else boundary,
            )
        )
        if cadence == "daily":
            result.append(
                CadenceState(
                    cadence="event",
                    due=material_event,
                    triggered=material_event,
                    next_due=request.as_of if material_event else None,
                )
            )
    return tuple(result)


def _branch_requirements(request: FundMonitorRequest) -> frozenset[str]:
    return _LEGAL_BRANCH_REQUIREMENTS.get(request.legal_structure, frozenset()) | _ECONOMIC_BRANCH_REQUIREMENTS.get(request.economic_structure, frozenset())


def _dimension_matches(value: str, dimension: str) -> bool:
    return value == dimension or value == "both"


def _qualifying_tier(category: str, source: FundSource) -> bool:
    if category in _PRIMARY_REQUIRED:
        return source.source_tier == "primary"
    return source.source_tier in {"primary", "calculated"}


def _coverage_ratio(covered: int, required: int) -> Decimal | None:
    if required == 0:
        return None
    with localcontext(_CALC_CONTEXT):
        return (Decimal(covered) / Decimal(required)).quantize(_RATIO_QUANTUM)


def _freshness_cutoff(as_of: datetime, cadence: str) -> datetime:
    return as_of - timedelta(days=FRESHNESS_DAYS[cadence])


def _add_requirement(
    requirements: dict[str, datetime],
    category: str,
    *,
    cadence: str,
    as_of: datetime,
) -> None:
    native_cadence = (
        "event" if cadence == "event" else _CATEGORY_CADENCE.get(category, cadence)
    )
    cutoff = _freshness_cutoff(as_of, native_cadence)
    prior = requirements.get(category)
    if prior is None or cutoff > prior:
        requirements[category] = cutoff


def _evidence_kind_can_cover(category: str, evidence_type: str) -> bool:
    if category in _FACT_ONLY_CATEGORIES:
        return evidence_type == "FACT"
    return evidence_type in {"FACT", "CALCULATION"}


def _coverage(
    required: Mapping[str, datetime],
    *,
    dimension: str,
    sources: Mapping[str, FundSource],
    evidence: tuple[FundEvidence, ...],
    metrics: tuple[FundMetric, ...],
    source_health_cutoff: datetime,
) -> SourceCoverage:
    covered: set[str] = set()
    missing: set[str] = set()
    unknown: set[str] = set()
    degraded: set[str] = set()
    stale: set[str] = set()
    social_only: set[str] = set()

    for category in sorted(required):
        category_cutoff = required[category]
        category_evidence = [
            item
            for item in evidence
            if item.category == category and _dimension_matches(item.dimension, dimension)
        ]
        category_metrics = [
            item
            for item in metrics
            if item.category == category and _dimension_matches(item.dimension, dimension)
        ]
        usable = False
        current_unknown = False
        saw_stale = False
        saw_degraded = False
        saw_fresh_social = False
        for item in (*category_evidence, *category_metrics):
            source = sources[item.source_key]
            if (
                item.observed_at < category_cutoff
                or source.observed_at < source_health_cutoff
            ):
                saw_stale = True
                continue
            if item.evidence_type == "SOCIAL_SIGNAL":
                saw_fresh_social = True
                continue
            if source.health != "healthy":
                saw_degraded = True
                continue
            if not _qualifying_tier(category, source):
                continue
            if not _evidence_kind_can_cover(category, item.evidence_type):
                continue
            if isinstance(item, FundMetric):
                if item.value_status == "unknown":
                    current_unknown = True
                elif item.value_status == "known":
                    usable = True
            elif item.assessment == "unknown":
                current_unknown = True
            elif item.assessment != "not_applicable":
                usable = True

        if usable and not current_unknown:
            covered.add(category)
        elif current_unknown:
            unknown.add(category)
        else:
            missing.add(category)
            if saw_stale:
                stale.add(category)
            if saw_degraded:
                degraded.add(category)
            if saw_fresh_social:
                social_only.add(category)

    denominator = len(required)
    ratio = _coverage_ratio(len(covered), denominator)
    return SourceCoverage(
        required_categories=tuple(sorted(required)),
        category_cutoffs=tuple(sorted(required.items())),
        source_health_cutoff=source_health_cutoff,
        covered_categories=tuple(sorted(covered)),
        missing_categories=tuple(sorted(missing)),
        unknown_categories=tuple(sorted(unknown)),
        degraded_categories=tuple(sorted(degraded)),
        stale_categories=tuple(sorted(stale)),
        social_only_categories=tuple(sorted(social_only)),
        ratio=ratio,
        healthy_primary_source_count=sum(
            source.source_tier == "primary"
            and source.health == "healthy"
            and source.observed_at >= source_health_cutoff
            for source in sources.values()
        ),
        healthy_calculated_source_count=sum(
            source.source_tier == "calculated"
            and source.health == "healthy"
            and source.observed_at >= source_health_cutoff
            for source in sources.values()
        ),
    )


def _assessment(
    *,
    dimension: str,
    due: bool,
    material_event: bool,
    coverage: SourceCoverage,
    evidence: tuple[FundEvidence, ...],
    metrics: tuple[FundMetric, ...],
    sources: Mapping[str, FundSource],
    extra_need_info: tuple[str, ...],
    required_cutoffs: Mapping[str, datetime],
    source_health_cutoff: datetime,
    hard_gate_blocked: bool = False,
    verified_structural_hard_reject: bool = False,
) -> DimensionAssessment:
    reasons: set[str] = set(extra_need_info)
    for category in coverage.missing_categories:
        reasons.add(f"missing_required_{category}")
    for category in coverage.unknown_categories:
        reasons.add(f"unknown_required_{category}")
    for category in coverage.degraded_categories:
        reasons.add(f"source_not_healthy_{category}")
    for category in coverage.stale_categories:
        reasons.add(f"stale_required_{category}")
    for category in coverage.social_only_categories:
        reasons.add(f"social_signal_unconfirmed_{category}")

    risk_observation_universe = [
        item
        for item in (*evidence, *metrics)
        if _dimension_matches(item.dimension, dimension)
        and item.category in coverage.required_categories
        and item.evidence_type != "SOCIAL_SIGNAL"
        and sources[item.source_key].health == "healthy"
        and sources[item.source_key].observed_at >= source_health_cutoff
        and item.observed_at >= required_cutoffs[item.category]
        and _qualifying_tier(item.category, sources[item.source_key])
    ]
    coverage_eligible = [
        item
        for item in risk_observation_universe
        if _evidence_kind_can_cover(item.category, item.evidence_type)
    ]
    nonconfirming_negative_observations = [
        item
        for item in risk_observation_universe
        if not _evidence_kind_can_cover(item.category, item.evidence_type)
        and item.assessment in {"watch", "reject"}
    ]
    confirmed_reject = any(
        item.assessment == "reject" and item.evidence_type in {"FACT", "CALCULATION"}
        for item in coverage_eligible
    )
    confirmed_watch = any(item.assessment == "watch" for item in coverage_eligible)
    if nonconfirming_negative_observations:
        reasons.add("nonconfirming_risk_observation")
        if any(
            item.evidence_type in {"INFERENCE", "JUDGMENT"}
            for item in nonconfirming_negative_observations
        ):
            reasons.add("judgment_inference_risk_observation")
        if any(
            item.evidence_type in {"FACT", "CALCULATION"}
            for item in nonconfirming_negative_observations
        ):
            reasons.add("coverage_ineligible_fact_calculation_risk_observation")
    if hard_gate_blocked or extra_need_info:
        status = "NEED_INFO"
        if hard_gate_blocked:
            reasons.add("structure_hard_gate_blocked")
    elif verified_structural_hard_reject:
        status = "REJECT"
        reasons.add("verified_structural_hard_reject")
    elif (
        coverage.missing_categories
        or coverage.unknown_categories
        or coverage.degraded_categories
        or coverage.stale_categories
    ):
        status = "NEED_INFO"
    elif confirmed_reject:
        status = "REJECT"
        reasons.add("confirmed_reject_signal")
    elif confirmed_watch or nonconfirming_negative_observations or material_event:
        status = "WATCH"
        if confirmed_watch:
            reasons.add("confirmed_watch_signal")
        if material_event:
            reasons.add("material_event_under_review")
    elif due:
        status = "PASS"
        reasons.add("scheduled_review_complete")
    else:
        status = "NOT_DUE"
        reasons.add("review_not_due")

    return DimensionAssessment(
        status=status,
        summary_code=f"fund_monitor.{dimension}.{status.lower()}",
        summary_zh=_SUMMARY_ZH[dimension][status],
        reason_codes=tuple(sorted(reasons)),
        coverage=coverage,
        verified_structural_hard_reject=verified_structural_hard_reject,
    )


def _combine_coverage(left: SourceCoverage, right: SourceCoverage) -> SourceCoverage:
    def scoped(prefix: str, values: tuple[str, ...]) -> set[str]:
        return {f"{prefix}.{value}" for value in values}

    required = scoped("product_quality", left.required_categories) | scoped(
        "portfolio_fit", right.required_categories
    )
    covered = scoped("product_quality", left.covered_categories) | scoped(
        "portfolio_fit", right.covered_categories
    )
    missing = scoped("product_quality", left.missing_categories) | scoped(
        "portfolio_fit", right.missing_categories
    )
    unknown = scoped("product_quality", left.unknown_categories) | scoped(
        "portfolio_fit", right.unknown_categories
    )
    degraded = scoped("product_quality", left.degraded_categories) | scoped(
        "portfolio_fit", right.degraded_categories
    )
    stale = scoped("product_quality", left.stale_categories) | scoped(
        "portfolio_fit", right.stale_categories
    )
    social_only = scoped("product_quality", left.social_only_categories) | scoped(
        "portfolio_fit", right.social_only_categories
    )
    category_cutoffs = {
        **{
            f"product_quality.{category}": cutoff
            for category, cutoff in left.category_cutoffs
        },
        **{
            f"portfolio_fit.{category}": cutoff
            for category, cutoff in right.category_cutoffs
        },
    }
    if left.source_health_cutoff != right.source_health_cutoff:
        raise AssertionError("dimension source-health cutoffs must match")
    ratio = _coverage_ratio(len(covered), len(required))
    return SourceCoverage(
        required_categories=tuple(sorted(required)),
        category_cutoffs=tuple(sorted(category_cutoffs.items())),
        source_health_cutoff=left.source_health_cutoff,
        covered_categories=tuple(sorted(covered)),
        missing_categories=tuple(sorted(missing)),
        unknown_categories=tuple(sorted(unknown)),
        degraded_categories=tuple(sorted(degraded)),
        stale_categories=tuple(sorted(stale)),
        social_only_categories=tuple(sorted(social_only)),
        ratio=ratio,
        healthy_primary_source_count=max(left.healthy_primary_source_count, right.healthy_primary_source_count),
        healthy_calculated_source_count=max(left.healthy_calculated_source_count, right.healthy_calculated_source_count),
    )


def monitor_fund(request: FundMonitorRequest) -> FundMonitorResult:
    """Evaluate due cadences and material events without network or trade access."""

    if not isinstance(request, FundMonitorRequest):
        raise FundMonitorValidationError("request must be FundMonitorRequest")

    if not all(isinstance(item, FundSource) for item in request.sources):
        raise FundMonitorValidationError("sources must contain FundSource")
    if not all(isinstance(item, FundEvidence) for item in request.evidence):
        raise FundMonitorValidationError("evidence must contain FundEvidence")
    if not all(isinstance(item, FundMetric) for item in request.metrics):
        raise FundMonitorValidationError("metrics must contain FundMetric")
    source_rows = sorted(request.sources, key=lambda item: item.source_key)
    evidence_rows = tuple(sorted(request.evidence, key=lambda item: item.evidence_key))
    metric_rows = tuple(sorted(request.metrics, key=lambda item: item.metric_key))
    sources: dict[str, FundSource] = {}
    for source in source_rows:
        if not isinstance(source, FundSource):
            raise FundMonitorValidationError("sources must contain FundSource")
        if source.source_key in sources:
            raise FundMonitorValidationError("duplicate source_key")
        if source.observed_at > request.as_of:
            raise FundMonitorValidationError("source observation may not be in the future")
        sources[source.source_key] = source

    seen_evidence: set[str] = set()
    for item in evidence_rows:
        if not isinstance(item, FundEvidence):
            raise FundMonitorValidationError("evidence must contain FundEvidence")
        if item.evidence_key in seen_evidence:
            raise FundMonitorValidationError("duplicate evidence_key")
        seen_evidence.add(item.evidence_key)
        if item.source_key not in sources:
            raise FundMonitorValidationError("evidence references unknown source_key")
        if item.observed_at > request.as_of:
            raise FundMonitorValidationError("evidence observation may not be in the future")

    seen_metrics: set[str] = set()
    for item in metric_rows:
        if not isinstance(item, FundMetric):
            raise FundMonitorValidationError("metrics must contain FundMetric")
        if item.metric_key in seen_metrics:
            raise FundMonitorValidationError("duplicate metric_key")
        seen_metrics.add(item.metric_key)
        if item.source_key not in sources:
            raise FundMonitorValidationError("metric references unknown source_key")
        if item.observed_at > request.as_of:
            raise FundMonitorValidationError("metric observation may not be in the future")

    event_cutoff = _freshness_cutoff(request.as_of, "event")
    acknowledged = set(request.acknowledged_event_keys)
    fresh_material_events = tuple(
        item
        for item in evidence_rows
        if item.material_change
        and item.observed_at >= event_cutoff
    )
    event_key_by_evidence = {
        item.evidence_key: compute_event_acknowledgement_key(request.fund_key, item)
        for item in fresh_material_events
    }
    current_event_keys = set(event_key_by_evidence.values())
    unknown_acknowledgements = acknowledged - current_event_keys
    if unknown_acknowledgements:
        raise FundMonitorValidationError(
            "acknowledged_event_keys contains a pre-confirmation, stale, or unknown digest"
        )
    material_events = tuple(
        item
        for item in fresh_material_events
        if event_key_by_evidence[item.evidence_key] not in acknowledged
    )
    triggered_event_keys = tuple(
        sorted(event_key_by_evidence[item.evidence_key] for item in material_events)
    )
    schedules = _schedules(request, bool(material_events))
    triggered = tuple(state.cadence for state in schedules if state.triggered)

    product_required: dict[str, datetime] = {}
    fit_required: dict[str, datetime] = {}
    for cadence in triggered:
        for category in _PRODUCT_REQUIREMENTS.get(cadence, frozenset()):
            _add_requirement(
                product_required,
                category,
                cadence=cadence,
                as_of=request.as_of,
            )
        for category in _FIT_REQUIREMENTS.get(cadence, frozenset()):
            _add_requirement(
                fit_required,
                category,
                cadence=cadence,
                as_of=request.as_of,
            )
    if triggered:
        for category in {
            "legal_structure",
            "economic_structure",
            *_branch_requirements(request),
        }:
            _add_requirement(
                product_required,
                category,
                cadence="annual",
                as_of=request.as_of,
            )
    if material_events:
        for item in material_events:
            _add_requirement(
                product_required,
                item.category,
                cadence="event",
                as_of=request.as_of,
            )
        _add_requirement(
            fit_required,
            "portfolio_impact",
            cadence="event",
            as_of=request.as_of,
        )

    product_extra: list[str] = []
    fit_extra: list[str] = []
    if product_required and request.legal_structure == "unknown":
        product_extra.append("unknown_legal_structure")
    if product_required and request.economic_structure == "unknown":
        product_extra.append("unknown_economic_structure")
    if fit_required and request.portfolio_role == "unknown":
        fit_extra.append("unknown_portfolio_role")

    source_health_cutoff = _freshness_cutoff(request.as_of, "daily")
    product_coverage = _coverage(
        product_required,
        dimension="product_quality",
        sources=sources,
        evidence=evidence_rows,
        metrics=metric_rows,
        source_health_cutoff=source_health_cutoff,
    )
    fit_coverage = _coverage(
        fit_required,
        dimension="portfolio_fit",
        sources=sources,
        evidence=evidence_rows,
        metrics=metric_rows,
        source_health_cutoff=source_health_cutoff,
    )
    product_due = bool(product_required)
    fit_due = bool(fit_required)
    structural_gaps = {"legal_structure", "economic_structure"} & set(
        product_coverage.missing_categories
        + product_coverage.unknown_categories
        + product_coverage.degraded_categories
        + product_coverage.stale_categories
    )
    structure_hard_gate = bool(product_required) and bool(
        structural_gaps
        or request.legal_structure == "unknown"
        or request.economic_structure == "unknown"
    )
    verified_structural_hard_reject = any(
        item.category in _FACT_ONLY_CATEGORIES
        and item.category in product_required
        and _dimension_matches(item.dimension, "product_quality")
        and item.evidence_type == "FACT"
        and item.assessment == "reject"
        and item.observed_at >= product_required[item.category]
        and sources[item.source_key].source_tier == "primary"
        and sources[item.source_key].health == "healthy"
        and sources[item.source_key].observed_at >= source_health_cutoff
        for item in evidence_rows
    )
    product = _assessment(
        dimension="product_quality",
        due=product_due,
        material_event=bool(material_events),
        coverage=product_coverage,
        evidence=evidence_rows,
        metrics=metric_rows,
        sources=sources,
        extra_need_info=tuple(product_extra),
        required_cutoffs=product_required,
        source_health_cutoff=source_health_cutoff,
        hard_gate_blocked=structure_hard_gate,
        verified_structural_hard_reject=verified_structural_hard_reject,
    )
    portfolio_fit = _assessment(
        dimension="portfolio_fit",
        due=fit_due,
        material_event=bool(material_events),
        coverage=fit_coverage,
        evidence=evidence_rows,
        metrics=metric_rows,
        sources=sources,
        extra_need_info=tuple(fit_extra),
        required_cutoffs=fit_required,
        source_health_cutoff=source_health_cutoff,
    )

    statuses = {product.status, portfolio_fit.status}
    partial_not_due = statuses == {"PASS", "NOT_DUE"}
    if structure_hard_gate:
        status = "NEED_INFO"
    elif "REJECT" in statuses:
        status = "REJECT"
    elif "NEED_INFO" in statuses:
        status = "NEED_INFO"
    elif "WATCH" in statuses:
        status = "WATCH"
    elif statuses == {"PASS"}:
        status = "PASS"
    else:
        status = "NOT_DUE"
    assert status in STATUSES

    due_times = [state.next_due for state in schedules if state.next_due is not None]
    next_due = min(due_times) if due_times else request.as_of
    return FundMonitorResult(
        fund_key=request.fund_key,
        as_of=request.as_of,
        review_timezone=request.review_timezone,
        status=status,
        summary_code=(
            "fund_monitor.overall.partial_not_due"
            if partial_not_due
            else f"fund_monitor.overall.{status.lower()}"
        ),
        summary_zh=_SUMMARY_ZH["overall"][
            "PARTIAL_NOT_DUE" if partial_not_due else status
        ],
        product_quality_status=product.status,
        portfolio_fit_status=portfolio_fit.status,
        product_quality=product,
        portfolio_fit=portfolio_fit,
        triggered_cadences=triggered,
        triggered_event_keys=triggered_event_keys,
        schedules=schedules,
        next_due=next_due,
        source_coverage=_combine_coverage(product_coverage, fit_coverage),
    )


__all__ = [
    "ASSESSMENTS",
    "CADENCES",
    "DIMENSIONS",
    "ECONOMIC_STRUCTURES",
    "EVIDENCE_TYPES",
    "EVENT_CATEGORIES",
    "FRESHNESS_DAYS",
    "FUND_MONITOR_CATEGORIES",
    "FUND_MONITOR_REASON_CODES",
    "FUND_MONITOR_SCOPED_CATEGORIES",
    "FundEvidence",
    "FundMetric",
    "FundMonitorRequest",
    "FundMonitorResult",
    "FundMonitorValidationError",
    "FundSource",
    "LastCompleted",
    "LEGAL_STRUCTURES",
    "METRIC_UNITS",
    "PORTFOLIO_ROLES",
    "REVIEW_TIMEZONES",
    "SOURCE_HEALTH",
    "SOURCE_TIERS",
    "STATUSES",
    "compute_event_acknowledgement_key",
    "monitor_fund",
]
