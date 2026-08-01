from __future__ import annotations

import pandas as pd

from serenity_monitor.evidence import EvidenceAssessment
from serenity_monitor.indicators import Indicators
from serenity_monitor.regime import MarketRegime
from serenity_monitor.rules import ResearchAction, ResearchSettings, evaluate_holding


def indicators(**overrides):
    data = dict(
        price=75.0,
        drawdown_from_peak=-0.30,
        peak_price=100.0,
        ret_1w=-0.02,
        ret_1m=-0.10,
        ret_3m=-0.12,
        ann_vol_30d=0.30,
        volume_ratio=1.0,
        last_day_change=-0.01,
        max_1d_drop_1m=-0.05,
        avg_dollar_vol_20d=20_000_000.0,
        ma50=80.0,
        ma200=70.0,
        rsi14=40.0,
    )
    data.update(overrides)
    return Indicators(**data)


def assessment(stance="neutral", coverage=0.8, score=50.0):
    return EvidenceAssessment(
        ticker="TEST", stance=stance, risk_score=score, coverage=coverage,
        item_count=3, reasons=["test evidence"], item_ids=["a"],
        primary_source_present=True,
        independent_groups=2,
        can_support_add=stance in {"neutral", "supportive"} and coverage >= 0.6,
    )


def holding(**overrides):
    row = {
        "ticker": "TEST",
        "name": "Test Corp",
        "framework": "serenity_stock",
        "thesis_checks": {
            "chokepoint_intact": True,
            "tam_still_expanding": True,
        },
    }
    row.update(overrides)
    return row


def test_drawdown_add_requires_evidence():
    rec = evaluate_holding(
        holding(), indicators(), assessment("insufficient", 0.1),
        MarketRegime("neutral", 0.85, 0, ()), ResearchSettings(),
        market_cap=1e9, asset_type="stock", evidence_min_coverage=0.6,
    )
    assert rec.action == ResearchAction.HOLD
    assert "证据" in " ".join(rec.reasons)


def test_drawdown_add_when_evidence_is_adequate():
    rec = evaluate_holding(
        holding(), indicators(), assessment("neutral", 0.8),
        MarketRegime("neutral", 0.85, 0, ()), ResearchSettings(),
        market_cap=1e9, asset_type="stock", evidence_min_coverage=0.6,
    )
    assert rec.action == ResearchAction.ADD


def test_external_thesis_risk_triggers_review_not_exit():
    rec = evaluate_holding(
        holding(), indicators(drawdown_from_peak=-0.05), assessment("thesis_risk", 0.9, 85),
        MarketRegime("neutral", 0.85, 0, ()), ResearchSettings(),
        market_cap=1e9, asset_type="stock",
    )
    assert rec.action == ResearchAction.REVIEW


def test_manual_thesis_break_is_exit():
    rec = evaluate_holding(
        holding(thesis_checks={"chokepoint_intact": False}),
        indicators(drawdown_from_peak=-0.05), assessment(),
        MarketRegime("neutral", 0.85, 0, ()), ResearchSettings(),
        market_cap=1e9, asset_type="stock",
    )
    assert rec.action == ResearchAction.EXIT
