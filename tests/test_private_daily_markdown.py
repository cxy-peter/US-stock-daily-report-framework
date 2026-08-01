from __future__ import annotations

import copy

import pytest

from serenity_monitor.private_daily_markdown import render_private_daily_markdown
from serenity_monitor.private_daily_report import (
    PrivateDailyReportIdentityError,
    finalize_private_daily_report,
)


TARGET_HASH = "9" * 64


def _performance(*, session: str | None = "2026-07-31", daily: bool = False) -> dict:
    return {
        "valuation_session": session,
        "prior_nav": "100" if daily else None,
        "prior_cumulative_twr": "0.02" if daily else None,
        "net_external_flow": "0",
        "weighted_external_flow": "0",
        "daily_pnl": "1" if daily else None,
        "daily_return": "0.01" if daily else None,
        "cumulative_twr": "0.0302" if daily else None,
    }


def _book(*, session: str | None = "2026-07-31", daily: bool = False) -> dict:
    return {
        "valuation_status": "fresh" if daily else "carried_forward_display_only",
        "cash": "0",
        "nav": "0",
        "market_value": "0",
        "total_economic_cost": "0",
        "realized_pnl": "0",
        "fees": "0",
        "performance": _performance(session=session, daily=daily),
        "positions": [],
    }


def _draft() -> dict:
    return {
        "classification": "synthetic_example",
        "simulation": True,
        "report_status": "no_new_close",
        "prepared_at": "2026-08-01T05:15:00Z",
        "delivery": {
            "delivery_date": "2026-08-01",
            "timezone": "Asia/Shanghai",
            "channel": "codex",
        },
        "calendar": {
            "calendar_id": "XNYS",
            "exchange_mic": "XNAS",
            "exchange_timezone": "America/New_York",
            "report_timezone": "Asia/Shanghai",
            "as_of": "2026-08-01T05:15:00Z",
            "mode": "none",
            "latest_completed_session": "2026-07-31",
            "last_settled_session_before_run": "2026-07-31",
            "unsettled_sessions": [],
            "provenance": [
                {
                    "instrument_mic": "XNAS",
                    "calendar_name": "XNYS",
                    "calendar_version": "4.13.2",
                    "exchange_timezone": "America/New_York",
                }
            ],
            "new_sessions_count": 0,
            "no_new_close": True,
        },
        "session_results": [],
        "portfolio": {
            "currency": "USD",
            "as_of_session": "2026-07-31",
            "ledger_last_event_hash": "0" * 64,
            "confirmed": _book(),
            "modeled": _book(),
        },
        "dca": {
            "plan_id": "demo-plan",
            "version": "v1",
            "currency": "USD",
            "funding_mode": "modeled_external_contribution",
            "items": [
                {
                    "symbol": "DEMO.EQ",
                    "configured": {"amount": "10"},
                    "proposed": {
                        "amount": "25",
                        "action": "increase_review",
                        "rationale_codes": ["research_only"],
                        "automatic_execution": False,
                    },
                    "modeled": {
                        "execution_claim": False,
                        "sessions": [],
                    },
                    "broker_confirmed": {
                        "availability": "unavailable",
                        "status": "not_connected",
                        "amount": None,
                        "quantity": None,
                        "price": None,
                        "trade_id": None,
                    },
                }
            ],
        },
        "research": {
            "overall_view": "Synthetic.",
            "market_regime": "unknown",
            "risk_budget_multiplier": "0",
            "fund_monitoring": [],
            "social_attention": [],
            "notes": [],
        },
        "source_health": [],
        "actions": [],
        "manual_trade_prompt": {
            "required": False,
            "prompt": None,
            "accepted_response_kinds": ["no_manual_trade"],
            "default_if_no_response": "no_new_owner_confirmed_event",
            "broker_execution_available": False,
        },
        "privacy": {
            "contains_private_portfolio_data": False,
            "contains_target_identifier": False,
            "github_persistence_allowed": False,
            "public_artifact_allowed": False,
            "gpt_owner_delivery_only": True,
            "redaction_status": "synthetic_only",
            "warnings": [],
        },
    }


def _report(draft: dict | None = None) -> dict:
    return finalize_private_daily_report(
        draft or _draft(), target_key_sha256=TARGET_HASH
    )


