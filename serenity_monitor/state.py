"""Persistent day-over-day state and change detection."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .sizing import PositionPlan


@dataclass
class PlanChange:
    ticker: str
    previous_action: str
    current_action: str
    previous_delta_usd: float
    current_delta_usd: float
    detail: str


def load_state(path: str | Path) -> dict:
    target = Path(path)
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def compare_state(previous: dict, plans: list[PositionPlan]) -> list[PlanChange]:
    old = previous.get("plans", {}) if isinstance(previous, dict) else {}
    changes: list[PlanChange] = []
    for plan in plans:
        before = old.get(plan.ticker, {})
        old_action = str(before.get("action", "首次运行"))
        old_delta = float(before.get("model_delta_usd", 0.0) or 0.0)
        changed_action = old_action != plan.action.value
        changed_amount = abs(old_delta - plan.model_delta_usd) >= max(100.0, abs(plan.model_delta_usd) * 0.20)
        if changed_action or changed_amount:
            changes.append(
                PlanChange(
                    ticker=plan.ticker,
                    previous_action=old_action,
                    current_action=plan.action.value,
                    previous_delta_usd=old_delta,
                    current_delta_usd=plan.model_delta_usd,
                    detail=("动作变化" if changed_action else "建议金额显著变化"),
                )
            )
    return changes


def save_state(path: str | Path, date: str, plans: list[PositionPlan], evidence_ids: dict[str, list[str]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": date,
        "plans": {
            plan.ticker: {
                "action": plan.action.value,
                "research_action": plan.research_action.value,
                "model_delta_usd": round(plan.model_delta_usd, 2),
                "target_weight": round(plan.target_weight, 6),
                "evidence_ids": evidence_ids.get(plan.ticker, []),
            }
            for plan in plans
        },
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
