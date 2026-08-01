from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

import pytest

from serenity_monitor.fund_monitor import (
    FRESHNESS_DAYS,
    STATUSES,
    FundEvidence,
    FundMetric,
    FundMonitorRequest,
    FundMonitorValidationError,
    FundSource,
    LastCompleted,
    compute_event_acknowledgement_key,
    monitor_fund,
)


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
SHANGHAI = ZoneInfo("Asia/Shanghai")
FUND_KEY = "DEMO_ETF"
DAILY_PRODUCT = {
    "legal_structure",
    "economic_structure",
    "source_health",
    "manager",
    "fees",
    "prospectus",
}
MONTHLY_PRODUCT = {"exposure", "style", "factor", "liquidity", "capacity"}
QUARTERLY_PRODUCT = {"holdings", "attribution", "thesis", "manager_skill"}
MONTHLY_FIT = {"portfolio_role", "overlap", "marginal_contribution", "liquidity"}
QUARTERLY_FIT = {"thesis", "risk_budget", "implementation"}
ANNUAL_PRODUCT = DAILY_PRODUCT | MONTHLY_PRODUCT | QUARTERLY_PRODUCT | {"full_review"}
ANNUAL_FIT = MONTHLY_FIT | QUARTERLY_FIT | {"full_review", "tax", "costs"}


def _source(
    key: str = "official_filing",
    *,
    tier: str = "primary",
    health: str = "healthy",
    observed_at: datetime = NOW,
) -> FundSource:
    return FundSource(
        source_key=key,
        source_tier=tier,
        health=health,
        observed_at=observed_at,
    )


def _evidence(
    category: str,
    *,
    dimension: str = "both",
    source_key: str = "official_filing",
    evidence_type: str = "FACT",
    assessment: str = "supports",
    material: bool = False,
    suffix: str = "base",
    observed_at: datetime = NOW,
) -> FundEvidence:
    return FundEvidence(
        evidence_key=f"{category}.{dimension}.{suffix}0",
        source_key=source_key,
        category=category,
        evidence_type=evidence_type,
        dimension=dimension,
        assessment=assessment,
        observed_at=observed_at,
        material_change=material,
    )


def _metric(
    category: str,
    *,
    dimension: str = "both",
    source_key: str = "official_filing",
    value_status: str = "known",
    value: Decimal | None = Decimal("1"),
    assessment: str = "supports",
    suffix: str = "base",
    observed_at: datetime = NOW,
) -> FundMetric:
    return FundMetric(
        metric_key=f"{category}.{dimension}.{suffix}0",
        source_key=source_key,
        category=category,
        evidence_type="CALCULATION",
        dimension=dimension,
        observed_at=observed_at,
        value_status=value_status,
        value=value,
        unit="score",
        assessment=assessment,
    )


def _last(
    *,
    daily: datetime = NOW,
    monthly: datetime = NOW,
    quarterly: datetime = NOW,
    annual: datetime = NOW,
) -> LastCompleted:
    return LastCompleted(
        daily=daily,
        monthly=monthly,
        quarterly=quarterly,
        annual=annual,
    )


def _request(
    *,
    fund_key: str = FUND_KEY,
    as_of: datetime = NOW,
    last_completed: LastCompleted | None = None,
    legal_structure: str = "open_end_fund",
    economic_structure: str = "physical",
    portfolio_role: str = "core_equity",
    sources: tuple[FundSource, ...] | None = None,
    evidence: tuple[FundEvidence, ...] = (),
    metrics: tuple[FundMetric, ...] = (),
    review_timezone: str = "Asia/Shanghai",
    acknowledged_event_keys: tuple[str, ...] = (),
) -> FundMonitorRequest:
    return FundMonitorRequest(
        fund_key=fund_key,
        as_of=as_of,
        legal_structure=legal_structure,
        economic_structure=economic_structure,
        portfolio_role=portfolio_role,
        last_completed=last_completed or _last(),
        sources=(_source(observed_at=as_of),) if sources is None else sources,
        evidence=evidence,
        metrics=metrics,
        review_timezone=review_timezone,
        acknowledged_event_keys=acknowledged_event_keys,
    )


def _support(categories: set[str], *, dimension: str = "both") -> tuple[FundEvidence, ...]:
    return tuple(
        _evidence(category, dimension=dimension, suffix=f"support_{index}")
        for index, category in enumerate(sorted(categories))
    )


def _schedule(result, cadence: str):
    return next(item for item in result.schedules if item.cadence == cadence)