def _set_complete(draft: dict) -> None:
    draft["report_status"] = "complete"
    draft["calendar"].update(
        {
            "mode": "backfill",
            "last_settled_session_before_run": "2026-07-29",
            "unsettled_sessions": ["2026-07-30", "2026-07-31"],
            "new_sessions_count": 2,
            "no_new_close": False,
        }
    )
    draft["session_results"] = [
        {
            "session_date": "2026-07-30",
            "status": "settled",
            "is_backfill": True,
            "close_batch_id": "b" * 64,
            "ledger_batch_id": "ledger-0730",
            "calendar_gate": "passed",
            "price_gate": "passed",
            "corporate_action_gate": "passed",
            "funding_gate": "passed",
            "dca_status": "settled",
            "confirmed_valuation_status": "fresh",
            "modeled_valuation_status": "fresh",
            "confirmed_valuation_id": "confirmed-0730",
            "modeled_valuation_id": "modeled-0730",
            "reason_codes": [],
        },
        {
            "session_date": "2026-07-31",
            "status": "settled",
            "is_backfill": False,
            "close_batch_id": "d" * 64,
            "ledger_batch_id": "ledger-0731",
            "calendar_gate": "passed",
            "price_gate": "passed",
            "corporate_action_gate": "passed",
            "funding_gate": "passed",
            "dca_status": "settled",
            "confirmed_valuation_status": "fresh",
            "modeled_valuation_status": "fresh",
            "confirmed_valuation_id": "confirmed-0731",
            "modeled_valuation_id": "modeled-0731",
            "reason_codes": [],
        },
    ]
    draft["portfolio"]["confirmed"]["performance"] = _performance(daily=False)
    draft["portfolio"]["modeled"]["performance"] = _performance(daily=False)
    draft["portfolio"]["confirmed"]["valuation_status"] = "fresh"
    draft["portfolio"]["modeled"]["valuation_status"] = "fresh"
    draft["dca"]["items"][0]["modeled"]["sessions"] = [
        {
            "session_date": session,
            "status": "settled",
            "amount": "10",
            "spend": "10",
            "residual": "0",
            "quantity": "0.1",
            "accepted_close": "100",
            "accepted_close_id": close_id * 64,
            "settlement_event_id": f"event-{session}",
        }
        for session, close_id in (("2026-07-30", "c"), ("2026-07-31", "e"))
    ]


def test_render_is_byte_deterministic_uses_lf_and_does_not_modify_input() -> None:
    report = _report()
    original = copy.deepcopy(report)

    first = render_private_daily_markdown(report)
    second = render_private_daily_markdown(report)

    assert first.encode("utf-8") == second.encode("utf-8")
    assert "\r" not in first
    assert first.endswith("\n")
    assert not first.endswith("\n\n")
    assert report == original
    assert "没有新增 owner-confirmed 本地事件" in first
    assert "不能据此判断现实账户是否有交易" in first


def test_renderer_uses_validated_json_and_rejects_changed_content_identity() -> None:
    report = _report()
    report["research"]["overall_view"] = "changed after finalization"

    with pytest.raises(PrivateDailyReportIdentityError):
        render_private_daily_markdown(report)


def test_escapes_markdown_and_redacts_private_paths_and_credentials() -> None:
    draft = _draft()
    escape = chr(92)
    windows_path = "C:" + escape + "private" + escape + "portfolio.private.yaml"
    posix_path = "/" + "root/private/report.json"
    draft["research"]["overall_view"] = (
        "含 | 管道\n第二行 `代码`；token=supersecret；"
        f"文件 {windows_path}"
    )
    draft["research"]["notes"] = ["api_key:anothersecret"]
    draft["research"]["notes"].append(f"缓存 {posix_path}")

    rendered = render_private_daily_markdown(_report(draft))

    assert escape + "|" in rendered
    assert "第二行" in rendered and "<br>" in rendered
    assert escape + "`代码" + escape + "`" in rendered
    assert "supersecret" not in rendered
    assert "anothersecret" not in rendered
    assert windows_path not in rendered
    assert posix_path not in rendered
    assert "已隐藏凭据" in rendered
    assert "已隐藏私有路径" in rendered


def test_untrusted_research_text_cannot_render_active_markdown_or_html() -> None:
    draft = _draft()
    draft["research"]["social_attention"] = [
        {
            "platform": "reddit",
            "topic": "tracking-test",
            "direction": "unknown",
            "status": "degraded",
            "score": None,
            "research_only": True,
            "summary": (
                "![track](https://evil.invalid/pixel?tag=private) "
                '<img src="https://evil.invalid/html-pixel">'
            ),
        }
    ]

    rendered = render_private_daily_markdown(_report(draft))

    assert "![track](" not in rendered
    assert '<img src="' not in rendered
    assert r"\!\[track\]\(" in rendered
    assert "&lt;img src=" in rendered


