from __future__ import annotations

import datetime as dt
import json

import pytest

from serenity_monitor.political_collectors import (
    RssMediaCollector,
    WhiteHouseCollector,
    XOfficialCollector,
)
from serenity_monitor.political_communications import build_political_brief
from serenity_monitor.polymarket_live import (
    BookLevel,
    LiveMarketSnapshot,
    PolymarketPublicClient,
    PricePoint,
    aggregate_live_markets,
    score_live_market,
)


NOW = dt.datetime(2026, 8, 2, 13, 0, tzinfo=dt.timezone.utc)


ACTORS = [
    {
        "actor_id": "donald_trump",
        "display_name": "Donald J. Trump",
        "role": "President",
        "institution": "White House",
        "actor_type": "president",
        "base_weight": 1.0,
        "policy_authority": 1.0,
        "monitored_topics": ["trade_tariff", "ai_semiconductor", "monetary_rates"],
        "holdings_tags": ["semiconductors", "broad_market"],
    },
    {
        "actor_id": "press_secretary",
        "display_name": "Press Secretary",
        "role": "White House Press Secretary",
        "institution": "White House",
        "actor_type": "spokesperson",
        "base_weight": 0.55,
        "policy_authority": 0.45,
        "monitored_topics": ["trade_tariff", "ai_semiconductor"],
    },
]


def test_policy_brief_extracts_complete_claims_not_mention_counts():
    documents = [
        {
            "document_id": "speech-1",
            "actor_id": "donald_trump",
            "observed_at": (NOW - dt.timedelta(hours=4)).isoformat(),
            "source_type": "official_speech",
            "title": "Technology speech",
            "body": (
                "AI AI AI AI. "
                "Beginning September 1, the Department of Commerce will review a 20 percent tariff on advanced semiconductor imports, and the administration will negotiate exemptions for companies that expand United States production. "
                "America will remain strong."
            ),
            "source_url": "https://www.whitehouse.gov/example",
        }
    ]
    result = build_political_brief(ACTORS, documents, as_of=NOW)
    assert result.accepted_claim_count == 1
    claim = result.top_claims[0]
    assert claim.actor_id == "donald_trump"
    assert claim.topic == "trade_tariff"
    assert "September 1" in claim.evidence_sentence
    assert "20 percent" in claim.evidence_sentence
    assert claim.specificity > 0.5
    assert claim.stage in {"directed", "announced_intent"}
    assert result.decision_score_contribution <= 0.08
    assert not result.automatic_trading_permitted


def test_actor_authority_and_media_disagreement_affect_importance():
    common_sentence = (
        "The administration will propose new export controls for advanced AI chips this quarter."
    )
    documents = [
        {
            "document_id": "trump",
            "actor_id": "donald_trump",
            "observed_at": (NOW - dt.timedelta(hours=3)).isoformat(),
            "source_type": "official_interview",
            "title": "Interview",
            "body": common_sentence,
        },
        {
            "document_id": "press",
            "actor_id": "press_secretary",
            "observed_at": (NOW - dt.timedelta(hours=2)).isoformat(),
            "source_type": "official_press_briefing",
            "title": "Briefing",
            "body": common_sentence.replace("this quarter", "later this year"),
        },
    ]
    media = [
        {
            "assessment_id": "m1",
            "observed_at": NOW.isoformat(),
            "outlet": "Outlet A",
            "outlet_weight": 0.8,
            "target_actor_id": "donald_trump",
            "target_topic": "ai_semiconductor",
            "stance": -0.8,
            "uncertainty": 0.2,
            "summary": "The proposal could restrict chip-sector revenue.",
        },
        {
            "assessment_id": "m2",
            "observed_at": NOW.isoformat(),
            "outlet": "Outlet B",
            "outlet_weight": 0.8,
            "target_actor_id": "donald_trump",
            "target_topic": "ai_semiconductor",
            "stance": 0.8,
            "uncertainty": 0.2,
            "summary": "Domestic capacity may benefit.",
        },
    ]
    result = build_political_brief(ACTORS, documents, media_assessments=media, as_of=NOW)
    claims = {claim.actor_id: claim for claim in result.top_claims}
    assert claims["donald_trump"].importance > claims["press_secretary"].importance
    assert claims["donald_trump"].media_disagreement is not None
    assert claims["donald_trump"].media_disagreement > 0.5