def test_not_due_has_no_fabricated_assessment_or_coverage_score():
    result = monitor_fund(_request())

    assert result.status == "NOT_DUE"
    assert result.product_quality_status == "NOT_DUE"
    assert result.portfolio_fit_status == "NOT_DUE"
    assert result.triggered_cadences == ()
    assert result.product_quality.coverage.ratio is None
    assert result.portfolio_fit.coverage.ratio is None
    assert result.source_coverage.ratio is None
    assert result.summary_code == "fund_monitor.overall.not_due"
    assert "未到复核时点" in result.summary_zh


def test_daily_review_checks_structure_and_source_health_but_not_portfolio_fit():
    evidence = _support(DAILY_PRODUCT)
    last = _last(daily=NOW - timedelta(days=1))

    result = monitor_fund(_request(last_completed=last, evidence=evidence))

    assert result.triggered_cadences == ("daily",)
    assert result.product_quality_status == "PASS"
    assert result.portfolio_fit_status == "NOT_DUE"
    assert result.status == "NOT_DUE"
    assert result.summary_code == "fund_monitor.overall.partial_not_due"
    assert "不得视为完整投资通过" in result.summary_zh
    assert result.product_quality.coverage.ratio == Decimal("1")


def test_monthly_review_covers_exposure_style_factor_liquidity_capacity_and_fit():
    product = {"legal_structure", "economic_structure"} | MONTHLY_PRODUCT
    fit = MONTHLY_FIT
    evidence = _support(product, dimension="product_quality") + _support(
        fit, dimension="portfolio_fit"
    )
    last = _last(monthly=datetime(2026, 6, 30, 23, 59, tzinfo=SHANGHAI))

    result = monitor_fund(_request(last_completed=last, evidence=evidence))

    assert result.triggered_cadences == ("monthly",)
    assert result.product_quality_status == "PASS"
    assert result.portfolio_fit_status == "PASS"
    assert result.product_quality.coverage.required_categories == tuple(sorted(product))
    assert result.portfolio_fit.coverage.required_categories == tuple(sorted(fit))
    assert result.source_coverage.ratio == Decimal("1")


def test_quarterly_review_keeps_product_quality_and_portfolio_fit_separate():
    product = {"legal_structure", "economic_structure"} | QUARTERLY_PRODUCT
    fit = QUARTERLY_FIT
    evidence = _support(product, dimension="product_quality") + _support(
        fit - {"risk_budget"}, dimension="portfolio_fit"
    )
    last = _last(quarterly=datetime(2026, 6, 30, 23, 59, tzinfo=SHANGHAI))

    result = monitor_fund(_request(last_completed=last, evidence=evidence))

    assert result.product_quality_status == "PASS"
    assert result.portfolio_fit_status == "NEED_INFO"
    assert result.status == "NEED_INFO"
    assert "missing_required_risk_budget" in result.portfolio_fit.reason_codes
    assert "risk_budget" not in result.product_quality.coverage.required_categories


def test_annual_index_product_requires_branch_specific_methodology_and_tracking():
    product = ANNUAL_PRODUCT | {"index_methodology", "tracking"}
    fit = ANNUAL_FIT
    evidence = _support(product, dimension="product_quality") + _support(
        fit, dimension="portfolio_fit"
    )
    last = _last(annual=datetime(2025, 12, 31, 23, 59, tzinfo=SHANGHAI))

    result = monitor_fund(
        _request(
            last_completed=last,
            economic_structure="index_tracking",
            evidence=evidence,
        )
    )

    assert result.triggered_cadences == ("annual",)
    assert result.status == "PASS"
    assert "index_methodology" in result.product_quality.coverage.required_categories
    assert "tracking" in result.product_quality.coverage.required_categories
    cutoffs = dict(result.product_quality.coverage.category_cutoffs)
    assert cutoffs["exposure"] == NOW - timedelta(days=FRESHNESS_DAYS["monthly"])
    assert cutoffs["holdings"] == NOW - timedelta(days=FRESHNESS_DAYS["quarterly"])
    assert cutoffs["full_review"] == NOW - timedelta(days=FRESHNESS_DAYS["annual"])


def test_legal_product_branch_is_checked_before_generic_performance_evidence():
    base = _support(DAILY_PRODUCT)
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            legal_structure="exchange_traded_note",
            evidence=base,
        )
    )

    assert result.product_quality_status == "NEED_INFO"
    assert "issuer_credit" in result.product_quality.coverage.missing_categories
    assert "call_terms" in result.product_quality.coverage.missing_categories


