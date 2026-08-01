"""Markdown rendering for the auditable daily portfolio-research report."""
from __future__ import annotations

import datetime as dt

from .dca_review import DcaReview
from .china_retail_attention import ChinaRetailAttentionResult
from .external_views import ExternalBundle
from .objective_signals import ObjectiveMarketSnapshot
from .regime import MarketRegime
from .rules import ResearchRecommendation
from .sizing import (
    ACTIONABLE,
    PortfolioAction,
    PortfolioSettings,
    PositionPlan,
    round_trade_shares,
)
from .state import PlanChange


def _pct(value: float | None, digits: int = 1) -> str:
    if value is None or value != value:
        return "-"
    return f"{value:.{digits}%}"


def _money(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "待核实现有余额"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}${value:,.0f}"


def _price(value: float | None) -> str:
    if value is None or value != value:
        return "-"
    return f"${value:,.2f}"


def _display_price(plan: PositionPlan) -> str:
    source = plan.price_source or "unknown"
    label = "券商快照回退，非实时" if source == "broker_snapshot_fallback" else source
    as_of = f" {plan.price_as_of}" if plan.price_as_of else ""
    return f"{_price(plan.current_price)}（{label}{as_of}）"


def _shares(value: float | None) -> str:
    value = round_trade_shares(value)
    if value is None:
        return "待核实现有余额"
    if abs(value) < 1e-9:
        return "0"
    return f"{value:+g}"


