"""File loading and deterministic demo data for the Pro daily suite."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd
import yaml

from .manager_skill import ManagerFragility


@dataclass(frozen=True)
class LoadedProInputs:
    report_date: dt.date
    portfolio_snapshot: Mapping[str, Any]
    asset_returns: pd.DataFrame | None
    factor_returns: pd.DataFrame | None
    policy_events: tuple[Mapping[str, Any], ...]
    polymarket_events: tuple[Mapping[str, Any], ...]
    dca_plan: Mapping[str, float]
    objective_risk_multiplier: float
    accepted_close_status: str
    social_heat: Mapping[str, Any] | None
    prediction_state: str
    manager_fund_returns: pd.Series | None
    manager_factor_returns: pd.DataFrame | None
    manager_fragility: ManagerFragility | None


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_returns(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if frame.empty or len(frame.columns) < 2:
        raise ValueError(f"returns file has no usable columns: {path}")
    date_column = frame.columns[0]
    frame[date_column] = pd.to_datetime(frame[date_column], errors="coerce")
    frame = frame.dropna(subset=[date_column]).set_index(date_column).sort_index()
    return frame.apply(pd.to_numeric, errors="coerce").dropna(how="all")


def _manager_fragility(data: Mapping[str, Any] | None) -> ManagerFragility | None:
    if not data:
        return None
    return ManagerFragility(
        gross_leverage=data.get("gross_leverage"),
        top10_concentration=data.get("top10_concentration"),
        liquidity_days=data.get("liquidity_days"),
        prime_broker_concentration=data.get("prime_broker_concentration"),
        tenure_months=data.get("tenure_months"),
        fund_age_months=data.get("fund_age_months"),
    )


def load_pro_config(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(target)
    config = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    runtime = config.get("runtime") or {}
    classification = str(runtime.get("data_classification") or "").strip()
    if classification not in {"synthetic_example", "private"}:
        raise ValueError("runtime.data_classification must be synthetic_example or private")
    if classification == "private" and not target.name.endswith((".private.yaml", ".private.yml")):
        raise ValueError("private configuration must use a .private.yaml name")
    if classification == "synthetic_example" and not bool(config.get("demo_mode", False)):
        raise ValueError("synthetic_example requires demo_mode=true")
    return config


def _synthetic_polymarket_events(
    rng: np.random.Generator,
    start_date: dt.date,
    count: int = 8,
) -> tuple[Mapping[str, Any], ...]:
    events: list[Mapping[str, Any]] = []
    for index in range(count):
        resolved_date = start_date + dt.timedelta(days=28 * index)
        resolved_at = dt.datetime.combine(
            resolved_date,
            dt.time(20, 0),
            tzinfo=dt.timezone.utc,
        )
        probability = 0.25 + 0.06 * index
        outcome = 1.0 if index % 3 != 0 else 0.0
        surprise = outcome - probability
        sessions = [resolved_date + dt.timedelta(days=offset) for offset in range(0, 90)]
        sessions = [day for day in sessions if day.weekday() < 5]
        base = 100.0
        prices = []
        level = base
        for session_index, session in enumerate(sessions):
            drift = 0.0002 + 0.0025 * surprise * np.exp(-session_index / 15)
            level *= float(np.exp(drift + rng.normal(0, 0.006)))
            prices.append({"session": session.isoformat(), "close": round(level, 6)})
        events.append(
            {
                "market_id": f"demo-market-{index + 1}",
                "question": "Synthetic Trump policy settlement market",
                "policy_topic": "trade_tariff" if index % 2 == 0 else "fiscal_tax",
                "resolved_at": resolved_at.isoformat(),
                "outcome": outcome,
                "probability_history": [
                    {
                        "observed_at": (resolved_at - dt.timedelta(days=3)).isoformat(),
                        "probability": round(max(0.05, probability - 0.04), 4),
                    },
                    {
                        "observed_at": (resolved_at - dt.timedelta(hours=30)).isoformat(),
                        "probability": round(probability, 4),
                    },
                    {
                        "observed_at": (resolved_at + dt.timedelta(hours=2)).isoformat(),
                        "probability": outcome,
                    },
                ],
                "asset_prices": {"SPY": prices},
            }
        )
    return tuple(events)


def demo_inputs(report_date: dt.date | None = None) -> LoadedProInputs:
    report_date = report_date or dt.date(2026, 1, 2)
    rng = np.random.default_rng(20260802)
    dates = pd.bdate_range(end=pd.Timestamp(report_date), periods=320)
    factor_returns = pd.DataFrame(
        {
            "MARKET": rng.normal(0.00035, 0.010, len(dates)),
            "VALUE": rng.normal(0.00010, 0.006, len(dates)),
            "MOMENTUM": rng.normal(0.00015, 0.007, len(dates)),
            "QUALITY": rng.normal(0.00012, 0.005, len(dates)),
            "SEMIS": rng.normal(0.00030, 0.014, len(dates)),
        },
        index=dates,
    )
    noise = rng.normal(0, 0.007, (len(dates), 3))
    asset_returns = pd.DataFrame(
        {
            "DEMO_CORE": 0.95 * factor_returns["MARKET"] + 0.15 * factor_returns["QUALITY"] + noise[:, 0],
            "DEMO_GROWTH": 1.15 * factor_returns["MARKET"] + 0.45 * factor_returns["SEMIS"] + 0.20 * factor_returns["MOMENTUM"] + noise[:, 1],
            "DEMO_DIV": 0.70 * factor_returns["MARKET"] + 0.35 * factor_returns["VALUE"] + 0.25 * factor_returns["QUALITY"] + noise[:, 2],
        },
        index=dates,
    )
    manager_returns = (
        0.00012
        + 0.85 * factor_returns["MARKET"]
        + 0.22 * factor_returns["QUALITY"]
        + rng.normal(0, 0.0045, len(dates))
    ).rename("manager_fund")
    portfolio = {
        "as_of": report_date.isoformat(),
        "account_value_usd": 100000,
        "cash_usd": 15000,
        "positions": [
            {"ticker": "DEMO_CORE", "weight": 0.45},
            {"ticker": "DEMO_GROWTH", "weight": 0.35},
            {"ticker": "DEMO_DIV", "weight": 0.20},
        ],
    }
    generated = dt.datetime.combine(report_date, dt.time(13, 15), tzinfo=dt.timezone.utc)
    policy_events = (
        {
            "event_id": "demo-policy-1",
            "observed_at": (generated - dt.timedelta(days=2)).isoformat(),
            "actor": "donald_trump",
            "source_tier": "official_statement",
            "stage": "official_statement",
            "policy_topic": "trade_tariff",
            "direction": -1,
            "magnitude": 0.7,
            "confidence": 0.8,
            "horizon_days": 90,
            "asset_impacts": {"SPY": 0.4, "SEMIS": 0.8},
            "title": "Synthetic tariff-policy statement",
        },
        {
            "event_id": "demo-policy-2",
            "observed_at": (generated - dt.timedelta(days=7)).isoformat(),
            "actor": "trump_administration",
            "source_tier": "signed_official_action",
            "stage": "signed",
            "policy_topic": "energy",
            "direction": 1,
            "magnitude": 0.5,
            "confidence": 0.9,
            "horizon_days": 180,
            "asset_impacts": {"ENERGY": 0.9, "SPY": 0.1},
            "title": "Synthetic signed energy action",
        },
    )
    poly_start = report_date - dt.timedelta(days=260)
    return LoadedProInputs(
        report_date=report_date,
        portfolio_snapshot=portfolio,
        asset_returns=asset_returns,
        factor_returns=factor_returns,
        policy_events=policy_events,
        polymarket_events=_synthetic_polymarket_events(rng, poly_start),
        dca_plan={"DEMO_CORE": 20.0, "DEMO_GROWTH": 20.0, "DEMO_DIV": 20.0},
        objective_risk_multiplier=0.94,
        accepted_close_status="healthy",
        social_heat={"status": "healthy", "manipulation_penalty": 0.12, "quarantined": False},
        prediction_state="active",
        manager_fund_returns=manager_returns,
        manager_factor_returns=factor_returns[["MARKET", "QUALITY", "VALUE"]],
        manager_fragility=ManagerFragility(
            gross_leverage=1.1,
            top10_concentration=0.48,
            liquidity_days=2.0,
            prime_broker_concentration=0.35,
            tenure_months=48,
            fund_age_months=60,
        ),
    )


def load_inputs_from_config(config_path: str | Path) -> LoadedProInputs:
    config = load_pro_config(config_path)
    if bool(config.get("demo_mode", False)):
        report_date = dt.date.fromisoformat(str(config.get("report_date") or "2026-01-02"))
        demo = demo_inputs(report_date)
        dca = config.get("dca_plan") or demo.dca_plan
        return LoadedProInputs(**{**demo.__dict__, "dca_plan": {str(k).upper(): float(v) for k, v in dca.items()}})

    base = Path(config_path).resolve().parent
    paths = config.get("paths") or {}
    def resolve(name: str) -> Path | None:
        raw = paths.get(name)
        if not raw:
            return None
        target = Path(str(raw))
        return target if target.is_absolute() else (base / target).resolve()

    portfolio_path = resolve("portfolio_snapshot")
    if portfolio_path is None:
        raise ValueError("paths.portfolio_snapshot is required")
    portfolio = _read_json(portfolio_path)
    asset_path = resolve("asset_returns")
    factor_path = resolve("factor_returns")
    policy_path = resolve("policy_events")
    polymarket_path = resolve("polymarket_events")
    social_path = resolve("social_heat")
    objective_path = resolve("objective_market")
    manager_path = resolve("manager_returns")

    asset_returns = None if asset_path is None else _read_returns(asset_path)
    factor_returns = None if factor_path is None else _read_returns(factor_path)
    policy_events = tuple(_read_json(policy_path)) if policy_path else ()
    polymarket_events = tuple(_read_json(polymarket_path)) if polymarket_path else ()
    social_heat = _read_json(social_path) if social_path else None
    objective_multiplier = float(config.get("objective_risk_multiplier", 1.0))
    if objective_path:
        objective = _read_json(objective_path)
        objective_multiplier = float(objective.get("risk_budget_multiplier", objective_multiplier))

    manager_fund_returns = None
    manager_factor_returns = None
    if manager_path:
        manager_frame = _read_returns(manager_path)
        fund_column = str((config.get("manager") or {}).get("fund_column") or "fund_return")
        factor_columns = list((config.get("manager") or {}).get("factor_columns") or [])
        if fund_column not in manager_frame:
            raise ValueError(f"manager fund column is missing: {fund_column}")
        manager_fund_returns = manager_frame[fund_column]
        manager_factor_returns = manager_frame[factor_columns] if factor_columns else manager_frame.drop(columns=[fund_column])

    report_date = dt.date.fromisoformat(str(config.get("report_date") or dt.date.today().isoformat()))
    return LoadedProInputs(
        report_date=report_date,
        portfolio_snapshot=portfolio,
        asset_returns=asset_returns,
        factor_returns=factor_returns,
        policy_events=policy_events,
        polymarket_events=polymarket_events,
        dca_plan={str(key).upper(): float(value) for key, value in dict(config.get("dca_plan") or {}).items()},
        objective_risk_multiplier=objective_multiplier,
        accepted_close_status=str(config.get("accepted_close_status") or "unknown"),
        social_heat=social_heat,
        prediction_state=str(config.get("prediction_state") or "research_only"),
        manager_fund_returns=manager_fund_returns,
        manager_factor_returns=manager_factor_returns,
        manager_fragility=_manager_fragility(config.get("manager_fragility")),
    )