@pytest.mark.parametrize(
    ("cadence", "last_value", "as_of"),
    [
        (
            "daily",
            datetime(2028, 2, 29, 23, 59, tzinfo=SHANGHAI),
            datetime(2028, 3, 1, 0, 0, tzinfo=SHANGHAI),
        ),
        (
            "monthly",
            datetime(2026, 1, 31, 23, 59, tzinfo=SHANGHAI),
            datetime(2026, 2, 1, 0, 0, tzinfo=SHANGHAI),
        ),
        (
            "quarterly",
            datetime(2026, 3, 31, 23, 59, tzinfo=SHANGHAI),
            datetime(2026, 4, 1, 0, 0, tzinfo=SHANGHAI),
        ),
        (
            "annual",
            datetime(2025, 12, 31, 23, 59, tzinfo=SHANGHAI),
            datetime(2026, 1, 1, 0, 0, tzinfo=SHANGHAI),
        ),
    ],
)
def test_calendar_period_boundaries_including_leap_year(
    cadence: str, last_value: datetime, as_of: datetime
):
    current = LastCompleted(daily=as_of, monthly=as_of, quarterly=as_of, annual=as_of)
    values = {
        "daily": current.daily,
        "monthly": current.monthly,
        "quarterly": current.quarterly,
        "annual": current.annual,
    }
    values[cadence] = last_value
    result = monitor_fund(
        _request(
            as_of=as_of,
            last_completed=LastCompleted(**values),
            sources=(_source(observed_at=as_of),),
        )
    )

    state = _schedule(result, cadence)
    assert state.due
    assert state.triggered
    assert state.next_due == as_of
    assert cadence in result.triggered_cadences


def test_next_due_is_the_first_future_boundary_when_nothing_is_due():
    as_of = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    result = monitor_fund(_request(as_of=as_of))

    assert result.next_due == datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc)
    assert _schedule(result, "daily").next_due == datetime(
        2026, 7, 31, 16, 0, tzinfo=timezone.utc
    )
    assert _schedule(result, "monthly").next_due == datetime(
        2026, 7, 31, 16, 0, tzinfo=timezone.utc
    )


def test_shanghai_month_boundary_is_not_shifted_to_the_wrong_utc_month():
    local_boundary = datetime(2026, 8, 1, 0, 0, tzinfo=SHANGHAI)
    last = LastCompleted(
        daily=local_boundary,
        monthly=datetime(2026, 7, 31, 23, 59, tzinfo=SHANGHAI),
        quarterly=local_boundary,
        annual=local_boundary,
    )
    result = monitor_fund(
        _request(
            as_of=local_boundary,
            last_completed=last,
            sources=(_source(observed_at=local_boundary),),
            review_timezone="Asia/Shanghai",
        )
    )

    monthly = _schedule(result, "monthly")
    assert monthly.due
    assert monthly.next_due == datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc)
    assert result.review_timezone == "Asia/Shanghai"


def test_review_timezone_is_a_closed_allowlist():
    with pytest.raises(FundMonitorValidationError, match="review_timezone"):
        _request(review_timezone="Etc/Uncontrolled")


def test_material_manager_event_triggers_immediately_even_when_no_cadence_is_due():
    evidence = (
        *_support({"legal_structure", "economic_structure"}),
        _evidence("manager", material=True, assessment="watch"),
        _evidence("portfolio_impact"),
    )

    result = monitor_fund(_request(evidence=evidence))

    assert result.triggered_cadences == ("event",)
    assert result.triggered_event_keys == (
        compute_event_acknowledgement_key(
            FUND_KEY,
            next(item for item in evidence if item.material_change)
        ),
    )
    assert _schedule(result, "event").next_due == NOW
    assert result.product_quality_status == "WATCH"
    assert result.portfolio_fit_status == "WATCH"
    assert result.status == "WATCH"
    assert "material_event_under_review" in result.product_quality.reason_codes


def test_expired_material_event_does_not_retrigger_forever():
    old_event = _evidence(
        "manager",
        material=True,
        assessment="watch",
        observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        suffix="old_event",
    )
    result = monitor_fund(_request(evidence=(old_event,)))

    assert not _schedule(result, "event").triggered
    assert result.triggered_cadences == ()
    assert result.status == "NOT_DUE"


def test_acknowledged_fresh_material_event_does_not_retrigger():
    event = _evidence(
        "manager",
        material=True,
        assessment="watch",
        suffix="acknowledged",
    )
    result = monitor_fund(
        _request(
            evidence=(event,),
            acknowledged_event_keys=(
                compute_event_acknowledgement_key(FUND_KEY, event),
            ),
        )
    )

    assert result.triggered_cadences == ()
    assert not _schedule(result, "event").triggered
    assert result.triggered_event_keys == ()
    assert result.status == "NOT_DUE"