def _clean(value: object) -> str:
    return str(value or "-").replace("|", "/").replace("\n", " ").strip()


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def render_markdown(
    plans: list[PositionPlan],
    regime: MarketRegime,
    external: ExternalBundle,
    equity: float,
    data_errors: list[str] | None = None,
    changes: list[PlanChange] | None = None,
    portfolio_as_of: str = "",
    cash_known: bool = False,
    generated_at: dt.datetime | None = None,
    broker_snapshot: dict | None = None,
    portfolio_settings: PortfolioSettings | None = None,
    risk_group_exposures: dict[str, float] | None = None,
    recommendations: dict[str, ResearchRecommendation] | None = None,
    dca_reviews: list[DcaReview] | None = None,
    objective_snapshot: ObjectiveMarketSnapshot | None = None,
    china_retail_attention: ChinaRetailAttentionResult | None = None,
) -> str:
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc).astimezone()
    report_date = generated_at.date().isoformat()
    data_errors = data_errors or []
    changes = changes or []
    broker_snapshot = broker_snapshot or {}
    risk_group_exposures = risk_group_exposures or {}
    recommendations = recommendations or {}
    dca_reviews = dca_reviews or []
    holdings = [plan for plan in plans if plan.current_shares > 0]
    watch = [plan for plan in plans if plan.current_shares <= 0]
    actionable = [
        plan
        for plan in holdings
        if plan.action in ACTIONABLE and abs(plan.model_delta_usd) >= 1
    ]
    review = [plan for plan in holdings if plan.action == PortfolioAction.REVIEW]

    out = [
        f"# 每日投资研究与组合风险报告 · {report_date}",
        "",
        "> 本报告将研究结论、证据门控、组合约束和可执行性分开记录。"
        "任何KOL或社交平台观点都不能单独触发 ADD、OPEN 或 EXIT；系统不连接券商，也不自动下单。",
        "",
        "## 今日结论",
    ]
    if not actionable and not review:
        out.append("**今日没有需要调整的持仓：继续持有。**")
    else:
        for plan in sorted(
            actionable + review,
            key=lambda item: (item.action != PortfolioAction.EXIT, -abs(item.model_delta_usd)),
        ):
            if plan.action == PortfolioAction.REVIEW:
                out.append(f"- **{plan.ticker}**：暂停交易并核实事件，本次不改变仓位。")
            else:
                out.append(
                    f"- **{plan.ticker}**：{plan.action.value}；模型变化 "
                    f"{_money(plan.model_delta_usd, signed=True)}；可执行变化 "
                    f"{_money(plan.executable_delta_usd, signed=True)} / {_shares(plan.trade_shares)} 股。"
                )

    deployable_cash = (
        portfolio_settings.immediately_deployable_cash(equity)
        if portfolio_settings is not None
        else None
    )
    if objective_snapshot is not None:
        stress = (
            "-"
            if objective_snapshot.stress_score is None
            else f"{objective_snapshot.stress_score:.2f}"
        )
        out += [
            "",
            "## 客观市场交叉确认（仅下调风险）",
            f"- 数据状态：**{objective_snapshot.status}**；压力分：**{stress}**；"
            f"风险预算乘数：**{objective_snapshot.risk_budget_multiplier:.0%}**。",
            f"- 健康信号组：**{objective_snapshot.healthy_groups}**；独立确认压力的信号组："
            f"**{objective_snapshot.confirming_groups}**；允许收紧风险："
            f"**{_yes_no(objective_snapshot.can_tighten_risk)}**。",
            "- 该层只能降低风险预算，不能提高风险，也不能单独产生 ADD/OPEN/EXIT。",
            "",
            "| 指标 | 信号组 | 状态 | 数值 | 压力分 | 来源/日期 |",
            "|---|---|---|---:|---:|---|",
        ]
        for component in objective_snapshot.components:
            value = "-" if component.value is None else f"{component.value:.4f}"
            component_stress = (
                "-" if component.stress_score is None else f"{component.stress_score:.2f}"
            )
            source = " / ".join(
                part for part in (component.source, component.as_of) if part
            ) or "-"
            out.append(
                f"| {_clean(component.name)} | {_clean(component.group)} | "
                f"{component.status} | {value} | {component_stress} | {_clean(source)} |"
            )
        china = objective_snapshot.china_context
        china_proxy = china.china_equity_proxy or "HXC/KWEB"
        out += [
            "",
            "### 小红书主题的中国/ADR 跨资产验证",
            f"- 状态：**{china.status}**；{china_proxy} 近1月收益："
            f"**{_pct(china.china_equity_return_1m)}**；USD/CNH 近1月变化："
            f"**{_pct(china.usd_cnh_return_1m)}**.",
            "- 该层只提供背景验证，不能触发交易。",
        ]

    if china_retail_attention is not None:
        xhs = china_retail_attention
        out += [
            "",
            "## 小红书 / 中国零售注意力（仅研究）",
            f"- 数据状态：**{xhs.status}**；授权且去重后的记录："
            f"**{xhs.unique_count}**；执行权重：**{xhs.execution_weight:.2%}**。",
            f"- 操纵惩罚：**{xhs.manipulation_penalty:.1%}**；重复爆发："
            f"**{xhs.duplicate_burst_score:.1%}**；来源集中度："
            f"**{xhs.source_concentration:.1%}**。",
            "- X、Reddit 与小红书统一属于一个 `social_media` 证据组；"
            "本层不能触发或逆转交易。",
            f"- 数据健康详情：{_clean(xhs.detail)}",
        ]
        if xhs.topics:
            out += [
                "",
                "| 主题 | 注意力 | 置信度 | ETF/ticker | 模型分贡献 |",
                "|---|---:|---:|---|---:|",
            ]
            for topic in xhs.topics:
                targets = ", ".join(topic.etfs + topic.tickers) or topic.sector or "-"
                out.append(
                    f"| {_clean(topic.topic)} | {topic.attention_score:.1f} | "
                    f"{topic.confidence:.1%} | {_clean(targets)} | "
                    f"{topic.model_weight_contribution:.3%} |"
                )
        for warning in xhs.warnings[:5]:
            out.append(f"- 边界：{_clean(warning)}")

    out += [
        "",
        "## 券商快照与组合摘要",
        f"- 快照日期：**{portfolio_as_of or '未填写'}**；来源："
        f"**{_clean(broker_snapshot.get('source', '配置文件'))}**。",
        f"- 账户总价值：**{_money(broker_snapshot.get('total_value_usd', equity))}**；"
        f"已投资：**{_money(broker_snapshot.get('invested_usd'))}**。",
        f"- 现金：**{_money(broker_snapshot.get('cash_usd'))}**；"
        f"购买力：**{_money(broker_snapshot.get('buying_power_usd'))}**；"
        f"扣除现金缓冲后可立即部署：**{_money(deployable_cash)}**。",
        f"- 券商快照累计盈亏：**{_money(broker_snapshot.get('total_pnl_usd'), signed=True)}** "
        f"({_pct(broker_snapshot.get('total_pnl_pct'))})。",
        f"- 按当前行情估算权益：**${equity:,.0f}**"
        f"（{'含已配置现金' if cash_known else '现金未知，仅按持仓市值估算'}）。",
        f"- 市场状态：**{regime.label}**；风险预算乘数：**{regime.risk_multiplier:.0%}**。",
    ]

    out += [
        "",
        "## 风险组暴露与上限",
        "| 风险组 | 当前暴露 | 上限 | 剩余容量 | 状态 |",
        "|---|---:|---:|---:|---|",
    ]
    caps = portfolio_settings.risk_group_caps if portfolio_settings else {}
    for group in sorted(set(risk_group_exposures) | set(caps)):
        exposure = risk_group_exposures.get(group, 0.0)
        cap = caps.get(group)
        remaining = None if cap is None else max(0.0, cap - exposure)
        status = "已满，阻止新增" if cap is not None and exposure >= cap else "可用"
        out.append(
            f"| {group} | {_pct(exposure)} | {_pct(cap)} | {_pct(remaining)} | {status} |"
        )

    out += [
        "",
        "## 持仓决策表（全部持仓）",
        "| 标的 | 名称 | 类型/状态 | 股数 | 行情价/来源 | 当前权重 | 估算成本 | 未实现盈亏 |"
        " 研究结论 | 最终动作 | 风险上限 | 目标权重 | 模型变化 | 可执行变化 | 交易股数 |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for plan in sorted(holdings, key=lambda item: (-item.current_weight, item.ticker)):
        entry = _price(plan.entry_price)
        if plan.entry_price_estimated and plan.entry_price is not None:
            entry += "（估）"
        pnl = (
            f"{_money(plan.unrealized_pnl_usd, signed=True)} / {_pct(plan.unrealized_pnl_pct)}"
            if plan.unrealized_pnl_usd is not None
            else "待核实"
        )
        out.append(
            "| {ticker} | {name} | {asset}/{status} | {shares:g} | {price} | {weight} |"
            " {entry} | {pnl} | {research} | **{action}** | {cap} | {target} |"
            " {model} | {executable} | {trade_shares} |".format(
                ticker=plan.ticker,
                name=_clean(plan.name),
                asset=plan.asset_type,
                status=plan.position_status,
                shares=plan.current_shares,
                price=_display_price(plan),
                weight=_pct(plan.current_weight),
                entry=entry,
                pnl=pnl,
                research=plan.research_action.value,
                action=plan.action.value,
                cap=_pct(plan.adjusted_max_weight),
                target=_pct(plan.target_weight),
                model=_money(plan.model_delta_usd, signed=True),
                executable=_money(plan.executable_delta_usd, signed=True),
                trade_shares=_shares(plan.trade_shares),
            )
        )

    out += ["", "## 持仓依据、约束与证据门控"]
    for plan in sorted(holdings, key=lambda item: (-abs(item.model_delta_usd), item.ticker)):
        rec = recommendations.get(plan.ticker)
        out.append(f"### {plan.ticker} · {plan.action.value}")
        out.append(
            f"- 风险组：{', '.join(plan.risk_groups) or '无'}；"
            f"仓位属性：{plan.position_status}；资产类型：{plan.asset_type}。"
        )
        if rec and rec.evidence:
            evidence = rec.evidence
            out.append(
                f"- 证据覆盖率：{evidence.coverage:.0%}；独立证据组："
                f"{evidence.independent_groups}；一级来源：{_yes_no(evidence.primary_source_present)}；"
                f"允许支持新增：{_yes_no(evidence.can_support_add)}。"
            )
            if evidence.gate_reasons:
                out.append("- 证据门控：" + "；".join(_clean(reason) for reason in evidence.gate_reasons))
        for reason in plan.reasons[:4]:
            out.append(f"- 原因：{_clean(reason)}")
        for constraint in plan.constraints[:6]:
            out.append(f"- 约束：{_clean(constraint)}")
        out.append(
            f"- 风险乘数：波动 {plan.volatility_multiplier:.0%} / "
            f"相关性 {plan.correlation_multiplier:.0%} / 市场 {plan.regime_multiplier:.0%}。"
        )

    if dca_reviews:
        out += [
            "",
            "## 工作日定投复核（外部计划，不自动执行）",
            "| 标的 | 基础日金额 | 下周期模型日金额 | 模型周金额 | 复核结论 |"
            " 证据门控 | 风险容量 | 需人工确认 |",
            "|---|---:|---:|---:|---|---|---|---|",
        ]
        for review_item in dca_reviews:
            out.append(
                f"| {review_item.ticker} | ${review_item.base_daily_amount_usd:,.0f} | "
                f"${review_item.proposed_daily_amount_usd:,.0f} | "
                f"${review_item.proposed_weekly_amount_usd:,.0f} | "
                f"{review_item.action.value} | {_yes_no(review_item.evidence_gate_passed)} | "
                f"{_yes_no(review_item.risk_capacity_passed)} | "
                f"{_yes_no(review_item.manual_confirmation_required)} |"
            )
            out.append(
                f"- **{review_item.ticker}**："
                + "；".join(_clean(reason) for reason in review_item.reasons)
            )
            for constraint in review_item.constraints:
                out.append(f"  - 约束：{_clean(constraint)}")

    if watch:
        out += [
            "",
            "## 观察清单",
            "| 标的 | 名称 | 研究结论 | 当前结论 | 模型开仓金额 | 核心原因 |",
            "|---|---|---|---|---:|---|",
        ]
        for plan in sorted(watch, key=lambda item: (-abs(item.model_delta_usd), item.ticker)):
            reason = plan.reasons[0] if plan.reasons else "-"
            out.append(
                f"| {plan.ticker} | {_clean(plan.name)} | {plan.research_action.value} | "
                f"{plan.action.value} | {_money(plan.model_delta_usd, signed=True)} | "
                f"{_clean(reason)} |"
            )

    if changes:
        out += [
            "",
            "## 相比上次运行的变化",
            "| 标的 | 上次动作 | 本次动作 | 上次模型金额 | 本次模型金额 | 变化 |",
            "|---|---|---|---:|---:|---|",
        ]
        for change in changes:
            out.append(
                f"| {change.ticker} | {change.previous_action} | {change.current_action} | "
                f"{_money(change.previous_delta_usd, True)} | "
                f"{_money(change.current_delta_usd, True)} | {_clean(change.detail)} |"
            )

    out += [
        "",
        "## KOL可信度与禁止跟单门控",
        "| 标的 | 来源 | source | claim | 脆弱度 | 操纵/拥挤 | 研究权重 |"
        " 独立组 | 可复制交易 | 红旗 |",
        "|---|---|---:|---:|---:|---:|---:|---|---|---|",
    ]
    evidence_count = 0
    for plan in holdings + watch:
        for item in external.view(plan.ticker).items[:8]:
            evidence_count += 1
            out.append(
                f"| {plan.ticker} | {_clean(item.source)} | {item.source_score:.1f} | "
                f"{item.claim_score:.1f} | {item.manager_fragility_score:.1f} | "
                f"{item.manipulation_risk_score:.1f} | {item.research_weight:.3f} | "
                f"{_clean(item.independence_group)} | {_yes_no(item.copy_trade_allowed)} | "
                f"{_clean('; '.join(item.red_flags[:3]))} |"
            )
    if evidence_count == 0:
        out.append("| - | 本次运行没有可用外部观点 | 0 | 0 | 0 | 0 | 0 | - | 否 | - |")

    out += [
        "",
        "## 数据源健康",
        "| 数据源 | 状态 | 详情 |",
        "|---|---|---|",
    ]
    for status in external.statuses:
        out.append(f"| {status.source} | {status.status} | {_clean(status.detail)} |")
    if data_errors:
        out += ["", "## 数据缺口"]
        out.extend(f"- {_clean(error)}" for error in data_errors)

    out += [
        "",
        "---",
        "### 使用边界",
        "- “继续持有”只表示本次运行没有满足系统化调整条件，不代表收益保证。",
        "- X、Reddit、小红书公开搜索摘要及其他KOL内容仅用于形成可核实假设；"
        "SEC文件、公司公告、财报和监管材料优先。",
        "- 小红书采集仅使用用户提供的公开材料或公开搜索摘要，不绕过登录、验证码、"
        "权限或反爬限制；失败会在数据源健康中显示。",
        "- 定投金额是下一周期研究复核结果。系统不会修改券商计划，不连接券商，也不自动交易。",
        "- 执行任何变化前，必须人工核对实时持仓、现金、税务、交易成本和一级来源。",
    ]
    return "\n".join(out) + "\n"
