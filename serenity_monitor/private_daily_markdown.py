"""Deterministic Markdown view of a validated private daily-report document.

The JSON document is the only machine contract.  This module deliberately has
no clock, file-system, network, market-data or ledger dependency: it validates
one supplied document and formats the normalized copy returned by the schema
validator.  It must never fill in, derive or recalculate portfolio results.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .private_daily_report import (
    LEGACY_SCHEMA_VERSION,
    validate_private_daily_report,
)


_WINDOWS_PATH_RE = re.compile(
    r"(?<![\w:])(?:[A-Za-z]:[\\/]|\\\\)[^\s|`<>\]\[\"']+"
)
_POSIX_PRIVATE_PATH_RE = re.compile(
    r"(?<![\w:/])/(?!/)[^\s|`<>\]\[\"']+",
    re.IGNORECASE,
)
_CREDENTIAL_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|bearer|"
    r"authorization|password|secret|token)\b(\s*[:=]\s*|\s+)([^\s,;|]+)"
)
_MARKDOWN_CONTROL_RE = re.compile(r"([\\|`!\[\]\(\)*])")


def _redact_private_text(value: Any) -> str:
    """Return text safe for the private report body.

    Schema validation is the primary privacy boundary.  This narrow second
    boundary protects free-text reason/action fields from accidentally echoing
    an absolute runtime path or a credential-shaped assignment.
    """

    if value is None:
        return "-"
    if isinstance(value, bool):
        text = "是" if value else "否"
    else:
        text = str(value)
    text = _WINDOWS_PATH_RE.sub("[已隐藏私有路径]", text)
    text = _POSIX_PRIVATE_PATH_RE.sub("[已隐藏私有路径]", text)
    text = _CREDENTIAL_RE.sub(lambda match: f"{match.group(1)}=[已隐藏凭据]", text)
    return text


def _cell(value: Any) -> str:
    """Escape one Markdown-table cell without creating physical line breaks."""

    text = _redact_private_text(value)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _MARKDOWN_CONTROL_RE.sub(r"\\\1", text)
    text = text.replace("\r\n", "<br>").replace("\r", "<br>").replace("\n", "<br>")
    return text


def _inline(value: Any) -> str:
    """Escape free text used outside a table cell."""

    return _cell(value)


def _short_id(value: Any) -> str:
    text = _redact_private_text(value)
    if text == "-":
        return text
    return _cell(text[:12])


def _join_reasons(values: Any) -> str:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return _cell(values)
    return "；".join(_cell(item) for item in values) if values else "-"


def _amount(value: Any, currency: str) -> str:
    if value is None:
        return "-"
    return f"{_cell(value)} {_cell(currency)}"


def _rate(value: Any) -> str:
    if value is None:
        return "不可用"
    return _cell(value)


def _status_label(value: Any) -> str:
    labels = {
        "complete": "完成",
        "complete_with_warnings": "完成但有警告",
        "completed": "完成",
        "settled": "已结算",
        "already_settled": "此前已结算",
        "skipped_by_owner": "用户明确跳过",
        "not_attempted_prior_session_blocked": "因较早交易日受阻而未尝试",
        "not_attempted": "未尝试",
        "passed": "通过",
        "fresh": "本期新估值",
        "blocked": "受阻",
        "partial": "部分完成",
        "no_new_close": "无新收盘",
        "holiday": "休市",
        "carried_forward": "沿用最近估值",
        "carried_forward_display_only": "沿用最近估值（仅展示）",
        "healthy": "健康",
        "degraded": "降级",
        "unavailable": "不可用",
        "pending": "待处理",
        "none": "无",
    }
    raw = str(value) if value is not None else "-"
    return labels.get(raw, _cell(raw))


def _render_session_results(lines: list[str], report: Mapping[str, Any]) -> None:
    lines.extend(
        [
            "## 交易日结算",
            "",
            "| 交易日 | 状态 | 类型 | 日历门 | 价格门 | 公司行动门 | 资金门 | DCA | Confirmed 估值 | Modeled 估值 | 原因代码 |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    sessions = report.get("session_results", [])
    if not sessions:
        lines.append("| - | 无新交易日 | - | - | - | - | - | - | - | - | - |")
    for session in sessions:
        session_kind = "补算" if session.get("is_backfill") else "当期"
        lines.append(
            "| {session} | {status} | {kind} | {calendar_gate} | {price_gate} | "
            "{corporate_action_gate} | {funding_gate} | {dca_status} | "
            "{confirmed_valuation} | {modeled_valuation} | {reasons} |".format(
                session=_cell(session.get("session_date")),
                status=_status_label(session.get("status")),
                kind=session_kind,
                calendar_gate=_status_label(session.get("calendar_gate")),
                price_gate=_status_label(session.get("price_gate")),
                corporate_action_gate=_status_label(
                    session.get("corporate_action_gate")
                ),
                funding_gate=_status_label(session.get("funding_gate")),
                dca_status=_status_label(session.get("dca_status")),
                confirmed_valuation=(
                    f"{_status_label(session.get('confirmed_valuation_status'))} / "
                    f"{_short_id(session.get('confirmed_valuation_id'))}"
                ),
                modeled_valuation=(
                    f"{_status_label(session.get('modeled_valuation_status'))} / "
                    f"{_short_id(session.get('modeled_valuation_id'))}"
                ),
                reasons=_join_reasons(session.get("reason_codes", [])),
            )
        )
    lines.extend(["", "| 交易日 | 收盘批次 | 账本批次 |", "|---|---|---|"])
    if not sessions:
        lines.append("| - | - | - |")
    for session in sessions:
        lines.append(
            f"| {_cell(session.get('session_date'))} | "
            f"{_short_id(session.get('close_batch_id'))} | "
            f"{_short_id(session.get('ledger_batch_id'))} |"
        )
    lines.append("")


def _position_value(position: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in position:
            return position[name]
    return None


def _render_book(
    lines: list[str],
    *,
    title: str,
    book: Mapping[str, Any],
    currency: str,
    no_new_close: bool,
) -> None:
    unavailable = book.get("valuation_status") == "unavailable"
    lines.extend(
        [
            f"### {title}",
            "",
            f"- 估值状态：**{_status_label(book.get('valuation_status'))}**",
            "",
            "| 现金 | 市值 | 净值 | 经济成本 | 已实现损益 | 费用 |",
            "|---:|---:|---:|---:|---:|---:|",
            "| {cash} | {market_value} | {nav} | {cost} | {realized} | {fees} |".format(
                cash=_amount(book.get("cash"), currency),
                market_value=(
                    "不可用"
                    if unavailable
                    else _amount(book.get("market_value"), currency)
                ),
                nav=(
                    "不可用" if unavailable else _amount(book.get("nav"), currency)
                ),
                cost=_amount(book.get("total_economic_cost"), currency),
                realized=_amount(book.get("realized_pnl"), currency),
                fees=_amount(book.get("fees"), currency),
            ),
            "",
            "| 标的 | 账本数量 | 其中模拟数量 | 收盘价 | 价格交易日 | 价格来源 | 市值 | 经济成本 | 平均经济成本 | 未实现损益 | 权重 |",
            "|---|---:|---:|---:|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    positions = book.get("positions", [])
    if not positions:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - |")
    for position in positions:
        lines.append(
            "| {symbol} | {quantity} | {modeled_quantity} | {close} | "
            "{price_session} | {provider} | {market_value} | {cost} | "
            "{average_cost} | {unrealized} | {weight} |".format(
                symbol=_cell(_position_value(position, "symbol", "ticker")),
                quantity=_cell(position.get("quantity")),
                modeled_quantity=_cell(position.get("modeled_quantity")),
                close=_amount(
                    _position_value(position, "accepted_close", "close_price", "price"),
                    currency,
                ),
                price_session=_cell(position.get("price_session")),
                provider=(
                    f"{_cell(position.get('selected_provider_id'))} / "
                    f"{_short_id(position.get('accepted_close_id'))}"
                ),
                market_value=(
                    "不可用"
                    if position.get("market_value") is None
                    else _amount(position.get("market_value"), currency)
                ),
                cost=_amount(
                    _position_value(position, "economic_cost", "total_economic_cost"),
                    currency,
                ),
                average_cost=_amount(
                    _position_value(
                        position, "average_economic_cost", "average_cost"
                    ),
                    currency,
                ),
                unrealized=(
                    "不可用"
                    if position.get("unrealized_pnl") is None
                    else _amount(position.get("unrealized_pnl"), currency)
                ),
                weight=(
                    "不可用"
                    if position.get("portfolio_weight") is None
                    else _rate(position.get("portfolio_weight"))
                ),
            )
        )

    performance = book.get("performance", {})
    valuation_session = performance.get("valuation_session") or "-"
    daily_pnl = (
        "不可用（无新收盘）"
        if no_new_close
        else _amount(performance.get("daily_pnl"), currency)
    )
    daily_return = (
        "不可用（无新收盘）"
        if no_new_close
        else _rate(performance.get("daily_return"))
    )
    lines.extend(
        [
            "",
            "| 估值交易日 | 前值净值 | 前期累计 TWR | 外部净流入 | 加权外部流量 | 当日损益 | 当日收益率 | 累计 TWR |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
            "| {session} | {prior_nav} | {prior_twr} | {flow} | {weighted_flow} | {pnl} | {daily_return} | {twr} |".format(
                session=_cell(valuation_session),
                prior_nav=_amount(performance.get("prior_nav"), currency),
                prior_twr=_rate(performance.get("prior_cumulative_twr")),
                flow=_amount(performance.get("net_external_flow"), currency),
                weighted_flow=_amount(
                    performance.get("weighted_external_flow"), currency
                ),
                pnl=daily_pnl,
                daily_return=daily_return,
                twr=_rate(performance.get("cumulative_twr")),
            ),
            "",
        ]
    )


def _configured_summary(layer: Mapping[str, Any], currency: str) -> str:
    return f"基础金额：{_amount(layer['amount'], currency)}"


def _proposed_summary(layer: Mapping[str, Any], currency: str) -> str:
    amount = _amount(layer.get("amount"), currency)
    reasons = _join_reasons(layer.get("rationale_codes", []))
    return (
        f"金额：{amount}；动作：{_cell(layer['action'])}；"
        f"理由：{reasons}；自动执行：否"
    )


def _modeled_session_summary(layer: Mapping[str, Any], currency: str) -> str:
    accepted_close = _amount(layer.get("accepted_close"), currency)
    return (
        f"状态：{_cell(layer.get('status'))}；金额：{_amount(layer.get('amount'), currency)}；"
        f"支出：{_amount(layer.get('spend'), currency)}；"
        f"余款：{_amount(layer.get('residual'), currency)}；"
        f"数量：{_cell(layer.get('quantity'))}；收盘价：{accepted_close}；"
        f"收盘ID：{_short_id(layer.get('accepted_close_id'))}；"
        f"事件：{_short_id(layer.get('settlement_event_id'))}；真实成交声明：否"
    )


def _render_dca(lines: list[str], report: Mapping[str, Any]) -> None:
    dca = report.get("dca", {})
    currency = str(dca.get("currency") or report.get("portfolio", {}).get("currency") or "-")
    lines.extend(
        [
            "## 定投四层状态",
            "",
            f"- 计划：{_inline(dca.get('plan_id'))} / v{_inline(dca.get('version'))}",
            f"- 资金模式：{_inline(dca.get('funding_mode'))}",
            "",
            "| 标的 | Configured（已配置） | Proposed（研究建议） | Modeled（模拟入账） | Broker-confirmed（券商确认） |",
            "|---|---|---|---|---|",
        ]
    )
    items = dca.get("items", [])
    if not items:
        lines.append("| - | - | - | - | 未连接，不可用 |")
    for item in items:
        modeled_sessions = item["modeled"]["sessions"]
        if not modeled_sessions:
            lines.append(
                "| {symbol} / - | {configured} | {proposed} | 无本期模拟结算记录；真实成交声明：否 | 未连接，不可用 |".format(
                    symbol=_cell(item["symbol"]),
                    configured=_configured_summary(item["configured"], currency),
                    proposed=_proposed_summary(item["proposed"], currency),
                )
            )
        for modeled_session in modeled_sessions:
            lines.append(
                "| {symbol} / {session} | {configured} | {proposed} | {modeled} | 未连接，不可用 |".format(
                    symbol=_cell(item["symbol"]),
                    session=_cell(modeled_session["session_date"]),
                    configured=_configured_summary(item["configured"], currency),
                    proposed=_proposed_summary(item["proposed"], currency),
                    modeled=_modeled_session_summary(modeled_session, currency),
                )
            )
    lines.extend(
        [
            "",
            "> Broker-confirmed 固定为“未连接，不可用”：系统不连接券商，也不把模拟定投当作真实成交。",
            "",
        ]
    )


def _render_research_v1_0(lines: list[str], report: Mapping[str, Any]) -> None:
    """Preserve byte-stable rendering for already persisted v1.0 outbox rows."""

    research = report["research"]
    lines.extend(
        [
            "## 研究结论",
            "",
            f"- 总体观点：{_inline(research['overall_view'])}",
            f"- 市场状态：{_inline(research['market_regime'])}",
            f"- 风险预算乘数：{_inline(research['risk_budget_multiplier'])}",
            "",
            "### 基金监控",
            "",
            "| 基金 | 状态 | 摘要 | 理由代码 |",
            "|---|---|---|---|",
        ]
    )
    funds = research["fund_monitoring"]
    if not funds:
        lines.append("| - | NOT_DUE | - | - |")
    for fund in funds:
        lines.append(
            f"| {_cell(fund['fund_key'])} | {_cell(fund['status'])} | "
            f"{_cell(fund['summary'])} | {_join_reasons(fund['reason_codes'])} |"
        )
    lines.extend(
        [
            "",
            "### 社交注意力（研究线索）",
            "",
            "| 平台 | 主题 | 方向 | 状态 | 分数 | 用途 | 摘要 |",
            "|---|---|---|---|---:|---|---|",
        ]
    )
    social = research["social_attention"]
    if not social:
        lines.append("| - | - | unknown | not_configured | - | 仅研究 | - |")
    for item in social:
        purpose = "仅研究" if item["research_only"] else "契约允许的研究输入"
        lines.append(
            f"| {_cell(item['platform'])} | {_cell(item['topic'])} | "
            f"{_cell(item['direction'])} | {_cell(item['status'])} | "
            f"{_cell(item['score'])} | {purpose} | {_cell(item['summary'])} |"
        )
    lines.append("")
    if research["notes"]:
        lines.append("### 研究备注")
        lines.append("")
        for note in research["notes"]:
            lines.append(f"- {_inline(note)}")
        lines.append("")


def _render_research(lines: list[str], report: Mapping[str, Any]) -> None:
    if report["schema_version"] == LEGACY_SCHEMA_VERSION:
        _render_research_v1_0(lines, report)
        return
    research = report["research"]
    lines.extend(
        [
            "## 研究结论",
            "",
            f"- 总体观点：{_inline(research['overall_view'])}",
            f"- 市场状态：{_inline(research['market_regime'])}",
            f"- 风险预算乘数：{_inline(research['risk_budget_multiplier'])}",
            "",
            "### 基金监控",
            "",
            "| 基金 | 总状态 | 产品质量 | 组合适配 | 下次复核 | 事件数 | 摘要 | 理由代码 |",
            "|---|---|---|---|---|---:|---|---|",
        ]
    )
    funds = research["fund_monitoring"]
    if not funds:
        lines.append("| - | NOT_CONFIGURED | - | - | - | 0 | - | - |")
    for fund in funds:
        lines.append(
            f"| {_cell(fund['fund_key'])} | {_cell(fund['status'])} | "
            f"{_cell(fund.get('product_quality_status'))} | "
            f"{_cell(fund.get('portfolio_fit_status'))} | "
            f"{_cell(fund.get('next_due'))} | "
            f"{len(fund.get('triggered_event_keys', []))} | "
            f"{_cell(fund['summary'])} | {_join_reasons(fund['reason_codes'])} |"
        )
    lines.extend(
        [
            "",
            "### 社交注意力（研究线索）",
            "",
            "| 平台 | 主题 | 方向 | 状态 | 注意力分数 | 注意力权重 | 候选执行权重 | 校准状态 | 有效执行权重 | 用途 | 摘要 |",
            "|---|---|---|---|---:|---:|---:|---|---:|---|---|",
        ]
    )
    social = research["social_attention"]
    if not social:
        lines.append("| - | - | unknown | not_configured | - | - | - | research_only | 0 | 仅研究 | - |")
    for item in social:
        purpose = "仅研究" if item["research_only"] else "契约允许的研究输入"
        lines.append(
            f"| {_cell(item['platform'])} | {_cell(item['topic'])} | "
            f"{_cell(item['direction'])} | {_cell(item['status'])} | "
            f"{_cell(item['score'])} | {_cell(item.get('attention_weight'))} | "
            f"{_cell(item.get('candidate_execution_weight'))} | "
            f"{_cell(item.get('calibration_state'))} | "
            f"{_cell(item.get('effective_execution_weight'))} | "
            f"{purpose} | {_cell(item['summary'])} |"
        )
    lines.append("")
    social_decision = research.get("social_decision")
    if social_decision is not None:
        lines.extend(
            [
                "### 社交候选分校准",
                "",
                f"- 原始候选贡献：{_inline(social_decision['raw_contribution'])}",
                f"- 校准后有效贡献：{_inline(social_decision['effective_contribution'])}",
                f"- 有效执行覆盖：{_inline(social_decision['effective_execution_coverage'])}",
                f"- 校准状态：{_inline(social_decision['calibration_state'])}",
                "- 交易权限：无；该分数只能用于研究排序。",
                "",
            ]
        )
    calibration = research.get("signal_calibration", [])
    if calibration:
        lines.extend(
            [
                "### 预测信号校准",
                "",
                "| 平台 | 主题 | 模型 | 状态 | 期限 | 样本 | 近期样本 | 理由 |",
                "|---|---|---|---|---:|---:|---:|---|",
            ]
        )
        for item in calibration:
            lines.append(
                f"| {_cell(item['platform'])} | {_cell(item['topic'])} | "
                f"{_cell(item['model_version'])} | {_cell(item['state'])} | "
                f"{item['horizon']} | {item['sample_count']} | "
                f"{item['recent_sample_count']} | {_join_reasons(item['reasons'])} |"
            )
        lines.append("")
    if research["notes"]:
        lines.append("### 研究备注")
        lines.append("")
        for note in research["notes"]:
            lines.append(f"- {_inline(note)}")
        lines.append("")


def _render_source_health(lines: list[str], report: Mapping[str, Any]) -> None:
    source_health = report["source_health"]
    lines.extend(
        [
            "## 数据源健康",
            "",
            "| 来源 | 类型 | 状态 | 必需 | 观测时间 | 详情代码 |",
            "|---|---|---|---|---|---|",
        ]
    )
    if not source_health:
        lines.append("| - | - | 不可用 | - | - | - |")
    for item in source_health:
        lines.append(
            "| {source} | {source_type} | {status} | {required} | {observed} | {detail} |".format(
                source=_cell(item["source_id"]),
                source_type=_cell(item["source_type"]),
                status=_status_label(item.get("status")),
                required="是" if item["required"] else "否",
                observed=_cell(item["observed_at"]),
                detail=_cell(item["detail_code"]),
            )
        )
    lines.append("")


def _render_actions(lines: list[str], report: Mapping[str, Any]) -> None:
    lines.extend(["## 待办与人工确认", ""])
    actions = report["actions"]
    if not actions:
        lines.append("- 无。")
    for item in actions:
        symbol = f" / {_inline(item['symbol'])}" if item["symbol"] else ""
        confirmation = "需人工确认" if item["owner_confirmation_required"] else "仅信息"
        lines.append(
            f"- **{_inline(item['action'])}{symbol}**："
            f"{_inline(item['priority'])} / {_inline(item['status'])} / {confirmation}；"
            f"理由：{_join_reasons(item['rationale_codes'])}；自动执行：否。"
        )

    prompt = report["manual_trade_prompt"]
    if prompt["required"]:
        lines.append(f"- **需要人工回报成交**：{_inline(prompt['prompt'])}")
    else:
        lines.append(
            "- 本次运行没有新增 owner-confirmed 本地事件；这仅描述本地账本，不能据此判断现实账户是否有交易。"
        )
    lines.append("")


def render_private_daily_markdown(report: Mapping[str, Any]) -> str:
    """Validate and deterministically render one private daily report.

    ``validate_private_daily_report`` supplies the normalized deep copy used
    below.  The caller's mapping is never modified or retained.
    """

    validated = validate_private_daily_report(report)
    calendar = validated["calendar"]
    portfolio = validated["portfolio"]
    delivery = validated["delivery"]
    no_new_close = bool(calendar["no_new_close"])

    blocked_reasons: list[Any] = []
    for session in validated.get("session_results", []):
        if session.get("status") == "blocked":
            blocked_reasons.extend(session.get("reason_codes", []))
    overall_status = validated["report_status"]

    lines = [
        f"# 私人美股投资日报 · {_inline(delivery['delivery_date'])}",
        "",
    ]
    if overall_status == "blocked" or blocked_reasons:
        lines.extend(["> **结算受阻**", ">"])
        if blocked_reasons:
            for reason in blocked_reasons:
                lines.append(f"> - {_inline(reason)}")
        else:
            lines.append("> - 日报契约标记为 blocked；没有提供可安全推断的补充原因。")
        lines.append("")
    elif no_new_close:
        lines.extend(
            [
                "> **休市或今日无新收盘。** 最近估值按真实估值交易日 carried-forward 展示；当日损益与当日收益率不可用。",
                "",
            ]
        )

    lines.extend(
        [
            "## 今日状态",
            "",
            f"- 报告状态：**{_status_label(overall_status)}**",
            f"- 报告观察时点：{_inline(calendar['as_of'])}",
            f"- 交易日历：{_inline(calendar['calendar_id'])} / {_inline(calendar['exchange_mic'])}",
            f"- 日历运行模式：{_inline(calendar['mode'])}",
            f"- 最近完成交易日：{_inline(calendar['latest_completed_session'])}",
            f"- 运行前最后已结算交易日：{_inline(calendar['last_settled_session_before_run'])}",
            f"- 待补交易日：{_join_reasons(calendar['unsettled_sessions'])}",
            f"- 真实估值交易日：**{_inline(portfolio['as_of_session'])}**",
            f"- 本次新增交易日：{_inline(calendar['new_sessions_count'])}",
            "",
        ]
    )
    lines.extend(
        [
            "### 交易日历来源",
            "",
            "| 标的 MIC | 日历 | 版本 | 交易所时区 |",
            "|---|---|---|---|",
        ]
    )
    if not calendar["provenance"]:
        lines.append("| - | - | - | - |")
    for provenance in calendar["provenance"]:
        lines.append(
            f"| {_cell(provenance['instrument_mic'])} | "
            f"{_cell(provenance['calendar_name'])} | "
            f"{_cell(provenance['calendar_version'])} | "
            f"{_cell(provenance['exchange_timezone'])} |"
        )
    lines.append("")

    _render_session_results(lines, validated)

    lines.extend(["## 组合账本", ""])
    currency = str(portfolio["currency"])
    _render_book(
        lines,
        title="已确认账本（Confirmed）",
        book=portfolio["confirmed"],
        currency=currency,
        no_new_close=no_new_close,
    )
    _render_book(
        lines,
        title="模拟账本（Modeled）",
        book=portfolio["modeled"],
        currency=currency,
        no_new_close=no_new_close,
    )

    _render_dca(lines, validated)
    _render_research(lines, validated)
    _render_source_health(lines, validated)
    _render_actions(lines, validated)

    lines.extend(
        [
            "---",
            "",
            "本报告仅保存在私人运行层并发送到 GPT；不写入公开仓库、Actions Summary 或公开 artifact。",
            f"Report ID：`{_short_id(validated['report_id'])}` · "
            f"Delivery ID：`{_short_id(delivery['delivery_id'])}` · "
            f"真实估值交易日：`{_inline(portfolio['as_of_session'])}`",
        ]
    )
    return "\n".join(lines).rstrip("\n") + "\n"


__all__ = ["render_private_daily_markdown"]