def test_event_acknowledgement_is_bound_to_the_full_immutable_event_digest():
    first = _evidence(
        "manager",
        material=True,
        assessment="watch",
        suffix="stable_id",
        observed_at=NOW - timedelta(hours=2),
    )
    changed_timestamp = replace(first, observed_at=NOW - timedelta(hours=1))
    first_key = compute_event_acknowledgement_key(FUND_KEY, first)
    changed_key = compute_event_acknowledgement_key(FUND_KEY, changed_timestamp)
    assert first.evidence_key == changed_timestamp.evidence_key
    assert first_key != changed_key

    changed_result = monitor_fund(_request(evidence=(changed_timestamp,)))
    assert changed_result.triggered_event_keys == (changed_key,)

    with pytest.raises(FundMonitorValidationError, match="stale, or unknown"):
        monitor_fund(
            _request(
                evidence=(changed_timestamp,),
                acknowledged_event_keys=(first_key,),
            )
        )


def test_event_digest_commits_every_required_structured_field():
    event = _evidence(
        "manager",
        material=True,
        assessment="watch",
        suffix="digest_fields",
    )
    baseline = compute_event_acknowledgement_key(FUND_KEY, event)
    variants = (
        replace(event, evidence_key="manager.both.changed_key"),
        replace(event, source_key="other_source"),
        replace(event, category="fees"),
        replace(event, evidence_type="CALCULATION"),
        replace(event, dimension="product_quality"),
        replace(event, assessment="supports"),
        replace(event, observed_at=NOW - timedelta(microseconds=1)),
        replace(event, material_change=False),
    )
    assert len(
        {compute_event_acknowledgement_key(FUND_KEY, item) for item in variants}
    ) == len(
        variants
    )
    assert all(
        compute_event_acknowledgement_key(FUND_KEY, item) != baseline
        for item in variants
    )


def test_event_acknowledgement_is_fund_scoped_and_cannot_cross_funds():
    event = _evidence(
        "manager",
        material=True,
        assessment="watch",
        suffix="fund_scope",
    )
    fund_a_key = compute_event_acknowledgement_key("FUND_A", event)
    fund_b_key = compute_event_acknowledgement_key("FUND_B", event)
    assert fund_a_key != fund_b_key

    with pytest.raises(FundMonitorValidationError, match="stale, or unknown"):
        monitor_fund(
            _request(
                fund_key="FUND_B",
                evidence=(event,),
                acknowledged_event_keys=(fund_a_key,),
            )
        )


def test_acknowledgement_rejects_preconfirmation_unknown_stale_and_noncanonical_tokens():
    event = _evidence(
        "manager",
        material=True,
        assessment="watch",
        suffix="fresh",
    )
    fake = "a" * 64
    with pytest.raises(FundMonitorValidationError, match="pre-confirmation"):
        monitor_fund(
            _request(evidence=(event,), acknowledged_event_keys=(fake,))
        )
    with pytest.raises(FundMonitorValidationError, match="lowercase SHA-256"):
        _request(
            evidence=(event,),
            acknowledged_event_keys=("A" * 64,),
        )

    stale_event = replace(
        event,
        observed_at=NOW - timedelta(days=FRESHNESS_DAYS["event"] + 1),
    )
    with pytest.raises(FundMonitorValidationError, match="stale, or unknown"):
        monitor_fund(
            _request(
                evidence=(stale_event,),
                acknowledged_event_keys=(
                    compute_event_acknowledgement_key(FUND_KEY, stale_event),
                ),
            )
        )

    nonmaterial = replace(event, material_change=False)
    with pytest.raises(FundMonitorValidationError, match="pre-confirmation"):
        monitor_fund(
            _request(
                evidence=(nonmaterial,),
                acknowledged_event_keys=(
                    compute_event_acknowledgement_key(FUND_KEY, nonmaterial),
                ),
            )
        )


def test_fixed_freshness_policy_requires_both_current_source_health_and_evidence():
    assert FRESHNESS_DAYS == {
        "daily": 2,
        "event": 7,
        "monthly": 45,
        "quarterly": 120,
        "annual": 400,
    }
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    stale_evidence = tuple(
        _evidence(category, observed_at=old, suffix=f"old_{index}")
        for index, category in enumerate(sorted(DAILY_PRODUCT))
    )
    stale_result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            evidence=stale_evidence,
        )
    )
    assert stale_result.product_quality_status == "NEED_INFO"
    assert set(stale_result.product_quality.coverage.stale_categories) == DAILY_PRODUCT
    assert not stale_result.product_quality.coverage.degraded_categories
    assert dict(stale_result.product_quality.coverage.category_cutoffs)["manager"] == (
        NOW - timedelta(days=FRESHNESS_DAYS["daily"])
    )
    assert stale_result.product_quality.coverage.source_health_cutoff == (
        NOW - timedelta(days=FRESHNESS_DAYS["daily"])
    )

    stale_source_result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            sources=(_source(observed_at=old),),
            evidence=_support(DAILY_PRODUCT),
        )
    )
    assert stale_source_result.product_quality_status == "NEED_INFO"
    assert set(stale_source_result.product_quality.coverage.stale_categories) == DAILY_PRODUCT
    assert stale_source_result.product_quality.coverage.healthy_primary_source_count == 0