def test_no_new_close_marks_carried_forward_and_daily_result_unavailable() -> None:
    rendered = render_private_daily_markdown(_report())

    assert "今日无新收盘" in rendered
    assert "休市" in rendered
    assert "carried-forward" in rendered
    assert rendered.count("沿用最近估值（仅展示）") == 2
    assert "真实估值交易日：**2026-07-31**" in rendered
    assert rendered.count("不可用（无新收盘）") == 4
    assert "| - | 无新交易日 |" in rendered


def test_blocked_reasons_are_at_top_before_regular_sections() -> None:
    draft = _draft()
    draft["report_status"] = "blocked"
    draft["calendar"].update(
        {
            "mode": "single",
            "last_settled_session_before_run": "2026-07-30",
            "unsettled_sessions": ["2026-07-31"],
            "new_sessions_count": 1,
            "no_new_close": False,
        }
    )
    draft["session_results"] = [
        {
            "session_date": "2026-07-31",
            "status": "blocked",
            "is_backfill": False,
            "close_batch_id": None,
            "ledger_batch_id": None,
            "calendar_gate": "passed",
            "price_gate": "blocked",
            "corporate_action_gate": "not_attempted",
            "funding_gate": "not_attempted",
            "dca_status": "blocked",
            "confirmed_valuation_status": "unavailable",
            "modeled_valuation_status": "unavailable",
            "confirmed_valuation_id": None,
            "modeled_valuation_id": None,
            "reason_codes": ["accepted_close | missing"],
        }
    ]
    draft["portfolio"]["as_of_session"] = None
    unavailable_position = {
        "symbol": "DEMO.EQ",
        "quantity": "1",
        "modeled_quantity": "0",
        "accepted_close": None,
        "accepted_close_id": None,
        "selected_provider_id": None,
        "price_session": None,
        "market_value": None,
        "economic_cost": "9",
        "average_economic_cost": "9",
        "unrealized_pnl": None,
        "portfolio_weight": None,
    }
    for book_name in ("confirmed", "modeled"):
        draft["portfolio"][book_name].update(
            {
                "valuation_status": "unavailable",
                "cash": "100",
                "nav": None,
                "market_value": None,
                "total_economic_cost": "9",
                "performance": _performance(session=None),
                "positions": [copy.deepcopy(unavailable_position)],
            }
        )
    draft["portfolio"]["modeled"].update(
        {
            "total_economic_cost": "11",
            "positions": [
                {
                    **copy.deepcopy(unavailable_position),
                    "quantity": "1.1",
                    "modeled_quantity": "0.1",
                    "economic_cost": "11",
                    "average_economic_cost": "10",
                }
            ],
        }
    )
    draft["dca"]["items"][0]["modeled"]["sessions"] = [
        {
            "session_date": "2026-07-31",
            "status": "blocked",
            "amount": "0",
            "spend": "0",
            "residual": "0",
            "quantity": "0",
            "accepted_close": None,
            "accepted_close_id": None,
            "settlement_event_id": None,
        }
    ]

    rendered = render_private_daily_markdown(_report(draft))

    assert rendered.index("结算受阻") < rendered.index("## 今日状态")
    assert f"> - accepted_close {chr(92)}| missing" in rendered
    assert "| 2026-07-31 | 受阻 |" in rendered
    assert rendered.count("估值状态：**不可用**") == 2
    assert rendered.count("| 100 USD | 不可用 | 不可用 |") == 2


