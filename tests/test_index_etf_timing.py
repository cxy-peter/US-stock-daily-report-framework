from __future__ import annotations

import numpy as np
import pandas as pd

from serenity_monitor.index_etf_timing import (
    analyze_index_etf_timing,
    render_index_timing_markdown,
)


def _frame(values: np.ndarray, symbol: str = "SPY") -> pd.DataFrame:
    return pd.DataFrame(
        {symbol: values},
        index=pd.bdate_range("2024-01-02", periods=len(values)),
    )


def test_index_timing_uses_staged_pullback_add_and_blocks_single_names():
    base = np.linspace(100.0, 160.0, 330)
    pullback = np.linspace(160.0, 147.0, 20)
    prices = _frame(np.concatenate([base, pullback]))
    result = analyze_index_etf_timing(
        prices,
        ["VOO", "MU"],
        risk_budget=0.92,
        current_weights={"VOO": 0.20, "MU": 0.08},
        tactical_sleeve_nav=0.15,
        tranche_nav=0.025,
    )
    by_symbol = {row.symbol: row for row in result.signals}
    assert by_symbol["VOO"].role == "core_index"
    assert by_symbol["VOO"].action in {"TACTICAL_ADD_1", "TACTICAL_ADD_2"}
    assert 0 < by_symbol["VOO"].tactical_add_nav_fraction <= 0.05
    assert by_symbol["MU"].action == "NO_NEW_CAPITAL"
    assert by_symbol["MU"].tactical_add_nav_fraction == 0
    assert not result.automatic_trading_permitted


def test_broken_long_term_trend_pauses_add_but_does_not_sell_core():
    prices = _frame(np.linspace(180.0, 90.0, 350))
    result = analyze_index_etf_timing(
        prices,
        ["VOO"],
        risk_budget=0.90,
    )
    signal = result.signals[0]
    assert signal.regime == "downtrend"
    assert signal.action == "PAUSE_ADD"
    assert signal.tactical_trim_nav_fraction == 0
    assert "strategic" in result.warnings[0].casefold()


def test_tactical_trim_requires_explicit_target_and_only_trims_excess():
    first = np.linspace(100.0, 125.0, 280)
    # Alternate small pauses with a strong final extension so RSI is high but
    # not the degenerate all-gain case.
    tail = np.array(
        [125, 126, 126.2, 127, 126.8, 128, 129, 129.2, 130.5, 131,
         132, 131.8, 133.5, 134, 135.5, 136, 137.5, 138, 140, 142,
         144, 146, 148, 150, 152, 154, 156, 158, 160, 162],
        dtype=float,
    )
    prices = _frame(np.concatenate([first, tail]))
    without_target = analyze_index_etf_timing(
        prices,
        ["VOO"],
        risk_budget=1.0,
        current_weights={"VOO": 0.40},
    ).signals[0]
    with_target = analyze_index_etf_timing(
        prices,
        ["VOO"],
        risk_budget=1.0,
        current_weights={"VOO": 0.40},
        target_weights={"VOO": 0.25},
    ).signals[0]
    assert without_target.action != "TRIM_TACTICAL_ONLY"
    assert with_target.action == "TRIM_TACTICAL_ONLY"
    assert 0 < with_target.tactical_trim_nav_fraction <= 0.15


def test_render_explains_core_and_tactical_sleeve():
    result = analyze_index_etf_timing(
        _frame(np.linspace(100.0, 150.0, 350)),
        ["VOO", "BOXX", "GLDM", "SPCX"],
        risk_budget=0.95,
    )
    markdown = render_index_timing_markdown(result)
    assert "指数 ETF 择时" in markdown
    assert "核心仓长期持有" in markdown
    assert "RESERVE_HOLD" in markdown
    assert "DIVERSIFIER_HOLD" in markdown
    assert "NO_NEW_CAPITAL" in markdown