def test_freshness_cutoff_is_inclusive_and_one_microsecond_older_is_stale():
    cutoff = NOW - timedelta(days=FRESHNESS_DAYS["daily"])
    current = tuple(
        _evidence(category, observed_at=cutoff, suffix=f"edge_{index}")
        for index, category in enumerate(sorted(DAILY_PRODUCT))
    )
    accepted = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            evidence=current,
        )
    )
    assert accepted.product_quality_status == "PASS"

    stale_manager = tuple(
        replace(
            item,
            observed_at=cutoff - timedelta(microseconds=1),
        )
        if item.category == "manager"
        else item
        for item in current
    )
    stale = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            evidence=stale_manager,
        )
    )
    assert stale.product_quality_status == "NEED_INFO"
    assert stale.product_quality.coverage.stale_categories == ("manager",)


def test_social_material_lead_triggers_research_but_cannot_satisfy_primary_evidence():
    social = _source("social_export", tier="social")
    evidence = (
        *_support({"legal_structure", "economic_structure"}),
        _evidence(
            "manager",
            source_key="social_export",
            evidence_type="SOCIAL_SIGNAL",
            assessment="lead",
            material=True,
        ),
        _evidence("portfolio_impact"),
    )

    result = monitor_fund(
        _request(sources=(_source(), social), evidence=evidence)
    )

    assert result.triggered_cadences == ("event",)
    assert result.product_quality_status == "NEED_INFO"
    assert "manager" in result.product_quality.coverage.social_only_categories
    assert "manager" in result.product_quality.coverage.missing_categories
    assert "social_signal_unconfirmed_manager" in result.product_quality.reason_codes
    assert result.status != "PASS"


def test_social_signal_may_not_claim_support_watch_or_reject():
    for assessment in ("supports", "watch", "reject"):
        with pytest.raises(FundMonitorValidationError, match="weak lead"):
            _evidence(
                "manager",
                evidence_type="SOCIAL_SIGNAL",
                assessment=assessment,
                source_key="social_export",
            )


@pytest.mark.parametrize("evidence_type", ["INFERENCE", "JUDGMENT"])
def test_inference_and_judgment_cannot_close_required_coverage(evidence_type: str):
    evidence = list(_support(DAILY_PRODUCT - {"source_health"}))
    evidence.append(
        _evidence(
            "source_health",
            evidence_type=evidence_type,
            assessment="supports",
            suffix=evidence_type.lower(),
        )
    )
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            evidence=tuple(evidence),
        )
    )

    assert result.product_quality_status == "NEED_INFO"
    assert "source_health" in result.product_quality.coverage.missing_categories
    assert "source_health" not in result.product_quality.coverage.covered_categories


@pytest.mark.parametrize("assessment", ["supports", "watch", "reject"])
def test_structure_hard_gate_requires_official_fact_not_calculation(
    assessment: str,
):
    evidence = list(_support(DAILY_PRODUCT - {"legal_structure"}))
    evidence.append(
        _evidence(
            "legal_structure",
            evidence_type="CALCULATION",
            assessment=assessment,
            suffix=f"calculated_{assessment}",
        )
    )
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            evidence=tuple(evidence),
        )
    )

    assert result.product_quality_status == "NEED_INFO"
    assert "structure_hard_gate_blocked" in result.product_quality.reason_codes
    assert "legal_structure" in result.product_quality.coverage.missing_categories
    assert "confirmed_watch_signal" not in result.product_quality.reason_codes
    assert "confirmed_reject_signal" not in result.product_quality.reason_codes


def test_metric_evidence_type_is_restricted_to_fact_or_calculation():
    for evidence_type in ("INFERENCE", "JUDGMENT", "SOCIAL_SIGNAL"):
        with pytest.raises(FundMonitorValidationError, match="unsupported"):
            FundMetric(
                metric_key=f"factor.{evidence_type.lower()}0",
                source_key="official_filing",
                category="factor",
                evidence_type=evidence_type,
                dimension="product_quality",
                observed_at=NOW,
                value_status="known",
                value=Decimal("1"),
                unit="score",
                assessment="supports",
            )


