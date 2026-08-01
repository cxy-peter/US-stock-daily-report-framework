"""Deterministic research council for thesis, price, evidence, and regime."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .evidence import EvidenceAssessment
from .indicators import Indicators
from .regime import MarketRegime


class ResearchAction(str, Enum):
    EXIT = "退出候选"
    REVIEW = "暂停并核实"
    TRIM = "减仓候选"
    ADD = "加仓候选"
    HOLD_NO_CHASE = "持有不追高"
    HOLD = "继续持有"
    OPEN = "开仓候选"
    WATCH = "继续观察"


RESEARCH_SEVERITY = {
    ResearchAction.EXIT: 6,
    ResearchAction.REVIEW: 5,
    ResearchAction.TRIM: 4,
    ResearchAction.ADD: 3,
    ResearchAction.OPEN: 3,
    ResearchAction.HOLD_NO_CHASE: 1,
    ResearchAction.HOLD: 0,
    ResearchAction.WATCH: 0,
}


@dataclass
class ResearchSettings:
    drawdown_add_threshold: float = -0.25
    fomo_runup_1m: float = 0.35
    anomaly_1d_drop: float = -0.12
    volume_spike_mult: float = 2.5
    trim_gain_threshold: float = 1.0
    institutional_graduation_mcap: float = 8.0e9
    add_requires_evidence: bool = True
    allow_add_in_risk_off: bool = False

    @classmethod
    def from_dict(cls, data: dict | None) -> "ResearchSettings":
        data = data or {}
        boolean_fields = {"add_requires_evidence", "allow_add_in_risk_off"}
        values = {}
        for name in cls.__dataclass_fields__:
            if name not in data:
                continue
            values[name] = bool(data[name]) if name in boolean_fields else float(data[name])
        return cls(**values)


@dataclass
class ResearchRecommendation:
    ticker: str
    name: str
    action: ResearchAction
    reasons: list[str] = field(default_factory=list)
    confidence: int = 50
    thesis_intact: bool = True
    thesis_cracks: list[str] = field(default_factory=list)
    evidence: EvidenceAssessment | None = None
    indicators: Indicators | None = None
    market_cap: float | None = None
    asset_type: str = "unknown"

    @property
    def severity(self) -> int:
        return RESEARCH_SEVERITY[self.action]


THESIS_LABELS = {
    "chokepoint_intact": "关键卡位仍然成立",
    "tam_still_expanding": "可服务市场仍在扩张",
    "no_credible_competitor": "尚未被可信竞品替代",
    "vertical_or_moat_intact": "护城河或垂直整合仍然成立",
    "cashflow_or_index_role_intact": "现金流或指数配置角色仍然成立",
    "regulatory_access_intact": "监管与市场准入仍然成立",
    "manager_process_intact": "管理流程仍然成立",
    "balance_sheet_runway_intact": "资产负债表与资金续航仍然成立",
    "execution_intact": "执行能力仍然成立",
    "power_pipeline_intact": "电力资源管线仍然成立",
    "launch_economics_intact": "发射经济性仍然成立",
    "starlink_growth_intact": "Starlink增长假设仍然成立",
    "capital_intensity_funded": "资本开支仍有资金支持",
    "valuation_dilution_acceptable": "估值与稀释风险仍在容忍范围",
}


def _thesis_state(checks: dict | None) -> tuple[bool, list[str]]:
    cracks = [
        THESIS_LABELS.get(key, key)
        for key, value in (checks or {}).items()
        if value is False
    ]
    return not cracks, cracks


def _evidence_allows_add(
    evidence: EvidenceAssessment | None,
    min_coverage: float,
) -> bool:
    return bool(
        evidence
        and evidence.can_support_add
        and evidence.coverage >= min_coverage
        and evidence.stance in {"supportive", "neutral"}
    )


def evaluate_holding(
    holding: dict,
    indicators: Indicators,
    evidence: EvidenceAssessment,
    regime: MarketRegime,
    settings: ResearchSettings,
    market_cap: float | None = None,
    asset_type: str = "unknown",
    evidence_min_coverage: float = 0.60,
) -> ResearchRecommendation:
    ticker = str(holding["ticker"]).upper()
    intact, cracks = _thesis_state(holding.get("thesis_checks"))
    rec = ResearchRecommendation(
        ticker=ticker,
        name=str(holding.get("name", ticker)),
        action=ResearchAction.HOLD,
        thesis_intact=intact,
        thesis_cracks=cracks,
        evidence=evidence,
        indicators=indicators,
        market_cap=market_cap,
        asset_type=asset_type,
    )

    if not intact:
        rec.action = ResearchAction.EXIT
        rec.confidence = 95
        rec.reasons.append("人工维护的核心论点检查失败：" + "；".join(cracks))
        return rec

    anomaly = (
        indicators.last_day_change <= settings.anomaly_1d_drop
        and indicators.volume_ratio == indicators.volume_ratio
        and indicators.volume_ratio >= settings.volume_spike_mult
    )
    if anomaly:
        rec.action = ResearchAction.REVIEW
        rec.confidence = 90
        rec.reasons.append(
            f"单日变化 {indicators.last_day_change:+.1%} 且量比 "
            f"{indicators.volume_ratio:.1f}x，先核实事件，本次不机械交易。"
        )
        return rec

    if evidence.stance == "thesis_risk":
        rec.action = ResearchAction.REVIEW
        rec.confidence = 85
        rec.reasons.append(
            f"外部证据风险分 {evidence.risk_score:.0f}/100，出现潜在论点风险；"
            "必须回到公告、财报或监管材料核实。"
        )
        rec.reasons.extend(evidence.reasons[:2])
        return rec

    entry = holding.get("entry_price")
    unrealized = None
    try:
        if entry not in (None, "", 0):
            unrealized = indicators.price / float(entry) - 1
    except (TypeError, ValueError, ZeroDivisionError):
        unrealized = None
    framework = str(holding.get("framework", "serenity_stock"))
    graduated = market_cap is not None and market_cap >= settings.institutional_graduation_mcap
    if (
        framework == "serenity_stock"
        and graduated
        and unrealized is not None
        and unrealized >= settings.trim_gain_threshold
    ):
        rec.action = ResearchAction.TRIM
        rec.confidence = 75
        rec.reasons.append(
            f"市值已进入机构区间且估算浮盈 {unrealized:+.0%}，进入部分减仓复核，而非自动退出。"
        )
        return rec

    if indicators.ret_1m >= settings.fomo_runup_1m:
        rec.action = ResearchAction.HOLD_NO_CHASE
        rec.confidence = 80
        rec.reasons.append(f"近1个月已上涨 {indicators.ret_1m:+.1%}，继续持有但不追高。")
        return rec

    if indicators.drawdown_from_peak <= settings.drawdown_add_threshold:
        if regime.label == "risk_off" and not settings.allow_add_in_risk_off:
            rec.confidence = 75
            rec.reasons.append(
                f"距峰值回撤 {indicators.drawdown_from_peak:+.1%}，但市场处于 risk_off；"
                "不能把价格下跌直接当作新增条件。"
            )
            return rec
        if settings.add_requires_evidence and not _evidence_allows_add(
            evidence, evidence_min_coverage
        ):
            rec.confidence = 70
            rec.reasons.append(
                f"距峰值回撤 {indicators.drawdown_from_peak:+.1%}，但一级来源、独立证据组、"
                "覆盖率或操纵风险门控未全部通过，不能进入加仓候选。"
            )
            rec.reasons.extend(evidence.gate_reasons[:2])
            return rec
        rec.action = ResearchAction.ADD
        rec.confidence = 75
        rec.reasons.append(
            f"距峰值回撤 {indicators.drawdown_from_peak:+.1%}，论点未破且完整证据门控通过。"
        )
        return rec

    rec.confidence = 70 if evidence.stance != "insufficient" else 55
    rec.reasons.append("论点未破、无异常事件且未触发增减仓条件：继续持有。")
    if evidence.stance == "insufficient":
        rec.reasons.append("外部证据覆盖不足，因此不会提高仓位或定投量。")
    return rec


def evaluate_watchlist(
    row: dict,
    indicators: Indicators,
    evidence: EvidenceAssessment,
    regime: MarketRegime,
    settings: ResearchSettings,
    market_cap: float | None,
    evidence_min_coverage: float = 0.60,
) -> ResearchRecommendation:
    ticker = str(row["ticker"]).upper()
    rec = ResearchRecommendation(
        ticker=ticker,
        name=str(row.get("name", ticker)),
        action=ResearchAction.WATCH,
        evidence=evidence,
        indicators=indicators,
        market_cap=market_cap,
        asset_type=str(row.get("asset_type", "stock")),
    )
    cond = row.get("entry_when", {}) or {}
    cap_limit = float(cond.get("mcap_below", 2.0e9))
    market_cap_ok = market_cap is not None and market_cap <= cap_limit
    tam_ok = bool(cond.get("tam_confirmed", cond.get("tam_above", False)))
    evidence_ok = _evidence_allows_add(evidence, evidence_min_coverage)
    not_extended = indicators.ret_1m < settings.fomo_runup_1m
    regime_ok = regime.label != "risk_off"

    if market_cap_ok and tam_ok and evidence_ok and not_extended and regime_ok:
        rec.action = ResearchAction.OPEN
        rec.confidence = 65
        rec.reasons.append(
            f"市值 ${market_cap / 1e9:.2f}B、TAM检查、完整证据门控、价格和市场状态均通过；"
            "进入人工尽调后的开仓候选。"
        )
        return rec

    missing = []
    if not market_cap_ok:
        missing.append("市值条件不满足或缺失")
    if not tam_ok:
        missing.append("TAM未人工确认")
    if not evidence_ok:
        missing.append("一级来源、独立证据、覆盖率或操纵风险门控未通过")
    if not not_extended:
        missing.append("近1个月涨幅过高")
    if not regime_ok:
        missing.append("市场处于 risk_off")
    rec.reasons.append("继续观察：" + "；".join(missing))
    return rec