def test_sessions_and_positions_follow_validated_json_order() -> None:
    draft = _draft()
    _set_complete(draft)
    positions = [
        {
            "symbol": "AAA",
            "quantity": "1",
            "modeled_quantity": "0",
            "accepted_close": "10",
            "accepted_close_id": "a" * 64,
            "selected_provider_id": "twelve_data",
            "price_session": "2026-07-31",
            "market_value": "10",
            "economic_cost": "8",
            "average_economic_cost": "8",
            "unrealized_pnl": "2",
            "portfolio_weight": "0.2",
        },
        {
            "symbol": "ZZZ",
            "quantity": "2",
            "modeled_quantity": "0",
            "accepted_close": "20",
            "accepted_close_id": "f" * 64,
            "selected_provider_id": "alpha_vantage",
            "price_session": "2026-07-31",
            "market_value": "40",
            "economic_cost": "30",
            "average_economic_cost": "15",
            "unrealized_pnl": "10",
            "portfolio_weight": "0.8",
        },
    ]
    for book_name in ("confirmed", "modeled"):
        draft["portfolio"][book_name].update(
            {
                "cash": "0",
                "nav": "50",
                "market_value": "50",
                "total_economic_cost": "38",
                "positions": copy.deepcopy(positions),
            }
        )
    rendered = render_private_daily_markdown(_report(draft))

    session_table = rendered.split("## 交易日结算", 1)[1].split("## 组合账本", 1)[0]
    assert session_table.index("2026-07-30") < session_table.index("2026-07-31")
    assert "日历门" in session_table and "价格门" in session_table
    assert "Confirmed 估值" in session_table and "Modeled 估值" in session_table
    confirmed = rendered.split("### 已确认账本（Confirmed）", 1)[1].split(
        "### 模拟账本（Modeled）", 1
    )[0]
    assert confirmed.index("| AAA |") < confirmed.index("| ZZZ |")
    assert "其中模拟数量" in rendered
    modeled = rendered.split("### 模拟账本（Modeled）", 1)[1].split(
        "## 定投四层状态", 1
    )[0]
    assert "| AAA | 1 | 0 |" in modeled


def test_dca_displays_four_distinct_layers_and_never_claims_broker_execution() -> None:
    draft = _draft()
    _set_complete(draft)
    rendered = render_private_daily_markdown(_report(draft))

    assert "Configured（已配置）" in rendered
    assert "Proposed（研究建议）" in rendered
    assert "Modeled（模拟入账）" in rendered
    assert "Broker-confirmed（券商确认）" in rendered
    assert "基础金额：10 USD" in rendered
    assert "金额：25 USD；动作：increase_review" in rendered
    assert "DEMO.EQ / 2026-07-30" in rendered
    assert "DEMO.EQ / 2026-07-31" in rendered
    assert "状态：settled；金额：10 USD" in rendered
    assert "收盘ID：" in rendered
    assert "真实成交声明：否" in rendered
    assert "未连接，不可用" in rendered


def test_actions_manual_prompt_sources_and_research_have_fixed_sections() -> None:
    draft = _draft()
    draft["research"]["fund_monitoring"] = [
        {
            "fund_key": "FUND_A",
            "status": "WATCH",
            "summary": "风格需要复核",
            "reason_codes": ["style_drift"],
        }
    ]
    draft["research"]["social_attention"] = [
        {
            "platform": "reddit",
            "topic": "semiconductor",
            "direction": "mixed",
            "status": "degraded",
            "score": "0.2",
            "research_only": True,
            "summary": "仅作注意力线索",
        }
    ]
    draft["source_health"] = [
        {
            "source_id": "accepted-close",
            "source_type": "accepted_close",
            "status": "healthy",
            "required": True,
            "observed_at": "2026-08-01T04:00:00Z",
            "detail_code": "dual_source_ok",
        }
    ]
    draft["actions"] = [
        {
            "action_id": "review-demo",
            "scope": "position",
            "symbol": "DEMO.EQ",
            "action": "REVIEW",
            "priority": "normal",
            "status": "proposed",
            "owner_confirmation_required": True,
            "automatic_execution": False,
            "rationale_codes": ["valuation_review"],
        }
    ]
    draft["manual_trade_prompt"] = {
        "required": True,
        "prompt": "请回报实际成交；未回报则按无手工交易处理。",
        "accepted_response_kinds": ["confirmed_fill", "no_manual_trade"],
        "default_if_no_response": "no_new_owner_confirmed_event",
        "broker_execution_available": False,
    }

    rendered = render_private_daily_markdown(_report(draft))

    headings = [
        "## 今日状态",
        "## 交易日结算",
        "## 组合账本",
        "## 定投四层状态",
        "## 研究结论",
        "## 数据源健康",
        "## 待办与人工确认",
    ]
    assert [rendered.index(heading) for heading in headings] == sorted(
        rendered.index(heading) for heading in headings
    )
    assert "FUND_A" in rendered and "reddit" in rendered
    assert "accepted-close" in rendered and "dual_source_ok" in rendered
    assert "需要人工回报成交" in rendered


def test_footer_uses_short_ids_and_real_valuation_session() -> None:
    report = _report()
    rendered = render_private_daily_markdown(report)

    assert f"Report ID：`{report['report_id'][:12]}`" in rendered
    assert f"Delivery ID：`{report['delivery']['delivery_id'][:12]}`" in rendered
    assert "真实估值交易日：`2026-07-31`" in rendered
    assert report["report_id"] not in rendered
    assert report["delivery"]["delivery_id"] not in rendered