def test_unknown_metric_remains_unknown_and_is_never_imputed_as_zero():
    product_categories = {
        "legal_structure",
        "economic_structure",
        "exposure",
        "style",
        "liquidity",
        "capacity",
    }
    fit_categories = {"portfolio_role", "overlap", "marginal_contribution", "liquidity"}
    evidence = _support(product_categories, dimension="product_quality") + _support(
        fit_categories, dimension="portfolio_fit"
    )
    metrics = (
        _metric(
            "factor",
            dimension="product_quality",
            value_status="unknown",
            value=None,
            assessment="unknown",
        ),
    )
    result = monitor_fund(
        _request(
            last_completed=_last(monthly=datetime(2026, 6, 30, tzinfo=timezone.utc)),
            evidence=evidence,
            metrics=metrics,
        )
    )

    assert result.product_quality_status == "NEED_INFO"
    assert "factor" in result.product_quality.coverage.unknown_categories
    assert "factor" not in result.product_quality.coverage.covered_categories
    assert "unknown_required_factor" in result.product_quality.reason_codes


def test_stale_metric_is_reported_separately_and_cannot_close_monthly_coverage():
    product_categories = {
        "legal_structure",
        "economic_structure",
        *MONTHLY_PRODUCT,
    } - {"factor"}
    evidence = _support(product_categories, dimension="product_quality") + _support(
        MONTHLY_FIT, dimension="portfolio_fit"
    )
    old_metric = _metric(
        "factor",
        dimension="product_quality",
        observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        suffix="old",
    )
    result = monitor_fund(
        _request(
            last_completed=_last(monthly=datetime(2026, 6, 30, tzinfo=SHANGHAI)),
            evidence=evidence,
            metrics=(old_metric,),
        )
    )

    assert result.product_quality_status == "NEED_INFO"
    assert "factor" in result.product_quality.coverage.stale_categories
    assert "factor" in result.product_quality.coverage.missing_categories


def test_unknown_structure_and_role_fail_closed_instead_of_becoming_zero_scores():
    product = _support(DAILY_PRODUCT)
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            legal_structure="unknown",
            economic_structure="unknown",
            portfolio_role="unknown",
            evidence=product,
        )
    )

    assert result.product_quality_status == "NEED_INFO"
    assert "unknown_legal_structure" in result.product_quality.reason_codes
    assert "unknown_economic_structure" in result.product_quality.reason_codes
    assert result.portfolio_fit_status == "NOT_DUE"


def test_unknown_structure_hard_gate_cannot_be_overridden_by_ordinary_reject():
    evidence = list(_support(DAILY_PRODUCT - {"fees"}))
    evidence.append(_evidence("fees", assessment="reject", suffix="ordinary_reject"))
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            legal_structure="unknown",
            economic_structure="unknown",
            evidence=tuple(evidence),
        )
    )

    assert result.product_quality_status == "NEED_INFO"
    assert result.status == "NEED_INFO"
    assert "structure_hard_gate_blocked" in result.product_quality.reason_codes
    assert "confirmed_reject_signal" not in result.product_quality.reason_codes


def test_verified_structural_hard_reject_overrides_only_nonstructural_coverage_gaps():
    evidence = (
        _evidence("legal_structure", assessment="reject", suffix="hard_reject"),
        _evidence("economic_structure"),
    )
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            evidence=evidence,
        )
    )

    assert result.product_quality_status == "REJECT"
    assert result.status == "REJECT"
    assert "verified_structural_hard_reject" in result.product_quality.reason_codes
    assert result.product_quality.verified_structural_hard_reject
    assert not result.portfolio_fit.verified_structural_hard_reject
    assert "manager" in result.product_quality.coverage.missing_categories


def test_structural_reject_cannot_override_a_missing_peer_structure_fact():
    evidence = (
        _evidence("legal_structure", assessment="reject", suffix="hard_reject"),
    )
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            evidence=evidence,
        )
    )

    assert result.product_quality_status == "NEED_INFO"
    assert "economic_structure" in result.product_quality.coverage.missing_categories
    assert "structure_hard_gate_blocked" in result.product_quality.reason_codes
    assert "verified_structural_hard_reject" not in result.product_quality.reason_codes
    assert result.product_quality.verified_structural_hard_reject


def test_missing_required_evidence_cannot_be_bypassed_by_an_ordinary_reject():
    evidence = list(_support(DAILY_PRODUCT - {"fees", "manager"}))
    evidence.append(_evidence("fees", assessment="reject", suffix="reject"))
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            evidence=tuple(evidence),
        )
    )

    assert result.product_quality_status == "NEED_INFO"
    assert "manager" in result.product_quality.coverage.missing_categories
    assert "confirmed_reject_signal" not in result.product_quality.reason_codes


