"""Command-line entrypoint for one private/synthetic Pro daily report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .daily import build_pro_daily_report, render_pro_daily_markdown
from .io import load_inputs_from_config


def run(config_path: str | Path, out_dir: str | Path) -> tuple[Path, Path]:
    inputs = load_inputs_from_config(config_path)
    report = build_pro_daily_report(
        report_date=inputs.report_date,
        portfolio_snapshot=inputs.portfolio_snapshot,
        asset_returns=inputs.asset_returns,
        factor_returns=inputs.factor_returns,
        policy_events=inputs.policy_events,
        polymarket_events=inputs.polymarket_events,
        dca_plan=inputs.dca_plan,
        objective_risk_multiplier=inputs.objective_risk_multiplier,
        accepted_close_status=inputs.accepted_close_status,
        social_heat=inputs.social_heat,
        prediction_state=inputs.prediction_state,
        manager_fund_returns=inputs.manager_fund_returns,
        manager_factor_returns=inputs.manager_factor_returns,
        manager_fragility=inputs.manager_fragility,
    )
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "pro_daily_report.json"
    markdown_path = target / "pro_daily_report.md"
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(render_pro_daily_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate one Pro daily research report")
    parser.add_argument("--config", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    try:
        json_path, markdown_path = run(args.config, args.out_dir)
    except (OSError, ValueError, KeyError) as exc:
        print(f"PRO_DAILY_FAILED:{type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"PRO_DAILY_OK:{json_path.name}:{markdown_path.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
