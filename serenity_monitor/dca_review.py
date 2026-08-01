"""Deterministic review of externally managed recurring investments.

The engine never submits or changes broker orders. It produces a next-cycle
research proposal that must be manually confirmed outside this repository.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .regime import MarketRegime
from .rules import ResearchAction, ResearchRecommendation
from .sizing import PortfolioAction, PositionPlan


class DcaReviewAction(str, Enum):
    KEEP_BASE = "维持基础定投"
    HOLD_BASE_NO_INCREASE = "维持基础定投且不加码"
    REDUCE_CANDIDATE = "下周期减量复核"
    INCREASE_CANDIDATE = "下周期增量复核"
    PAUSE_FOR_REVIEW = "暂停并人工复核"


@dataclass
class DcaReview:
    ticker: str
    base_daily_amount_usd: float
    proposed_daily_amount_usd: float
    proposed_weekly_amount_usd: float
    action: DcaReviewAction
    automatic_execution: bool = False
    manual_confirmation_required: bool = False
    evidence_gate_passed: bool = False
    risk_capacity_passed: bool = True
    reasons: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)


def recurring_amounts(config: dict | None) -> dict[str, float]:
    config = config or {}
    if not bool(config.get("enabled", False)):
        return {}
    base = max(0.0, float(config.get("base_amount_usd_per_ticker", 0.0) or 0.0))
    return {
        str(ticker).upper(): base
        for ticker in (config.get("tickers") or [])
        if str(ticker).strip() and base > 0
    }


def build_dca_reviews(
    plans: list[PositionPlan],
    recommendations: dict[str, ResearchRecommendation],
    regime: MarketRegime,
    config: dict | None,
    risk_group_exposures: dict[str, float],
    risk_group_caps: dict[str, float],
    deployable_cash_usd: float | None,
) -> list[DcaReview]:
    config = config or {}
    amounts = recurring_amounts(config)
    if not amounts:
        return []
    max_multiple = max(1.0, float(config.get("max_increase_multiple", 2.0) or 2.0))
    minimum = max(0.0, float(config.get("minimum_amount_usd", 0.0) or 0.0))
    plans_by_ticker = {plan.ticker: plan for plan in plans}
    reviews: list[DcaReview] = []

    for ticker, base in amounts.items():
        plan = plans_by_ticker.get(ticker)
        rec = recommendations.get(ticker)
        if plan is None or rec is None:
            reviews.append(
                DcaReview(
                    ticker=ticker,
                    base_daily_amount_usd=base,
                    proposed_daily_amount_usd=base,
                    proposed_weekly_amount_usd=base * 5,
                    action=DcaReviewAction.PAUSE_FOR_REVIEW,
                    manual_confirmation_required=True,
                    risk_capacity_passed=False,
                    reasons=["缺少持仓、行情或研究结论，无法完成定投复核。"],
                )
            )
            continue

        evidence_gate_passed = bool(rec.evidence and rec.evidence.can_support_add)
        full_groups = [
            group
            for group in plan.risk_groups
            if group in risk_group_caps
            and risk_group_exposures.get(group, 0.0) >= risk_group_caps[group]
        ]
        near_groups = [
            group
            for group in plan.risk_groups
            if group in risk_group_caps
            and risk_group_exposures.get(group, 0.0) >= 0.90 * risk_group_caps[group]
            and group not in full_groups
        ]
        action = DcaReviewAction.KEEP_BASE
        proposed = base
        reasons: list[str] = []
        constraints = [
            "这是外部券商定投计划的研究复核，系统不会修改计划或提交订单。",
            "单一KOL、单一基金关联账号或社交平台热度不能改变定投金额。",
        ]

        if plan.action in {PortfolioAction.REVIEW, PortfolioAction.EXIT} or rec.action in {
            ResearchAction.REVIEW,
            ResearchAction.EXIT,
        }:
            action = DcaReviewAction.PAUSE_FOR_REVIEW
            proposed = minimum
            reasons.append("当前标的处于事件核实或退出复核状态。")
        elif full_groups:
            action = DcaReviewAction.PAUSE_FOR_REVIEW
            proposed = minimum
            reasons.append("风险组已达到上限：" + ", ".join(full_groups))
        elif near_groups:
            action = DcaReviewAction.REDUCE_CANDIDATE
            proposed = max(minimum, base * 0.5)
            reasons.append("风险组接近上限：" + ", ".join(near_groups))
        elif regime.label == "risk_off":
            action = DcaReviewAction.REDUCE_CANDIDATE
            proposed = max(minimum, base * 0.5)
            reasons.append("市场状态为 risk_off，下一周期定投量进入人工减量复核。")
        elif (
            rec.action == ResearchAction.ADD
            and evidence_gate_passed
            and plan.scheduled_dca_status != "manual_review_risk_cap"
        ):
            action = DcaReviewAction.INCREASE_CANDIDATE
            proposed = min(base * max_multiple, base * 2.0)
            reasons.append("深度回撤、证据门控和风险容量同时通过，进入增量候选复核。")
        elif rec.indicators and rec.indicators.ret_1m >= 0.30:
            action = DcaReviewAction.HOLD_BASE_NO_INCREASE
            reasons.append(f"近1个月涨幅为 {rec.indicators.ret_1m:+.1%}，维持基础量且不追高。")
        elif not evidence_gate_passed:
            action = DcaReviewAction.HOLD_BASE_NO_INCREASE
            reasons.append("一级来源或独立证据覆盖不足，不允许基于KOL观点加码。")
        else:
            reasons.append("未触发风险减量、事件暂停或合格增量条件。")

        reviews.append(
            DcaReview(
                ticker=ticker,
                base_daily_amount_usd=base,
                proposed_daily_amount_usd=round(proposed, 2),
                proposed_weekly_amount_usd=round(proposed * 5, 2),
                action=action,
                manual_confirmation_required=abs(proposed - base) > 1e-9,
                evidence_gate_passed=evidence_gate_passed,
                risk_capacity_passed=not full_groups,
                reasons=reasons,
                constraints=constraints,
            )
        )

    weekly_total = sum(review.proposed_weekly_amount_usd for review in reviews)
    if deployable_cash_usd is not None and weekly_total > deployable_cash_usd:
        warning = (
            f"下周期模型定投合计 ${weekly_total:,.0f}，高于扣除现金缓冲后的"
            f"可立即部署现金 ${deployable_cash_usd:,.0f}；需结合最新入金和券商余额人工确认。"
        )
        for review in reviews:
            review.constraints.append(warning)
            review.manual_confirmation_required = True
    return reviews
