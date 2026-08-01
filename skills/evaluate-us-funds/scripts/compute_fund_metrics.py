#!/usr/bin/env python3
"""Compute transparent fund metrics from aligned periodic simple returns.

Input CSV columns:
    date,fund_return[,benchmark_return][,risk_free_return]

Returns are decimals by default (0.01 = 1%). Use --returns-in-percent when
the file stores 1 for 1%. Missing benchmark/risk-free observations are allowed;
relative metrics use only aligned rows and disclose the reduced sample. Sharpe
and CAPM alpha/beta are null when risk-free returns are incomplete unless the
caller explicitly enables --assume-zero-risk-free.

This utility deliberately does not download data, infer frequency, optimize a
portfolio, or turn point estimates into an investment score.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional


@dataclass(frozen=True)
class Row:
    date_label: str
    date_value: date
    fund: float
    benchmark: Optional[float]
    risk_free: Optional[float]


def parse_date(value: str) -> date:
    text = value.strip()
    if not text:
        raise ValueError("date is blank")
    normalized = text.replace("/", "-")
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        try:
            return datetime.fromisoformat(normalized).date()
        except ValueError as exc:
            raise ValueError(
                f"unsupported date {value!r}; use ISO YYYY-MM-DD"
            ) from exc


def parse_optional_float(value: Optional[str], scale: float) -> Optional[float]:
    if value is None or not value.strip():
        return None
    result = float(value.strip()) / scale
    if not math.isfinite(result):
        raise ValueError(f"non-finite return {value!r}")
    if result <= -1.0:
        raise ValueError(
            f"simple return {result} is <= -100%; check units and data quality"
        )
    return result


def load_rows(path: Path, returns_in_percent: bool) -> tuple[list[Row], list[str]]:
    scale = 100.0 if returns_in_percent else 1.0
    warnings: list[str] = []
    rows: list[Row] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        required = {"date", "fund_return"}
        missing = required - headers
        if missing:
            raise ValueError(f"missing required CSV columns: {sorted(missing)}")
        for line_number, raw in enumerate(reader, start=2):
            try:
                fund = parse_optional_float(raw.get("fund_return"), scale)
                if fund is None:
                    warnings.append(f"line {line_number}: blank fund_return dropped")
                    continue
                label = (raw.get("date") or "").strip()
                rows.append(
                    Row(
                        date_label=label,
                        date_value=parse_date(label),
                        fund=fund,
                        benchmark=parse_optional_float(
                            raw.get("benchmark_return"), scale
                        ),
                        risk_free=parse_optional_float(
                            raw.get("risk_free_return"), scale
                        ),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"line {line_number}: {exc}") from exc

    rows.sort(key=lambda item: item.date_value)
    duplicate_dates = [
        rows[index].date_label
        for index in range(1, len(rows))
        if rows[index].date_value == rows[index - 1].date_value
    ]
    if duplicate_dates:
        raise ValueError(f"duplicate dates are not allowed: {duplicate_dates[:5]}")
    if not rows:
        raise ValueError("no usable fund-return rows")
    return rows, warnings


def compounded_return(values: Iterable[float]) -> float:
    wealth = 1.0
    for value in values:
        wealth *= 1.0 + value
    return wealth - 1.0


def annualized_return(values: list[float], periods_per_year: int) -> Optional[float]:
    if not values:
        return None
    wealth = 1.0 + compounded_return(values)
    return wealth ** (periods_per_year / len(values)) - 1.0


def sample_std(values: list[float]) -> Optional[float]:
    return statistics.stdev(values) if len(values) >= 2 else None


def safe_ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or abs(denominator) < 1e-15:
        return None
    return numerator / denominator


def max_drawdown(rows: list[Row]) -> dict[str, object]:
    wealth = 1.0
    peak_wealth = 1.0
    peak_date = "SAMPLE_START"
    worst = 0.0
    worst_peak_date: Optional[str] = None
    trough_date: Optional[str] = None
    recovery_date: Optional[str] = None
    waiting_for_recovery = False

    for item in rows:
        wealth *= 1.0 + item.fund
        if wealth >= peak_wealth:
            peak_wealth = wealth
            peak_date = item.date_label
            if waiting_for_recovery and recovery_date is None:
                recovery_date = item.date_label
                waiting_for_recovery = False
        drawdown = wealth / peak_wealth - 1.0
        if drawdown < worst:
            worst = drawdown
            worst_peak_date = peak_date
            trough_date = item.date_label
            recovery_date = None
            waiting_for_recovery = True

    return {
        "value": worst,
        "peak_date": worst_peak_date,
        "trough_date": trough_date,
        "recovery_date": recovery_date,
        "recovered_by_sample_end": not waiting_for_recovery,
    }


def historical_tail(values: list[float], confidence: float) -> tuple[float, float]:
    """Return loss VaR/ES using an equal-weight nearest-rank lower tail.

    The tail contains ceil((1-confidence) * n) worst observations, with a small
    floating-point tolerance so 95% of exactly 20 observations selects one.
    Ties do not expand the fixed-count tail.
    """
    ordered = sorted(values)
    tail_count = max(
        1,
        min(
            len(ordered),
            math.ceil(((1.0 - confidence) * len(ordered)) - 1e-12),
        ),
    )
    tail = ordered[:tail_count]
    threshold = tail[-1]
    var_loss = max(0.0, -threshold)
    es_loss = max(0.0, -statistics.fmean(tail))
    return var_loss, es_loss


def correlation(left: list[float], right: list[float]) -> Optional[float]:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_std = statistics.stdev(left)
    right_std = statistics.stdev(right)
    if left_std == 0.0 or right_std == 0.0:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    covariance = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    ) / (len(left) - 1)
    return covariance / (left_std * right_std)


def geometric_period_mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return (1.0 + compounded_return(values)) ** (1.0 / len(values)) - 1.0


def capture_ratio(fund: list[float], benchmark: list[float]) -> Optional[float]:
    fund_geometric = geometric_period_mean(fund)
    benchmark_geometric = geometric_period_mean(benchmark)
    return safe_ratio(fund_geometric, benchmark_geometric)


def rolling_summary(
    rows: list[Row], window: int
) -> Optional[dict[str, object]]:
    if window <= 0 or len(rows) < window:
        return None
    fund_returns: list[float] = []
    paired_excess: list[float] = []
    period_ends: list[str] = []
    for end in range(window, len(rows) + 1):
        sample = rows[end - window : end]
        fund_returns.append(compounded_return([item.fund for item in sample]))
        period_ends.append(sample[-1].date_label)
        if all(item.benchmark is not None for item in sample):
            benchmark_return = compounded_return(
                [float(item.benchmark) for item in sample]
            )
            paired_excess.append(fund_returns[-1] - benchmark_return)

    worst_index = min(range(len(fund_returns)), key=fund_returns.__getitem__)
    best_index = max(range(len(fund_returns)), key=fund_returns.__getitem__)
    return {
        "window_periods": window,
        "observations": len(fund_returns),
        "worst_return": fund_returns[worst_index],
        "worst_period_end": period_ends[worst_index],
        "median_return": statistics.median(fund_returns),
        "best_return": fund_returns[best_index],
        "best_period_end": period_ends[best_index],
        "positive_window_rate": sum(value > 0.0 for value in fund_returns)
        / len(fund_returns),
        "excess_win_rate": (
            sum(value > 0.0 for value in paired_excess) / len(paired_excess)
            if paired_excess
            else None
        ),
        "paired_excess_observations": len(paired_excess),
    }


def calculate(
    rows: list[Row],
    periods_per_year: int,
    mar_annual: float,
    confidence: float,
    rolling_window: int,
    initial_warnings: list[str],
    assume_zero_risk_free: bool = False,
) -> dict[str, object]:
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if rolling_window <= 0:
        raise ValueError("rolling_window must be positive")
    if not math.isfinite(mar_annual) or mar_annual <= -1.0:
        raise ValueError("mar_annual must be finite and greater than -1")
    if not math.isfinite(confidence) or not 0.5 < confidence < 1.0:
        raise ValueError("confidence must be between 0.5 and 1.0")
    warnings = list(initial_warnings)
    if len(rows) < 30:
        warnings.append("fewer than 30 fund observations; estimates are fragile")

    fund = [item.fund for item in rows]
    risk_free_missing = sum(item.risk_free is None for item in rows)
    risk_free: Optional[list[float]]
    if risk_free_missing == 0:
        risk_free = [float(item.risk_free) for item in rows]
        risk_free_handling = "provided_for_all_rows"
    elif assume_zero_risk_free:
        risk_free = [
            item.risk_free if item.risk_free is not None else 0.0 for item in rows
        ]
        risk_free_handling = "explicit_zero_for_missing_rows"
        warnings.append(
            f"risk_free_return missing for {risk_free_missing} fund rows; explicit zero assumption used"
        )
    else:
        risk_free = None
        risk_free_handling = "incomplete_metrics_null"
        warnings.append(
            f"risk_free_return missing for {risk_free_missing} fund rows; Sharpe is null and CAPM metrics require complete aligned risk-free data"
        )
    excess = (
        [value - rf for value, rf in zip(fund, risk_free)]
        if risk_free is not None
        else None
    )
    fund_std = sample_std(fund)
    excess_std = sample_std(excess) if excess is not None else None
    annual_volatility = (
        fund_std * math.sqrt(periods_per_year) if fund_std is not None else None
    )
    drawdown = max_drawdown(rows)
    annual_return = annualized_return(fund, periods_per_year)

    mar_period = (1.0 + mar_annual) ** (1.0 / periods_per_year) - 1.0
    downside_squared = [min(value - mar_period, 0.0) ** 2 for value in fund]
    annual_downside_deviation = math.sqrt(statistics.fmean(downside_squared)) * math.sqrt(
        periods_per_year
    )
    annual_return_above_mar = statistics.fmean(
        [value - mar_period for value in fund]
    ) * periods_per_year
    var_loss, es_loss = historical_tail(fund, confidence)

    result: dict[str, object] = {
        "metadata": {
            "start_date": rows[0].date_label,
            "end_date": rows[-1].date_label,
            "fund_observations": len(rows),
            "periods_per_year": periods_per_year,
            "return_type": "simple_total_return_assumed",
            "minimum_acceptable_return_annual": mar_annual,
            "historical_tail_confidence": confidence,
            "historical_tail_method": "nearest_rank_fixed_count_lower_tail",
            "risk_free_handling": risk_free_handling,
        },
        "absolute_metrics": {
            "total_return": compounded_return(fund),
            "annualized_return": annual_return,
            "annualized_volatility": annual_volatility,
            "annualized_downside_deviation": annual_downside_deviation,
            "sharpe_ratio": (
                safe_ratio(
                    statistics.fmean(excess) * math.sqrt(periods_per_year),
                    excess_std,
                )
                if excess is not None
                else None
            ),
            "sortino_ratio": safe_ratio(
                annual_return_above_mar, annual_downside_deviation
            ),
            "calmar_ratio": safe_ratio(annual_return, abs(float(drawdown["value"]))),
            "positive_period_rate": sum(value > 0.0 for value in fund) / len(fund),
            "historical_var_loss": var_loss,
            "historical_expected_shortfall_loss": es_loss,
            "maximum_drawdown": drawdown,
        },
        "rolling": rolling_summary(rows, rolling_window),
        "relative_metrics": None,
        "warnings": warnings,
    }

    paired = [item for item in rows if item.benchmark is not None]
    if not paired:
        warnings.append("benchmark_return absent; relative metrics not calculated")
        return result
    if len(paired) < len(rows):
        warnings.append(
            f"relative metrics use {len(paired)} of {len(rows)} rows with aligned benchmark returns"
        )
    if len(paired) < 30:
        warnings.append("fewer than 30 aligned benchmark observations; relative estimates are fragile")

    paired_fund = [item.fund for item in paired]
    benchmark = [float(item.benchmark) for item in paired]
    active = [left - right for left, right in zip(paired_fund, benchmark)]
    tracking_std = sample_std(active)
    tracking_error = (
        tracking_std * math.sqrt(periods_per_year)
        if tracking_std is not None
        else None
    )

    beta: Optional[float] = None
    alpha_annual: Optional[float] = None
    paired_rf_missing = sum(item.risk_free is None for item in paired)
    if paired_rf_missing == 0 or assume_zero_risk_free:
        paired_rf = [
            item.risk_free if item.risk_free is not None else 0.0 for item in paired
        ]
        benchmark_excess = [value - rf for value, rf in zip(benchmark, paired_rf)]
        fund_excess = [value - rf for value, rf in zip(paired_fund, paired_rf)]
        benchmark_variance = (
            statistics.variance(benchmark_excess) if len(paired) >= 2 else None
        )
        if benchmark_variance is not None and benchmark_variance > 0.0:
            fund_mean = statistics.fmean(fund_excess)
            benchmark_mean = statistics.fmean(benchmark_excess)
            covariance = sum(
                (x - fund_mean) * (y - benchmark_mean)
                for x, y in zip(fund_excess, benchmark_excess)
            ) / (len(paired) - 1)
            beta = covariance / benchmark_variance
            alpha_annual = (
                statistics.fmean(fund_excess)
                - beta * statistics.fmean(benchmark_excess)
            ) * periods_per_year
    else:
        warnings.append(
            f"CAPM alpha/beta are null because risk_free_return is missing for {paired_rf_missing} aligned benchmark rows"
        )

    up_pairs = [(f, b) for f, b in zip(paired_fund, benchmark) if b > 0.0]
    down_pairs = [(f, b) for f, b in zip(paired_fund, benchmark) if b < 0.0]
    fund_annual = annualized_return(paired_fund, periods_per_year)
    benchmark_annual = annualized_return(benchmark, periods_per_year)

    result["relative_metrics"] = {
        "aligned_observations": len(paired),
        "fund_annualized_return_aligned": fund_annual,
        "benchmark_annualized_return": benchmark_annual,
        "annualized_return_difference": (
            fund_annual - benchmark_annual
            if fund_annual is not None and benchmark_annual is not None
            else None
        ),
        "annualized_arithmetic_active_return": statistics.fmean(active)
        * periods_per_year,
        "tracking_error": tracking_error,
        "information_ratio": safe_ratio(
            statistics.fmean(active) * periods_per_year, tracking_error
        ),
        "beta": beta,
        "alpha_annual_arithmetic": alpha_annual,
        "correlation": correlation(paired_fund, benchmark),
        "excess_win_rate": sum(value > 0.0 for value in active) / len(active),
        "up_capture_ratio": capture_ratio(
            [item[0] for item in up_pairs], [item[1] for item in up_pairs]
        ),
        "up_periods": len(up_pairs),
        "down_capture_ratio": capture_ratio(
            [item[0] for item in down_pairs], [item[1] for item in down_pairs]
        ),
        "down_periods": len(down_pairs),
    }
    return result


def round_floats(value: object) -> object:
    if isinstance(value, float):
        return round(value, 10) if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: round_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [round_floats(item) for item in value]
    return value


def run_self_test() -> None:
    rows = [
        Row("2026-01-01", date(2026, 1, 1), 0.10, 0.08, 0.0),
        Row("2026-01-02", date(2026, 1, 2), -0.05, -0.04, 0.0),
        Row("2026-01-03", date(2026, 1, 3), 0.02, 0.01, 0.0),
        Row("2026-01-04", date(2026, 1, 4), 0.01, 0.00, 0.0),
    ]
    result = calculate(rows, 252, 0.0, 0.95, 2, [])
    expected_total = 1.10 * 0.95 * 1.02 * 1.01 - 1.0
    actual_total = float(result["absolute_metrics"]["total_return"])
    assert abs(actual_total - expected_total) < 1e-12
    assert result["relative_metrics"]["aligned_observations"] == 4
    assert result["rolling"]["observations"] == 3
    actual_drawdown = float(result["absolute_metrics"]["maximum_drawdown"]["value"])
    assert abs(actual_drawdown - (-0.05)) < 1e-12

    tail_values = [-value / 100.0 for value in range(1, 21)]
    var_loss, es_loss = historical_tail(tail_values, 0.95)
    assert abs(var_loss - 0.20) < 1e-12
    assert abs(es_loss - 0.20) < 1e-12

    missing_rf_rows = [
        Row("2026-01-01", date(2026, 1, 1), 0.01, 0.005, None),
        Row("2026-01-02", date(2026, 1, 2), 0.02, 0.010, None),
    ]
    missing_rf_result = calculate(missing_rf_rows, 252, 0.0, 0.95, 2, [])
    assert missing_rf_result["absolute_metrics"]["sharpe_ratio"] is None
    assert missing_rf_result["relative_metrics"]["beta"] is None
    assert missing_rf_result["relative_metrics"]["alpha_annual_arithmetic"] is None

    first_loss_rows = [
        Row("2026-01-01", date(2026, 1, 1), -0.10, None, None),
        Row("2026-01-02", date(2026, 1, 2), 0.05, None, None),
    ]
    assert max_drawdown(first_loss_rows)["peak_date"] == "SAMPLE_START"

    for invalid_mar in (-1.0, -2.0, float("nan"), float("inf")):
        try:
            calculate(rows, 252, invalid_mar, 0.95, 2, [])
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid MAR accepted: {invalid_mar}")

    try:
        calculate(rows, 252, 0.0, 0.95, 0, [])
    except ValueError:
        pass
    else:
        raise AssertionError("zero rolling window accepted")
    print("self-test passed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="UTF-8 CSV of aligned periodic returns")
    parser.add_argument("--output", type=Path, help="optional JSON output path")
    parser.add_argument("--periods-per-year", type=int, default=252)
    parser.add_argument("--rolling-window", type=int, default=None)
    parser.add_argument("--mar-annual", type=float, default=0.0)
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--returns-in-percent", action="store_true")
    parser.add_argument(
        "--assume-zero-risk-free",
        action="store_true",
        help="explicitly fill missing risk_free_return observations with zero",
    )
    parser.add_argument("--self-test", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        run_self_test()
        return 0
    if args.input is None:
        raise ValueError("--input is required unless --self-test is used")
    rows, warnings = load_rows(args.input, args.returns_in_percent)
    rolling_window = (
        args.rolling_window
        if args.rolling_window is not None
        else args.periods_per_year
    )
    result = calculate(
        rows,
        args.periods_per_year,
        args.mar_annual,
        args.confidence,
        rolling_window,
        warnings,
        args.assume_zero_risk_free,
    )
    payload = json.dumps(round_floats(result), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, csv.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
