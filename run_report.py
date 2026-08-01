#!/usr/bin/env python3
"""Daily investment-research pipeline and portfolio decision entrypoint."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from serenity_monitor import (
    BaostockProvider,
    CboeIndexProvider,
    ChinaRetailAttentionResult,
    ChinaRetailAttentionSettings,
    EvidenceSettings,
    ExternalSettings,
    HybridProvider,
    MarketContext,
    MockProvider,
    ObjectiveSignalSettings,
    PortfolioSettings,
    ResearchSettings,
    YFinanceProvider,
    UsageAuthorization,
    apply_objective_overlay,
    analyze_authorized_csv,
    topic_rules_from_config,
    assess_view,
    build_objective_market_snapshot,
    build_dca_reviews,
    build_position_plans,
    calculate_risk_group_exposures,
    classify_regime,
    collect_external_views,
    compare_state,
    compute,
    evaluate_holding,
    evaluate_watchlist,
    load_state,
    recurring_amounts,
    render_markdown,
    save_state,
    snapshot_fallback_quote,
)


def _provider(name: str):
    name = _normalized_provider_name(name)
    if name == "mock":
        return MockProvider()
    if name == "baostock":
        return BaostockProvider()
    if name == "cboe":
        return CboeIndexProvider()
    if name == "yfinance":
        return YFinanceProvider()
    if name == "hybrid":
        return HybridProvider()
    raise ValueError(f"unsupported market-data provider: {name}")


def _normalized_provider_name(value) -> str:
    return str(value or "").strip().casefold()


def _configured_mock_locations(config: dict, cli_provider: str | None) -> list[str]:
    locations: list[str] = []
    if _normalized_provider_name(cli_provider) == "mock":
        locations.append("command line")

    market_cfg = config.get("market_data", {}) or {}
    if _normalized_provider_name(market_cfg.get("provider")) == "mock":
        locations.append("market_data.provider")

    for section in ("holdings", "watchlist"):
        for index, row in enumerate(config.get(section, []) or []):
            if _normalized_provider_name((row or {}).get("data_provider")) == "mock":
                locations.append(f"{section}[{index}].data_provider")

    objective_cfg = config.get("objective_signals", {}) or {}
    if _normalized_provider_name(objective_cfg.get("provider")) == "mock":
        locations.append("objective_signals.provider")
    for key, value in (objective_cfg.get("providers", {}) or {}).items():
        if _normalized_provider_name(value) == "mock":
            locations.append(f"objective_signals.providers.{key}")
    return locations


def _validate_provider_mode(
    config: dict,
    *,
    cli_provider: str | None,
    mock: bool,
    no_external: bool,
) -> None:
    if mock:
        if not no_external:
            raise ValueError("Mock mode requires --mock --no-external.")
        if cli_provider and _normalized_provider_name(cli_provider) != "mock":
            raise ValueError("--mock cannot be combined with a live --provider value.")
        return
    locations = _configured_mock_locations(config, cli_provider)
    if locations:
        raise ValueError(
            "Mock providers are simulation-only; rerun explicitly with "
            "--mock --no-external."
        )


def _is_mock_source(value) -> bool:
    return _normalized_provider_name(value).startswith("mock")


def _assert_no_mock_lineage(
    *,
    simulation: bool,
    quotes: dict,
    benchmark_quote=None,
    objective_quotes: dict | None = None,
    plans=(),
) -> None:
    if simulation:
        return
    sources = [getattr(quote, "source", "") for quote in quotes.values()]
    if benchmark_quote is not None:
        sources.append(getattr(benchmark_quote, "source", ""))
    sources.extend(
        getattr(quote, "source", "")
        for quote in (objective_quotes or {}).values()
    )
    sources.extend(getattr(plan, "price_source", "") for plan in plans)
    if any(_is_mock_source(source) for source in sources):
        raise ValueError(
            "Mock market-data lineage is forbidden outside explicit simulation mode."
        )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _is_git_ignored(path: Path, repo_root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return False
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", relative],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def _has_git_tracked_path(path: Path, repo_root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return False
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", relative],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
    except OSError:
        return True
    return result.returncode != 0 or bool(result.stdout)


def _validate_private_repo_path(path: Path, repo_root: Path, label: str) -> None:
    if not _is_within(path, repo_root):
        return
    if _has_git_tracked_path(path, repo_root):
        raise ValueError(f"{label} is tracked by Git.")
    if not _is_git_ignored(path, repo_root):
        raise ValueError(f"{label} is inside Git but is not ignored.")


def _report_output_paths(out_dir: Path, report_date: dt.date) -> dict[str, Path]:
    date_text = report_date.isoformat()
    return {
        "dated_report": out_dir / f"report_{date_text}.md",
        "latest_report": out_dir / "latest.md",
        "dated_decisions": out_dir / f"decisions_{date_text}.csv",
        "latest_decisions": out_dir / "latest_decisions.csv",
        "state": out_dir / "state.json",
        "source_health": out_dir / "source_health.json",
        "external_evidence": out_dir / "external_evidence.json",
        "dca_review": out_dir / "dca_review.json",
        "objective_market": out_dir / "objective_market.json",
        "china_retail_attention": out_dir / "china_retail_attention.json",
    }


def _validate_runtime_privacy(
    config_path: Path,
    config: dict,
    *,
    mock: bool,
    no_external: bool,
) -> str:
    runtime = config.get("runtime", {}) or {}
    classification = str(runtime.get("data_classification", "")).strip().lower()
    if classification == "synthetic_example":
        repo_root = Path(__file__).resolve().parent
        canonical_example = repo_root / "config" / "portfolio.example.yaml"
        if config_path.resolve() != canonical_example.resolve():
            raise ValueError(
                "synthetic_example is reserved for config/portfolio.example.yaml."
            )
        if not mock or not no_external:
            raise ValueError(
                "Synthetic public configuration is restricted to --mock --no-external."
            )
        if bool(runtime.get("allow_live_report", False)):
            raise ValueError("Synthetic public configuration cannot allow live reports.")
        return classification
    if classification != "private":
        raise ValueError(
            "Configuration must declare runtime.data_classification as private "
            "or synthetic_example."
        )
    if not config_path.name.endswith((".private.yaml", ".private.yml")):
        raise ValueError("Private runtime configuration must use a .private.yaml name.")
    if not mock and not bool(runtime.get("allow_live_report", False)):
        raise ValueError("Private configuration has not opted in to live reporting.")
    repo_root = Path(__file__).resolve().parent
    _validate_private_repo_path(
        config_path,
        repo_root,
        "Private runtime configuration",
    )
    return classification


def _validate_output_privacy(
    out_dir: Path,
    classification: str,
    output_paths: dict[str, Path] | None = None,
    repo_root: Path | None = None,
) -> None:
    if classification != "private":
        return
    repo_root = (repo_root or Path(__file__).resolve().parent).resolve()
    candidates = {out_dir, *(output_paths or {}).values()}
    for candidate in candidates:
        _validate_private_repo_path(candidate, repo_root, "Private report output")


def _validate_external_input_privacy(
    settings: ExternalSettings,
    classification: str,
    repo_root: Path | None = None,
) -> None:
    if classification != "private" or not settings.enabled:
        return
    repo_root = (repo_root or Path(__file__).resolve().parent).resolve()
    _validate_private_repo_path(
        Path(settings.source_profiles_path),
        repo_root,
        "Private source-profile input",
    )
    if settings.manual_kol_enabled:
        _validate_private_repo_path(
            Path(settings.manual_kol_path),
            repo_root,
            "Private manual-KOL input",
        )


def _objective_quote_age_error(
    quote,
    report_date: dt.date,
    max_staleness_days: int,
) -> str:
    try:
        age_days = (report_date - dt.date.fromisoformat(quote.as_of)).days
    except (AttributeError, TypeError, ValueError):
        return f"invalid as-of date {getattr(quote, 'as_of', '') or 'UNKNOWN'}"
    if age_days < 0:
        return f"future-dated as of {quote.as_of}"
    if age_days > max_staleness_days:
        return f"stale as of {quote.as_of}"
    return ""


def _disabled_xhs_result() -> ChinaRetailAttentionResult:
    return ChinaRetailAttentionResult(
        status="disabled",
        detail="China retail attention disabled by config; no file was read.",
        authorization_basis="",
        input_count=0,
        accepted_count=0,
        unique_count=0,
        rejected_count=0,
        exact_duplicate_count=0,
        normalized_duplicate_count=0,
        engagement_winsor_cap=0.0,
        ad_ratio=0.0,
        duplicate_burst_score=0.0,
        source_concentration=0.0,
        manipulation_penalty=0.0,
        execution_weight=0.0,
        warnings=("No social signal was produced.",),
    )


def _public_xhs_artifact(result: ChinaRetailAttentionResult) -> dict:
    """Return aggregate research output safe for a public report repository."""
    payload = asdict(result)
    payload["record_count_not_persisted"] = len(payload.get("records", []))
    payload["records"] = []
    payload["privacy_note"] = (
        "Record-level hashes, timestamps and engagement are intentionally not "
        "persisted in the public repository."
    )
    return payload


def _xhs_topic_rules(xhs_config: dict):
    """Resolve asset-agnostic defaults or ignored private asset mappings."""

    return topic_rules_from_config(xhs_config.get("topic_rules"))


def _safe_float(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _asset_type_hint(row: dict) -> str | None:
    explicit = str(row.get("asset_type", "")).strip().lower()
    if explicit in {"stock", "etf"}:
        return explicit
    framework = str(row.get("framework", "")).strip().lower()
    if "etf" in framework or framework in {"cash_equivalent", "hedge_etf"}:
        return "etf"
    if framework == "serenity_stock":
        return "stock"
    return None


def _write_csv(path: Path, plans) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "ticker", "name", "asset_type", "position_status", "risk_groups",
        "price_source", "price_as_of",
        "research_action", "portfolio_action", "current_shares", "current_price",
        "current_value", "current_weight", "entry_price", "entry_price_estimated",
        "unrealized_pnl_usd", "unrealized_pnl_pct", "target_weight",
        "adjusted_max_weight", "model_delta_usd", "executable_delta_usd",
        "trade_shares", "scheduled_dca_usd", "scheduled_dca_status",
        "confidence", "reasons", "constraints",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for plan in plans:
            writer.writerow(
                {
                    "ticker": plan.ticker,
                    "name": plan.name,
                    "asset_type": plan.asset_type,
                    "position_status": plan.position_status,
                    "risk_groups": " | ".join(plan.risk_groups),
                    "price_source": plan.price_source,
                    "price_as_of": plan.price_as_of,
                    "research_action": plan.research_action.value,
                    "portfolio_action": plan.action.value,
                    "current_shares": round(plan.current_shares, 6),
                    "current_price": round(plan.current_price, 4),
                    "current_value": round(plan.current_value, 2),
                    "current_weight": round(plan.current_weight, 6),
                    "entry_price": "" if plan.entry_price is None else round(plan.entry_price, 4),
                    "entry_price_estimated": plan.entry_price_estimated,
                    "unrealized_pnl_usd": (
                        "" if plan.unrealized_pnl_usd is None else round(plan.unrealized_pnl_usd, 2)
                    ),
                    "unrealized_pnl_pct": (
                        "" if plan.unrealized_pnl_pct is None else round(plan.unrealized_pnl_pct, 6)
                    ),
                    "target_weight": round(plan.target_weight, 6),
                    "adjusted_max_weight": round(plan.adjusted_max_weight, 6),
                    "model_delta_usd": round(plan.model_delta_usd, 2),
                    "executable_delta_usd": (
                        "" if plan.executable_delta_usd is None else round(plan.executable_delta_usd, 2)
                    ),
                    "trade_shares": "" if plan.trade_shares is None else round(plan.trade_shares, 6),
                    "scheduled_dca_usd": round(plan.scheduled_dca_usd, 2),
                    "scheduled_dca_status": plan.scheduled_dca_status,
                    "confidence": plan.confidence,
                    "reasons": " | ".join(plan.reasons),
                    "constraints": " | ".join(plan.constraints),
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--period", default="1y")
    parser.add_argument("--provider", choices=["hybrid", "baostock", "mock"])
    parser.add_argument("--mock", action="store_true", help="offline deterministic market data")
    parser.add_argument("--no-external", action="store_true", help="skip all external web/social sources")
    parser.add_argument("--date", help="override report date (YYYY-MM-DD) for tests/backfills")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_file():
        parser.error(
            "configuration file not found; copy config/portfolio.example.yaml "
            "to config/portfolio.private.yaml for private local use"
        )
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    try:
        classification = _validate_runtime_privacy(
            config_path,
            config,
            mock=args.mock,
            no_external=args.no_external,
        )
        _validate_provider_mode(
            config,
            cli_provider=args.provider,
            mock=args.mock,
            no_external=args.no_external,
        )
    except ValueError as exc:
        parser.error(str(exc))

    report_date = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    out_dir = Path(args.out_dir)
    output_paths = _report_output_paths(out_dir, report_date)
    try:
        _validate_output_privacy(out_dir, classification, output_paths)
    except ValueError as exc:
        parser.error(str(exc))
    report_timezone = ZoneInfo(str(config.get("report_timezone", "Asia/Shanghai")))
    generated_at = dt.datetime.combine(
        report_date,
        dt.time(hour=8, minute=30),
        tzinfo=report_timezone,
    )

    holdings = list(config.get("holdings", []) or [])
    watchlist = list(config.get("watchlist", []) or [])
    portfolio_cfg = config.get("portfolio", {}) or {}
    market_cfg = config.get("market_data", {}) or {}
    provider_name = _normalized_provider_name(
        "mock" if args.mock else (args.provider or market_cfg.get("provider", "hybrid"))
    )
    default_provider = _provider(provider_name)
    provider_cache = {provider_name: default_provider}

    def provider_for(row: dict):
        if args.mock:
            return default_provider
        name = _normalized_provider_name(row.get("data_provider", provider_name))
        if name not in provider_cache:
            provider_cache[name] = _provider(name)
        return provider_cache[name]

    targets = holdings + watchlist
    benchmark = str(market_cfg.get("benchmark", "SPY")).upper()
    quotes = {}
    data_errors: list[str] = []
    for row in targets:
        ticker = str(row.get("ticker", "")).upper()
        symbol = str(row.get("data_symbol", ticker))
        if not ticker:
            continue
        try:
            asset_type_hint = _asset_type_hint(row)
            quote = provider_for(row).get(symbol, args.period, asset_type_hint)
            asset_type_override = str(row.get("asset_type", "")).strip().lower()
            if asset_type_override in {"stock", "etf"}:
                quote.asset_type = asset_type_override
            quotes[ticker] = quote
        except Exception as exc:
            data_errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
            fallback = snapshot_fallback_quote(
                row,
                str(portfolio_cfg.get("positions_as_of", "")),
            )
            if fallback is not None:
                quotes[ticker] = fallback
                data_errors.append(
                    f"{ticker}: using stale broker snapshot fallback from "
                    f"{fallback.as_of}; not a live price"
                )
    benchmark_quote = quotes.get(benchmark)
    if benchmark_quote is None:
        try:
            benchmark_quote = default_provider.get(benchmark, args.period)
        except Exception as exc:
            data_errors.append(f"benchmark {benchmark}: {type(exc).__name__}: {exc}")

    indicators_by_ticker = {
        ticker: compute(quote.closes, quote.volumes)
        for ticker, quote in quotes.items()
    }
    benchmark_indicators = (
        compute(benchmark_quote.closes, benchmark_quote.volumes) if benchmark_quote else None
    )
    base_regime = classify_regime(benchmark_indicators)

    objective_settings = ObjectiveSignalSettings.from_dict(
        config.get("objective_signals")
    )
    objective_quotes = {}
    if objective_settings.enabled:
        objective_provider_cache = {}
        for key, symbol in objective_settings.symbols.items():
            try:
                objective_provider_name = (
                    "mock"
                    if args.mock
                    else objective_settings.providers.get(
                        key,
                        objective_settings.provider,
                    )
                )
                if objective_provider_name not in objective_provider_cache:
                    objective_provider_cache[objective_provider_name] = _provider(
                        objective_provider_name
                    )
                objective_quotes[key] = objective_provider_cache[
                    objective_provider_name
                ].get(
                    symbol,
                    objective_settings.period,
                )
            except Exception as exc:
                data_errors.append(
                    f"objective {key} ({symbol}): {type(exc).__name__}: {exc}"
                )
        for key, quote in list(objective_quotes.items()):
            freshness_error = _objective_quote_age_error(
                quote,
                report_date,
                objective_settings.max_staleness_days,
            )
            if freshness_error:
                data_errors.append(
                    f"objective {key} ({quote.ticker}): {freshness_error}; "
                    "excluded from risk-budget calculation"
                )
                del objective_quotes[key]
    try:
        _assert_no_mock_lineage(
            simulation=args.mock,
            quotes=quotes,
            benchmark_quote=benchmark_quote,
            objective_quotes=objective_quotes,
        )
    except ValueError as exc:
        parser.error(str(exc))
    objective_snapshot = build_objective_market_snapshot(
        objective_quotes,
        objective_settings,
    )
    regime = apply_objective_overlay(base_regime, objective_snapshot)

    xhs_cfg = config.get("china_retail_attention", {}) or {}
    if not bool(xhs_cfg.get("enabled", True)):
        xhs_result = _disabled_xhs_result()
    else:
        xhs_settings = ChinaRetailAttentionSettings.from_dict(xhs_cfg.get("settings"))
        xhs_authorization = UsageAuthorization.from_dict(xhs_cfg.get("authorization"))
        try:
            xhs_topic_rules = _xhs_topic_rules(xhs_cfg)
        except (TypeError, ValueError) as exc:
            parser.error(str(exc))
        xhs_path = Path(
            str(xhs_cfg.get("authorized_csv_path", "config/xiaohongshu_authorized.csv"))
        )
        try:
            _validate_private_repo_path(
                xhs_path,
                Path(__file__).resolve().parent,
                "Authorized Xiaohongshu input",
            )
        except ValueError as exc:
            parser.error(str(exc))
        xhs_result = analyze_authorized_csv(
            xhs_path,
            xhs_authorization,
            settings=xhs_settings,
            topic_rules=xhs_topic_rules,
            now=generated_at.astimezone(dt.timezone.utc),
        )

    external_settings = ExternalSettings.from_dict(config.get("external_views"))
    try:
        _validate_external_input_privacy(external_settings, classification)
    except ValueError as exc:
        parser.error(str(exc))
    market_contexts = {
        ticker: MarketContext(
            market_cap_usd=quote.market_cap,
            avg_dollar_volume_20d=indicators_by_ticker[ticker].avg_dollar_vol_20d,
            ret_1m=indicators_by_ticker[ticker].ret_1m,
            volume_ratio=indicators_by_ticker[ticker].volume_ratio,
        )
        for ticker, quote in quotes.items()
    }
    external = collect_external_views(
        holdings,
        watchlist,
        external_settings,
        market_contexts=market_contexts,
        network_enabled=not (args.no_external or args.mock),
    )
    evidence_settings = EvidenceSettings.from_dict(config.get("evidence"))
    research_settings = ResearchSettings.from_dict(config.get("research"))

    recommendations = {}
    evidence_ids: dict[str, list[str]] = {}
    for row in holdings:
        ticker = str(row["ticker"]).upper()
        quote = quotes.get(ticker)
        if quote is None:
            continue
        indicators = indicators_by_ticker[ticker]
        assessment = assess_view(
            ticker,
            external.view(ticker),
            evidence_settings,
            extra_positive_terms=row.get("positive_terms"),
            extra_risk_terms=row.get("risk_terms"),
            extra_break_terms=row.get("break_terms"),
        )
        evidence_ids[ticker] = assessment.item_ids
        recommendations[ticker] = evaluate_holding(
            row,
            indicators,
            assessment,
            regime,
            research_settings,
            market_cap=quote.market_cap or _safe_float(row.get("market_cap")),
            asset_type=quote.asset_type,
            evidence_min_coverage=evidence_settings.min_coverage_for_add,
        )

    for row in watchlist:
        ticker = str(row["ticker"]).upper()
        quote = quotes.get(ticker)
        if quote is None:
            continue
        indicators = indicators_by_ticker[ticker]
        assessment = assess_view(
            ticker,
            external.view(ticker),
            evidence_settings,
            extra_positive_terms=row.get("positive_terms"),
            extra_risk_terms=row.get("risk_terms"),
            extra_break_terms=row.get("break_terms"),
        )
        evidence_ids[ticker] = assessment.item_ids
        recommendations[ticker] = evaluate_watchlist(
            row,
            indicators,
            assessment,
            regime,
            research_settings,
            market_cap=quote.market_cap or _safe_float(row.get("market_cap")),
            evidence_min_coverage=evidence_settings.min_coverage_for_add,
        )

    broker_snapshot = config.get("broker_snapshot", {}) or {}
    portfolio_input = dict(portfolio_cfg)
    portfolio_input.setdefault("account_value_usd", broker_snapshot.get("total_value_usd"))
    portfolio_input.setdefault("cash_usd", broker_snapshot.get("cash_usd"))
    portfolio_input.setdefault("buying_power_usd", broker_snapshot.get("buying_power_usd"))
    portfolio_settings = PortfolioSettings.from_dict(portfolio_input)
    recurring_cfg = config.get("recurring_investments", {}) or {}
    recurring = recurring_amounts(recurring_cfg)
    plans, equity = build_position_plans(
        holdings,
        watchlist,
        quotes,
        recommendations,
        regime,
        portfolio_settings,
        recurring_investments=recurring,
    )
    holding_values = {
        plan.ticker: plan.current_value for plan in plans if plan.current_shares > 0
    }
    risk_group_exposures = calculate_risk_group_exposures(
        holdings, holding_values, equity
    )
    dca_reviews = build_dca_reviews(
        plans,
        recommendations,
        regime,
        recurring_cfg,
        risk_group_exposures,
        portfolio_settings.risk_group_caps,
        portfolio_settings.immediately_deployable_cash(equity),
    )

    try:
        _assert_no_mock_lineage(
            simulation=args.mock,
            quotes=quotes,
            benchmark_quote=benchmark_quote,
            objective_quotes=objective_quotes,
            plans=plans,
        )
    except ValueError as exc:
        parser.error(str(exc))

    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_paths["state"]
    previous = {} if args.mock else load_state(state_path)
    changes = compare_state(previous, plans)
    markdown = render_markdown(
        plans,
        regime,
        external,
        equity,
        data_errors=data_errors,
        changes=changes,
        portfolio_as_of=str(portfolio_cfg.get("positions_as_of", "")),
        cash_known=portfolio_settings.cash_usd is not None,
        generated_at=generated_at,
        broker_snapshot=broker_snapshot,
        portfolio_settings=portfolio_settings,
        risk_group_exposures=risk_group_exposures,
        recommendations=recommendations,
        dca_reviews=dca_reviews,
        objective_snapshot=objective_snapshot,
        china_retail_attention=xhs_result,
    )
    if args.mock:
        markdown = (
            "> **SIMULATION ONLY:** deterministic mock data; no live state or broker "
            "action.\n\n"
            + markdown
        )

    dated = output_paths["dated_report"]
    latest = output_paths["latest_report"]
    dated.write_text(markdown, encoding="utf-8")
    latest.write_text(markdown, encoding="utf-8")
    _write_csv(output_paths["dated_decisions"], plans)
    _write_csv(output_paths["latest_decisions"], plans)
    if not args.mock:
        save_state(state_path, report_date.isoformat(), plans, evidence_ids)

    source_health = [status.__dict__ for status in external.statuses]
    source_health.append(
        {
            "source": "Xiaohongshu authorized export",
            "status": xhs_result.status,
            "detail": xhs_result.detail,
        }
    )
    output_paths["source_health"].write_text(
        json.dumps(source_health, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    evidence_rows = []
    for ticker, view in external.by_ticker.items():
        for item in view.items:
            evidence_rows.append({**asdict(item), "ticker": ticker})
    for item in external.global_items:
        evidence_rows.append(asdict(item))
    output_paths["external_evidence"].write_text(
        json.dumps(evidence_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    output_paths["dca_review"].write_text(
        json.dumps([asdict(review) for review in dca_reviews], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    output_paths["objective_market"].write_text(
        json.dumps(objective_snapshot.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    output_paths["china_retail_attention"].write_text(
        json.dumps(_public_xhs_artifact(xhs_result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[OK] Wrote {dated} and {output_paths['latest_decisions']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
