"""Project aggregate research into the owner-only daily-report contract.

This module is deliberately a pure adapter.  It accepts only the already
aggregated outputs of :mod:`fund_monitor` and :mod:`social_heat`; it does not
read posts, files, credentials or network resources.  The projection is
research-only and cannot write the portfolio ledger, alter recurring
investment amounts or create an executable action.

The private-daily-report v1.1 contract carries structured fund, platform and
prediction-weight-state fields.  The report validator continues to replay
stored v1.0 documents, while new research snapshots always use v1.1 semantics.
Raw posts, URLs, author identities and prediction-event paths remain outside
the report.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from .fund_monitor import (
    FUND_MONITOR_REASON_CODES,
    FUND_MONITOR_SCOPED_CATEGORIES,
    FundMonitorResult,
)
from .prediction_ledger import (
    PREDICTION_WEIGHT_MARKET_REGIMES,
    PREDICTION_WEIGHT_MODEL_VERSIONS,
    PREDICTION_WEIGHT_REASON_CODES,
    PREDICTION_WEIGHT_TOPICS,
    PredictionWeightState,
)
from .private_daily_report import (
    SCHEMA_VERSION,
    PrivateDailyReportSemanticError,
    canonical_json,
    validate_private_daily_research_section,
)
from .social_heat import SOCIAL_DECISION_WEIGHT_CAP, SocialHeatResult


_ZERO = Decimal("0")
_OUTPUT_QUANTUM = Decimal("0.000000000001")
_FUND_KEY = re.compile(r"^(?=[A-Z0-9._-]*[A-Z])[A-Z0-9][A-Z0-9._-]{0,31}$")
_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SECRETISH = re.compile(
    r"(?:^|[_.-])(?:account|authorization|cookie|handle|password|passwd|"
    r"query|secret|token|url|username)(?:$|[_.-])",
    re.IGNORECASE,
)
_SOCIAL_STATUSES = frozenset(
    {"ok", "no_eligible_data", "no_current_data", "quarantined"}
)
_FUND_STATUSES = frozenset({"PASS", "WATCH", "REJECT", "NEED_INFO", "NOT_DUE"})
_PREDICTION_STATES = frozenset(
    {"active", "decayed", "quarantined", "research_only"}
)
_STATE_MULTIPLIER: Mapping[str, Decimal] = {
    "active": Decimal("1"),
    "decayed": Decimal("0.25"),
    "quarantined": _ZERO,
    "research_only": _ZERO,
}
_STATE_PRECEDENCE: Mapping[str, int] = {
    "active": 0,
    "decayed": 1,
    "research_only": 2,
    "quarantined": 3,
}
_REPORT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "private_daily_report.v1.schema.json"
)
_RESEARCH_SNAPSHOT_MAX_AGE = dt.timedelta(days=2)
_OVERALL_VIEW = (
    "基金监控与社交热度聚合快照已接入；仅供研究，"
    "不会改变固定定投、会计账本或形成自动交易。"
)
_SOCIAL_SUMMARY = "平台聚合热度仅供研究；不可单独触发调仓、退出或定投加码。"
_XHS_SUMMARY = "小红书聚合热度仅供注意力研究；直接执行权重固定为零。"
_STALE_FUND_REJECT_SUMMARY = (
    "研究快照已超过两天；已验证的拒绝结论保留，其他维度降为信息不足。"
)
_STALE_FUND_NEED_INFO_SUMMARY = (
    "研究快照已超过两天；原结论仅作历史线索，当前状态降为信息不足。"
)
_BASE_NOTES = frozenset(
    {
        "aggregate_research_snapshot_read_only",
        "research_cannot_change_ledger_dca_or_trades",
        "social_topic_detail_requires_separate_private_evidence",
    }
)
_OPTIONAL_NOTES = frozenset(
    {
        "fund_monitoring_research_only",
        "social_heat_research_only",
        "prediction_calibration_applied_to_social_candidate_score",
        "social_candidate_score_disabled_without_calibration",
        "prediction_calibration_research_only",
        "stale_fund_conclusion_downgraded",
        "social_research_snapshot_stale_candidate_score_disabled",
        "research_snapshot_stale_candidate_score_disabled",
    }
)
_ALLOWED_CADENCES = frozenset({"daily", "event", "monthly", "quarterly", "annual"})
_SOCIAL_HEAT_STATUS_NOTES = frozenset(
    f"social_heat_status_{status}" for status in _SOCIAL_STATUSES
)
_ADAPTER_FUND_REASON_CODES = frozenset(
    {
        "fund_monitor.material_event_triggered",
        "research_snapshot_stale",
    }
)
_ADAPTER_CALIBRATION_REASON_CODES = frozenset(
    {
        "research_snapshot_stale",
        "social_research_snapshot_stale",
    }
)


class PrivateResearchAdapterError(ValueError):
    """Raised when aggregate research cannot safely enter a private report."""


def _aware_utc(value: object, field_name: str) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        raise PrivateResearchAdapterError(f"{field_name}_must_be_datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PrivateResearchAdapterError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(dt.timezone.utc)


def _decimal(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise PrivateResearchAdapterError(f"{field_name}_must_be_decimal")
    if not value.is_finite():
        raise PrivateResearchAdapterError(f"{field_name}_must_be_finite")
    return value


def _fund_key(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not _FUND_KEY.fullmatch(value)
        or _SECRETISH.search(value)
    ):
        raise PrivateResearchAdapterError(f"{field_name}_must_be_controlled_fund_key")
    return value


def _reason_code(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not _REASON_CODE.fullmatch(value)
        or _SECRETISH.search(value)
    ):
        raise PrivateResearchAdapterError(f"{field_name}_must_be_controlled_reason_code")
    return value


def _utc_text(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_text(value: object, field_name: str) -> dt.datetime:
    if not isinstance(value, str):
        raise PrivateResearchAdapterError(f"{field_name}_must_be_date_time")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrivateResearchAdapterError(f"{field_name}_must_be_date_time") from exc
    return _aware_utc(parsed, field_name)


def _expected_projection_fund_status(
    product_status: str,
    fit_status: str,
) -> str:
    statuses = {product_status, fit_status}
    if "REJECT" in statuses:
        return "REJECT"
    if "NEED_INFO" in statuses:
        return "NEED_INFO"
    if "WATCH" in statuses:
        return "WATCH"
    if statuses == {"PASS"}:
        return "PASS"
    return "NOT_DUE"


def _downgrade_stale_fund_row(item: dict[str, Any]) -> None:
    if item["product_quality_status"] != "REJECT":
        item["product_quality_status"] = "NEED_INFO"
    if item["portfolio_fit_status"] != "REJECT":
        item["portfolio_fit_status"] = "NEED_INFO"
    item["status"] = _expected_projection_fund_status(
        item["product_quality_status"],
        item["portfolio_fit_status"],
    )
    item["reason_codes"] = sorted(
        {*item["reason_codes"], "research_snapshot_stale"}
    )
    item["summary"] = (
        _STALE_FUND_REJECT_SUMMARY
        if item["status"] == "REJECT"
        else _STALE_FUND_NEED_INFO_SUMMARY
    )


def _fund_summary(
    status: str,
    product_quality_status: str,
    portfolio_fit_status: str,
) -> str:
    return (
        f"基金监控状态={status}；"
        f"产品质量={product_quality_status}；"
        f"组合适配={portfolio_fit_status}；仅供研究。"
    )


def _disable_social_candidate_score(
    *,
    social: list[dict[str, Any]],
    calibration: list[dict[str, Any]],
    decision: dict[str, Any] | None,
    reason_code: str,
) -> None:
    for item in calibration:
        item["state"] = "research_only"
        item["reasons"] = sorted({*item["reasons"], reason_code})
    for item in social:
        if item["status"] not in {"blocked", "quarantined"}:
            item["status"] = "degraded"
        item["effective_execution_weight"] = "0"
        item["calibration_state"] = "research_only"
    if decision is not None:
        decision["effective_contribution"] = "0"
        decision["effective_execution_coverage"] = "0"
        decision["calibration_state"] = "research_only"


def _validate_fund_result(value: FundMonitorResult, *, as_of: dt.datetime) -> None:
    if not isinstance(value, FundMonitorResult):
        raise PrivateResearchAdapterError("fund_results_must_contain_monitor_results")
    if _aware_utc(value.as_of, "fund_result.as_of") > as_of:
        raise PrivateResearchAdapterError("fund_result_may_not_be_from_the_future")
    _fund_key(value.fund_key, "fund_result.fund_key")
    if value.status not in _FUND_STATUSES:
        raise PrivateResearchAdapterError("fund_result_status_is_unsupported")
    if (
        value.product_quality_status not in _FUND_STATUSES
        or value.portfolio_fit_status not in _FUND_STATUSES
    ):
        raise PrivateResearchAdapterError(
            "fund_result_dimension_status_is_unsupported"
        )
    if (
        not value.research_only
        or value.broker_access
        or value.can_trade
        or value.can_submit_order
        or value.can_change_position
        or value.can_change_dca
        or value.automatic_execution
    ):
        raise PrivateResearchAdapterError("fund_result_crosses_no_trade_boundary")
    for field_name, code in (
        ("summary_code", value.summary_code),
        *(('reason_code', item) for item in value.product_quality.reason_codes),
        *(('reason_code', item) for item in value.portfolio_fit.reason_codes),
    ):
        _reason_code(code, f"fund_result.{field_name}")
        if code not in FUND_MONITOR_REASON_CODES:
            raise PrivateResearchAdapterError(
                "fund_result_reason_code_is_not_monitor_controlled"
            )


def _validate_social_result(value: SocialHeatResult, *, as_of: dt.datetime) -> None:
    if not isinstance(value, SocialHeatResult):
        raise PrivateResearchAdapterError("social_heat_must_be_social_heat_result")
    if _aware_utc(value.as_of, "social_heat.as_of") > as_of:
        raise PrivateResearchAdapterError("social_heat_may_not_be_from_the_future")
    if value.status not in _SOCIAL_STATUSES:
        raise PrivateResearchAdapterError("social_heat_status_is_unsupported")
    if (
        not value.research_only
        or value.can_trigger_open
        or value.can_trigger_add
        or value.can_trigger_trim
        or value.can_trigger_exit
        or value.can_increase_dca
    ):
        raise PrivateResearchAdapterError("social_heat_crosses_no_trade_boundary")
    cap = _decimal(value.decision_weight_cap, "social_heat.decision_weight_cap")
    contribution = _decimal(
        value.decision_contribution,
        "social_heat.decision_contribution",
    )
    if cap < _ZERO or cap > SOCIAL_DECISION_WEIGHT_CAP:
        raise PrivateResearchAdapterError("social_heat_decision_cap_exceeds_five_percent")
    if abs(contribution) > cap:
        raise PrivateResearchAdapterError("social_heat_contribution_exceeds_cap")
    if value.quarantine and contribution != _ZERO:
        raise PrivateResearchAdapterError("quarantined_social_heat_must_have_zero_contribution")
    if value.status == "ok" and value.quarantine:
        raise PrivateResearchAdapterError("social_heat_status_quarantine_inconsistent")
    if value.status == "quarantined" and not value.quarantine:
        raise PrivateResearchAdapterError("social_heat_status_quarantine_inconsistent")
    if value.status in {"no_eligible_data", "no_current_data"} and value.platforms:
        raise PrivateResearchAdapterError("social_heat_empty_status_contains_platforms")
    if value.status == "quarantined" and any(
        not item.quarantine for item in value.platforms
    ):
        raise PrivateResearchAdapterError("social_heat_platform_quarantine_inconsistent")

    execution_weights = dict(value.execution_platform_weights)
    if execution_weights.get("xiaohongshu", _ZERO) != _ZERO:
        raise PrivateResearchAdapterError("xiaohongshu_execution_weight_must_be_zero")
    platform_names = [item.platform for item in value.platforms]
    if len(platform_names) != len(set(platform_names)):
        raise PrivateResearchAdapterError("social_heat_contains_duplicate_platforms")
    for platform in value.platforms:
        if platform.platform not in {"xiaohongshu", "x", "reddit", "other"}:
            raise PrivateResearchAdapterError("social_heat_platform_is_unsupported")
        if (
            platform.platform == "xiaohongshu"
            and _decimal(
                platform.normalized_execution_weight,
                "social_heat.xiaohongshu_execution_weight",
            )
            != _ZERO
        ):
            raise PrivateResearchAdapterError("xiaohongshu_execution_weight_must_be_zero")
        for field_name in (
            "attention_score",
            "manipulation_risk",
            "normalized_attention_weight",
            "normalized_execution_weight",
        ):
            number = _decimal(
                getattr(platform, field_name),
                f"social_heat.platform.{field_name}",
            )
            if number < _ZERO:
                raise PrivateResearchAdapterError(
                    f"social_heat_platform_{field_name}_must_be_nonnegative"
                )


def _validated_fund_results(
    value: object,
    *,
    as_of: dt.datetime,
) -> tuple[FundMonitorResult, ...]:
    if not isinstance(value, tuple):
        raise PrivateResearchAdapterError("fund_results_must_be_tuple")
    for item in value:
        _validate_fund_result(item, as_of=as_of)
    fund_keys = [item.fund_key for item in value]
    if len(fund_keys) != len(set(fund_keys)):
        raise PrivateResearchAdapterError("fund_results_contains_duplicate_fund_key")
    return tuple(sorted(value, key=lambda item: item.fund_key))


def _validated_prediction_states(
    value: object,
) -> tuple[PredictionWeightState, ...]:
    if not isinstance(value, tuple):
        raise PrivateResearchAdapterError("prediction_weight_states_must_be_tuple")
    rows: list[PredictionWeightState] = []
    identities: set[tuple[str, str, str, str, int]] = set()
    for item in value:
        if not isinstance(item, PredictionWeightState):
            raise PrivateResearchAdapterError(
                "prediction_weight_states_must_contain_weight_states"
            )
        if item.automatic_trading_permitted:
            raise PrivateResearchAdapterError(
                "prediction_weight_state_crosses_no_trade_boundary"
            )
        if item.platform not in {"xiaohongshu", "x", "reddit", "other"}:
            raise PrivateResearchAdapterError(
                "prediction_weight_state_platform_is_unsupported"
            )
        for field_name, raw in (
            ("topic", item.topic),
            ("model_version", item.model_version),
            ("market_regime", item.market_regime),
        ):
            _reason_code(raw, f"prediction_weight_state.{field_name}")
        if item.topic not in PREDICTION_WEIGHT_TOPICS:
            raise PrivateResearchAdapterError(
                "prediction_weight_state_topic_is_unsupported"
            )
        if item.model_version not in PREDICTION_WEIGHT_MODEL_VERSIONS:
            raise PrivateResearchAdapterError(
                "prediction_weight_state_model_version_is_unsupported"
            )
        if item.market_regime not in PREDICTION_WEIGHT_MARKET_REGIMES:
            raise PrivateResearchAdapterError(
                "prediction_weight_state_market_regime_is_unsupported"
            )
        if item.horizon not in {1, 5, 20, 60}:
            raise PrivateResearchAdapterError(
                "prediction_weight_state_horizon_is_unsupported"
            )
        if item.state not in _PREDICTION_STATES:
            raise PrivateResearchAdapterError(
                "prediction_weight_state_is_unsupported"
            )
        if (
            type(item.sample_count) is not int
            or type(item.recent_sample_count) is not int
            or item.sample_count < 0
            or item.recent_sample_count < 0
            or item.recent_sample_count > item.sample_count
        ):
            raise PrivateResearchAdapterError(
                "prediction_weight_state_sample_count_is_invalid"
            )
        for reason in item.reasons:
            _reason_code(reason, "prediction_weight_state.reason")
            if reason not in PREDICTION_WEIGHT_REASON_CODES:
                raise PrivateResearchAdapterError(
                    "prediction_weight_state_reason_is_not_ledger_controlled"
                )
        identity = (
            item.platform,
            item.topic,
            item.model_version,
            item.market_regime,
            item.horizon,
        )
        if identity in identities:
            raise PrivateResearchAdapterError(
                "prediction_weight_states_contains_duplicate_scope"
            )
        identities.add(identity)
        rows.append(item)
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                item.platform,
                item.topic,
                item.model_version,
                item.market_regime,
                item.horizon,
            ),
        )
    )


@dataclass(frozen=True, repr=False)
class PrivateResearchInput:
    """Sanitized aggregate inputs for one report preparation attempt."""

    as_of: dt.datetime
    fund_results: tuple[FundMonitorResult, ...] = field(default_factory=tuple, repr=False)
    social_heat: SocialHeatResult | None = field(default=None, repr=False)
    prediction_weight_states: tuple[PredictionWeightState, ...] = field(
        default_factory=tuple,
        repr=False,
    )

    def __post_init__(self) -> None:
        cutoff = _aware_utc(self.as_of, "as_of")
        object.__setattr__(self, "as_of", cutoff)
        object.__setattr__(
            self,
            "fund_results",
            _validated_fund_results(self.fund_results, as_of=cutoff),
        )
        if self.social_heat is not None:
            _validate_social_result(self.social_heat, as_of=cutoff)
        object.__setattr__(
            self,
            "prediction_weight_states",
            _validated_prediction_states(self.prediction_weight_states),
        )


@dataclass(frozen=True, repr=False)
class PrivateResearchProjection:
    """Detached report sections; all fields are aggregate and research-only."""

    research: Mapping[str, Any] = field(repr=False)
    source_health: tuple[Mapping[str, Any], ...] = field(repr=False)
    can_change_ledger: bool = field(default=False, init=False)
    can_change_dca: bool = field(default=False, init=False)
    can_create_trade_action: bool = field(default=False, init=False)


def _fund_health(result: FundMonitorResult) -> str:
    coverage = result.source_coverage
    if coverage.stale_categories or coverage.degraded_categories:
        return "degraded"
    if coverage.missing_categories or coverage.unknown_categories:
        return "partial"
    return "healthy"


def _fund_row(result: FundMonitorResult) -> dict[str, Any]:
    reasons = {
        result.summary_code,
        *result.product_quality.reason_codes,
        *result.portfolio_fit.reason_codes,
    }
    if result.triggered_event_keys:
        reasons.add("fund_monitor.material_event_triggered")
    return {
        "fund_key": result.fund_key,
        "status": result.status,
        "product_quality_status": result.product_quality_status,
        "portfolio_fit_status": result.portfolio_fit_status,
        "observed_at": _utc_text(result.as_of),
        "summary": _fund_summary(
            result.status,
            result.product_quality_status,
            result.portfolio_fit_status,
        ),
        "reason_codes": sorted(reasons),
        "triggered_cadences": sorted(result.triggered_cadences),
        "triggered_event_keys": sorted(result.triggered_event_keys),
        "next_due": _utc_text(result.next_due),
        "coverage_ratio": result.source_coverage.ratio,
        "missing_categories": sorted(result.source_coverage.missing_categories),
        "stale_categories": sorted(result.source_coverage.stale_categories),
        "degraded_categories": sorted(result.source_coverage.degraded_categories),
    }


def _social_direction(mean: Decimal | None, disagreement: Decimal | None) -> str:
    if mean is None:
        return "unknown"
    mean = _decimal(mean, "social_heat.sentiment_mean")
    if disagreement is not None:
        disagreement = _decimal(disagreement, "social_heat.sentiment_disagreement")
        if disagreement < _ZERO:
            raise PrivateResearchAdapterError(
                "social_heat_sentiment_disagreement_must_be_nonnegative"
            )
        if disagreement > _ZERO and disagreement >= abs(mean):
            return "mixed"
    if mean > _ZERO:
        return "positive"
    if mean < _ZERO:
        return "negative"
    return "neutral"


def _platform_calibration(
    states: tuple[PredictionWeightState, ...],
) -> dict[str, tuple[str, Decimal]]:
    grouped: dict[str, list[PredictionWeightState]] = {}
    for item in states:
        grouped.setdefault(item.platform, []).append(item)
    result: dict[str, tuple[str, Decimal]] = {}
    for platform in {"xiaohongshu", "x", "reddit", "other"}:
        platform_states = grouped.get(platform, [])
        if not platform_states:
            result[platform] = ("research_only", _ZERO)
            continue
        state = max(
            (item.state for item in platform_states),
            key=lambda item: _STATE_PRECEDENCE[item],
        )
        multiplier = min(_STATE_MULTIPLIER[item.state] for item in platform_states)
        result[platform] = (state, multiplier)
    # Xiaohongshu remains attention-only regardless of future calibration.
    result["xiaohongshu"] = (result["xiaohongshu"][0], _ZERO)
    return result


def _social_decision(
    result: SocialHeatResult | None,
    calibration: Mapping[str, tuple[str, Decimal]],
) -> dict[str, Any]:
    if result is None:
        return {
            "raw_contribution": _ZERO,
            "effective_contribution": _ZERO,
            "effective_execution_coverage": _ZERO,
            "decision_weight_cap": _ZERO,
            "calibration_state": "research_only",
            "research_only": True,
        }
    direction = _ZERO
    effective_coverage = _ZERO
    applicable_states: list[str] = []
    for platform in result.platforms:
        state, multiplier = calibration[platform.platform]
        if platform.normalized_execution_weight > _ZERO:
            applicable_states.append(state)
        effective_weight = platform.normalized_execution_weight * multiplier
        effective_coverage += effective_weight
        if platform.sentiment_mean is not None:
            direction += (
                effective_weight
                * platform.attention_score
                * platform.sentiment_mean
            )
    contribution = direction * result.decision_weight_cap
    contribution = max(
        -result.decision_weight_cap,
        min(result.decision_weight_cap, contribution),
    ).quantize(_OUTPUT_QUANTUM)
    effective_coverage = effective_coverage.quantize(_OUTPUT_QUANTUM)
    aggregate_state = (
        max(applicable_states, key=lambda item: _STATE_PRECEDENCE[item])
        if applicable_states
        else "research_only"
    )
    return {
        "raw_contribution": result.decision_contribution,
        "effective_contribution": contribution,
        "effective_execution_coverage": effective_coverage,
        "decision_weight_cap": result.decision_weight_cap,
        "calibration_state": aggregate_state,
        "research_only": True,
    }


def _calibration_rows(
    states: tuple[PredictionWeightState, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "platform": item.platform,
            "topic": item.topic,
            "model_version": item.model_version,
            "market_regime": item.market_regime,
            "horizon": item.horizon,
            "state": item.state,
            "sample_count": item.sample_count,
            "recent_sample_count": item.recent_sample_count,
            "reasons": sorted(item.reasons),
            "automatic_trading_permitted": False,
        }
        for item in states
    ]


def _social_rows(
    result: SocialHeatResult,
    calibration: Mapping[str, tuple[str, Decimal]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for platform in sorted(result.platforms, key=lambda item: item.platform):
        calibration_state, calibration_multiplier = calibration[platform.platform]
        rows.append(
            {
                "platform": platform.platform,
                "topic": "platform_aggregate",
                "direction": _social_direction(
                    platform.sentiment_mean,
                    platform.sentiment_disagreement,
                ),
                "status": "quarantined" if platform.quarantine else "healthy",
                "score": platform.attention_score,
                "attention_weight": platform.normalized_attention_weight,
                "candidate_execution_weight": platform.normalized_execution_weight,
                "calibration_state": calibration_state,
                "effective_execution_weight": (
                    platform.normalized_execution_weight * calibration_multiplier
                ),
                "research_only": True,
                "summary": (
                    _SOCIAL_SUMMARY
                    if platform.platform != "xiaohongshu"
                    else _XHS_SUMMARY
                ),
            }
        )
    return rows


def _social_health_status(result: SocialHeatResult) -> str:
    if result.status == "ok":
        return "healthy"
    if result.status == "no_current_data":
        return "partial"
    return "blocked"


@lru_cache(maxsize=1)
def _projection_validator() -> Draft202012Validator:
    try:
        base = json.loads(_REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrivateResearchAdapterError(
            "private_research_projection_schema_unavailable"
        ) from exc
    schema = {
        "$schema": base.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "$defs": base["$defs"],
        "type": "object",
        "additionalProperties": False,
        "required": ["research", "source_health"],
        "properties": {
            "research": {
                "allOf": [
                    {"$ref": "#/$defs/research"},
                    {
                        "type": "object",
                        "required": ["social_decision", "signal_calibration"],
                        "properties": {
                            "fund_monitoring": {
                                "type": "array",
                                "items": {
                                    "required": [
                                        "product_quality_status",
                                        "portfolio_fit_status",
                                        "observed_at",
                                        "triggered_cadences",
                                        "triggered_event_keys",
                                        "next_due",
                                        "coverage_ratio",
                                        "missing_categories",
                                        "stale_categories",
                                        "degraded_categories",
                                    ]
                                },
                            },
                            "social_attention": {
                                "type": "array",
                                "items": {
                                    "required": [
                                        "attention_weight",
                                        "candidate_execution_weight",
                                        "calibration_state",
                                        "effective_execution_weight",
                                    ]
                                },
                            },
                        },
                    },
                ]
            },
            "source_health": {
                "type": "array",
                "items": {"$ref": "#/$defs/source_health_item"},
            },
        },
    }
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def _require_controlled_codes(values: list[object], field_name: str) -> None:
    for value in values:
        _reason_code(value, field_name)


def _validate_closed_projection_contract(
    document: dict[str, Any],
    *,
    prepared_at: dt.datetime,
) -> None:
    """Accept only the aggregate vocabulary emitted by this adapter.

    The transport deliberately has no generic free-text lane.  This closes the
    owner-only request file against credentials, URLs, handles and a forged
    market/risk narrative even when a caller constructs a schema-valid object.
    """

    research = document["research"]
    health = document["source_health"]
    if research["overall_view"] != _OVERALL_VIEW:
        raise PrivateResearchAdapterError(
            "private_research_overall_view_must_match_adapter_template"
        )
    if research["market_regime"] != "unknown":
        raise PrivateResearchAdapterError(
            "private_research_market_regime_must_be_unknown"
        )
    if research["risk_budget_multiplier"] != "0":
        raise PrivateResearchAdapterError(
            "private_research_risk_budget_multiplier_must_be_zero"
        )

    funds = research["fund_monitoring"]
    fund_by_key: dict[str, dict[str, Any]] = {}
    stale_fund_keys: set[str] = set()
    any_stale_fund = False
    for fund in funds:
        key = _fund_key(fund["fund_key"], "fund.fund_key")
        fund_by_key[key] = fund
        observed_at = _parse_utc_text(fund["observed_at"], f"fund.{key}.observed_at")
        stale = prepared_at - observed_at > _RESEARCH_SNAPSHOT_MAX_AGE
        any_stale_fund = any_stale_fund or stale
        if stale:
            stale_fund_keys.add(key)
        expected_summary = _fund_summary(
            fund["status"],
            fund["product_quality_status"],
            fund["portfolio_fit_status"],
        )
        stale_summary = (
            _STALE_FUND_REJECT_SUMMARY
            if fund["status"] == "REJECT"
            else _STALE_FUND_NEED_INFO_SUMMARY
        )
        if fund["summary"] != expected_summary and not (
            stale and fund["summary"] == stale_summary
        ):
            raise PrivateResearchAdapterError(
                "private_research_fund_summary_must_match_adapter_template"
            )
        _require_controlled_codes(
            fund["reason_codes"],
            f"fund.{key}.reason_code",
        )
        if not set(fund["reason_codes"]).issubset(
            FUND_MONITOR_REASON_CODES | _ADAPTER_FUND_REASON_CODES
        ):
            raise PrivateResearchAdapterError(
                "private_research_fund_reason_is_not_monitor_controlled"
            )
        if not set(fund["triggered_cadences"]).issubset(_ALLOWED_CADENCES):
            raise PrivateResearchAdapterError(
                "private_research_fund_cadence_is_unsupported"
            )
        for field_name in (
            "missing_categories",
            "stale_categories",
            "degraded_categories",
        ):
            _require_controlled_codes(
                fund[field_name],
                f"fund.{key}.{field_name}",
            )
            if not set(fund[field_name]).issubset(FUND_MONITOR_SCOPED_CATEGORIES):
                raise PrivateResearchAdapterError(
                    "private_research_fund_category_is_not_monitor_controlled"
                )

    social = research["social_attention"]
    for item in social:
        if item["topic"] != "platform_aggregate":
            raise PrivateResearchAdapterError(
                "private_research_social_topic_must_be_platform_aggregate"
            )
        expected_summary = (
            _XHS_SUMMARY if item["platform"] == "xiaohongshu" else _SOCIAL_SUMMARY
        )
        if item["summary"] != expected_summary:
            raise PrivateResearchAdapterError(
                "private_research_social_summary_must_match_adapter_template"
            )

    calibration = research["signal_calibration"]
    for item in calibration:
        if item["topic"] not in PREDICTION_WEIGHT_TOPICS:
            raise PrivateResearchAdapterError(
                "private_research_calibration_topic_is_not_ledger_controlled"
            )
        if item["model_version"] not in PREDICTION_WEIGHT_MODEL_VERSIONS:
            raise PrivateResearchAdapterError(
                "private_research_calibration_model_is_not_ledger_controlled"
            )
        if item["market_regime"] not in PREDICTION_WEIGHT_MARKET_REGIMES:
            raise PrivateResearchAdapterError(
                "private_research_calibration_regime_is_not_ledger_controlled"
            )
        _require_controlled_codes(
            item["reasons"],
            "signal_calibration.reason",
        )
        if not set(item["reasons"]).issubset(
            PREDICTION_WEIGHT_REASON_CODES | _ADAPTER_CALIBRATION_REASON_CODES
        ):
            raise PrivateResearchAdapterError(
                "private_research_calibration_reason_is_not_ledger_controlled"
            )

    notes = set(research["notes"])
    _require_controlled_codes(list(notes), "research.note")
    if not _BASE_NOTES.issubset(notes):
        raise PrivateResearchAdapterError(
            "private_research_required_notes_missing"
        )
    if not notes.issubset(_BASE_NOTES | _OPTIONAL_NOTES | _SOCIAL_HEAT_STATUS_NOTES):
        raise PrivateResearchAdapterError(
            "private_research_note_is_not_adapter_controlled"
        )
    if ("fund_monitoring_research_only" in notes) != bool(funds):
        raise PrivateResearchAdapterError(
            "private_research_fund_note_mismatch"
        )
    if ("prediction_calibration_research_only" in notes) != bool(calibration):
        raise PrivateResearchAdapterError(
            "private_research_calibration_note_mismatch"
        )
    if "stale_fund_conclusion_downgraded" in notes and not any_stale_fund:
        raise PrivateResearchAdapterError(
            "private_research_stale_fund_note_without_stale_fund"
        )

    health_by_id = {item["source_id"]: item for item in health}
    snapshot = health_by_id.get("research.snapshot")
    if snapshot is None or snapshot["observed_at"] is None:
        raise PrivateResearchAdapterError(
            "private_research_snapshot_health_identity_invalid"
        )
    snapshot_observed_at = _parse_utc_text(
        snapshot["observed_at"],
        "research.snapshot.observed_at",
    )
    snapshot_stale = prepared_at - snapshot_observed_at > _RESEARCH_SNAPSHOT_MAX_AGE
    if (
        "research_snapshot_stale_candidate_score_disabled" in notes
        and not snapshot_stale
    ):
        raise PrivateResearchAdapterError(
            "private_research_stale_snapshot_note_without_stale_snapshot"
        )

    social_aggregate = health_by_id.get("research.social.aggregate")
    has_social = social_aggregate is not None
    if bool(social) and not has_social:
        raise PrivateResearchAdapterError(
            "private_research_social_rows_require_aggregate_health"
        )
    if ("social_heat_research_only" in notes) != has_social:
        raise PrivateResearchAdapterError(
            "private_research_social_note_mismatch"
        )
    social_status_notes = notes.intersection(_SOCIAL_HEAT_STATUS_NOTES)
    if len(social_status_notes) != (1 if has_social else 0):
        raise PrivateResearchAdapterError(
            "private_research_social_status_note_mismatch"
        )
    if has_social:
        expected_calibration_note = (
            "prediction_calibration_applied_to_social_candidate_score"
            if calibration
            else "social_candidate_score_disabled_without_calibration"
        )
        if expected_calibration_note not in notes:
            raise PrivateResearchAdapterError(
                "private_research_social_calibration_note_mismatch"
            )
    elif notes.intersection(
        {
            "prediction_calibration_applied_to_social_candidate_score",
            "social_candidate_score_disabled_without_calibration",
        }
    ):
        raise PrivateResearchAdapterError(
            "private_research_social_calibration_note_without_social_input"
        )

    social_stale = False
    if social_aggregate is not None:
        if social_aggregate["observed_at"] is None:
            raise PrivateResearchAdapterError(
                "private_research_social_health_observed_at_required"
            )
        social_observed_at = _parse_utc_text(
            social_aggregate["observed_at"],
            "research.social.aggregate.observed_at",
        )
        social_stale = prepared_at - social_observed_at > _RESEARCH_SNAPSHOT_MAX_AGE
    if (
        "social_research_snapshot_stale_candidate_score_disabled" in notes
        and not social_stale
    ):
        raise PrivateResearchAdapterError(
            "private_research_stale_social_note_without_stale_social_input"
        )
    if any(item["status"] == "degraded" for item in social) and not (
        social_stale or snapshot_stale
    ):
        raise PrivateResearchAdapterError(
            "private_research_social_row_degraded_without_stale_input"
        )

    expected_source_ids = {"research.snapshot"}
    expected_source_ids.update(f"research.fund.{key}" for key in fund_by_key)
    if has_social:
        expected_source_ids.add("research.social.aggregate")
    expected_source_ids.update(
        f"research.social.{item['platform']}" for item in social
    )
    calibration_source_states: dict[str, str] = {}
    for item in calibration:
        identity = "|".join(
            (
                item["platform"],
                item["topic"],
                item["model_version"],
                item["market_regime"],
                str(item["horizon"]),
            )
        )
        source_id = (
            "research.calibration."
            + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        )
        expected_source_ids.add(source_id)
        calibration_source_states[source_id] = item["state"]
    if set(health_by_id) != expected_source_ids:
        raise PrivateResearchAdapterError(
            "private_research_source_health_scope_mismatch"
        )

    for source_id, item in health_by_id.items():
        if item["required"] is not False or item["observed_at"] is None:
            raise PrivateResearchAdapterError(
                "private_research_source_health_boundary_invalid"
            )
        detail_code = item["detail_code"]
        if source_id == "research.snapshot":
            expected_type = "other"
            allowed_detail_codes = {"aggregate_research_snapshot_accepted"}
        elif source_id.startswith("research.fund."):
            expected_type = "primary_research"
            fund_key = source_id.removeprefix("research.fund.")
            allowed_detail_codes = {
                f"fund_monitor_{fund_by_key[fund_key]['status'].lower()}"
            }
            if fund_key in stale_fund_keys:
                allowed_detail_codes.add("fund_research_snapshot_stale")
            if item["observed_at"] != fund_by_key[fund_key]["observed_at"]:
                raise PrivateResearchAdapterError(
                    "private_research_fund_health_time_mismatch"
                )
        else:
            expected_type = "social"
            if source_id == "research.social.aggregate":
                social_status = next(iter(social_status_notes)).removeprefix(
                    "social_heat_status_"
                )
                allowed_detail_codes = {f"social_heat_{social_status}"}
            elif source_id.startswith("research.social."):
                allowed_detail_codes = {
                    "social_platform_aggregate_accepted",
                    "social_platform_quarantined",
                }
            else:
                allowed_detail_codes = {
                    f"prediction_{calibration_source_states[source_id]}"
                }
        if social_stale and (
            source_id.startswith("research.social.")
            or source_id.startswith("research.calibration.")
        ):
            allowed_detail_codes.add("social_research_snapshot_stale")
        if snapshot_stale:
            allowed_detail_codes.add("research_snapshot_stale")
        if detail_code not in allowed_detail_codes:
            raise PrivateResearchAdapterError(
                "private_research_source_health_detail_is_not_adapter_controlled"
            )
        if item["source_type"] != expected_type:
            raise PrivateResearchAdapterError(
                "private_research_source_health_type_mismatch"
            )
        if source_id.startswith("research.social.") and source_id != (
            "research.social.aggregate"
        ):
            if social_aggregate is None or item["observed_at"] != social_aggregate["observed_at"]:
                raise PrivateResearchAdapterError(
                    "private_research_social_health_time_mismatch"
                )
        if source_id.startswith("research.calibration.") and (
            item["observed_at"] != snapshot["observed_at"]
        ):
            raise PrivateResearchAdapterError(
                "private_research_calibration_health_time_mismatch"
            )


def validate_private_research_projection(
    value: PrivateResearchProjection,
    *,
    prepared_at: dt.datetime,
) -> PrivateResearchProjection:
    """Validate a detached or persisted projection before any ledger mutation."""

    if not isinstance(value, PrivateResearchProjection):
        raise PrivateResearchAdapterError(
            "value_must_be_private_research_projection"
        )
    prepared = _aware_utc(prepared_at, "prepared_at")
    try:
        normalized = json.loads(
            canonical_json(
                {
                    "research": value.research,
                    "source_health": list(value.source_health),
                }
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PrivateResearchAdapterError(
            "private_research_projection_is_not_canonical_json"
        ) from exc
    errors = sorted(
        _projection_validator().iter_errors(normalized),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        raise PrivateResearchAdapterError(
            "private_research_projection_schema_invalid"
        )
    research = normalized["research"]
    try:
        validate_private_daily_research_section(
            research,
            schema_version=SCHEMA_VERSION,
            prepared_at=prepared,
        )
    except PrivateDailyReportSemanticError as exc:
        raise PrivateResearchAdapterError(
            "private_research_projection_semantics_invalid"
        ) from exc
    _validate_closed_projection_contract(
        normalized,
        prepared_at=prepared,
    )
    funds = research["fund_monitoring"]
    fund_keys = [item["fund_key"] for item in funds]
    if fund_keys != sorted(fund_keys) or len(fund_keys) != len(set(fund_keys)):
        raise PrivateResearchAdapterError(
            "private_research_funds_must_be_sorted_unique"
        )
    for fund in funds:
        for field_name in (
            "reason_codes",
            "triggered_cadences",
            "triggered_event_keys",
            "missing_categories",
            "stale_categories",
            "degraded_categories",
        ):
            if field_name in fund and fund[field_name] != sorted(set(fund[field_name])):
                raise PrivateResearchAdapterError(
                    "private_research_fund_lists_must_be_sorted_unique"
                )

    social = research["social_attention"]
    social_keys = [(item["platform"], item["topic"]) for item in social]
    if social_keys != sorted(social_keys) or len(social_keys) != len(set(social_keys)):
        raise PrivateResearchAdapterError(
            "private_research_social_rows_must_be_sorted_unique"
        )
    for item in social:
        if not item["research_only"]:
            raise PrivateResearchAdapterError(
                "private_research_social_row_crosses_no_trade_boundary"
            )
        if item["platform"] == "xiaohongshu" and (
            Decimal(item.get("candidate_execution_weight", "0")) != _ZERO
            or Decimal(item.get("effective_execution_weight", "0")) != _ZERO
        ):
            raise PrivateResearchAdapterError(
                "xiaohongshu_execution_weight_must_be_zero"
            )

    calibration = research.get("signal_calibration", [])
    calibration_keys = [
        (
            item["platform"],
            item["topic"],
            item["model_version"],
            item["market_regime"],
            item["horizon"],
        )
        for item in calibration
    ]
    if calibration_keys != sorted(calibration_keys) or len(calibration_keys) != len(
        set(calibration_keys)
    ):
        raise PrivateResearchAdapterError(
            "private_research_calibration_rows_must_be_sorted_unique"
        )
    if any(item["automatic_trading_permitted"] for item in calibration):
        raise PrivateResearchAdapterError(
            "private_research_calibration_crosses_no_trade_boundary"
        )
    decision = research.get("social_decision")
    if decision is not None:
        cap = Decimal(decision["decision_weight_cap"])
        raw = Decimal(decision["raw_contribution"])
        effective = Decimal(decision["effective_contribution"])
        coverage = Decimal(decision["effective_execution_coverage"])
        if (
            not decision["research_only"]
            or cap > SOCIAL_DECISION_WEIGHT_CAP
            or abs(raw) > cap
            or abs(effective) > cap
            or coverage > Decimal("1")
        ):
            raise PrivateResearchAdapterError(
                "private_research_social_decision_is_unsafe"
            )
        if not calibration and (
            effective != _ZERO
            or coverage != _ZERO
            or decision["calibration_state"] != "research_only"
        ):
            raise PrivateResearchAdapterError(
                "private_research_uncalibrated_social_score_must_be_disabled"
            )

    health = normalized["source_health"]
    source_ids = [item["source_id"] for item in health]
    if source_ids != sorted(source_ids) or len(source_ids) != len(set(source_ids)):
        raise PrivateResearchAdapterError(
            "private_research_source_health_must_be_sorted_unique"
        )
    for item in health:
        if item["observed_at"] is not None and _parse_utc_text(
            item["observed_at"],
            "source_health.observed_at",
        ) > prepared:
            raise PrivateResearchAdapterError(
                "private_research_source_health_may_not_be_from_the_future"
            )
    snapshot_rows = [
        item for item in health if item["source_id"] == "research.snapshot"
    ]
    if len(snapshot_rows) != 1 or snapshot_rows[0]["observed_at"] is None:
        raise PrivateResearchAdapterError(
            "private_research_snapshot_health_identity_invalid"
        )
    snapshot_observed_at = _parse_utc_text(
        snapshot_rows[0]["observed_at"],
        "research.snapshot.observed_at",
    )
    stale_fund_keys: set[str] = set()
    for item in funds:
        observed_at = _parse_utc_text(
            item["observed_at"],
            f"fund.{item['fund_key']}.observed_at",
        )
        if prepared - observed_at > _RESEARCH_SNAPSHOT_MAX_AGE:
            stale_fund_keys.add(item["fund_key"])
            _downgrade_stale_fund_row(item)
    if stale_fund_keys:
        for item in health:
            if item["source_id"] in {
                f"research.fund.{fund_key}" for fund_key in stale_fund_keys
            }:
                item["status"] = "degraded"
                item["detail_code"] = "fund_research_snapshot_stale"
        research["notes"] = sorted(
            {*research["notes"], "stale_fund_conclusion_downgraded"}
        )
    social_aggregate_rows = [
        item
        for item in health
        if item["source_id"] == "research.social.aggregate"
    ]
    if social_aggregate_rows:
        social_observed_at = _parse_utc_text(
            social_aggregate_rows[0]["observed_at"],
            "research.social.aggregate.observed_at",
        )
        if prepared - social_observed_at > _RESEARCH_SNAPSHOT_MAX_AGE:
            for item in health:
                if (
                    item["source_id"].startswith("research.social.")
                    or item["source_id"].startswith("research.calibration.")
                ) and item["status"] not in {"blocked", "error"}:
                    item["status"] = "degraded"
                    item["detail_code"] = "social_research_snapshot_stale"
            _disable_social_candidate_score(
                social=social,
                calibration=calibration,
                decision=decision,
                reason_code="social_research_snapshot_stale",
            )
            research["notes"] = sorted(
                {
                    *research["notes"],
                    "social_research_snapshot_stale_candidate_score_disabled",
                }
            )
    if prepared - snapshot_observed_at > _RESEARCH_SNAPSHOT_MAX_AGE:
        for item in health:
            if item["source_id"].startswith("research.") and item["status"] not in {
                "blocked",
                "error",
            }:
                item["status"] = "degraded"
                item["detail_code"] = "research_snapshot_stale"
        for item in funds:
            _downgrade_stale_fund_row(item)
        _disable_social_candidate_score(
            social=social,
            calibration=calibration,
            decision=decision,
            reason_code="research_snapshot_stale",
        )
        research["notes"] = sorted(
            {*research["notes"], "research_snapshot_stale_candidate_score_disabled"}
        )
    try:
        validate_private_daily_research_section(
            research,
            schema_version=SCHEMA_VERSION,
            prepared_at=prepared,
        )
    except PrivateDailyReportSemanticError as exc:
        raise PrivateResearchAdapterError(
            "private_research_stale_projection_semantics_invalid"
        ) from exc
    return PrivateResearchProjection(
        research=research,
        source_health=tuple(health),
    )


def build_private_research_projection(
    value: PrivateResearchInput,
    *,
    prepared_at: dt.datetime,
) -> PrivateResearchProjection:
    """Return a deterministic, no-trade projection for report v1.1.

    ``prepared_at`` is a truth gate, not an input to scoring.  Aggregate
    research observed in the future is rejected before the runtime can mutate
    its accounting ledger.
    """

    if not isinstance(value, PrivateResearchInput):
        raise PrivateResearchAdapterError("value_must_be_private_research_input")
    prepared = _aware_utc(prepared_at, "prepared_at")
    input_as_of = _aware_utc(value.as_of, "as_of")
    if input_as_of > prepared:
        raise PrivateResearchAdapterError("research_snapshot_may_not_be_from_the_future")
    fund_results = _validated_fund_results(value.fund_results, as_of=input_as_of)
    prediction_states = _validated_prediction_states(value.prediction_weight_states)
    social_heat = value.social_heat
    if social_heat is not None:
        _validate_social_result(social_heat, as_of=input_as_of)

    calibration = _platform_calibration(prediction_states)
    fund_rows = [_fund_row(item) for item in fund_results]
    social_rows = (
        [] if social_heat is None else _social_rows(social_heat, calibration)
    )
    social_decision = _social_decision(social_heat, calibration)
    calibration_rows = _calibration_rows(prediction_states)
    notes = set(_BASE_NOTES)
    if fund_results:
        notes.add("fund_monitoring_research_only")
    if social_heat is not None:
        notes.add("social_heat_research_only")
        notes.add(f"social_heat_status_{social_heat.status}")
        if prediction_states:
            notes.add("prediction_calibration_applied_to_social_candidate_score")
        else:
            notes.add("social_candidate_score_disabled_without_calibration")
    if prediction_states:
        notes.add("prediction_calibration_research_only")

    health_rows: list[dict[str, Any]] = []
    for result in fund_results:
        health_rows.append(
            {
                "source_id": f"research.fund.{result.fund_key}",
                "source_type": "primary_research",
                "status": _fund_health(result),
                "required": False,
                "observed_at": _utc_text(result.as_of),
                "detail_code": f"fund_monitor_{result.status.lower()}",
            }
        )
    if social_heat is not None:
        health_rows.append(
            {
                "source_id": "research.social.aggregate",
                "source_type": "social",
                "status": _social_health_status(social_heat),
                "required": False,
                "observed_at": _utc_text(social_heat.as_of),
                "detail_code": f"social_heat_{social_heat.status}",
            }
        )
        for platform in sorted(social_heat.platforms, key=lambda item: item.platform):
            health_rows.append(
                {
                    "source_id": f"research.social.{platform.platform}",
                    "source_type": "social",
                    "status": "blocked" if platform.quarantine else "healthy",
                    "required": False,
                    "observed_at": _utc_text(social_heat.as_of),
                    "detail_code": (
                        "social_platform_quarantined"
                        if platform.quarantine
                        else "social_platform_aggregate_accepted"
                    ),
                }
            )
    for item in prediction_states:
        identity = "|".join(
            (
                item.platform,
                item.topic,
                item.model_version,
                item.market_regime,
                str(item.horizon),
            )
        )
        health_rows.append(
            {
                "source_id": (
                    "research.calibration."
                    + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                ),
                "source_type": "social",
                "status": {
                    "active": "healthy",
                    "decayed": "degraded",
                    "quarantined": "blocked",
                    "research_only": "partial",
                }[item.state],
                "required": False,
                "observed_at": _utc_text(input_as_of),
                "detail_code": f"prediction_{item.state}",
            }
        )

    snapshot_status = "healthy" if fund_rows or social_rows else "not_configured"
    if any(item["status"] in {"partial", "degraded", "blocked"} for item in health_rows):
        snapshot_status = "partial"
    health_rows.append(
        {
            "source_id": "research.snapshot",
            "source_type": "other",
            "status": snapshot_status,
            "required": False,
            "observed_at": _utc_text(input_as_of),
            "detail_code": "aggregate_research_snapshot_accepted",
        }
    )

    return PrivateResearchProjection(
        research={
            "overall_view": _OVERALL_VIEW,
            "market_regime": "unknown",
            "risk_budget_multiplier": _ZERO,
            "fund_monitoring": fund_rows,
            "social_attention": social_rows,
            "social_decision": social_decision,
            "signal_calibration": calibration_rows,
            "notes": sorted(notes),
        },
        source_health=tuple(sorted(health_rows, key=lambda item: item["source_id"])),
    )


__all__ = [
    "PrivateResearchAdapterError",
    "PrivateResearchInput",
    "PrivateResearchProjection",
    "build_private_research_projection",
    "validate_private_research_projection",
]
