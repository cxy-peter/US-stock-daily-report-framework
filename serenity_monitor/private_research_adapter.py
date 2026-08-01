"""Project aggregate research into the owner-only daily-report contract.

This module is deliberately a pure adapter.  It accepts only the already
aggregated outputs of :mod:`fund_monitor` and :mod:`social_heat`; it does not
read posts, files, credentials or network resources.  The projection is
research-only and cannot write the portfolio ledger, alter recurring
investment amounts or create an executable action.

The current private-daily-report v1.0 contract can faithfully carry the fund
status and one aggregate row per social platform.  Topic-level and prediction
ledger details need a future versioned report contract and are therefore not
fabricated or squeezed into free-text notes here.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from .fund_monitor import FundMonitorResult
from .social_heat import SOCIAL_DECISION_WEIGHT_CAP, SocialHeatResult


_ZERO = Decimal("0")
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


@dataclass(frozen=True, repr=False)
class PrivateResearchInput:
    """Sanitized aggregate inputs for one report preparation attempt."""

    as_of: dt.datetime
    fund_results: tuple[FundMonitorResult, ...] = field(default_factory=tuple, repr=False)
    social_heat: SocialHeatResult | None = field(default=None, repr=False)

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
        "summary": (
            f"基金监控状态={result.status}；"
            f"产品质量={result.product_quality_status}；"
            f"组合适配={result.portfolio_fit_status}；仅供研究。"
        ),
        "reason_codes": sorted(reasons),
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


def _social_rows(result: SocialHeatResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for platform in sorted(result.platforms, key=lambda item: item.platform):
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
                "research_only": True,
                "summary": (
                    "平台聚合热度仅供研究；不可单独触发调仓、退出或定投加码。"
                    if platform.platform != "xiaohongshu"
                    else "小红书聚合热度仅供注意力研究；直接执行权重固定为零。"
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


def build_private_research_projection(
    value: PrivateResearchInput,
    *,
    prepared_at: dt.datetime,
) -> PrivateResearchProjection:
    """Return a deterministic, no-trade projection for report v1.0.

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
    social_heat = value.social_heat
    if social_heat is not None:
        _validate_social_result(social_heat, as_of=input_as_of)

    fund_rows = [_fund_row(item) for item in fund_results]
    social_rows = [] if social_heat is None else _social_rows(social_heat)
    notes = {
        "aggregate_research_snapshot_read_only",
        "research_cannot_change_ledger_dca_or_trades",
        "report_v1_omits_prediction_and_social_topic_detail",
    }
    if fund_results:
        notes.add("fund_monitoring_research_only")
    if social_heat is not None:
        notes.add("social_heat_research_only")
        notes.add(f"social_heat_status_{social_heat.status}")

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
            "overall_view": (
                "基金监控与社交热度聚合快照已接入；仅供研究，"
                "不会改变固定定投、会计账本或形成自动交易。"
            ),
            "market_regime": "unknown",
            "risk_budget_multiplier": _ZERO,
            "fund_monitoring": fund_rows,
            "social_attention": social_rows,
            "notes": sorted(notes),
        },
        source_health=tuple(sorted(health_rows, key=lambda item: item["source_id"])),
    )


__all__ = [
    "PrivateResearchAdapterError",
    "PrivateResearchInput",
    "PrivateResearchProjection",
    "build_private_research_projection",
]
