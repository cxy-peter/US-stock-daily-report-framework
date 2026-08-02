"""Plan and execute due prediction-ledger settlements safely.

The scheduler is deliberately separated from data collection and from the
append-only PredictionLedger. It determines which 1/5/20/60-session outcomes are
due, requires a complete accepted-close path, enforces factor-model-version
lineage, and creates stable idempotency keys. A caller may pass a settlement
callback; no broker or portfolio action is available here.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence


_HORIZONS = (1, 5, 20, 60)


class SettlementSchedulerError(ValueError):
    """Raised when a point-in-time settlement plan is internally inconsistent."""


def _date(value: dt.date | str, name: str) -> dt.date:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise SettlementSchedulerError(f"{name} must be an ISO date") from exc


def _aware(value: dt.datetime | str, name: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        result = value
    else:
        try:
            result = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise SettlementSchedulerError(f"{name} must be an ISO date-time") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise SettlementSchedulerError(f"{name} must be timezone-aware")
    return result.astimezone(dt.timezone.utc)


def _safe_id(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256:
        raise SettlementSchedulerError(f"{name} must be a non-empty identifier")
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-" for character in text):
        raise SettlementSchedulerError(f"{name} contains unsupported characters")
    return text


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AcceptedCloseReference:
    symbol: str
    session: dt.date | str
    accepted_close_id: str
    accepted_at: dt.datetime | str

    def __post_init__(self) -> None:
        symbol = str(self.symbol).strip().upper()
        if not symbol:
            raise SettlementSchedulerError("symbol is required")
        close_id = str(self.accepted_close_id).strip().casefold()
        if len(close_id) != 64 or any(character not in "0123456789abcdef" for character in close_id):
            raise SettlementSchedulerError("accepted_close_id must be lowercase SHA-256")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "session", _date(self.session, "session"))
        object.__setattr__(self, "accepted_close_id", close_id)
        object.__setattr__(self, "accepted_at", _aware(self.accepted_at, "accepted_at"))


@dataclass(frozen=True)
class FactorResidualReference:
    signal_id: str
    horizon_sessions: int
    target_session: dt.date | str
    factor_model_version: str
    evidence_id: str
    available_at: dt.datetime | str

    def __post_init__(self) -> None:
        signal_id = _safe_id(self.signal_id, "signal_id")
        horizon = int(self.horizon_sessions)
        if horizon not in _HORIZONS:
            raise SettlementSchedulerError("horizon_sessions must be 1, 5, 20, or 60")
        factor_version = _safe_id(self.factor_model_version, "factor_model_version").casefold()
        evidence_id = str(self.evidence_id).strip().casefold()
        if len(evidence_id) != 64 or any(character not in "0123456789abcdef" for character in evidence_id):
            raise SettlementSchedulerError("evidence_id must be lowercase SHA-256")
        object.__setattr__(self, "signal_id", signal_id)
        object.__setattr__(self, "horizon_sessions", horizon)
        object.__setattr__(self, "target_session", _date(self.target_session, "target_session"))
        object.__setattr__(self, "factor_model_version", factor_version)
        object.__setattr__(self, "evidence_id", evidence_id)
        object.__setattr__(self, "available_at", _aware(self.available_at, "available_at"))


@dataclass(frozen=True)
class SignalSettlementState:
    signal_id: str
    valuation_symbol: str
    observation_session: dt.date | str
    first_observed_at: dt.datetime | str
    session_path: Mapping[int, dt.date | str]
    signal_model_version: str
    factor_model_version: str | None = None
    settled_horizons: Sequence[int] = field(default_factory=tuple)
    reversed: bool = False
    calibration_eligible: bool = True
    require_factor_residual: bool = False

    def __post_init__(self) -> None:
        signal_id = _safe_id(self.signal_id, "signal_id")
        symbol = str(self.valuation_symbol).strip().upper()
        if not symbol:
            raise SettlementSchedulerError("valuation_symbol is required")
        observation_session = _date(self.observation_session, "observation_session")
        first_observed_at = _aware(self.first_observed_at, "first_observed_at")
        normalized_path: dict[int, dt.date] = {}
        for raw_offset, raw_session in self.session_path.items():
            offset = int(raw_offset)
            if offset < 1 or offset > max(_HORIZONS):
                raise SettlementSchedulerError("session_path offset is outside 1..60")
            normalized_path[offset] = _date(raw_session, f"session_path[{offset}]")
        if tuple(sorted(normalized_path)) != tuple(range(1, max(_HORIZONS) + 1)):
            raise SettlementSchedulerError("session_path must cover every offset from 1 through 60")
        prior = observation_session
        for offset in range(1, max(_HORIZONS) + 1):
            current = normalized_path[offset]
            if current <= prior:
                raise SettlementSchedulerError("session_path must be strictly increasing")
            prior = current
        settled = tuple(sorted({int(item) for item in self.settled_horizons}))
        if not set(settled).issubset(_HORIZONS):
            raise SettlementSchedulerError("settled_horizons contains unsupported horizon")
        factor_version = (
            None
            if self.factor_model_version in (None, "")
            else _safe_id(self.factor_model_version, "factor_model_version").casefold()
        )
        if self.require_factor_residual and factor_version is None:
            raise SettlementSchedulerError("require_factor_residual needs factor_model_version")
        if not isinstance(self.reversed, bool) or not isinstance(self.calibration_eligible, bool):
            raise SettlementSchedulerError("reversed and calibration_eligible must be boolean")
        object.__setattr__(self, "signal_id", signal_id)
        object.__setattr__(self, "valuation_symbol", symbol)
        object.__setattr__(self, "observation_session", observation_session)
        object.__setattr__(self, "first_observed_at", first_observed_at)
        object.__setattr__(self, "session_path", dict(sorted(normalized_path.items())))
        object.__setattr__(self, "signal_model_version", _safe_id(self.signal_model_version, "signal_model_version").casefold())
        object.__setattr__(self, "factor_model_version", factor_version)
        object.__setattr__(self, "settled_horizons", settled)


@dataclass(frozen=True)
class SettlementTask:
    signal_id: str
    horizon_sessions: int
    valuation_symbol: str
    target_session: str
    close_path_ids: tuple[str, ...]
    final_close_id: str
    factor_model_version: str | None
    factor_residual_evidence_id: str | None
    calibration_eligible: bool
    idempotency_key: str


@dataclass(frozen=True)
class SettlementBlock:
    signal_id: str
    horizon_sessions: int
    target_session: str
    reason_code: str
    detail: str


@dataclass(frozen=True)
class SettlementPlan:
    as_of: str
    task_count: int
    blocked_count: int
    tasks: tuple[SettlementTask, ...]
    blocked: tuple[SettlementBlock, ...]
    automatic_trading_permitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "task_count": self.task_count,
            "blocked_count": self.blocked_count,
            "tasks": [item.__dict__ for item in self.tasks],
            "blocked": [item.__dict__ for item in self.blocked],
            "automatic_trading_permitted": False,
        }


@dataclass(frozen=True)
class SettlementExecutionReceipt:
    signal_id: str
    horizon_sessions: int
    status: str
    idempotency_key: str
    external_receipt: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class SettlementExecutionResult:
    attempted_count: int
    succeeded_count: int
    failed_count: int
    receipts: tuple[SettlementExecutionReceipt, ...]
    automatic_trading_permitted: bool = False


def build_settlement_plan(
    signals: Iterable[SignalSettlementState],
    accepted_closes: Iterable[AcceptedCloseReference],
    factor_residuals: Iterable[FactorResidualReference] = (),
    *,
    as_of: dt.datetime | str,
) -> SettlementPlan:
    """Plan every due unsettled horizon using complete point-in-time evidence."""

    cutoff = _aware(as_of, "as_of")
    signal_rows = list(signals)
    close_rows = [item for item in accepted_closes if item.accepted_at <= cutoff]
    residual_rows = [item for item in factor_residuals if item.available_at <= cutoff]
    close_index: dict[tuple[str, dt.date], AcceptedCloseReference] = {}
    for item in close_rows:
        key = (item.symbol, item.session)
        prior = close_index.get(key)
        if prior is not None and prior.accepted_close_id != item.accepted_close_id:
            raise SettlementSchedulerError("accepted-close index contains a conflicting session")
        close_index[key] = item
    residual_index: dict[tuple[str, int], FactorResidualReference] = {}
    for item in residual_rows:
        key = (item.signal_id, item.horizon_sessions)
        prior = residual_index.get(key)
        if prior is not None and prior.evidence_id != item.evidence_id:
            raise SettlementSchedulerError("factor residual index contains conflicting evidence")
        residual_index[key] = item

    tasks: list[SettlementTask] = []
    blocked: list[SettlementBlock] = []
    seen_signal_ids: set[str] = set()
    for signal in signal_rows:
        if signal.signal_id in seen_signal_ids:
            raise SettlementSchedulerError("duplicate signal_id")
        seen_signal_ids.add(signal.signal_id)
        if signal.reversed:
            continue
        for horizon in _HORIZONS:
            if horizon in signal.settled_horizons:
                continue
            target = signal.session_path[horizon]
            if target > cutoff.date():
                continue
            required_sessions = [signal.session_path[offset] for offset in range(1, horizon + 1)]
            missing_sessions = [
                session
                for session in required_sessions
                if (signal.valuation_symbol, session) not in close_index
            ]
            if missing_sessions:
                blocked.append(
                    SettlementBlock(
                        signal_id=signal.signal_id,
                        horizon_sessions=horizon,
                        target_session=target.isoformat(),
                        reason_code="accepted_close_path_incomplete",
                        detail=f"missing_sessions={len(missing_sessions)}",
                    )
                )
                continue
            path = [close_index[(signal.valuation_symbol, session)] for session in required_sessions]
            if any(item.accepted_at < signal.first_observed_at for item in path):
                blocked.append(
                    SettlementBlock(
                        signal_id=signal.signal_id,
                        horizon_sessions=horizon,
                        target_session=target.isoformat(),
                        reason_code="close_available_before_signal",
                        detail="accepted-close ordering violates the signal timestamp",
                    )
                )
                continue

            residual = residual_index.get((signal.signal_id, horizon))
            if residual is not None:
                if residual.target_session != target:
                    blocked.append(
                        SettlementBlock(
                            signal_id=signal.signal_id,
                            horizon_sessions=horizon,
                            target_session=target.isoformat(),
                            reason_code="factor_residual_target_mismatch",
                            detail="factor residual target session differs from signal path",
                        )
                    )
                    continue
                if signal.factor_model_version != residual.factor_model_version:
                    blocked.append(
                        SettlementBlock(
                            signal_id=signal.signal_id,
                            horizon_sessions=horizon,
                            target_session=target.isoformat(),
                            reason_code="factor_model_version_mismatch",
                            detail="factor residual cannot be substituted across model versions",
                        )
                    )
                    continue
                if residual.available_at < path[-1].accepted_at:
                    blocked.append(
                        SettlementBlock(
                            signal_id=signal.signal_id,
                            horizon_sessions=horizon,
                            target_session=target.isoformat(),
                            reason_code="factor_residual_point_in_time_violation",
                            detail="residual was timestamped before the final close became available",
                        )
                    )
                    continue
            elif signal.require_factor_residual:
                blocked.append(
                    SettlementBlock(
                        signal_id=signal.signal_id,
                        horizon_sessions=horizon,
                        target_session=target.isoformat(),
                        reason_code="factor_residual_missing",
                        detail="signal contract requires exact-version residual evidence",
                    )
                )
                continue

            identity = {
                "signal_id": signal.signal_id,
                "horizon_sessions": horizon,
                "target_session": target.isoformat(),
                "close_path_ids": [item.accepted_close_id for item in path],
                "factor_model_version": signal.factor_model_version,
                "factor_residual_evidence_id": None if residual is None else residual.evidence_id,
            }
            canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
            idempotency_key = f"prediction-settlement:{_digest(canonical)}"
            tasks.append(
                SettlementTask(
                    signal_id=signal.signal_id,
                    horizon_sessions=horizon,
                    valuation_symbol=signal.valuation_symbol,
                    target_session=target.isoformat(),
                    close_path_ids=tuple(item.accepted_close_id for item in path),
                    final_close_id=path[-1].accepted_close_id,
                    factor_model_version=signal.factor_model_version,
                    factor_residual_evidence_id=None if residual is None else residual.evidence_id,
                    calibration_eligible=signal.calibration_eligible,
                    idempotency_key=idempotency_key,
                )
            )

    tasks.sort(key=lambda item: (item.target_session, item.signal_id, item.horizon_sessions))
    blocked.sort(key=lambda item: (item.target_session, item.signal_id, item.horizon_sessions))
    return SettlementPlan(
        as_of=cutoff.isoformat().replace("+00:00", "Z"),
        task_count=len(tasks),
        blocked_count=len(blocked),
        tasks=tuple(tasks),
        blocked=tuple(blocked),
    )


def execute_settlement_plan(
    plan: SettlementPlan,
    settle_callback: Callable[[SettlementTask], Any],
    *,
    continue_on_error: bool = True,
) -> SettlementExecutionResult:
    """Execute a due plan through an injected idempotent ledger callback."""

    receipts: list[SettlementExecutionReceipt] = []
    for task in plan.tasks:
        try:
            external = settle_callback(task)
            if external is None:
                external_receipt = None
            elif isinstance(external, str):
                external_receipt = external
            else:
                external_receipt = str(getattr(external, "event_id", external))
            receipts.append(
                SettlementExecutionReceipt(
                    signal_id=task.signal_id,
                    horizon_sessions=task.horizon_sessions,
                    status="settled_or_idempotent_replay",
                    idempotency_key=task.idempotency_key,
                    external_receipt=external_receipt,
                )
            )
        except Exception as exc:  # caller controls the ledger exception taxonomy
            receipts.append(
                SettlementExecutionReceipt(
                    signal_id=task.signal_id,
                    horizon_sessions=task.horizon_sessions,
                    status="failed",
                    idempotency_key=task.idempotency_key,
                    error_code=type(exc).__name__,
                )
            )
            if not continue_on_error:
                break
    succeeded = sum(item.status != "failed" for item in receipts)
    failed = len(receipts) - succeeded
    return SettlementExecutionResult(
        attempted_count=len(receipts),
        succeeded_count=succeeded,
        failed_count=failed,
        receipts=tuple(receipts),
    )


__all__ = [
    "AcceptedCloseReference",
    "FactorResidualReference",
    "SettlementBlock",
    "SettlementExecutionReceipt",
    "SettlementExecutionResult",
    "SettlementPlan",
    "SettlementSchedulerError",
    "SettlementTask",
    "SignalSettlementState",
    "build_settlement_plan",
    "execute_settlement_plan",
]