def test_missing_or_degraded_primary_source_fails_closed():
    categories = DAILY_PRODUCT
    secondary = _source("secondary_research", tier="secondary")
    secondary_evidence = tuple(
        _evidence(category, source_key="secondary_research") for category in categories
    )
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            sources=(secondary,),
            evidence=secondary_evidence,
        )
    )
    assert result.product_quality_status == "NEED_INFO"
    assert set(result.product_quality.coverage.missing_categories) == categories

    degraded = _source(health="degraded")
    degraded_result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            sources=(degraded,),
            evidence=_support(categories),
        )
    )
    assert degraded_result.product_quality_status == "NEED_INFO"
    assert set(degraded_result.product_quality.coverage.degraded_categories) == categories


def test_primary_fact_rejects_product_without_collapsing_portfolio_fit():
    product = ANNUAL_PRODUCT
    fit = ANNUAL_FIT
    evidence = list(_support(product - {"fees"}, dimension="product_quality"))
    evidence.append(
        _evidence("fees", dimension="product_quality", assessment="reject", suffix="reject")
    )
    evidence.extend(_support(fit, dimension="portfolio_fit"))
    result = monitor_fund(
        _request(
            last_completed=_last(annual=datetime(2025, 12, 31, tzinfo=timezone.utc)),
            evidence=tuple(evidence),
        )
    )

    assert result.product_quality_status == "REJECT"
    assert result.portfolio_fit_status == "PASS"
    assert result.status == "REJECT"
    assert "confirmed_reject_signal" in result.product_quality.reason_codes


@pytest.mark.parametrize("evidence_type", ["INFERENCE", "JUDGMENT"])
def test_complete_coverage_analytical_negative_downgrades_pass_to_watch(
    evidence_type: str,
):
    categories = DAILY_PRODUCT
    evidence = list(_support(categories))
    evidence.append(
        _evidence(
            "source_health",
            evidence_type=evidence_type,
            assessment="reject",
            suffix=evidence_type.lower(),
        )
    )
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            evidence=tuple(evidence),
        )
    )

    assert result.product_quality_status == "WATCH"
    assert result.product_quality.coverage.ratio == Decimal("1.000000000000")
    assert "nonconfirming_risk_observation" in result.product_quality.reason_codes
    assert "judgment_inference_risk_observation" in result.product_quality.reason_codes
    assert "confirmed_reject_signal" not in result.product_quality.reason_codes


@pytest.mark.parametrize("evidence_type", ["INFERENCE", "JUDGMENT"])
def test_negative_analytical_observation_cannot_hide_incomplete_coverage(
    evidence_type: str,
):
    evidence = list(_support(DAILY_PRODUCT - {"manager"}))
    evidence.append(
        _evidence(
            "manager",
            evidence_type=evidence_type,
            assessment="reject",
            suffix=f"negative_{evidence_type.lower()}",
        )
    )
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            evidence=tuple(evidence),
        )
    )

    assert result.product_quality_status == "NEED_INFO"
    assert "manager" in result.product_quality.coverage.missing_categories
    assert "judgment_inference_risk_observation" in result.product_quality.reason_codes
    assert "confirmed_reject_signal" not in result.product_quality.reason_codes


def test_structure_calculation_negative_is_watch_only_when_fact_coverage_exists():
    evidence = list(_support(DAILY_PRODUCT))
    evidence.append(
        _evidence(
            "legal_structure",
            evidence_type="CALCULATION",
            assessment="reject",
            suffix="negative_calculation",
        )
    )
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            evidence=tuple(evidence),
        )
    )

    assert result.product_quality_status == "WATCH"
    assert not result.product_quality.verified_structural_hard_reject
    assert (
        "coverage_ineligible_fact_calculation_risk_observation"
        in result.product_quality.reason_codes
    )
    assert "confirmed_reject_signal" not in result.product_quality.reason_codes


