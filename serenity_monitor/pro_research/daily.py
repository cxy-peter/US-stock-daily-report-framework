"""Unified one-report Pro research engine.

The engine enriches an owner-only accounting snapshot with policy, prediction-
market, factor-risk, dynamic-exposure and manager-skill research. It does not
connect to a broker, place an order, or infer an executed trade.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from .barra import BarraProxyResult, fit_barra_proxy
from .common import clamp, safe_float
from .kalman import KalmanExposureResult, kalman_dynamic_exposures
from .manager_skill import ManagerFragility, ManagerSkillResult, evaluate_manager_skill
from .policy import TrumpPolicyIndexResult, compute_trump_policy_index
from .polymarket import PolymarketStudyResult, study_resolved_markets


@dataclass(frozen=True)
class DcaProposal:
    ticker: str
    configured_daily_usd: float
    proposed_daily_usd: float
    status: str
    reason: str
    automatic_execution: bool = False


@dataclass(frozen=True)
class ProDailyReport:
    schema_version: str
    report_date: str
    generated_at: str
    report_status: str
    portfolio_as_of: str
    data_confidence: float
    effective_risk_budget: float
    market_state: str
    portfolio_action: str
    summary: str
    source_health: tuple[Mapping[str, Any], ...]
    dca: tuple[DcaProposal, ...]
    trump_policy: TrumpPolicyIndexResult
    polymarket: PolymarketStudyResult
    barra: BarraProxyResult | None
    kalman: KalmanExposureResult | None
    manager_skill: ManagerSkillResult | None
    warnings: tuple[str, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "report_date": self.report_date,
            "generated_at": self.generated_at,
            "report_status": self.report_status,
            "portfolio_as_of": self.portfolio_as_of,
            "data_confidence": self.data_confidence,
            "effective_risk_budget": self.effective_risk_budget,
            "market_state": self.market_state,
            "portfolio_action": self.portfolio_action,
            "summary": self.summary,
            "source_health": [dict(item) for item in self.source_health],
            "dca": [
                {
                    "ticker": item.ticker,
                    "configured_daily_usd": item.configured_daily_usd,
                    "proposed_daily_usd": item.proposed_daily_usd,
                    "status": item.status,
                    "reason": item.reason,
                    "automatic_execution": False,
                }
                for item in self.dca
            ],
            "models": {
                "trump_policy": self.trump_policy.to_dict(),
                "polymarket": self.polymarket.to_dict(),
                "barra": None if self.barra is None else self.barra.to_dict(),
                "kalman": None if self.kalman is None else self.kalman.to_dict(),
                "manager_skill": (
                    None if self.manager_skill is None else self.manager_skill.to_dict()
                ),
            },
            "warnings": list(self.warnings),
            "automatic_trading_permitted": False,
        }


def _portfolio_weights(snapshot: Mapping[str, Any], asset_columns: Iterable[str]) -> dict[str, float]:
    columns = {str(item).upper() for item in asset_columns}
    rows = list(snapshot.get("positions") or [])
    values: dict[str, float] = {}
    explicit_weights: dict[str, float] = {}
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        if ticker not in columns:
            continue
        weight = safe_float(row.get("weight"))
        value = safe_float(row.get("market_value_usd"))
        if weight is not None and weight >= 0:
            explicit_weights[ticker] = weight
        elif value is not None and value >= 0:
            values[ticker] = value
    if explicit_weights:
        return explicit_weights
    if values:
        total = sum(values.values())
        if total > 0:
            return {key: value / total for key, value in values.items()}
    equal = sorted(columns)
    if not equal:
        raise ValueError("portfolio snapshot has no asset represented in returns")
    return {key: 1.0 / len(equal) for key in equal}


def _portfolio_return(asset_returns: pd.DataFrame, weights: Mapping[str, float]) -> pd.Series:
    columns = [column for column in asset_returns.columns if str(column).upper() in weights]
    if not columns:
        raise ValueError("portfolio weights do not overlap asset returns")
    vector = np.array([weights[str(column).upper()] for column in columns], dtype=float)
    vector = vector / vector.sum()
    return asset_returns[columns].mul(vector, axis=1).sum(axis=1).rename("portfolio")


def _date_age_days(value: str, report_date: dt.date) -> int | None:
    try:
        date_value = dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return (report_date - date_value).days


def _health(source: str, status: str, required: bool, detail: str) -> dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "required": bool(required),
        "detail": detail,
    }


def build_pro_daily_report(
    *,
    report_date: dt.date,
    portfolio_snapshot: Mapping[str, Any],
    asset_returns: pd.DataFrame | None,
    factor_returns: pd.DataFrame | None,
    policy_events: Iterable[Mapping[str, Any]] = (),
    polymarket_events: Iterable[Mapping[str, Any]] = (),
    dca_plan: Mapping[str, float] | None = None,
    objective_risk_multiplier: float = 1.0,
    accepted_close_status: str = "unknown",
    social_heat: Mapping[str, Any] | None = None,
    prediction_state: str = "research_only",
    manager_fund_returns: pd.Series | None = None,
    manager_factor_returns: pd.DataFrame | None = None,
    manager_fragility: ManagerFragility | None = None,
    generated_at: dt.datetime | None = None,
) -> ProDailyReport:
    """Build one daily report whether or not a position change is proposed."""

    generated = generated_at or dt.datetime.now(dt.timezone.utc)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("generated_at must include timezone")
    generated = generated.astimezone(dt.timezone.utc)
    source_health: list[Mapping[str, Any]] = []
    warnings: list[str] = [
        "The report is decision support only and has no broker order endpoint.",
        "Configured, modeled and broker-confirmed DCA must remain separate.",
    ]

    portfolio_as_of = str(portfolio_snapshot.get("as_of") or "")
    age_days = _date_age_days(portfolio_as_of, report_date)
    if age_days is None:
        portfolio_confidence = 0.40
        source_health.append(_health("portfolio_snapshot", "blocked", True, "as_of is missing or invalid"))
    elif age_days < 0:
        portfolio_confidence = 0.0
        source_health.append(_health("portfolio_snapshot", "error", True, "snapshot is future-dated"))
    elif age_days <= 1:
        portfolio_confidence = 1.0
        source_health.append(_health("portfolio_snapshot", "healthy", True, f"as_of={portfolio_as_of}"))
    elif age_days <= 4:
        portfolio_confidence = 0.80
        source_health.append(_health("portfolio_snapshot", "degraded", True, f"stale_days={age_days}"))
    else:
        portfolio_confidence = 0.50
        source_health.append(_health("portfolio_snapshot", "stale", True, f"stale_days={age_days}"))

    close_status = str(accepted_close_status or "unknown").casefold()
    close_confidence = {
        "healthy": 1.0,
        "accepted": 1.0,
        "warning": 0.75,
        "degraded": 0.60,
        "unknown": 0.50,
        "blocked": 0.35,
        "error": 0.0,
    }.get(close_status, 0.40)
    source_health.append(
        _health(
            "accepted_close",
            close_status,
            True,
            "two-source settlement-grade close status",
        )
    )

    trump = compute_trump_policy_index(policy_events, as_of=generated)
    source_health.append(_health("trump_policy", trump.status, False, f"events={trump.accepted_count}"))
    polymarket = study_resolved_markets(polymarket_events)
    source_health.append(
        _health("polymarket_settlements", polymarket.status, False, f"events={polymarket.accepted_count}")
    )

    barra: BarraProxyResult | None = None
    kalman: KalmanExposureResult | None = None
    factor_confidence = 0.0
    if asset_returns is not None and factor_returns is not None:
        try:
            weights = _portfolio_weights(portfolio_snapshot, asset_returns.columns)
            barra = fit_barra_proxy(asset_returns, factor_returns, weights)
            portfolio_return = _portfolio_return(asset_returns, weights)
            kalman = kalman_dynamic_exposures(portfolio_return, factor_returns)
            factor_confidence = 1.0
            source_health.append(
                _health("factor_returns", "healthy", True, f"observations={barra.observations}")
            )
        except (ValueError, np.linalg.LinAlgError) as exc:
            source_health.append(_health("factor_returns", "error", True, type(exc).__name__))
            warnings.append("Factor and dynamic-beta models failed closed.")
    else:
        source_health.append(_health("factor_returns", "blocked", True, "asset/factor returns missing"))

    manager_skill: ManagerSkillResult | None = None
    if manager_fund_returns is not None and manager_factor_returns is not None:
        try:
            manager_skill = evaluate_manager_skill(
                manager_fund_returns,
                manager_factor_returns,
                fragility=manager_fragility,
            )
            source_health.append(
                _health("manager_returns", "healthy", False, f"observations={manager_skill.observations}")
            )
        except (ValueError, np.linalg.LinAlgError) as exc:
            source_health.append(_health("manager_returns", "error", False, type(exc).__name__))
    else:
        source_health.append(_health("manager_returns", "disabled", False, "no manager series supplied"))

    social_multiplier = 1.0
    if social_heat:
        social_status = str(social_heat.get("status") or "unknown").casefold()
        manipulation = clamp(float(social_heat.get("manipulation_penalty") or 0.0), 0.0, 1.0)
        quarantined = bool(social_heat.get("quarantined", False))
        social_multiplier = 1.0 - min(0.05, 0.05 * manipulation + (0.03 if quarantined else 0.0))
        source_health.append(_health("social_heat", social_status, False, "research-only downside overlay"))
    else:
        source_health.append(_health("social_heat", "disabled", False, "no authorized aggregate supplied"))

    prediction_multiplier = {
        "active": 1.0,
        "research_only": 1.0,
        "decayed": 0.95,
        "quarantined": 0.85,
    }.get(str(prediction_state).casefold(), 0.95)
    source_health.append(
        _health("prediction_calibration", str(prediction_state), False, "model-weight state")
    )

    data_confidence = clamp(
        0.45 * portfolio_confidence + 0.30 * close_confidence + 0.25 * factor_confidence,
        0.0,
        1.0,
    )
    objective_multiplier = clamp(float(objective_risk_multiplier), 0.70, 1.00)
    barra_multiplier = 1.0 if barra is None else barra.risk_budget_multiplier
    kalman_multiplier = 1.0 if kalman is None else kalman.risk_budget_multiplier
    effective_risk_budget = clamp(
        objective_multiplier
        * barra_multiplier
        * kalman_multiplier
        * trump.risk_budget_multiplier
        * polymarket.risk_budget_multiplier
        * social_multiplier
        * prediction_multiplier
        * (0.50 + 0.50 * data_confidence),
        0.40,
        1.05,
    )

    if data_confidence < 0.65 or close_status in {"blocked", "error"}:
        report_status = "blocked"
        market_state = "data_degraded"
        portfolio_action = "PAUSE_AND_VERIFY"
        summary = "关键账户或正式收盘数据不足：暂停新增风险并核验数据。"
    elif effective_risk_budget < 0.72:
        report_status = "completed"
        market_state = "risk_off"
        portfolio_action = "RISK_REBALANCE"
        summary = "风险预算显著收紧：不新增高波动风险，复核超限仓位。"
    elif effective_risk_budget < 0.88:
        report_status = "completed"
        market_state = "cautious"
        portfolio_action = "HOLD"
        summary = "市场与组合风险偏谨慎：继续持有，基础定投不加码。"
    else:
        report_status = "completed"
        market_state = "neutral_or_supportive"
        portfolio_action = "HOLD"
        summary = "今日没有需要调整的持仓：继续持有。"

    dca_rows: list[DcaProposal] = []
    for ticker, raw_amount in sorted((dca_plan or {}).items()):
        amount = max(0.0, float(raw_amount))
        if report_status == "blocked":
            proposed = 0.0
            status = "PAUSE_FOR_REVIEW"
            reason = "账户或正式收盘数据未通过；不把计划金额冒充成交。"
        elif effective_risk_budget < 0.72:
            proposed = round(amount * 0.50, 2)
            status = "REDUCE_CANDIDATE"
            reason = "整体风险预算低于72%，下周期减量复核。"
        else:
            proposed = round(amount, 2)
            status = "KEEP_BASE" if effective_risk_budget >= 0.88 else "HOLD_BASE_NO_INCREASE"
            reason = "维持基础计划；没有通过加码所需的独立证据和容量门控。"
        dca_rows.append(
            DcaProposal(
                ticker=str(ticker).upper(),
                configured_daily_usd=round(amount, 2),
                proposed_daily_usd=proposed,
                status=status,
                reason=reason,
            )
        )

    if not dca_rows:
        warnings.append("No DCA plan was supplied in the private configuration.")
    if trump.confidence < 0.35:
        warnings.append("Trump-policy coverage is too weak to influence more than context.")
    if polymarket.decision_score_contribution == 0:
        warnings.append("Polymarket history remains research-only because calibration is insufficient.")

    return ProDailyReport(
        schema_version="pro_daily_report/v1.0.0",
        report_date=report_date.isoformat(),
        generated_at=generated.isoformat(),
        report_status=report_status,
        portfolio_as_of=portfolio_as_of,
        data_confidence=round(data_confidence, 6),
        effective_risk_budget=round(effective_risk_budget, 6),
        market_state=market_state,
        portfolio_action=portfolio_action,
        summary=summary,
        source_health=tuple(source_health),
        dca=tuple(dca_rows),
        trump_policy=trump,
        polymarket=polymarket,
        barra=barra,
        kalman=kalman,
        manager_skill=manager_skill,
        warnings=tuple(warnings),
    )


def render_pro_daily_markdown(report: ProDailyReport) -> str:
    """Render a compact Chinese report from the validated result object."""

    lines = [
        f"# 美股 Pro 每日研究报告 · {report.report_date}",
        "",
        "> 研究与风控辅助，不连接券商、不自动下单；计划、模型与实际成交必须分开。",
        "",
        "## 今日结论",
        f"**{report.summary}**",
        "",
        f"- 报告状态：`{report.report_status}`",
        f"- 市场状态：`{report.market_state}`",
        f"- 组合动作：`{report.portfolio_action}`",
        f"- 数据置信度：{report.data_confidence:.1%}",
        f"- 有效风险预算：{report.effective_risk_budget:.1%}",
        f"- 账户快照日期：{report.portfolio_as_of or 'UNKNOWN'}",
        "",
        "## 定投复核",
        "| 标的 | 配置金额 | 模型建议 | 状态 | 原因 |",
        "|---|---:|---:|---|---|",
    ]
    for row in report.dca:
        lines.append(
            f"| {row.ticker} | ${row.configured_daily_usd:.2f} | "
            f"${row.proposed_daily_usd:.2f} | {row.status} | {row.reason} |"
        )
    if not report.dca:
        lines.append("| - | - | - | NEED_INFO | 私人配置未提供定投计划。 |")

    tpti = report.trump_policy
    lines += [
        "",
        "## Trump Policy Transmission Index",
        f"- 状态：{tpti.status}；有效事件：{tpti.accepted_count}；综合方向：{tpti.composite_score:+.3f}",
        f"- 政策落地持续度：{tpti.policy_persistence:.1%}；置信度：{tpti.confidence:.1%}",
        f"- 风险预算乘数：{tpti.risk_budget_multiplier:.3f}；模型分贡献：{tpti.decision_score_contribution:+.3%}",
    ]
    if tpti.topic_scores:
        lines.append("- 主题：" + "；".join(f"{key}={value:+.2f}" for key, value in tpti.topic_scores.items()))

    poly = report.polymarket
    active_studies = [item for item in poly.studies if item.status in {"active", "neutral"}]
    lines += [
        "",
        "## Polymarket 结算事件研究",
        f"- 状态：{poly.status}；通过点时约束的事件：{poly.accepted_count}",
        f"- 冻结窗口：结算前 {poly.freeze_hours} 小时；可用研究组：{len(active_studies)}",
        "- 结算后的概率不得回填到结算前信号；样本不足时保持 research_only。",
    ]

    lines += ["", "## Barra 风格风险代理"]
    if report.barra is None:
        lines.append("- `BLOCKED`：缺少对齐的资产和因子收益。")
    else:
        lines += [
            f"- 年化波动：{report.barra.annualized_volatility:.1%}",
            f"- 系统性/特异风险：{report.barra.systematic_risk_share:.1%} / {report.barra.specific_risk_share:.1%}",
            f"- 最大风险因子：{report.barra.top_factor or '-'}（{report.barra.top_factor_risk_share:.1%}）",
            f"- 风险预算乘数：{report.barra.risk_budget_multiplier:.3f}",
        ]

    lines += ["", "## Kalman 动态暴露"]
    if report.kalman is None:
        lines.append("- `BLOCKED`：动态暴露未计算。")
    else:
        exposures = "；".join(
            f"{key}={value:+.2f}" for key, value in report.kalman.latest_exposures.items()
        )
        lines += [
            f"- 最新估计：{exposures}",
            f"- 风险预算乘数：{report.kalman.risk_budget_multiplier:.3f}",
            "- 这是净值/收益推断，不是披露持仓。",
        ]

    lines += ["", "## 基金经理能力"]
    if report.manager_skill is None:
        lines.append("- `NOT_DUE / NEED_INFO`：未提供基金或经理收益序列。")
    else:
        manager = report.manager_skill
        lines += [
            f"- 结论：{manager.verdict}；能力分：{manager.skill_score:.1%}",
            f"- 年化 Alpha：{manager.annualized_alpha:+.2%}；Bootstrap 能力概率："
            + ("-" if manager.bootstrap_skill_probability is None else f"{manager.bootstrap_skill_probability:.1%}"),
            f"- 脆弱性：{manager.fragility_score:.1%}；允许复制交易：{'是' if manager.copy_trade_allowed else '否'}",
        ]

    lines += [
        "",
        "## 数据源健康",
        "| 来源 | 状态 | 必需 | 说明 |",
        "|---|---|---|---|",
    ]
    for item in report.source_health:
        lines.append(
            f"| {item['source']} | {item['status']} | "
            f"{'是' if item['required'] else '否'} | {item['detail']} |"
        )
    if report.warnings:
        lines += ["", "## 边界与待办"]
        lines.extend(f"- {warning}" for warning in report.warnings)
    return "\n".join(lines).rstrip() + "\n"
