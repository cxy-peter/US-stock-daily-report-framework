"""Stable JSON contract for one private, owner-only daily report.

The JSON document is the machine contract.  Renderers may derive Markdown from
an already validated document, but must not add accounting or delivery facts.
No broker target, account identifier, chat identifier, or even its hash is
stored in the document.  A target hash is accepted only while deriving the
opaque ``delivery_id``.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator, FormatChecker


SCHEMA_VERSION = "private_daily_report/v1.0.0"
JSON_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "schemas"
    / "private_daily_report.v1.schema.json"
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPORT_DECIMAL_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
_SAFE_CHANNEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_FORBIDDEN_TARGET_KEYS = frozenset(
    {
        "target",
        "target_id",
        "target_key",
        "target_key_sha256",
        "delivery_target",
        "chat_id",
        "thread_id",
    }
)


class PrivateDailyReportError(ValueError):
    """Base class for private-report contract failures."""


class PrivateDailyReportCanonicalizationError(PrivateDailyReportError):
    """Raised when a value cannot enter deterministic JSON."""


class PrivateDailyReportSchemaError(PrivateDailyReportError):
    """Raised when the JSON document violates the versioned schema."""


class PrivateDailyReportSemanticError(PrivateDailyReportError):
    """Raised when individually valid fields make a false combined claim."""


class PrivateDailyReportIdentityError(PrivateDailyReportSemanticError):
    """Raised when a content or delivery identity does not verify."""


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise PrivateDailyReportCanonicalizationError(
            "report decimals must be finite"
        )
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _json_value(value: Any, *, path: str = "$") -> Any:
    """Return a detached JSON value while canonicalizing Decimal instances."""

    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, float):
        raise PrivateDailyReportCanonicalizationError(
            f"{path} must not contain binary floating point"
        )
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PrivateDailyReportCanonicalizationError(
                    f"{path} contains a non-string object key"
                )
            if key in result:
                raise PrivateDailyReportCanonicalizationError(
                    f"{path} contains a duplicate object key"
                )
            result[key] = _json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise PrivateDailyReportCanonicalizationError(
        f"{path} contains unsupported type {type(value).__name__}"
    )


def canonical_json(value: Any) -> str:
    """Serialize deterministic UTF-8 JSON, rejecting every Python float.

    :class:`~decimal.Decimal` values are emitted as canonical, non-exponent
    JSON strings.  This keeps the accounting representation identical before
    and after a JSON round trip.
    """

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compute_target_key_sha256(target_key: str) -> str:
    """Hash a private delivery target without retaining or returning it."""

    if not isinstance(target_key, str) or not target_key:
        raise PrivateDailyReportIdentityError("target_key must be a non-empty string")
    return _sha256_text(target_key)


def compute_delivery_id(
    *,
    delivery_date: dt.date | str,
    timezone: str,
    channel: str,
    target_key_sha256: str,
    schema_version: str = SCHEMA_VERSION,
) -> str:
    """Derive a stable same-day delivery identity from private routing data.

    ``target_key_sha256`` is used only as hash input.  It must remain in the
    private outbox and is deliberately absent from the report schema.
    """

    if schema_version != SCHEMA_VERSION:
        raise PrivateDailyReportIdentityError("unsupported schema_version")
    date_text = delivery_date.isoformat() if isinstance(delivery_date, dt.date) else str(delivery_date)
    try:
        parsed_date = dt.date.fromisoformat(date_text)
    except ValueError as exc:
        raise PrivateDailyReportIdentityError(
            "delivery_date must be ISO YYYY-MM-DD"
        ) from exc
    timezone_text = str(timezone).strip()
    try:
        ZoneInfo(timezone_text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PrivateDailyReportIdentityError("timezone must be an IANA timezone") from exc
    channel_text = str(channel).strip()
    if not _SAFE_CHANNEL.fullmatch(channel_text):
        raise PrivateDailyReportIdentityError("channel is not a safe identifier")
    target_hash = str(target_key_sha256).strip()
    if not _SHA256.fullmatch(target_hash):
        raise PrivateDailyReportIdentityError(
            "target_key_sha256 must be a lowercase SHA-256 digest"
        )
    identity = {
        "channel": channel_text,
        "delivery_date": parsed_date.isoformat(),
        "schema_version": schema_version,
        "target_key_sha256": target_hash,
        "timezone": timezone_text,
    }
    return _sha256_text(canonical_json(identity))


build_delivery_id = compute_delivery_id
compute_delivery_identity = compute_delivery_id


def compute_report_id(report: Mapping[str, Any]) -> str:
    """Hash canonical report content after removing only top-level report_id."""

    if not isinstance(report, Mapping):
        raise PrivateDailyReportIdentityError("report must be an object")
    content = _json_value(report)
    content.pop("report_id", None)
    return _sha256_text(canonical_json(content))


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PrivateDailyReportSchemaError(
            f"cannot load private daily report schema: {SCHEMA_PATH.name}"
        ) from exc
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate_schema(report: dict[str, Any]) -> None:
    errors = sorted(
        _validator().iter_errors(report),
        key=lambda item: ([str(part) for part in item.absolute_path], item.message),
    )
    if errors:
        error = errors[0]
        path = "$"
        for part in error.absolute_path:
            path += f"[{part}]" if isinstance(part, int) else f".{part}"
        raise PrivateDailyReportSchemaError(f"{path}: {error.message}")


def _utc_z_date_time(value: str, field_name: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PrivateDailyReportSemanticError(
            f"{field_name} must be RFC3339 UTC with a Z suffix"
        )
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrivateDailyReportSemanticError(
            f"{field_name} must be RFC3339 UTC with a Z suffix"
        ) from exc
    if result.tzinfo is None or result.utcoffset() != dt.timedelta(0):
        raise PrivateDailyReportSemanticError(
            f"{field_name} must be RFC3339 UTC with a Z suffix"
        )
    return result


def _require_sorted_unique(
    values: list[dict[str, Any]],
    key: str,
    field_name: str,
) -> None:
    observed = [str(item[key]) for item in values]
    if observed != sorted(observed):
        raise PrivateDailyReportSemanticError(
            f"{field_name} must be sorted by {key}"
        )
    if len(observed) != len(set(observed)):
        raise PrivateDailyReportSemanticError(
            f"{field_name} contains duplicate {key} values"
        )


def _require_sorted_unique_scalars(values: list[str], field_name: str) -> None:
    if values != sorted(values):
        raise PrivateDailyReportSemanticError(f"{field_name} must be sorted")
    if len(values) != len(set(values)):
        raise PrivateDailyReportSemanticError(
            f"{field_name} contains duplicate values"
        )


def _require_no_delivery_target(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in _FORBIDDEN_TARGET_KEYS:
                raise PrivateDailyReportSemanticError(
                    f"{path}.{key} must not appear in a report"
                )
            _require_no_delivery_target(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _require_no_delivery_target(item, path=f"{path}[{index}]")


def _validate_dca(report: dict[str, Any], session_results: list[dict[str, Any]]) -> None:
    items = report["dca"]["items"]
    _require_sorted_unique(items, "symbol", "dca.items")
    expected_sessions = [item["session_date"] for item in session_results]
    result_by_session = {item["session_date"]: item for item in session_results}
    # Keep every arithmetic decision independent of the caller's ambient
    # Decimal precision, as the ledger does for settlement and valuation.
    with localcontext(_REPORT_DECIMAL_CONTEXT):
        for item in items:
            symbol = item["symbol"]
            configured = Decimal(item["configured"]["amount"])
            _require_sorted_unique_scalars(
                item["proposed"]["rationale_codes"],
                f"dca {symbol} proposed.rationale_codes",
            )
            modeled = item["modeled"]
            sessions = modeled["sessions"]
            _require_sorted_unique(
                sessions,
                "session_date",
                f"dca {symbol} modeled.sessions",
            )
            modeled_sessions = [entry["session_date"] for entry in sessions]
            if modeled_sessions != expected_sessions:
                raise PrivateDailyReportSemanticError(
                    f"dca {symbol} modeled.sessions must match session_results"
                )
            for entry in sessions:
                session = entry["session_date"]
                status = entry["status"]
                if status != result_by_session[session]["dca_status"]:
                    raise PrivateDailyReportSemanticError(
                        f"dca {symbol} {session} status must match session_results.dca_status"
                    )
                amount = Decimal(entry["amount"])
                spend = Decimal(entry["spend"])
                residual = Decimal(entry["residual"])
                quantity = Decimal(entry["quantity"])
                accepted_close = entry["accepted_close"]
                accepted_close_id = entry["accepted_close_id"]
                settlement_event_id = entry["settlement_event_id"]
                if status == "settled":
                    if amount != configured:
                        raise PrivateDailyReportSemanticError(
                            f"dca {symbol} {session} amount must equal configured.amount"
                        )
                    if spend <= 0 or quantity <= 0 or accepted_close is None:
                        raise PrivateDailyReportSemanticError(
                            f"settled dca {symbol} {session} requires positive spend, quantity and accepted_close"
                        )
                    if accepted_close_id is None or settlement_event_id is None:
                        raise PrivateDailyReportSemanticError(
                            f"settled dca {symbol} {session} requires close and settlement identities"
                        )
                    if spend + residual != configured:
                        raise PrivateDailyReportSemanticError(
                            f"dca {symbol} {session} spend plus residual must equal configured.amount"
                        )
                    if quantity * Decimal(accepted_close) != spend:
                        raise PrivateDailyReportSemanticError(
                            f"dca {symbol} {session} quantity times accepted_close must equal spend"
                        )
                else:
                    if amount != 0 or spend != 0 or residual != 0 or quantity != 0:
                        raise PrivateDailyReportSemanticError(
                            f"non-settled dca {symbol} {session} must use zero amounts"
                        )
                    if (
                        accepted_close is not None
                        or accepted_close_id is not None
                        or settlement_event_id is not None
                    ):
                        raise PrivateDailyReportSemanticError(
                            f"non-settled dca {symbol} {session} must not claim close or settlement identities"
                        )


def _validate_calendar(report: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    calendar = report["calendar"]
    _utc_z_date_time(calendar["as_of"], "calendar.as_of")
    for field_name in ("exchange_timezone", "report_timezone"):
        try:
            ZoneInfo(calendar[field_name])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise PrivateDailyReportSemanticError(
                f"calendar.{field_name} must be an IANA timezone"
            ) from exc
    unsettled = calendar["unsettled_sessions"]
    _require_sorted_unique_scalars(unsettled, "calendar.unsettled_sessions")
    sessions = report["session_results"]
    _require_sorted_unique(sessions, "session_date", "session_results")
    session_dates = [item["session_date"] for item in sessions]
    if session_dates != unsettled:
        raise PrivateDailyReportSemanticError(
            "session_results must exactly match calendar.unsettled_sessions"
        )
    if calendar["new_sessions_count"] != len(unsettled):
        raise PrivateDailyReportSemanticError(
            "calendar.new_sessions_count must equal unsettled_sessions length"
        )
    expected_mode = "none" if not unsettled else "single" if len(unsettled) == 1 else "backfill"
    if calendar["mode"] != expected_mode:
        raise PrivateDailyReportSemanticError(
            "calendar.mode must match unsettled_sessions cardinality"
        )
    latest = calendar["latest_completed_session"]
    prior = calendar["last_settled_session_before_run"]
    if latest is None and unsettled:
        raise PrivateDailyReportSemanticError(
            "unsettled_sessions require latest_completed_session"
        )
    if latest is not None and unsettled and unsettled[-1] > latest:
        raise PrivateDailyReportSemanticError(
            "unsettled_sessions may not exceed latest_completed_session"
        )
    if latest is not None and unsettled and unsettled[-1] != latest:
        raise PrivateDailyReportSemanticError(
            "unsettled_sessions must extend through latest_completed_session"
        )
    if latest is not None and prior is not None and prior > latest:
        raise PrivateDailyReportSemanticError(
            "last_settled_session_before_run may not exceed latest_completed_session"
        )
    if prior is not None and unsettled and prior >= unsettled[0]:
        raise PrivateDailyReportSemanticError(
            "last_settled_session_before_run must precede unsettled_sessions"
        )
    provenance = calendar["provenance"]
    _require_sorted_unique(
        provenance,
        "instrument_mic",
        "calendar.provenance",
    )
    for item in provenance:
        try:
            ZoneInfo(item["exchange_timezone"])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise PrivateDailyReportSemanticError(
                "calendar provenance exchange_timezone must be an IANA timezone"
            ) from exc
    primary = [
        item
        for item in provenance
        if item["instrument_mic"] == calendar["exchange_mic"]
    ]
    if report["report_status"] != "blocked" and len(primary) != 1:
        raise PrivateDailyReportSemanticError(
            "calendar provenance must contain the primary exchange_mic"
        )
    if primary and (
        primary[0]["calendar_name"] != calendar["calendar_id"]
        or primary[0]["exchange_timezone"] != calendar["exchange_timezone"]
    ):
        raise PrivateDailyReportSemanticError(
            "primary calendar provenance must match calendar identity"
        )

    no_new_close = calendar["no_new_close"]
    if no_new_close != (report["report_status"] == "no_new_close"):
        raise PrivateDailyReportSemanticError(
            "calendar.no_new_close must agree with report_status"
        )
    if no_new_close and (unsettled or sessions or calendar["mode"] != "none"):
        raise PrivateDailyReportSemanticError(
            "no_new_close reports must have no unsettled session work"
        )
    if no_new_close and (latest is None or prior != latest):
        raise PrivateDailyReportSemanticError(
            "no_new_close requires the latest completed session to be already settled"
        )
    if report["report_status"] in {"complete", "complete_with_warnings"} and not sessions:
        raise PrivateDailyReportSemanticError(
            "a completed report must contain at least one session result"
        )
    return sessions, no_new_close


def _validate_session_results(
    sessions: list[dict[str, Any]],
    latest_completed_session: str | None,
    report_status: str,
) -> None:
    prior_blocked = False
    complete_status = report_status in {"complete", "complete_with_warnings"}
    for item in sessions:
        session = item["session_date"]
        _require_sorted_unique_scalars(item["reason_codes"], f"session {session} reason_codes")
        if latest_completed_session is not None:
            expected_backfill = session < latest_completed_session
            if item["is_backfill"] is not expected_backfill:
                raise PrivateDailyReportSemanticError(
                    f"session {session} is_backfill does not match latest_completed_session"
                )
        status = item["status"]
        gates = [
            item["calendar_gate"],
            item["price_gate"],
            item["corporate_action_gate"],
            item["funding_gate"],
        ]
        if item["dca_status"] != status:
            raise PrivateDailyReportSemanticError(
                f"session {session} status and dca_status must agree"
            )
        if prior_blocked:
            if status != "not_attempted_prior_session_blocked":
                raise PrivateDailyReportSemanticError(
                    "sessions after a blocked session must not be attempted"
                )
        elif status == "not_attempted_prior_session_blocked":
            raise PrivateDailyReportSemanticError(
                "not_attempted_prior_session_blocked requires an earlier blocked session"
            )
        if status == "not_attempted_prior_session_blocked":
            if any(gate != "not_attempted" for gate in gates):
                raise PrivateDailyReportSemanticError(
                    f"session {session} not-attempted status requires all gates not_attempted"
                )
            if item["dca_status"] != status:
                raise PrivateDailyReportSemanticError(
                    f"session {session} dca_status must also be not attempted"
                )
            if item["close_batch_id"] is not None or item["ledger_batch_id"] is not None:
                raise PrivateDailyReportSemanticError(
                    f"session {session} not-attempted status may not claim batch identities"
                )
        else:
            stopped = False
            for gate in gates:
                if stopped and gate != "not_attempted":
                    raise PrivateDailyReportSemanticError(
                        f"session {session} has a gate result after processing stopped"
                    )
                if gate in {"blocked", "not_attempted"}:
                    stopped = True
            if status in {"settled", "already_settled"}:
                if any(gate != "passed" for gate in gates):
                    raise PrivateDailyReportSemanticError(
                        f"session {session} settlement requires all gates passed"
                    )
                if item["close_batch_id"] is None or item["ledger_batch_id"] is None:
                    raise PrivateDailyReportSemanticError(
                        f"session {session} settlement requires batch identities"
                    )
            elif status == "blocked":
                if "blocked" not in gates:
                    raise PrivateDailyReportSemanticError(
                        f"session {session} blocked status requires a blocked gate"
                    )
                if item["ledger_batch_id"] is not None:
                    raise PrivateDailyReportSemanticError(
                        f"session {session} blocked status may not claim a ledger batch"
                    )
            elif status == "skipped_by_owner" and "blocked" in gates:
                raise PrivateDailyReportSemanticError(
                    f"session {session} owner skip may not contain a blocked gate"
                )
        for book_name in ("confirmed", "modeled"):
            valuation_status = item[f"{book_name}_valuation_status"]
            valuation_id = item[f"{book_name}_valuation_id"]
            if (valuation_status == "fresh") != (valuation_id is not None):
                raise PrivateDailyReportSemanticError(
                    f"session {session} {book_name} valuation identity must match fresh status"
                )
            if status == "not_attempted_prior_session_blocked" and valuation_status != "not_attempted":
                raise PrivateDailyReportSemanticError(
                    f"session {session} later valuation must not be attempted"
                )
            if complete_status and valuation_status != "fresh":
                raise PrivateDailyReportSemanticError(
                    f"completed session {session} requires fresh {book_name} valuation"
                )
            if (
                any(gate != "passed" for gate in gates[:3])
                and valuation_status == "fresh"
            ):
                raise PrivateDailyReportSemanticError(
                    f"session {session} cannot claim fresh {book_name} valuation "
                    "before calendar, price and corporate-action gates pass"
                )
        if status in {"blocked", "not_attempted_prior_session_blocked"} and not item["reason_codes"]:
            raise PrivateDailyReportSemanticError(
                f"session {session} blocked state requires reason_codes"
            )
        if status == "blocked":
            prior_blocked = True
    if complete_status and prior_blocked:
        raise PrivateDailyReportSemanticError(
            "completed reports may not contain blocked sessions"
        )


def _validate_book(
    book_name: str,
    book: dict[str, Any],
    portfolio_as_of: str | None,
    *,
    no_new_close: bool,
) -> None:
    positions = book["positions"]
    _require_sorted_unique(positions, "symbol", f"portfolio.{book_name}.positions")
    valuation_status = book["valuation_status"]
    performance = book["performance"]
    valuation_session = performance["valuation_session"]
    if no_new_close and valuation_status != "carried_forward_display_only":
        raise PrivateDailyReportSemanticError(
            f"no_new_close requires {book_name} carried_forward_display_only valuation"
        )
    valuation_fields = ("accepted_close", "accepted_close_id", "selected_provider_id", "price_session", "market_value", "unrealized_pnl", "portfolio_weight")
    if valuation_status == "unavailable":
        if book["nav"] is not None or book["market_value"] is not None or valuation_session is not None:
            raise PrivateDailyReportSemanticError(
                f"unavailable {book_name} valuation must not claim NAV, market value or session"
            )
        if performance["prior_nav"] is not None or performance["daily_pnl"] is not None or performance["daily_return"] is not None:
            raise PrivateDailyReportSemanticError(
                f"unavailable {book_name} valuation must not claim current performance"
            )
        for position in positions:
            if any(position[field] is not None for field in valuation_fields):
                raise PrivateDailyReportSemanticError(
                    f"unavailable {book_name} position {position['symbol']} must not claim valuation fields"
                )
    else:
        if book["nav"] is None or book["market_value"] is None or valuation_session is None:
            raise PrivateDailyReportSemanticError(
                f"{valuation_status} {book_name} valuation requires NAV, market value and session"
            )
        if valuation_session != portfolio_as_of:
            raise PrivateDailyReportSemanticError(
                f"{book_name} valuation_session must equal portfolio.as_of_session"
            )
        for position in positions:
            if any(position[field] is None for field in valuation_fields):
                raise PrivateDailyReportSemanticError(
                    f"valued {book_name} position {position['symbol']} requires price provenance and values"
                )
            if position["price_session"] != valuation_session:
                raise PrivateDailyReportSemanticError(
                    f"{book_name} position {position['symbol']} price_session must equal valuation_session"
                )
    if valuation_status != "fresh":
        if performance["daily_pnl"] is not None or performance["daily_return"] is not None:
            raise PrivateDailyReportSemanticError(
                f"non-fresh {book_name} valuation must not declare daily P&L or return"
            )
    for position in positions:
        quantity = Decimal(position["quantity"])
        modeled_quantity = Decimal(position["modeled_quantity"])
        if book_name == "confirmed" and modeled_quantity != 0:
            raise PrivateDailyReportSemanticError(
                f"confirmed position {position['symbol']} modeled_quantity must be zero"
            )
        if modeled_quantity > quantity:
            raise PrivateDailyReportSemanticError(
                f"position {position['symbol']} modeled_quantity may not exceed quantity"
            )

    with localcontext(_REPORT_DECIMAL_CONTEXT):
        position_cost = sum(
            (Decimal(position["economic_cost"]) for position in positions),
            Decimal("0"),
        )
        if position_cost != Decimal(book["total_economic_cost"]):
            raise PrivateDailyReportSemanticError(
                f"{book_name} total_economic_cost must equal position costs"
            )
        for position in positions:
            quantity = Decimal(position["quantity"])
            economic_cost = Decimal(position["economic_cost"])
            average_economic_cost = Decimal(position["average_economic_cost"])
            if economic_cost / quantity != average_economic_cost:
                raise PrivateDailyReportSemanticError(
                    f"{book_name} position {position['symbol']} average economic cost "
                    "must equal economic cost divided by quantity"
                )

        if valuation_status != "unavailable":
            position_market_value = Decimal("0")
            declared_market_value = Decimal(book["market_value"])
            for position in positions:
                quantity = Decimal(position["quantity"])
                close = Decimal(position["accepted_close"])
                market_value = Decimal(position["market_value"])
                economic_cost = Decimal(position["economic_cost"])
                unrealized_pnl = Decimal(position["unrealized_pnl"])
                weight = Decimal(position["portfolio_weight"])
                if quantity * close != market_value:
                    raise PrivateDailyReportSemanticError(
                        f"{book_name} position {position['symbol']} market value "
                        "must equal quantity times accepted close"
                    )
                if market_value - economic_cost != unrealized_pnl:
                    raise PrivateDailyReportSemanticError(
                        f"{book_name} position {position['symbol']} unrealized P/L "
                        "must equal market value minus economic cost"
                    )
                expected_weight = (
                    Decimal("0")
                    if declared_market_value == 0
                    else market_value / declared_market_value
                )
                if weight != expected_weight:
                    raise PrivateDailyReportSemanticError(
                        f"{book_name} position {position['symbol']} portfolio weight "
                        "must equal its share of book market value"
                    )
                position_market_value += market_value

            market_value = declared_market_value
            nav = Decimal(book["nav"])
            cash = Decimal(book["cash"])
            if position_market_value != market_value:
                raise PrivateDailyReportSemanticError(
                    f"{book_name} market_value must equal position market values"
                )
            if cash + market_value != nav:
                raise PrivateDailyReportSemanticError(
                    f"{book_name} NAV must equal cash plus market value"
                )

        prior_nav = performance["prior_nav"]
        prior_cumulative_twr = performance["prior_cumulative_twr"]
        daily_pnl = performance["daily_pnl"]
        daily_return = performance["daily_return"]
        if valuation_status == "fresh":
            if prior_nav is None:
                if daily_pnl is not None or daily_return is not None:
                    raise PrivateDailyReportSemanticError(
                        f"first {book_name} valuation may not claim daily performance"
                    )
                if performance["cumulative_twr"] is not None:
                    raise PrivateDailyReportSemanticError(
                        f"first {book_name} valuation must have null cumulative TWR"
                    )
                if prior_cumulative_twr is not None:
                    raise PrivateDailyReportSemanticError(
                        f"first {book_name} valuation must have null prior cumulative TWR"
                    )
            else:
                if daily_pnl is None or daily_return is None:
                    raise PrivateDailyReportSemanticError(
                        f"linked {book_name} valuation requires daily P/L and return"
                    )
                expected_pnl = (
                    Decimal(book["nav"])
                    - Decimal(prior_nav)
                    - Decimal(performance["net_external_flow"])
                )
                if Decimal(daily_pnl) != expected_pnl:
                    raise PrivateDailyReportSemanticError(
                        f"{book_name} daily P/L does not reconcile to NAV and external flow"
                    )
                denominator = Decimal(prior_nav) + Decimal(
                    performance["weighted_external_flow"]
                )
                if denominator <= 0:
                    raise PrivateDailyReportSemanticError(
                        f"{book_name} weighted starting capital must be positive"
                    )
                if Decimal(daily_return) != Decimal(daily_pnl) / denominator:
                    raise PrivateDailyReportSemanticError(
                        f"{book_name} daily return does not reconcile to daily P/L"
                    )
                if performance["cumulative_twr"] is None:
                    raise PrivateDailyReportSemanticError(
                        f"linked {book_name} valuation requires cumulative TWR"
                    )
                expected_cumulative_twr = (
                    Decimal(daily_return)
                    if prior_cumulative_twr is None
                    else (
                        (Decimal("1") + Decimal(prior_cumulative_twr))
                        * (Decimal("1") + Decimal(daily_return))
                        - Decimal("1")
                    )
                )
                if Decimal(performance["cumulative_twr"]) != expected_cumulative_twr:
                    raise PrivateDailyReportSemanticError(
                        f"{book_name} cumulative TWR does not reconcile to prior TWR "
                        "and daily return"
                    )


def _validate_semantics(report: dict[str, Any]) -> None:
    _require_no_delivery_target(report)

    expected_report_id = compute_report_id(report)
    if report["report_id"] != expected_report_id:
        raise PrivateDailyReportIdentityError(
            "report_id does not match canonical report content"
        )

    delivery = report["delivery"]
    delivery_zone = delivery["timezone"]
    try:
        zone = ZoneInfo(delivery_zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PrivateDailyReportSemanticError(
            "delivery.timezone must be an IANA timezone"
        ) from exc
    prepared_at = _utc_z_date_time(report["prepared_at"], "prepared_at")
    if prepared_at.astimezone(zone).date().isoformat() != delivery["delivery_date"]:
        raise PrivateDailyReportSemanticError(
            "delivery_date must equal prepared_at's date in delivery.timezone"
        )
    if report["calendar"]["report_timezone"] != delivery_zone:
        raise PrivateDailyReportSemanticError(
            "calendar.report_timezone must equal delivery.timezone"
        )
    calendar_as_of = _utc_z_date_time(report["calendar"]["as_of"], "calendar.as_of")
    if calendar_as_of > prepared_at:
        raise PrivateDailyReportSemanticError(
            "calendar.as_of may not be later than prepared_at"
        )

    classification = report["classification"]
    if classification == "synthetic_example":
        if report["simulation"] is not True:
            raise PrivateDailyReportSemanticError(
                "synthetic_example reports must set simulation=true"
            )
        if report["privacy"]["contains_private_portfolio_data"] is not False:
            raise PrivateDailyReportSemanticError(
                "synthetic_example reports may not claim private portfolio data"
            )
        if report["privacy"]["redaction_status"] != "synthetic_only":
            raise PrivateDailyReportSemanticError(
                "synthetic_example reports require synthetic_only redaction_status"
            )
    elif report["privacy"]["redaction_status"] != "private_owner_only":
        raise PrivateDailyReportSemanticError(
            "private reports require private_owner_only redaction_status"
        )
    elif report["privacy"]["contains_private_portfolio_data"] is not True:
        raise PrivateDailyReportSemanticError(
            "private reports must declare private portfolio data"
        )

    sessions, no_new_close = _validate_calendar(report)
    calendar = report["calendar"]
    _validate_session_results(
        sessions,
        calendar["latest_completed_session"],
        report["report_status"],
    )

    portfolio = report["portfolio"]
    for book_name in ("confirmed", "modeled"):
        _validate_book(
            book_name,
            portfolio[book_name],
            portfolio["as_of_session"],
            no_new_close=no_new_close,
        )
        if portfolio[book_name]["valuation_status"] == "fresh":
            matching_sessions = [
                item
                for item in sessions
                if item["session_date"] == portfolio["as_of_session"]
                and item[f"{book_name}_valuation_status"] == "fresh"
            ]
            if len(matching_sessions) != 1:
                raise PrivateDailyReportSemanticError(
                    f"fresh {book_name} portfolio requires one matching fresh session result"
                )
    confirmed_positions = {
        item["symbol"]: item for item in portfolio["confirmed"]["positions"]
    }
    modeled_positions = {
        item["symbol"]: item for item in portfolio["modeled"]["positions"]
    }
    for symbol, confirmed_position in confirmed_positions.items():
        modeled_position = modeled_positions.get(symbol)
        if modeled_position is None:
            raise PrivateDailyReportSemanticError(
                f"modeled book must contain confirmed position {symbol}"
            )
        if Decimal(modeled_position["quantity"]) < Decimal(
            confirmed_position["quantity"]
        ):
            raise PrivateDailyReportSemanticError(
                f"modeled position {symbol} quantity may not be below confirmed quantity"
            )
    with localcontext(_REPORT_DECIMAL_CONTEXT):
        for symbol, modeled_position in modeled_positions.items():
            confirmed_position = confirmed_positions.get(symbol)
            confirmed_quantity = (
                Decimal("0")
                if confirmed_position is None
                else Decimal(confirmed_position["quantity"])
            )
            modeled_quantity = Decimal(modeled_position["quantity"])
            modeled_source_quantity = Decimal(modeled_position["modeled_quantity"])
            projected_excess = modeled_quantity - confirmed_quantity
            if modeled_source_quantity > projected_excess:
                raise PrivateDailyReportSemanticError(
                    f"modeled position {symbol} modeled_quantity may not exceed "
                    "the modeled-versus-confirmed quantity difference"
                )
            if (projected_excess == 0) != (modeled_source_quantity == 0):
                raise PrivateDailyReportSemanticError(
                    f"modeled position {symbol} modeled_quantity must expose "
                    "a non-zero modeled-versus-confirmed quantity difference"
                )
    if (
        portfolio["confirmed"]["valuation_status"] == "unavailable"
        and portfolio["modeled"]["valuation_status"] == "unavailable"
        and portfolio["as_of_session"] is not None
    ):
        raise PrivateDailyReportSemanticError(
            "portfolio.as_of_session must be null when both valuations are unavailable"
        )
    if (
        portfolio["ledger_last_event_hash"] is None
        and any(
            portfolio[book_name]["valuation_status"] != "unavailable"
            for book_name in ("confirmed", "modeled")
        )
    ):
        raise PrivateDailyReportSemanticError(
            "a valued portfolio requires ledger_last_event_hash"
        )
    latest_completed = calendar["latest_completed_session"]
    if (
        portfolio["as_of_session"] is not None
        and latest_completed is not None
        and portfolio["as_of_session"] > latest_completed
    ):
        raise PrivateDailyReportSemanticError(
            "portfolio.as_of_session may not exceed latest_completed_session"
        )
    if report["dca"]["currency"] != portfolio["currency"]:
        raise PrivateDailyReportSemanticError(
            "dca.currency must equal portfolio.currency"
        )

    _validate_dca(report, sessions)
    _require_sorted_unique(report["source_health"], "source_id", "source_health")
    for item in report["source_health"]:
        if item["observed_at"] is not None:
            observed_at = _utc_z_date_time(
                item["observed_at"],
                f"source_health {item['source_id']} observed_at",
            )
            if observed_at > prepared_at:
                raise PrivateDailyReportSemanticError(
                    f"source_health {item['source_id']} observed_at may not exceed prepared_at"
                )

    research = report["research"]
    _require_sorted_unique(research["fund_monitoring"], "fund_key", "research.fund_monitoring")
    social_keys = [
        (item["platform"], item["topic"])
        for item in research["social_attention"]
    ]
    if social_keys != sorted(social_keys):
        raise PrivateDailyReportSemanticError(
            "research.social_attention must be sorted by platform and topic"
        )
    if len(social_keys) != len(set(social_keys)):
        raise PrivateDailyReportSemanticError(
            "research.social_attention contains duplicate platform/topic entries"
        )
    for item in research["fund_monitoring"]:
        _require_sorted_unique_scalars(
            item["reason_codes"],
            f"fund {item['fund_key']} reason_codes",
        )
    _require_sorted_unique_scalars(research["notes"], "research.notes")

    _require_sorted_unique(report["actions"], "action_id", "actions")
    for item in report["actions"]:
        _require_sorted_unique_scalars(
            item["rationale_codes"],
            f"action {item['action_id']} rationale_codes",
        )
    _require_sorted_unique_scalars(report["privacy"]["warnings"], "privacy.warnings")

    prompt = report["manual_trade_prompt"]
    _require_sorted_unique_scalars(
        prompt["accepted_response_kinds"],
        "manual_trade_prompt.accepted_response_kinds",
    )
    if prompt["required"]:
        if prompt["prompt"] is None or not prompt["accepted_response_kinds"]:
            raise PrivateDailyReportSemanticError(
                "a required manual trade prompt needs text and accepted responses"
            )
    elif prompt["prompt"] is not None:
        raise PrivateDailyReportSemanticError(
            "a non-required manual trade prompt must be null"
        )
    owner_confirmed_actions = [
        item
        for item in report["actions"]
        if item["status"] == "proposed" and item["owner_confirmation_required"]
    ]
    if owner_confirmed_actions and not prompt["required"]:
        raise PrivateDailyReportSemanticError(
            "proposed owner-confirmed actions require a manual trade prompt"
        )
    for item in report["actions"]:
        if (
            item["status"] == "proposed"
            and item["action"] in {"ADD", "REDUCE", "EXIT"}
            and not item["owner_confirmation_required"]
        ):
            raise PrivateDailyReportSemanticError(
                "proposed position changes require owner confirmation"
            )


def validate_private_daily_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate schema, privacy, accounting semantics and content identity.

    The returned report is a detached JSON-compatible copy.  Delivery identity
    is intentionally verified by the private outbox, which owns the target hash;
    the hash cannot be reconstructed from this privacy-minimized document.
    """

    if not isinstance(report, Mapping):
        raise PrivateDailyReportSchemaError("report must be an object")
    normalized = _json_value(report)
    _validate_schema(normalized)
    _validate_semantics(normalized)
    return copy.deepcopy(normalized)


