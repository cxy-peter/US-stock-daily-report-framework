#!/usr/bin/env python3
"""Validate requirement-ledger completeness and production-state semantics."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "requirements" / "DAILY_RESEARCH_REQUIREMENTS.yaml"
ALLOWED_STATUSES = {
    "implemented_library",
    "tested",
    "live_data_connected",
    "daily_report_integrated",
    "private_input_required",
    "private_deployment_required",
    "blocked",
    "non_negotiable",
}
REQUIRED_IDS = {
    "REPORT-0830",
    "REPORT-SINGLE",
    "REPORT-COST-CONDITIONAL",
    "REPORT-BUY-SIDE",
    "SOURCE-REDDIT-QUORA-FALLBACK",
    "SOURCE-XIAOHONGSHU-INBOX",
    "SOURCE-GITHUB-AGENT-DIGEST",
    "SOURCE-FUND-FINANCIAL-NEWS",
    "MODEL-POLITICAL-CLAIMS",
    "MODEL-TPTI",
    "MODEL-POLYMARKET-LIVE",
    "MODEL-POLYMARKET-RESOLVED",
    "MODEL-VOLATILITY",
    "MODEL-OPTION-TAIL",
    "MODEL-OVERNIGHT",
    "MODEL-BARRA-KALMAN",
    "MODEL-MANAGER-SKILL",
    "MODEL-FACTOR-VALIDATION",
    "MODEL-ATTRIBUTION-ALLOCATION",
    "CONTEXT-PERSISTENCE-SKILL",
    "EXECUTION-BOUNDARY",
}


def _read() -> dict[str, Any]:
    payload = yaml.safe_load(LEDGER.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise TypeError("ledger must be a mapping")
    return payload


def _path_exists(raw: str) -> bool:
    path = str(raw).strip()
    if not path or path.startswith("private:"):
        return True
    return (ROOT / path).exists()


def check() -> None:
    payload = _read()
    if payload.get("schema_version") != "daily_research_requirements/v1.0.0":
        raise ValueError("unsupported ledger schema")
    definitions = payload.get("state_definitions") or {}
    for state in (
        "implemented_library",
        "tested",
        "live_data_connected",
        "daily_report_integrated",
        "private_input_required",
        "blocked",
    ):
        if state not in definitions:
            raise ValueError(f"missing state definition: {state}")

    rows = payload.get("requirements") or []
    if not isinstance(rows, list):
        raise TypeError("requirements must be a list")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("requirement row must be a mapping")
        requirement_id = str(row.get("id") or "").strip()
        if not requirement_id or requirement_id in seen:
            raise ValueError("requirement IDs must be unique and non-empty")
        seen.add(requirement_id)
        status = str(row.get("status") or "").strip()
        if status not in ALLOWED_STATUSES:
            raise ValueError(f"unsupported status for {requirement_id}: {status}")
        if not str(row.get("intent") or "").strip():
            raise ValueError(f"missing intent for {requirement_id}")
        evidence = row.get("evidence") or []
        if status not in {"blocked", "non_negotiable"} and not evidence:
            raise ValueError(f"missing evidence for {requirement_id}")
        if not all(_path_exists(str(value)) for value in evidence):
            raise ValueError(f"missing repository evidence for {requirement_id}")

    missing = REQUIRED_IDS - seen
    if missing:
        raise ValueError(f"missing required IDs: {sorted(missing)}")

    by_id = {str(row["id"]): row for row in rows}
    schedule = by_id["REPORT-0830"].get("target") or {}
    if schedule.get("before_date") != "2026-08-10":
        raise ValueError("Shanghai schedule transition date drifted")
    if schedule.get("timezone") != "Asia/Shanghai":
        raise ValueError("pre-transition timezone drifted")
    if schedule.get("timezone_after") != "America/New_York":
        raise ValueError("post-transition timezone drifted")
    if schedule.get("local_time") != "08:30" or schedule.get("local_time_after") != "08:30":
        raise ValueError("08:30 local report contract drifted")

    social = by_id["SOURCE-XIAOHONGSHU-INBOX"]
    if social.get("hard_boundary") != "direct_add_open_weight_is_zero":
        raise ValueError("Xiaohongshu execution boundary drifted")
    agent = by_id["SOURCE-GITHUB-AGENT-DIGEST"]
    if agent.get("hard_boundary") != "agent_summary_is_not_original_evidence":
        raise ValueError("agent provenance boundary drifted")

    change_control = payload.get("change_control") or {}
    required_change_steps = set(change_control.get("every_change_must") or [])
    if "run scripts/check_requirement_ledger.py" not in required_change_steps:
        raise ValueError("ledger self-check is not part of change control")
    forbidden = set(change_control.get("forbidden_claims") or [])
    if "implemented means production complete" not in forbidden:
        raise ValueError("production-state anti-drift rule is missing")


def main() -> int:
    try:
        check()
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        print(f"requirement ledger check failed: {exc}", file=sys.stderr)
        return 1
    print("requirement ledger check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
