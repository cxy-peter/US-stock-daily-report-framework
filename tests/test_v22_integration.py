from __future__ import annotations

from pathlib import Path

import yaml

from serenity_monitor.data import MockProvider, snapshot_fallback_quote
from serenity_monitor.evidence import EvidenceSettings, assess_view
from serenity_monitor.external_views import (
    ExternalItem,
    ExternalSettings,
    ExternalView,
    collect_external_views,
)
from serenity_monitor.indicators import Indicators
from serenity_monitor.regime import MarketRegime
from serenity_monitor.rules import (
    ResearchAction,
    ResearchSettings,
    evaluate_holding,
)


ROOT = Path(__file__).resolve().parents[1]


def load_example_config() -> dict:
    return yaml.safe_load(
        (ROOT / "config" / "portfolio.example.yaml").read_text(encoding="utf-8")
    )


def test_public_portfolio_fixture_is_explicitly_synthetic():
    config = load_example_config()
    assert config["runtime"] == {
        "data_classification": "synthetic_example",
        "allow_live_report": False,
        "example_only": True,
    }
    assert set(row["ticker"] for row in config["holdings"]) == {
        "DEMO_EQ",
        "DEMO_BOND",
        "DEMO_CASH",
    }
    assert "broker_snapshot" not in config
    assert all("entry_price" not in row for row in config["holdings"])


def test_mock_asset_type_uses_explicit_hint_without_portfolio_fingerprint():
    provider = MockProvider()
    assert provider.get("VTI", asset_type_hint="etf").asset_type == "etf"
    assert provider.get("TEST", asset_type_hint="stock").asset_type == "stock"


def test_manual_social_opinion_is_context_only_and_fully_scored(tmp_path):
    profiles_path = tmp_path / "profiles.private.yaml"
    profiles_path.write_text(
        """profiles:
  example_researcher:
    label: Authorized example researcher
    source_type: independent_kol
    independence_group: example_researcher
    identity_verified: false
    regulated_entity: false
    audited_performance: false
    position_disclosure: unknown
    conflict_disclosure: unknown
    leverage_disclosure: unknown
    track_record: {observations: 0, hits: 0}
""",
        encoding="utf-8",
    )
    views_path = tmp_path / "views.private.yaml"
    views_path.write_text(
        """version: 1
items:
  - id: synthetic-research-note
    platform: authorized-export
    source_label: Synthetic research note
    source_id: example_researcher
    author: Example Researcher
    published: "2026-01-01"
    source_reference: synthetic fixture
    tickers: [VTI]
    title: Broad-market allocation hypothesis
    text: This is a synthetic test hypothesis and not investment advice.
    direction: neutral
    horizon_days: 20
    invalidation_condition: Fails out-of-sample validation.
    primary_evidence_count: 0
    position_disclosed: unknown
    conflict_disclosed: unknown
    sponsored: false
    engagement: 10
""",
        encoding="utf-8",
    )
    settings = ExternalSettings.from_dict(
        {
            "news": {"enabled": False},
            "stocktwits": {"enabled": False},
            "reddit": {"enabled": False},
            "x": {"enabled": False, "discovery_enabled": False},
            "sec": {"enabled": False},
            "public_web": {"enabled": False},
            "manual_kol": {"enabled": True, "path": str(views_path)},
            "source_profiles_path": str(profiles_path),
        }
    )
    bundle = collect_external_views(
        [{"ticker": "VTI", "name": "Synthetic Broad Market ETF"}],
        [],
        settings,
        network_enabled=False,
    )
    items = bundle.view("VTI").items
    assert len(items) == 1
    item = items[0]
    assert item.independence_group == "example_researcher"
    assert item.research_weight >= 0
    assert not item.copy_trade_allowed
    evidence = assess_view("VTI", bundle.view("VTI"), EvidenceSettings())
    assert evidence.independent_groups <= 1
    assert not evidence.primary_source_present
    assert not evidence.can_support_add


def test_synthetic_recurring_plan_is_review_only():
    recurring = load_example_config()["recurring_investments"]
    assert recurring["execution_mode"] == "external_broker_plan"
    assert recurring["base_amount_usd_per_ticker"] == 10
    assert recurring["tickers"] == ["DEMO_EQ", "DEMO_BOND"]


def test_failed_live_quote_can_use_explicit_stale_snapshot_fallback():
    quote = snapshot_fallback_quote(
        {
            "ticker": "TEST",
            "shares": 10,
            "entry_price": 100,
            "broker_pnl_usd": -100,
            "asset_type": "stock",
        },
        "2026-01-02",
    )
    assert quote is not None
    assert quote.source == "broker_snapshot_fallback"
    assert quote.as_of == "2026-01-02"
    assert quote.asset_type == "stock"
    assert quote.price == 90


def test_single_social_post_cannot_trigger_add():
    view = ExternalView(
        ticker="TEST",
        items=[
            ExternalItem(
                item_id="example-1",
                source="Authorized example",
                source_kind="kol",
                title="Synthetic demand hypothesis",
                ticker="TEST",
                source_id="example_researcher",
                source_score=55,
                claim_score=70,
                research_weight=0.30,
                independence_group="example_researcher",
                can_inform_research=True,
                copy_trade_allowed=False,
            )
        ],
    )
    evidence = assess_view("TEST", view, EvidenceSettings())
    assert not evidence.can_support_add
    recommendation = evaluate_holding(
        {
            "ticker": "TEST",
            "name": "Synthetic Test Company",
            "framework": "serenity_stock",
            "thesis_checks": {"chokepoint_intact": True},
        },
        Indicators(
            price=70,
            drawdown_from_peak=-0.30,
            peak_price=100,
            ret_1w=-0.02,
            ret_1m=-0.10,
            ret_3m=-0.15,
            ann_vol_30d=0.35,
            volume_ratio=1,
            last_day_change=-0.01,
            max_1d_drop_1m=-0.05,
            avg_dollar_vol_20d=100_000_000,
            ma50=75,
            ma200=80,
            rsi14=40,
        ),
        evidence,
        MarketRegime("neutral", 0.85, 0, ()),
        ResearchSettings(),
        market_cap=100e9,
        asset_type="stock",
    )
    assert recommendation.action == ResearchAction.HOLD
