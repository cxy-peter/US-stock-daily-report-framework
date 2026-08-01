from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import run_report
from run_report import (
    _assert_no_mock_lineage,
    _disabled_xhs_result,
    _objective_quote_age_error,
    _provider,
    _public_xhs_artifact,
    _validate_provider_mode,
    _xhs_topic_rules,
)
from serenity_monitor.china_retail_attention import RecordSignal


class QuoteDate:
    def __init__(self, as_of: str):
        self.as_of = as_of


def test_future_and_stale_objective_quotes_are_rejected():
    report_date = date(2026, 8, 1)
    assert "future-dated" in _objective_quote_age_error(
        QuoteDate("2026-08-02"), report_date, 7
    )
    assert "stale" in _objective_quote_age_error(
        QuoteDate("2026-07-20"), report_date, 7
    )
    assert _objective_quote_age_error(QuoteDate("2026-07-31"), report_date, 7) == ""


def test_disabled_xhs_path_has_no_signal_or_weight():
    result = _disabled_xhs_result()
    assert result.status == "disabled"
    assert result.input_count == 0
    assert result.execution_weight == 0
    assert not result.can_trigger_trade


def test_private_xhs_topic_rules_are_wired_into_run_report():
    rules = _xhs_topic_rules({
        "topic_rules": [
            {
                "topic": "private_theme",
                "keywords": ["private keyword"],
                "sector": "Private research sector",
                "etfs": ["demo_etf"],
                "tickers": ["demo_stock"],
                "base_confidence": 0.7,
            }
        ]
    })
    assert len(rules) == 1
    assert rules[0].etfs == ("DEMO_ETF",)
    assert rules[0].tickers == ("DEMO_STOCK",)


def test_invalid_private_xhs_topic_rules_fail_closed():
    with pytest.raises(ValueError, match="must be a list"):
        _xhs_topic_rules({"topic_rules": "not-a-list"})


def test_public_xhs_artifact_drops_record_level_linkage_fields():
    record = RecordSignal(
        record_id="stable-record-hash",
        platform="xiaohongshu",
        author_hash="stable-author-hash",
        published_at="2026-08-01T00:00:00+00:00",
        observed_at="2026-08-01T01:00:00+00:00",
        text_hash="text-hash",
        normalized_text_hash="normalized-hash",
        freshness_weight=1.0,
        raw_engagement=100,
        capped_engagement=50,
        engagement_weight=0.8,
        sponsored_or_ad=False,
        topic_mappings=(),
    )
    result = replace(_disabled_xhs_result(), status="ok", records=(record,))
    artifact = _public_xhs_artifact(result)
    assert artifact["records"] == []
    assert artifact["record_count_not_persisted"] == 1
    assert "stable-author-hash" not in str(artifact)


@pytest.mark.parametrize(
    ("config", "cli_provider"),
    [
        ({}, "mock"),
        ({"market_data": {"provider": " Mock "}}, None),
        ({"holdings": [{"data_provider": "mock"}]}, None),
        ({"watchlist": [{"data_provider": "MOCK"}]}, None),
        ({"objective_signals": {"provider": "mock"}}, None),
        ({"objective_signals": {"providers": {"vix": " mock "}}}, None),
    ],
)
def test_mock_provider_entrypoints_are_rejected_outside_simulation(
    config,
    cli_provider,
):
    with pytest.raises(ValueError, match="simulation-only"):
        _validate_provider_mode(
            config,
            cli_provider=cli_provider,
            mock=False,
            no_external=True,
        )


def test_mock_mode_requires_offline_external_boundary():
    with pytest.raises(ValueError, match="--mock --no-external"):
        _validate_provider_mode(
            {},
            cli_provider=None,
            mock=True,
            no_external=False,
        )
    _validate_provider_mode(
        {"market_data": {"provider": "mock"}},
        cli_provider="mock",
        mock=True,
        no_external=True,
    )


def test_unknown_provider_name_fails_closed():
    with pytest.raises(ValueError, match="unsupported"):
        _provider("misspelled-provider")


@pytest.mark.parametrize(
    "lineage",
    [
        {"quotes": {"DEMO": SimpleNamespace(source="mock")}},
        {"quotes": {}, "benchmark_quote": SimpleNamespace(source=" MOCK ")},
        {
            "quotes": {},
            "objective_quotes": {"vix": SimpleNamespace(source="mock-provider")},
        },
        {"quotes": {}, "plans": [SimpleNamespace(price_source="mock")]},
    ],
)
def test_mock_lineage_is_rejected_outside_simulation(lineage):
    with pytest.raises(ValueError, match="lineage"):
        _assert_no_mock_lineage(simulation=False, **lineage)
    _assert_no_mock_lineage(simulation=True, **lineage)


def test_unexpected_mock_quote_aborts_before_external_or_output(
    tmp_path,
    monkeypatch,
):
    config = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "portfolio.example.yaml")
        .read_text(encoding="utf-8")
    )
    config["runtime"] = {
        "data_classification": "private",
        "allow_live_report": True,
    }
    config["objective_signals"]["enabled"] = False
    config_path = tmp_path / "portfolio.private.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    out_dir = tmp_path / "must-not-exist"
    fake_provider = run_report.MockProvider()
    monkeypatch.setattr(run_report, "_provider", lambda _name: fake_provider)

    def fail_if_external_runs(*_args, **_kwargs):
        raise AssertionError("external collection must not run after mock lineage")

    monkeypatch.setattr(run_report, "collect_external_views", fail_if_external_runs)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_report.py",
            "--no-external",
            "--config",
            str(config_path),
            "--out-dir",
            str(out_dir),
            "--date",
            "2026-01-02",
        ],
    )
    with pytest.raises(SystemExit):
        run_report.main()
    assert not out_dir.exists()
