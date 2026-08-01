"""Load source credibility profiles without silently awarding missing trust."""
from __future__ import annotations

from pathlib import Path

import yaml

from .credibility import Disclosure, SourceProfile, SourceType, TrackRecord


def _disclosure(value: object) -> Disclosure:
    try:
        return Disclosure(str(value or "unknown"))
    except ValueError:
        return Disclosure.UNKNOWN


def _source_type(value: object) -> SourceType:
    try:
        return SourceType(str(value or "anonymous"))
    except ValueError:
        return SourceType.ANONYMOUS


def _profile(source_id: str, raw: dict) -> SourceProfile:
    track = raw.get("track_record") or {}
    return SourceProfile(
        source_id=source_id,
        label=str(raw.get("label") or source_id),
        source_type=_source_type(raw.get("source_type")),
        independence_group=str(raw.get("independence_group") or source_id),
        identity_verified=bool(raw.get("identity_verified", False)),
        regulated_entity=bool(raw.get("regulated_entity", False)),
        audited_performance=bool(raw.get("audited_performance", False)),
        position_disclosure=_disclosure(raw.get("position_disclosure")),
        conflict_disclosure=_disclosure(raw.get("conflict_disclosure")),
        leverage_disclosure=_disclosure(raw.get("leverage_disclosure")),
        track_record=TrackRecord(
            observations=int(track.get("observations", 0) or 0),
            hits=int(track.get("hits", 0) or 0),
            brier_score=_optional_float(track.get("brier_score")),
            mean_excess_return=_optional_float(track.get("mean_excess_return")),
            worst_mae=_optional_float(track.get("worst_mae")),
        ),
        paid_promotion_risk=float(raw.get("paid_promotion_risk", 0.0) or 0.0),
        legal_or_compliance_flags=int(raw.get("legal_or_compliance_flags", 0) or 0),
        fund_age_months=_optional_int(raw.get("fund_age_months")),
        aum_usd=_optional_float(raw.get("aum_usd")),
        reported_gross_leverage=_optional_float(raw.get("reported_gross_leverage")),
        top10_concentration=_optional_float(raw.get("top10_concentration")),
        estimated_liquidity_days=_optional_float(raw.get("estimated_liquidity_days")),
        prime_broker_concentration=_optional_float(raw.get("prime_broker_concentration")),
    )


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def default_source_profiles() -> dict[str, SourceProfile]:
    defaults = {
        "official_company_filing": {
            "label": "Company filing / SEC EDGAR",
            "source_type": "primary_document",
            "independence_group": "company_primary",
            "identity_verified": True,
            "regulated_entity": True,
            "audited_performance": True,
            "position_disclosure": "always",
            "conflict_disclosure": "always",
            "leverage_disclosure": "always",
        },
        "financial_news": {
            "label": "Financial news / publisher",
            "source_type": "regulated_institution",
            "independence_group": "financial_news",
            "identity_verified": True,
            "regulated_entity": False,
            "audited_performance": False,
            "position_disclosure": "unknown",
            "conflict_disclosure": "sometimes",
            "leverage_disclosure": "unknown",
        },
        "anonymous_social": {
            "label": "Unverified social/community source",
            "source_type": "anonymous",
            "independence_group": "unverified_social",
            "identity_verified": False,
            "regulated_entity": False,
            "audited_performance": False,
            "position_disclosure": "unknown",
            "conflict_disclosure": "unknown",
            "leverage_disclosure": "unknown",
        },
    }
    return {source_id: _profile(source_id, raw) for source_id, raw in defaults.items()}


def load_source_profiles(path: str | Path | None) -> dict[str, SourceProfile]:
    profiles = default_source_profiles()
    if not path:
        return profiles
    target = Path(path)
    if not target.exists():
        return profiles
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    for source_id, values in (raw.get("profiles") or {}).items():
        if isinstance(values, dict):
            profiles[str(source_id)] = _profile(str(source_id), values)
    return profiles
