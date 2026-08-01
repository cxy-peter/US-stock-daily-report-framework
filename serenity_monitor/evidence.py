"""Conservative evidence scoring and ADD/OPEN hard gates."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .credibility import CredibilityAssessment, aggregate_gate
from .external_views import ExternalView


DEFAULT_POSITIVE_TERMS = (
    "raise guidance",
    "raised guidance",
    "guidance raised",
    "beat expectations",
    "record revenue",
    "record demand",
    "share gain",
    "market share gain",
    "capacity expansion",
    "new contract",
    "design win",
    "demand remains strong",
    "上调指引",
    "订单增长",
    "份额提升",
    "需求强劲",
)
DEFAULT_RISK_TERMS = (
    "guidance cut",
    "lowered guidance",
    "downgrade",
    "demand slowdown",
    "inventory correction",
    "overcapacity",
    "margin pressure",
    "delay",
    "dilution",
    "secondary offering",
    "lawsuit",
    "regulatory probe",
    "下调指引",
    "需求放缓",
    "库存调整",
    "产能过剩",
    "监管调查",
)
DEFAULT_BREAK_TERMS = (
    "thesis broken",
    "designed out",
    "lost key customer",
    "lost market share",
    "fraud",
    "accounting irregularity",
    "bankruptcy",
    "default",
    "delisting",
    "sec investigation",
    "product ban",
    "structural substitution",
    "论点破裂",
    "被替代",
    "财务造假",
    "退市",
    "破产",
)


@dataclass
class EvidenceSettings:
    positive_terms: tuple[str, ...] = DEFAULT_POSITIVE_TERMS
    risk_terms: tuple[str, ...] = DEFAULT_RISK_TERMS
    break_terms: tuple[str, ...] = DEFAULT_BREAK_TERMS
    min_coverage_for_add: float = 0.60
    min_independent_groups_for_add: int = 2
    require_primary_source_for_add: bool = True
    manipulation_block_score: float = 60.0
    review_score: float = 70.0
    caution_score: float = 60.0
    supportive_score: float = 40.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "EvidenceSettings":
        data = data or {}
        return cls(
            positive_terms=tuple(data.get("positive_terms", DEFAULT_POSITIVE_TERMS)),
            risk_terms=tuple(data.get("risk_terms", DEFAULT_RISK_TERMS)),
            break_terms=tuple(data.get("break_terms", DEFAULT_BREAK_TERMS)),
            min_coverage_for_add=float(data.get("min_coverage_for_add", 0.60)),
            min_independent_groups_for_add=int(
                data.get("min_independent_groups_for_add", 2)
            ),
            require_primary_source_for_add=bool(
                data.get("require_primary_source_for_add", True)
            ),
            manipulation_block_score=float(data.get("manipulation_block_score", 60.0)),
            review_score=float(data.get("review_score", 70.0)),
            caution_score=float(data.get("caution_score", 60.0)),
            supportive_score=float(data.get("supportive_score", 40.0)),
        )


@dataclass
class EvidenceAssessment:
    ticker: str
    stance: str
    risk_score: float
    coverage: float
    item_count: int
    positive_points: float = 0.0
    risk_points: float = 0.0
    break_points: float = 0.0
    reasons: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    primary_source_present: bool = False
    independent_groups: int = 0
    max_manipulation_risk_score: float = 0.0
    can_support_add: bool = False
    aggregate_gate_decision: str = "context_only"
    gate_reasons: list[str] = field(default_factory=list)


def _contains(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    if re.fullmatch(r"[A-Za-z0-9_-]+", phrase):
        return (
            re.search(
                rf"(?i)(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])",
                text,
            )
            is not None
        )
    return phrase.casefold() in text.casefold()


def _matched_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if _contains(text, term)]


def assess_view(
    ticker: str,
    view: ExternalView,
    settings: EvidenceSettings,
    extra_positive_terms: list[str] | None = None,
    extra_risk_terms: list[str] | None = None,
    extra_break_terms: list[str] | None = None,
) -> EvidenceAssessment:
    positive_terms = settings.positive_terms + tuple(extra_positive_terms or [])
    risk_terms = settings.risk_terms + tuple(extra_risk_terms or [])
    break_terms = settings.break_terms + tuple(extra_break_terms or [])
    source_coverage: dict[str, float] = {}
    credibility_assessments: list[CredibilityAssessment] = []
    positive_points = risk_points = break_points = high_cred_break = 0.0
    matched_notes: list[tuple[float, str]] = []
    primary_present = False
    max_manipulation = 0.0

    for item in view.items:
        credibility = max(
            0.0,
            min(1.0, float(item.research_weight or item.credibility)),
        )
        group = item.independence_group or item.source_kind
        if item.can_inform_research or item.is_primary_source:
            source_coverage[group] = max(
                source_coverage.get(group, 0.0),
                credibility,
            )
        primary_present = primary_present or item.is_primary_source
        max_manipulation = max(
            max_manipulation,
            float(item.manipulation_risk_score or 0.0),
        )
        credibility_assessments.append(
            CredibilityAssessment(
                source_score=float(item.source_score or 0.0),
                claim_score=float(item.claim_score or 0.0),
                manager_fragility_score=float(
                    item.manager_fragility_score or 0.0
                ),
                manipulation_risk_score=float(
                    item.manipulation_risk_score or 0.0
                ),
                research_weight=credibility,
                can_inform_research=bool(
                    item.can_inform_research or item.is_primary_source
                ),
                copy_trade_allowed=bool(item.copy_trade_allowed),
                red_flags=tuple(item.red_flags),
                positives=(),
                independence_group=group,
            )
        )
        positives = _matched_terms(item.full_text, positive_terms)
        risks = _matched_terms(item.full_text, risk_terms)
        breaks = _matched_terms(item.full_text, break_terms)
        positive_points += credibility * len(positives)
        risk_points += credibility * len(risks)
        break_points += credibility * len(breaks)
        if breaks and credibility >= 0.70:
            high_cred_break += credibility
        impact = credibility * (
            2.5 * len(breaks) + len(risks) + 0.5 * len(positives)
        )
        if breaks or risks or positives:
            tags = []
            if breaks:
                tags.append("break:" + ",".join(breaks[:2]))
            if risks:
                tags.append("risk:" + ",".join(risks[:2]))
            if positives:
                tags.append("positive:" + ",".join(positives[:2]))
            matched_notes.append(
                (
                    impact,
                    f"{item.source} [{'; '.join(tags)}] {item.title[:90]}",
                )
            )

    coverage = min(1.0, sum(source_coverage.values()))
    gate = aggregate_gate(
        credibility_assessments,
        primary_source_present=primary_present,
        minimum_independent_groups=settings.min_independent_groups_for_add,
    )
    primary_ok = primary_present or not settings.require_primary_source_for_add
    can_support_add = bool(
        primary_ok
        and gate.independent_groups >= settings.min_independent_groups_for_add
        and coverage >= settings.min_coverage_for_add
        and max_manipulation < settings.manipulation_block_score
        and gate.decision == "support"
    )
    gate_reasons: list[str] = []
    if settings.require_primary_source_for_add and not primary_present:
        gate_reasons.append("缺少SEC文件、公司公告或其他一级来源。")
    if gate.independent_groups < settings.min_independent_groups_for_add:
        gate_reasons.append(
            f"独立证据组仅 {gate.independent_groups} 个，低于 "
            f"{settings.min_independent_groups_for_add} 个。"
        )
    if coverage < settings.min_coverage_for_add:
        gate_reasons.append(
            f"证据覆盖率 {coverage:.0%}，低于 {settings.min_coverage_for_add:.0%}。"
        )
    if max_manipulation >= settings.manipulation_block_score:
        gate_reasons.append(
            f"操纵/拥挤风险 {max_manipulation:.0f}，达到阻断阈值 "
            f"{settings.manipulation_block_score:.0f}。"
        )
    if not gate_reasons and gate.decision != "support":
        gate_reasons.append("独立证据总研究权重不足，不能支持新增暴露。")
    weighted = 2.5 * break_points + risk_points - 0.5 * positive_points
    risk_score = max(0.0, min(100.0, 50.0 + 15.0 * weighted))
    if not view.items or coverage < 0.25:
        stance = "insufficient"
    elif high_cred_break >= 0.70 or (
        risk_score >= settings.review_score
        and coverage >= settings.min_coverage_for_add
    ):
        stance = "thesis_risk"
    elif risk_score >= settings.caution_score:
        stance = "caution"
    elif risk_score <= settings.supportive_score:
        stance = "supportive"
    else:
        stance = "neutral"

    reasons = [
        note for _, note in sorted(matched_notes, reverse=True)[:3]
    ]
    if not reasons:
        reasons.append(
            "外部证据覆盖不足，不能据此提高仓位或定投量。"
            if stance == "insufficient"
            else "已采集证据未命中预设的论点破坏或强化关键词。"
        )
    return EvidenceAssessment(
        ticker=ticker.upper(),
        stance=stance,
        risk_score=round(risk_score, 1),
        coverage=round(coverage, 2),
        item_count=len(view.items),
        positive_points=round(positive_points, 2),
        risk_points=round(risk_points, 2),
        break_points=round(break_points, 2),
        reasons=reasons,
        item_ids=[item.item_id for item in view.items],
        primary_source_present=primary_present,
        independent_groups=gate.independent_groups,
        max_manipulation_risk_score=round(max_manipulation, 1),
        can_support_add=can_support_add,
        aggregate_gate_decision=gate.decision,
        gate_reasons=gate_reasons,
    )
