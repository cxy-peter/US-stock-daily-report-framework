from serenity_monitor.rules import ResearchAction
from serenity_monitor.sizing import PortfolioAction, PositionPlan
from serenity_monitor.state import compare_state


def test_state_detects_action_change():
    previous = {"plans": {"A": {"action": "继续持有", "model_delta_usd": 0}}}
    plan = PositionPlan(
        ticker="A", name="A", research_action=ResearchAction.HOLD,
        action=PortfolioAction.REBALANCE, current_shares=10, current_price=100,
        current_value=1000, current_weight=0.2, target_weight=0.1,
        adjusted_max_weight=0.1, model_delta_usd=-500,
        executable_delta_usd=-500, trade_shares=-5, avg_correlation=0.5,
        volatility_multiplier=1, correlation_multiplier=1, regime_multiplier=1,
        confidence=80, reasons=[], constraints=[]
    )
    changes = compare_state(previous, [plan])
    assert len(changes) == 1
    assert changes[0].current_action == "风险再平衡"
