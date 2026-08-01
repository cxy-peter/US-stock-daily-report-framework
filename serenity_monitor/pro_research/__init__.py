"""Optional advanced research models and one-report orchestration."""

from .barra import AssetFactorFit, BarraProxyResult, fit_barra_proxy
from .daily import DcaProposal, ProDailyReport, build_pro_daily_report, render_pro_daily_markdown
from .io import LoadedProInputs, demo_inputs, load_inputs_from_config, load_pro_config
from .kalman import KalmanExposureResult, kalman_dynamic_exposures
from .manager_skill import ManagerFragility, ManagerSkillResult, evaluate_manager_skill
from .policy import PolicyEvent, TrumpPolicyIndexResult, compute_trump_policy_index
from .polymarket import PolymarketStudyResult, ResolvedMarketEvent, study_resolved_markets

__all__ = [
    "AssetFactorFit",
    "BarraProxyResult",
    "DcaProposal",
    "KalmanExposureResult",
    "LoadedProInputs",
    "ManagerFragility",
    "ManagerSkillResult",
    "PolicyEvent",
    "PolymarketStudyResult",
    "ProDailyReport",
    "ResolvedMarketEvent",
    "TrumpPolicyIndexResult",
    "build_pro_daily_report",
    "compute_trump_policy_index",
    "demo_inputs",
    "evaluate_manager_skill",
    "fit_barra_proxy",
    "kalman_dynamic_exposures",
    "load_inputs_from_config",
    "load_pro_config",
    "render_pro_daily_markdown",
    "study_resolved_markets",
]
