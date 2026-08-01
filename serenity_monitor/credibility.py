"""Dynamic credibility and copy-trade risk gates for KOL/fund-manager opinions.

The key design choice is to keep three questions separate:
1. Is the source epistemically credible?
2. Is this specific claim well formed and independently verifiable?
3. Is copying the manager's trade survivable under leverage/liquidity/funding stress?

A correct long-term thesis can still be an unsafe trade.  Therefore high source
credibility never overrides a failed fragility or market-manipulation gate.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from enum import Enum


class SourceType(str, Enum):
    PRIMARY_DOCUMENT = "primary_document"
    REGULATED_INSTITUTION = "regulated_institution"
    LARGE_FUND_MANAGER = "large_fund_manager"
    SMALL_FUND_MANAGER = "small_fund_manager"
    INDEPENDENT_KOL = "independent_kol"
    ANONYMOUS = "anonymous"


class Disclosure(str, Enum):
    ALWAYS = "always"
    SOMETIMES = "sometimes"
    NEVER = "never"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TrackRecord:
    observations: int = 0
    hits: int = 0
    brier_score: float | None = None
    mean_excess_return: float | None = None
    worst_mae: float | None = None

    @property
    def shrunk_hit_rate(self) -> float:
        """Beta(2,2) shrinkage prevents a 2-for-2 KOL from looking perfect."""
        n = max(0, int(self.observations))
        hits = min(max(0, int(self.hits)), n)
        return (hits + 2.0) / (n + 4.0)

    @property
    def sample_reliability(self) -> float:
        # Roughly 50 resolved calls are needed before full track-record weight.
        return min(1.0, max(0.0, self.observations / 50.0))


@dataclass(frozen=True)
class SourceProfile:
    source_id: str
    label: str
    source_type: SourceType
    independence_group: str
    identity_verified: bool = False
    regulated_entity: bool = False
    audited_performance: bool = False
    position_disclosure: Disclosure = Disclosure.UNKNOWN
    conflict_disclosure: Disclosure = Disclosure.UNKNOWN
    leverage_disclosure: Disclosure = Disclosure.UNKNOWN
    track_record: TrackRecord = field(default_factory=TrackRecord)
    paid_promotion_risk: float = 0.0  # 0..1
    legal_or_compliance_flags: int = 0
    fund_age_months: int | None = None
    aum_usd: float | None = None
    reported_gross_leverage: float | None = None
    top10_concentration: float | None = None  # 0..1
    estimated_liquidity_days: float | None = None
    prime_broker_concentration: float | None = None  # largest PB share, 0..1


@dataclass(frozen=True)
class Claim:
    claim_id: str
    source_id: str
    ticker: str
    text: str
    direction: str | None = None  # bullish | bearish | neutral
    horizon_days: int | None = None
    probability: float | None = None
    target_price: float | None = None
    invalidation_condition: str | None = None
    primary_evidence_count: int = 0
    position_disclosed: bool | None = None
    conflict_disclosed: bool | None = None
    sponsored: bool = False
    estimated_position_usd: float | None = None


@dataclass(frozen=True)
class MarketContext:
    market_cap_usd: float | None = None
    avg_dollar_volume_20d: float | None = None
    ret_1m: float | None = None
    volume_ratio: float | None = None
    short_interest_pct: float | None = None
    borrow_fee_pct: float | None = None
    options_iv_percentile: float | None = None


@dataclass(frozen=True)
class CredibilityAssessment:
    source_score: float
    claim_score: float
    manager_fragility_score: float
    manipulation_risk_score: float
    research_weight: float
    can_inform_research: bool
    copy_trade_allowed: bool
    red_flags: tuple[str, ...]
    positives: tuple[str, ...]
    independence_group: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AggregateGate:
    decision: str  # block | review | context_only | support
    support_score: float
    independent_groups: int
    primary_source_present: bool
    reasons: tuple[str, ...]


PROMOTIONAL_TERMS = (
    "10x", "100x", "guaranteed", "can't lose", "must buy", "load up",
    "moon", "squeeze incoming", "稳赚", "梭哈", "必涨", "翻倍", "财富自由",
)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _disclosure_points(value: Disclosure, full: float) -> float:
    return {
        Disclosure.ALWAYS: full,
        Disclosure.SOMETIMES: full * 0.45,
        Disclosure.UNKNOWN: 0.0,
        Disclosure.NEVER: -full * 0.8,
    }[value]


def assess_source(profile: SourceProfile) -> tuple[float, list[str], list[str]]:
    score = {
        SourceType.PRIMARY_DOCUMENT: 78.0,
        SourceType.REGULATED_INSTITUTION: 64.0,
        SourceType.LARGE_FUND_MANAGER: 55.0,
        SourceType.SMALL_FUND_MANAGER: 44.0,
        SourceType.INDEPENDENT_KOL: 34.0,
        SourceType.ANONYMOUS: 18.0,
    }[profile.source_type]
    positives: list[str] = []
    flags: list[str] = []

    if profile.identity_verified:
        score += 8
        positives.append("身份已核验")
    else:
        score -= 8
        flags.append("身份或任职关系未充分核验")
    if profile.regulated_entity:
        score += 6
        positives.append("受监管实体")
    if profile.audited_performance:
        score += 8
        positives.append("业绩经审计/第三方验证")
    else:
        flags.append("业绩可能为自报或选择性展示")

    tr = profile.track_record
    if tr.observations:
        skill = (tr.shrunk_hit_rate - 0.5) * 36.0
        score += skill * tr.sample_reliability
        if tr.brier_score is not None:
            score += (0.25 - _clamp(tr.brier_score, 0, 1)) * 18.0 * tr.sample_reliability
        positives.append(f"已解析历史观点 {tr.observations} 条")
        if tr.observations < 20:
            flags.append("历史样本较少，命中率已做贝叶斯收缩")
    else:
        score -= 7
        flags.append("没有可复核的历史观点台账")

    score += _disclosure_points(profile.position_disclosure, 7)
    score += _disclosure_points(profile.conflict_disclosure, 6)
    score += _disclosure_points(profile.leverage_disclosure, 5)
    if profile.position_disclosure in {Disclosure.NEVER, Disclosure.UNKNOWN}:
        flags.append("持仓方向/变动披露不足")
    if profile.conflict_disclosure in {Disclosure.NEVER, Disclosure.UNKNOWN}:
        flags.append("利益冲突披露不足")
    if profile.leverage_disclosure in {Disclosure.NEVER, Disclosure.UNKNOWN}:
        flags.append("杠杆与融资约束不透明")

    score -= 22.0 * _clamp(profile.paid_promotion_risk, 0, 1)
    if profile.paid_promotion_risk >= 0.35:
        flags.append("存在较高推广/带货激励风险")
    if profile.legal_or_compliance_flags:
        score -= min(36.0, 12.0 * profile.legal_or_compliance_flags)
        flags.append(f"存在 {profile.legal_or_compliance_flags} 项法律/合规风险标记")

    # Hard caps when evidence quality is structurally weak.
    cap = {
        SourceType.PRIMARY_DOCUMENT: 100,
        SourceType.REGULATED_INSTITUTION: 90,
        SourceType.LARGE_FUND_MANAGER: 82,
        SourceType.SMALL_FUND_MANAGER: 72,
        SourceType.INDEPENDENT_KOL: 62,
        SourceType.ANONYMOUS: 35,
    }[profile.source_type]
    if not profile.audited_performance and profile.source_type in {
        SourceType.SMALL_FUND_MANAGER, SourceType.INDEPENDENT_KOL
    }:
        cap = min(cap, 58)
    return round(_clamp(score, 0, cap), 1), positives, flags


def assess_claim(claim: Claim) -> tuple[float, list[str], list[str]]:
    score = 30.0
    positives: list[str] = []
    flags: list[str] = []
    if claim.direction in {"bullish", "bearish", "neutral"}:
        score += 10
        positives.append("方向明确")
    else:
        flags.append("观点方向不可检验")
    if claim.horizon_days and claim.horizon_days > 0:
        score += 10
        positives.append("期限明确")
    else:
        flags.append("没有明确验证期限")
    if claim.invalidation_condition:
        score += 12
        positives.append("给出失效条件")
    else:
        flags.append("没有失效条件，容易事后改口径")
    if claim.primary_evidence_count:
        score += min(18, 6 * claim.primary_evidence_count)
        positives.append("引用原始材料")
    else:
        flags.append("未引用公告/财报/监管数据等原始材料")
    if claim.position_disclosed is True:
        score += 8
        positives.append("披露相关持仓")
    elif claim.position_disclosed is False:
        score -= 10
        flags.append("明确未披露相关持仓")
    else:
        flags.append("是否持仓未知")
    if claim.conflict_disclosed is True:
        score += 5
    elif claim.conflict_disclosed is False:
        score -= 8
        flags.append("利益冲突未披露")
    if claim.sponsored:
        score -= 25
        flags.append("内容存在付费/赞助关系")
    lowered = claim.text.casefold()
    promotional_hits = [term for term in PROMOTIONAL_TERMS if term.casefold() in lowered]
    if promotional_hits:
        score -= min(30, 10 + 5 * len(promotional_hits))
        flags.append("使用高煽动性或确定性措辞")
    return round(_clamp(score), 1), positives, flags


def assess_manager_fragility(profile: SourceProfile) -> tuple[float, list[str]]:
    if profile.source_type not in {
        SourceType.LARGE_FUND_MANAGER,
        SourceType.SMALL_FUND_MANAGER,
        SourceType.INDEPENDENT_KOL,
    }:
        return 0.0, []
    risk = 0.0
    flags: list[str] = []
    if profile.fund_age_months is not None and profile.fund_age_months < 24:
        risk += 12
        flags.append("基金跨周期历史不足两年")
    if profile.reported_gross_leverage is None:
        risk += 12
        flags.append("总杠杆未知")
    elif profile.reported_gross_leverage >= 3.0:
        risk += 35
        flags.append(f"总杠杆较高（约 {profile.reported_gross_leverage:.1f}x）")
    elif profile.reported_gross_leverage >= 2.0:
        risk += 22
        flags.append(f"总杠杆偏高（约 {profile.reported_gross_leverage:.1f}x）")
    elif profile.reported_gross_leverage >= 1.4:
        risk += 10

    if profile.top10_concentration is None:
        risk += 8
        flags.append("前十大集中度未知")
    elif profile.top10_concentration >= 0.80:
        risk += 30
        flags.append(f"前十大仓位集中度 {profile.top10_concentration:.0%}")
    elif profile.top10_concentration >= 0.60:
        risk += 18
        flags.append(f"前十大仓位集中度 {profile.top10_concentration:.0%}")

    if profile.estimated_liquidity_days is None:
        risk += 8
        flags.append("组合退出天数未知")
    elif profile.estimated_liquidity_days >= 10:
        risk += 30
        flags.append(f"估算退出需 {profile.estimated_liquidity_days:.1f} 个交易日")
    elif profile.estimated_liquidity_days >= 5:
        risk += 18

    if profile.prime_broker_concentration is not None:
        if profile.prime_broker_concentration >= 0.75:
            risk += 20
            flags.append("融资/主经纪商高度集中")
        elif profile.prime_broker_concentration >= 0.50:
            risk += 10
    return round(_clamp(risk), 1), flags


def assess_manipulation_and_crowding(
    profile: SourceProfile, claim: Claim, market: MarketContext, source_score: float
) -> tuple[float, list[str]]:
    risk = 0.0
    flags: list[str] = []
    adv = market.avg_dollar_volume_20d
    if adv is not None:
        if adv < 5_000_000:
            risk += 30
            flags.append("标的流动性很低，观点可能显著影响价格")
        elif adv < 20_000_000:
            risk += 16
            flags.append("标的流动性偏低")
    if claim.estimated_position_usd and adv and adv > 0:
        ratio = claim.estimated_position_usd / adv
        if ratio >= 1.0:
            risk += 35
            flags.append("披露/估算仓位超过一个日均成交额")
        elif ratio >= 0.25:
            risk += 18
            flags.append("仓位相对日均成交额偏大")
    if market.ret_1m is not None and market.ret_1m >= 0.30:
        risk += 18
        flags.append(f"近一月已上涨 {market.ret_1m:.0%}，存在拥挤/FOMO 风险")
    if market.volume_ratio is not None and market.volume_ratio >= 2.0:
        risk += 12
        flags.append(f"成交量约为常态 {market.volume_ratio:.1f}x")
    if market.short_interest_pct is not None and market.short_interest_pct >= 0.20:
        risk += 12
        flags.append("空头拥挤，挤仓与反转风险同时上升")
    if market.borrow_fee_pct is not None and market.borrow_fee_pct >= 0.20:
        risk += 10
        flags.append("融券成本异常")
    if claim.position_disclosed is not True:
        risk += 12
        flags.append("观点发布者的持仓/退出计划未知")
    if claim.sponsored or profile.paid_promotion_risk >= 0.35:
        risk += 22
    if source_score < 45:
        risk += 10
    return round(_clamp(risk), 1), flags


def assess_opinion(
    profile: SourceProfile, claim: Claim, market: MarketContext
) -> CredibilityAssessment:
    source_score, source_pos, source_flags = assess_source(profile)
    claim_score, claim_pos, claim_flags = assess_claim(claim)
    fragility, fragility_flags = assess_manager_fragility(profile)
    manipulation, manipulation_flags = assess_manipulation_and_crowding(
        profile, claim, market, source_score
    )

    type_cap = {
        SourceType.PRIMARY_DOCUMENT: 1.00,
        SourceType.REGULATED_INSTITUTION: 0.80,
        SourceType.LARGE_FUND_MANAGER: 0.65,
        SourceType.SMALL_FUND_MANAGER: 0.50,
        SourceType.INDEPENDENT_KOL: 0.35,
        SourceType.ANONYMOUS: 0.15,
    }[profile.source_type]
    raw = (source_score / 100.0) * (claim_score / 100.0)
    raw *= 1.0 - 0.70 * (manipulation / 100.0)
    research_weight = round(max(0.0, min(type_cap, raw)), 3)

    manager_hard_block = (
        (profile.reported_gross_leverage is not None and profile.reported_gross_leverage >= 2.0)
        or (profile.top10_concentration is not None and profile.top10_concentration >= 0.60)
        or (profile.estimated_liquidity_days is not None and profile.estimated_liquidity_days >= 5.0)
        or (
            profile.prime_broker_concentration is not None
            and profile.prime_broker_concentration >= 0.50
        )
    )
    copy_trade_allowed = (
        not manager_hard_block
        and
        fragility < 35
        and manipulation < 35
        and claim.primary_evidence_count >= 1
        and claim.invalidation_condition is not None
        and source_score >= 55
    )
    can_inform_research = research_weight >= 0.15 and manipulation < 75

    flags = tuple(dict.fromkeys(source_flags + claim_flags + fragility_flags + manipulation_flags))
    positives = tuple(dict.fromkeys(source_pos + claim_pos))
    return CredibilityAssessment(
        source_score=source_score,
        claim_score=claim_score,
        manager_fragility_score=fragility,
        manipulation_risk_score=manipulation,
        research_weight=research_weight,
        can_inform_research=can_inform_research,
        copy_trade_allowed=copy_trade_allowed,
        red_flags=flags,
        positives=positives,
        independence_group=profile.independence_group,
    )


def aggregate_gate(
    assessments: list[CredibilityAssessment],
    primary_source_present: bool,
    minimum_independent_groups: int = 2,
) -> AggregateGate:
    usable = [a for a in assessments if a.can_inform_research]
    groups: dict[str, float] = {}
    reasons: list[str] = []
    for assessment in usable:
        groups[assessment.independence_group] = max(
            groups.get(assessment.independence_group, 0.0), assessment.research_weight
        )
    support = sum(sorted(groups.values(), reverse=True)[:3])
    high_manipulation = any(a.manipulation_risk_score >= 60 for a in assessments)
    high_fragility = any(a.manager_fragility_score >= 60 for a in assessments)

    if high_manipulation:
        return AggregateGate(
            "block", round(support, 3), len(groups), primary_source_present,
            ("存在高操纵/拥挤风险，禁止依据该观点调仓",),
        )
    if not primary_source_present:
        reasons.append("缺少公告、财报、监管数据等一级来源")
    if len(groups) < minimum_independent_groups:
        reasons.append(f"独立证据组仅 {len(groups)} 个，低于 {minimum_independent_groups} 个")
    if high_fragility:
        reasons.append("至少一名基金经理存在高杠杆/集中/流动性脆弱性；观点可研究但不可复制仓位")

    if reasons:
        decision = "review" if primary_source_present and support >= 0.35 else "context_only"
        return AggregateGate(decision, round(support, 3), len(groups), primary_source_present, tuple(reasons))
    if support >= 0.70:
        return AggregateGate(
            "support", round(support, 3), len(groups), True,
            ("一级来源与至少两个独立证据组相互印证",),
        )
    return AggregateGate(
        "context_only", round(support, 3), len(groups), True,
        ("证据独立性合格，但总支持权重仍不足以触发调仓",),
    )