def finalize_private_daily_report(
    report: Mapping[str, Any],
    *,
    target_key_sha256: str | None = None,
) -> dict[str, Any]:
    """Fill contract and content identities, then return a validated report.

    New reports should pass the private outbox's ``target_key_sha256`` so this
    function can derive ``delivery_id`` without writing the target hash into
    the report.  A pre-derived delivery ID is accepted for deterministic replay.
    """

    if not isinstance(report, Mapping):
        raise PrivateDailyReportSchemaError("report must be an object")
    finalized = _json_value(report)
    finalized["$schema"] = JSON_SCHEMA_URI
    finalized["schema_version"] = SCHEMA_VERSION
    delivery = finalized.get("delivery")
    if not isinstance(delivery, dict):
        raise PrivateDailyReportSchemaError("delivery must be an object")
    if target_key_sha256 is not None:
        target_hash = str(target_key_sha256).strip()
        if not _SHA256.fullmatch(target_hash):
            raise PrivateDailyReportIdentityError(
                "target_key_sha256 must be a lowercase SHA-256 digest"
            )
        if target_hash in canonical_json(finalized):
            raise PrivateDailyReportIdentityError(
                "target digest must not appear in report content"
            )
        delivery["delivery_id"] = compute_delivery_id(
            delivery_date=delivery.get("delivery_date", ""),
            timezone=delivery.get("timezone", ""),
            channel=delivery.get("channel", ""),
            target_key_sha256=target_hash,
        )
    elif not _SHA256.fullmatch(str(delivery.get("delivery_id", ""))):
        raise PrivateDailyReportIdentityError(
            "target_key_sha256 is required when delivery_id is not finalized"
        )
    finalized["report_id"] = compute_report_id(finalized)
    return validate_private_daily_report(finalized)


finalize_report = finalize_private_daily_report
validate_report = validate_private_daily_report


__all__ = [
    "JSON_SCHEMA_URI",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "PrivateDailyReportCanonicalizationError",
    "PrivateDailyReportError",
    "PrivateDailyReportIdentityError",
    "PrivateDailyReportSchemaError",
    "PrivateDailyReportSemanticError",
    "build_delivery_id",
    "canonical_json",
    "compute_delivery_id",
    "compute_delivery_identity",
    "compute_report_id",
    "compute_target_key_sha256",
    "finalize_private_daily_report",
    "finalize_report",
    "validate_private_daily_report",
    "validate_report",
]