@pytest.mark.parametrize("mode", ["stale", "degraded", "secondary"])
def test_nonqualifying_analytical_negative_does_not_create_watch(mode: str):
    evidence = list(_support(DAILY_PRODUCT))
    sources = [_source()]
    observed_at = NOW
    source_key = "official_filing"
    if mode == "stale":
        observed_at = NOW - timedelta(days=FRESHNESS_DAYS["daily"] + 1)
    elif mode == "degraded":
        source_key = "degraded_source"
        sources.append(_source(source_key, health="degraded"))
    else:
        source_key = "secondary_source"
        sources.append(_source(source_key, tier="secondary"))
    evidence.append(
        _evidence(
            "manager",
            source_key=source_key,
            evidence_type="JUDGMENT",
            assessment="reject",
            observed_at=observed_at,
            suffix=f"ignored_{mode}",
        )
    )
    result = monitor_fund(
        _request(
            last_completed=_last(daily=NOW - timedelta(days=1)),
            sources=tuple(sources),
            evidence=tuple(evidence),
        )
    )

    assert result.product_quality_status == "PASS"
    assert "nonconfirming_risk_observation" not in result.product_quality.reason_codes


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _source("https://example.test/fund"),
        lambda: _source("https:example.com"),
        lambda: _source("http:example.com"),
        lambda: _source("file:local_path"),
        lambda: _source("mailto:user"),
        lambda: _source("www.example.com"),
        lambda: _source("example.com"),
        lambda: _source("example.de"),
        lambda: _source("example.jp"),
        lambda: _source("example.ca"),
        lambda: _source("example.com.archive"),
        lambda: _source("192.168.1.1"),
        lambda: _source("folder/path"),
        lambda: _source("folder\\path"),
        lambda: _source("@raw_handle"),
        lambda: _source("api_token"),
        lambda: _request(fund_key="HTTPS.EXAMPLE.COM"),
        lambda: _request(fund_key="WWW.EXAMPLE.COM"),
        lambda: _request(fund_key="EXAMPLE.COM"),
        lambda: FundMonitorRequest(
            fund_key="OWNER_ACCOUNT",
            as_of=NOW,
            legal_structure="open_end_fund",
            economic_structure="physical",
            portfolio_role="core_equity",
            last_completed=_last(),
            sources=(),
            evidence=(),
            metrics=(),
        ),
    ],
)
def test_raw_urls_accounts_handles_query_or_secret_identifiers_are_rejected(factory):
    with pytest.raises(FundMonitorValidationError):
        factory()


def test_fund_key_requires_at_least_one_ascii_letter():
    with pytest.raises(FundMonitorValidationError, match="controlled fund key"):
        _request(fund_key="123456789")


def test_valid_dotted_ticker_and_controlled_sec_source_id_are_not_blocked():
    result = monitor_fund(
        _request(
            fund_key="BRK.B",
            sources=(_source("sec.n1a"),),
        )
    )
    assert result.fund_key == "BRK.B"
    assert result.status == "NOT_DUE"


def test_float_nonfinite_naive_and_future_values_fail_closed():
    with pytest.raises(FundMonitorValidationError, match="Decimal"):
        _metric("factor", value=0.1)  # type: ignore[arg-type]
    with pytest.raises(FundMonitorValidationError, match="finite"):
        _metric("factor", value=Decimal("NaN"))
    with pytest.raises(FundMonitorValidationError, match="timezone-aware"):
        _request(as_of=datetime(2026, 7, 31, 12, 0))

    future = NOW + timedelta(microseconds=1)
    with pytest.raises(FundMonitorValidationError, match="future"):
        monitor_fund(_request(sources=(_source(observed_at=future),)))
    with pytest.raises(FundMonitorValidationError, match="future"):
        monitor_fund(_request(last_completed=_last(daily=future)))


def test_unknown_metric_constructor_requires_none_and_never_accepts_zero_placeholder():
    with pytest.raises(FundMonitorValidationError, match="never zero"):
        _metric(
            "factor",
            value_status="unknown",
            value=Decimal("0"),
            assessment="unknown",
        )


def test_input_order_and_callers_decimal_context_cannot_change_output():
    categories = DAILY_PRODUCT - {"manager"}
    evidence = _support(categories)
    sources = (
        _source("official_filing"),
        _source("internal_model", tier="calculated"),
    )
    request = _request(
        last_completed=_last(daily=NOW - timedelta(days=1)),
        sources=sources,
        evidence=evidence,
    )
    forward = monitor_fund(request)
    reversed_request = _request(
        last_completed=request.last_completed,
        sources=tuple(reversed(sources)),
        evidence=tuple(reversed(evidence)),
    )
    with localcontext() as context:
        context.prec = 6
        reverse = monitor_fund(reversed_request)

    assert forward == reverse
    assert forward.product_quality.coverage.ratio == Decimal("0.833333333333")


def test_all_statuses_and_summary_codes_are_closed_and_trade_permissions_are_immutable():
    result = monitor_fund(_request())
    assert result.status in STATUSES
    assert result.product_quality.status in STATUSES
    assert result.portfolio_fit.status in STATUSES
    assert result.summary_code.startswith("fund_monitor.overall.")

    permissions = {
        item.name: (item.init, getattr(result, item.name))
        for item in fields(result)
        if item.name
        in {
            "broker_access",
            "can_trade",
            "can_submit_order",
            "can_change_position",
            "can_change_dca",
            "automatic_execution",
        }
    }
    assert permissions == {
        "broker_access": (False, False),
        "can_trade": (False, False),
        "can_submit_order": (False, False),
        "can_change_position": (False, False),
        "can_change_dca": (False, False),
        "automatic_execution": (False, False),
    }
    with pytest.raises(ValueError, match="init=False"):
        replace(result, can_submit_order=True)
    assert not hasattr(result, "submit_order")
    assert not hasattr(result, "connect_broker")