class _Response:
    def __init__(self, text="", payload=None):
        self.text = text
        self.content = text.encode("utf-8") if payload is None else json.dumps(payload).encode()
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _QueueSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_white_house_collector_reads_public_listing_and_article():
    listing = (
        '<html><body><a href="/presidential-actions/2026/08/example-order/">Order</a></body></html>'
    )
    article = (
        '<html><head><meta property="og:title" content="Example Order">'
        '<meta property="article:published_time" content="2026-08-01T12:00:00Z"></head>'
        '<body><article><p>The President signed an executive order directing the Department of Commerce to review semiconductor imports.</p></article></body></html>'
    )
    session = _QueueSession([_Response(listing), _Response(article)])
    collector = WhiteHouseCollector(session=session, clock=lambda: NOW)
    result = collector.collect(listing_urls=["https://www.whitehouse.gov/presidential-actions/"], since=NOW - dt.timedelta(days=3))
    assert len(result.documents) == 1
    assert result.documents[0].source_type == "signed_official_action"
    assert result.source_health[0].status == "healthy"


def test_x_collector_redacts_token_and_returns_official_posts():
    session = _QueueSession(
        [
            _Response(payload={"data": {"id": "42"}}),
            _Response(
                payload={
                    "data": [
                        {
                            "id": "9001",
                            "created_at": "2026-08-02T10:00:00Z",
                            "text": "We will publish an AI export framework next week.",
                            "public_metrics": {"like_count": 10, "retweet_count": 3, "reply_count": 2, "quote_count": 1},
                        }
                    ]
                }
            ),
        ]
    )
    collector = XOfficialCollector(bearer_token="super-secret", session=session, clock=lambda: NOW)
    result = collector.collect({"donald_trump": "POTUS"}, start_time=NOW - dt.timedelta(days=1))
    assert "super-secret" not in repr(collector)
    assert len(result.documents) == 1
    assert result.documents[0].source_type == "official_x"
    assert result.documents[0].engagement == 16


def test_rss_media_collector_preserves_media_as_separate_source():
    rss = """<rss><channel><item><title>Trump outlines tariff timeline</title>
    <description>Analysts said the proposal could raise uncertainty.</description>
    <link>https://example.com/story</link><pubDate>Sat, 02 Aug 2026 10:00:00 GMT</pubDate>
    </item></channel></rss>"""
    collector = RssMediaCollector(session=_QueueSession([_Response(rss)]), clock=lambda: NOW)
    result = collector.collect(
        [{"url": "https://example.com/feed.xml", "source_id": "example", "actor_id": "media", "outlet": "Example", "source_type": "media_analysis"}],
        since=NOW - dt.timedelta(days=1),
    )
    assert len(result.documents) == 1
    assert result.documents[0].source_type == "media_analysis"
    assert not result.documents[0].direct_quote


def _live_snapshot(spread: float = 0.02, liquidity: float = 2_000_000) -> LiveMarketSnapshot:
    history = tuple(
        PricePoint(NOW - dt.timedelta(hours=hours), probability)
        for hours, probability in [(168, 0.45), (24, 0.52), (6, 0.58), (1, 0.60)]
    )
    return LiveMarketSnapshot(
        market_id="market-1",
        question="Will the tariff proposal take effect before October?",
        slug="tariff-before-october",
        token_id="yes-token",
        topic="trade_tariff",
        resolution_source="official government action",
        observed_at=NOW,
        end_date=NOW + dt.timedelta(days=30),
        probability=0.62,
        spread=spread,
        liquidity=liquidity,
        volume=3_000_000,
        open_interest=500_000,
        bids=(BookLevel(0.61, 100_000), BookLevel(0.60, 80_000)),
        asks=(BookLevel(0.63, 90_000), BookLevel(0.64, 70_000)),
        price_history=history,
        asset_sensitivities={"SPY": -0.4, "SMH": -0.8},
    )


def test_live_polymarket_uses_velocity_liquidity_and_spread():
    good = score_live_market(_live_snapshot(), calibration_multiplier=0.8)
    poor = score_live_market(_live_snapshot(spread=0.18, liquidity=100), calibration_multiplier=0.2)
    assert good.change_24h == pytest.approx(0.10)
    assert good.reliability > poor.reliability
    assert good.status == "active"
    assert poor.status in {"research_only", "blocked"}
    result = aggregate_live_markets([good, poor])
    assert -0.03 <= result.decision_score_contribution <= 0.03
    assert result.risk_budget_multiplier <= 1.01
    assert not result.automatic_trading_permitted


def test_polymarket_public_client_has_no_order_methods_and_parses_public_data():
    markets = [
        {
            "id": "1",
            "question": "Example",
            "slug": "example",
            "clobTokenIds": '["yes", "no"]',
            "outcomePrices": '["0.55", "0.45"]',
            "liquidity": "1000",
            "volume": "5000",
            "endDate": "2026-09-01T00:00:00Z",
        }
    ]
    session = _QueueSession([_Response(payload=markets)])
    client = PolymarketPublicClient(session=session)
    assert client.list_markets(limit=1)[0]["id"] == "1"
    method_names = {name for name in dir(client) if not name.startswith("_")}
    assert not ({"place_order", "create_order", "cancel_order", "post_order"} & method_names)
