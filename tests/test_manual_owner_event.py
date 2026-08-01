from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from serenity_monitor.manual_owner_event import (
    APPROVAL_CONTRACT_VERSION,
    RECEIPT_CONTRACT_VERSION,
    REQUEST_CONTRACT_VERSION,
    ManualEventReceipt,
    ManualOwnerEventError,
    approve_manual_event,
    interactive_manual_event_presence,
    load_manual_event_queue,
    load_manual_event_request,
    publish_manual_event_receipt,
    record_approved_event,
)
from serenity_monitor.portfolio_ledger import DcaPlan, PortfolioLedger
from serenity_monitor.private_runtime_config import (
    PUBLIC_EXAMPLE_NAME,
    load_private_daily_runtime_config,
)
from serenity_monitor.private_runtime_paths import PrivateRuntimePaths
from serenity_monitor.private_runtime_paths import ensure_private_storage, tighten_private_file
import serenity_monitor.private_runtime_paths as private_runtime_paths
import serenity_monitor.manual_owner_event as manual_owner_event
from serenity_monitor.private_windows_security import secure_create_owner_only_directory


ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 1, 6, 1, 0, tzinfo=dt.timezone.utc)
SESSION = "2026-01-05"


@pytest.fixture(autouse=True)
def _isolate_profile_git_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(private_runtime_paths, "_inside_git_worktree", lambda _path: False)


def _config():
    return load_private_daily_runtime_config(
        ROOT / "config" / PUBLIC_EXAMPLE_NAME,
        allow_synthetic=True,
    )


def _paths(tmp_path: Path) -> PrivateRuntimePaths:
    root = tmp_path / "private-runtime"
    paths = PrivateRuntimePaths(
        root=root,
        ledger_database=root / "portfolio-ledger.sqlite3",
        outbox_database=root / "daily-outbox.sqlite3",
        report_directory=root / "reports",
        lock_file=root / "private-daily-runtime.lock",
    )
    ensure_private_storage(paths)
    return paths


def _body(
    *,
    nonce: str = "1" * 32,
    kind: str = "confirmed_fill",
    session: str = SESSION,
    occurred_at: str | None = "2026-01-05T18:30:00Z",
    payload: dict | None = None,
) -> dict:
    if payload is None:
        payload = {
            "fees": "0",
            "modeled_dca_replacement": False,
            "plan_id": None,
            "plan_version": None,
            "price": "100",
            "quantity": "1",
            "side": "buy",
            "symbol": "DEMO_EQ",
        }
    return {
        "contract_version": REQUEST_CONTRACT_VERSION,
        "event_kind": kind,
        "event_nonce": nonce,
        "occurred_at": occurred_at,
        "payload": payload,
        "session": session,
    }


def _write_request(paths: PrivateRuntimePaths, body: dict, *, canonical: bool = False) -> bytes:
    payload = (
        json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if canonical
        else json.dumps(body, ensure_ascii=False, indent=2)
    ).encode("utf-8") + b"\n"
    paths.manual_event_request_file.write_bytes(payload)
    tighten_private_file(paths.manual_event_request_file)
    return payload


def _make_manual_directories(paths: PrivateRuntimePaths) -> None:
    if os.name == "nt":
        secure_create_owner_only_directory(paths.manual_event_directory, parents=False)
        secure_create_owner_only_directory(paths.manual_event_approved_directory, parents=False)
        secure_create_owner_only_directory(paths.manual_event_receipt_directory, parents=False)
    else:
        paths.manual_event_directory.mkdir(mode=0o700)
        paths.manual_event_approved_directory.mkdir(mode=0o700)
        paths.manual_event_receipt_directory.mkdir(mode=0o700)


def _tty(response: str):
    input_stream = io.StringIO(response)
    output_stream = io.StringIO()
    input_stream.isatty = lambda: True
    output_stream.isatty = lambda: True
    return input_stream, output_stream


def _approve(tmp_path: Path, body: dict | None = None):
    config = _config()
    paths = _paths(tmp_path)
    raw = _write_request(paths, _body() if body is None else body)
    request = load_manual_event_request(config, paths)
    input_stream, output_stream = _tty(
        f"CONFIRM 23456789AB {hashlib.sha256(raw).hexdigest()[:8]}\n"
    )
    presence = interactive_manual_event_presence(
        request,
        input_stream,
        output_stream,
        challenge_factory=lambda: "23456789AB",
    )
    approval = approve_manual_event(config, paths, request, presence, lambda: NOW)
    return config, paths, request, approval, output_stream.getvalue()


@pytest.mark.parametrize(
    ("kind", "occurred_at", "payload"),
    [
        (
            "confirmed_fill",
            "2026-01-05T18:30:00Z",
            {
                "fees": "0",
                "modeled_dca_replacement": False,
                "plan_id": None,
                "plan_version": None,
                "price": "100",
                "quantity": "1",
                "side": "buy",
                "symbol": "DEMO_EQ",
            },
        ),
        (
            "cash_flow",
            None,
            {"amount": "25", "description": "deposit", "valuation_weight": None},
        ),
        ("fee", None, {"amount": "1", "description": "service"}),
        (
            "income",
            None,
            {"amount": "2", "description": "distribution", "symbol": "DEMO_EQ"},
        ),
        ("split", None, {"ratio": "2", "symbol": "DEMO_EQ"}),
        (
            "skip_dca",
            None,
            {
                "plan_id": "synthetic-daily-base",
                "plan_version": "v1",
                "reason": "owner decision",
            },
        ),
    ],
)
def test_request_contract_accepts_each_closed_event_kind(
    tmp_path: Path,
    kind: str,
    occurred_at: str | None,
    payload: dict,
) -> None:
    config = _config()
    paths = _paths(tmp_path)
    _write_request(paths, _body(kind=kind, occurred_at=occurred_at, payload=payload))

    request = load_manual_event_request(config, paths)

    assert request.event_kind == kind
    assert request.session.isoformat() == SESSION
    assert request.body()["payload"] == payload


@pytest.mark.parametrize("bad_value", [1, 1.0, True, "1.0", "1e0", "01"])
def test_decimal_values_must_be_canonical_strings(tmp_path: Path, bad_value) -> None:
    config = _config()
    paths = _paths(tmp_path)
    body = _body()
    body["payload"]["quantity"] = bad_value
    _write_request(paths, body)

    with pytest.raises(ManualOwnerEventError, match="manual_event_quantity_invalid"):
        load_manual_event_request(config, paths)


def test_closed_schema_unknown_top_and_payload_fields_fail(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    top = _body()
    top["unknown"] = "x"
    _write_request(paths, top)
    with pytest.raises(ManualOwnerEventError, match="request_schema_invalid"):
        load_manual_event_request(config, paths)

    payload = _body(nonce="2" * 32)
    payload["payload"]["internal_event_id"] = "f" * 64
    _write_request(paths, payload)
    with pytest.raises(ManualOwnerEventError, match="fill_schema_invalid"):
        load_manual_event_request(config, paths)


def test_duplicate_json_keys_fail_closed(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    raw = (
        '{"contract_version":"manual_owner_event_request/v1.0.0",'
        '"event_kind":"fee","event_kind":"income","event_nonce":"'
        + "3" * 32
        + '","occurred_at":null,"payload":{"amount":"1","description":"x"},'
        '"session":"2026-01-05"}\n'
    ).encode()
    paths.manual_event_request_file.write_bytes(raw)
    tighten_private_file(paths.manual_event_request_file)

    with pytest.raises(ManualOwnerEventError, match="duplicate_or_invalid_key"):
        load_manual_event_request(config, paths)


def test_symbol_and_dca_identity_are_bound_to_config(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    unknown = _body()
    unknown["payload"]["symbol"] = "UNKNOWN"
    _write_request(paths, unknown)
    with pytest.raises(ManualOwnerEventError, match="symbol_invalid"):
        load_manual_event_request(config, paths)

    replacement = _body(nonce="4" * 32)
    replacement["payload"].update(
        {
            "modeled_dca_replacement": True,
            "plan_id": "synthetic-daily-base",
            "plan_version": "wrong",
        }
    )
    _write_request(paths, replacement)
    with pytest.raises(ManualOwnerEventError, match="replacement_plan_mismatch"):
        load_manual_event_request(config, paths)


def test_replacement_never_accepts_owner_supplied_internal_event_id(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    replacement = _body()
    replacement["payload"].update(
        {
            "modeled_dca_replacement": True,
            "plan_id": "synthetic-daily-base",
            "plan_version": "v1",
            "replaces_modeled_event_id": "f" * 64,
        }
    )
    _write_request(paths, replacement)
    with pytest.raises(ManualOwnerEventError, match="fill_schema_invalid"):
        load_manual_event_request(config, paths)


def test_interactive_output_is_redacted_and_binds_exact_digest(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    raw = _write_request(paths, _body())
    request = load_manual_event_request(config, paths)
    digest = hashlib.sha256(raw).hexdigest()
    input_stream, output_stream = _tty(f"CONFIRM 23456789AB {digest[:8]}\n")

    proof = interactive_manual_event_presence(
        request,
        input_stream,
        output_stream,
        challenge_factory=lambda: "23456789AB",
    )

    rendered = output_stream.getvalue()
    assert proof.request_bytes_sha256 == digest
    assert "confirmed_fill" in rendered
    assert SESSION in rendered
    assert digest[:8] in rendered
    assert "DEMO_EQ" not in rendered
    assert "100" not in rendered
    assert str(paths.root) not in rendered


def test_non_tty_and_wrong_challenge_are_rejected(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    _write_request(paths, _body())
    request = load_manual_event_request(config, paths)
    with pytest.raises(ManualOwnerEventError, match="tty_required"):
        interactive_manual_event_presence(request, io.StringIO(), io.StringIO())
    input_stream, output_stream = _tty("CONFIRM WRONG\n")
    with pytest.raises(ManualOwnerEventError, match="challenge_rejected"):
        interactive_manual_event_presence(
            request,
            input_stream,
            output_stream,
            challenge_factory=lambda: "23456789AB",
        )


def test_approval_is_canonical_self_hashed_and_idempotent(tmp_path: Path) -> None:
    config, paths, request, first, _ = _approve(tmp_path)
    input_stream, output_stream = _tty(
        f"CONFIRM 23456789AB {request.request_bytes_sha256[:8]}\n"
    )
    proof = interactive_manual_event_presence(
        request,
        input_stream,
        output_stream,
        challenge_factory=lambda: "23456789AB",
    )
    second = approve_manual_event(
        config,
        paths,
        request,
        proof,
        lambda: NOW + dt.timedelta(minutes=10),
    )

    assert second.approval_sha256 == first.approval_sha256
    document = json.loads(
        (paths.manual_event_approved_directory / f"{request.event_nonce}.json").read_text()
    )
    assert document["contract_version"] == APPROVAL_CONTRACT_VERSION
    body = dict(document)
    stored_hash = body.pop("approval_sha256")
    canonical = (json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    assert stored_hash == hashlib.sha256(canonical).hexdigest()


def test_approval_recovers_crash_before_final_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    paths = _paths(tmp_path)
    raw = _write_request(paths, _body())
    request = load_manual_event_request(config, paths)
    input_stream, output_stream = _tty(
        f"CONFIRM 23456789AB {hashlib.sha256(raw).hexdigest()[:8]}\n"
    )
    presence = interactive_manual_event_presence(
        request,
        input_stream,
        output_stream,
        challenge_factory=lambda: "23456789AB",
    )
    original_write_temp = manual_owner_event._write_temp

    def crash_after_temp(path: Path, payload: bytes) -> Path:
        original_write_temp(path, payload)
        raise SystemExit("simulated process death before hardlink")

    monkeypatch.setattr(manual_owner_event, "_write_temp", crash_after_temp)
    with pytest.raises(ManualOwnerEventError, match="approval_persistence_failed"):
        approve_manual_event(config, paths, request, presence, lambda: NOW)
    monkeypatch.setattr(manual_owner_event, "_write_temp", original_write_temp)

    recovered = approve_manual_event(
        config,
        paths,
        request,
        presence,
        lambda: NOW + dt.timedelta(minutes=1),
    )

    assert recovered.approved_at == NOW
    assert not tuple(paths.manual_event_approved_directory.glob(".*.tmp"))
    assert (paths.manual_event_approved_directory / f"{request.event_nonce}.json").is_file()


def test_approval_rechecks_exact_bytes_after_challenge(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    original = _body()
    raw = _write_request(paths, original)
    request = load_manual_event_request(config, paths)
    input_stream, output_stream = _tty(
        f"CONFIRM 23456789AB {hashlib.sha256(raw).hexdigest()[:8]}\n"
    )
    proof = interactive_manual_event_presence(
        request,
        input_stream,
        output_stream,
        challenge_factory=lambda: "23456789AB",
    )
    changed = _body()
    changed["payload"]["price"] = "101"
    _write_request(paths, changed)

    with pytest.raises(ManualOwnerEventError, match="changed_after_confirmation"):
        approve_manual_event(config, paths, request, proof, lambda: NOW)


def test_same_nonce_different_content_conflicts(tmp_path: Path) -> None:
    config, paths, _request, _approval, _ = _approve(tmp_path)
    changed = _body()
    changed["payload"]["price"] = "101"
    raw = _write_request(paths, changed)
    request = load_manual_event_request(config, paths)
    input_stream, output_stream = _tty(
        f"CONFIRM 23456789AB {hashlib.sha256(raw).hexdigest()[:8]}\n"
    )
    proof = interactive_manual_event_presence(
        request,
        input_stream,
        output_stream,
        challenge_factory=lambda: "23456789AB",
    )
    with pytest.raises(ManualOwnerEventError, match="nonce_reused"):
        approve_manual_event(config, paths, request, proof, lambda: NOW)


def test_hardlinked_request_fails_owner_only_guard(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    _write_request(paths, _body())
    alias = paths.root / "alias.json"
    os.link(paths.manual_event_request_file, alias)
    try:
        with pytest.raises(ManualOwnerEventError, match="control_unsafe"):
            load_manual_event_request(config, paths)
    finally:
        alias.unlink(missing_ok=True)


def _initialized_ledger(paths: PrivateRuntimePaths, config) -> PortfolioLedger:
    ledger = PortfolioLedger(paths.ledger_database, policy=config.ledger_policy)
    ledger.initialize(
        config.opening.session,
        config.opening.cash,
        config.opening.positions,
    )
    return ledger


def test_record_approved_fill_returns_hash_bound_receipt(tmp_path: Path) -> None:
    config, paths, _request, approval, _ = _approve(tmp_path)
    ledger = _initialized_ledger(paths, config)

    receipt = record_approved_event(
        approval,
        ledger,
        config,
        clock=lambda: NOW,
    )

    checkpoint = ledger.event_checkpoint(receipt.ledger_event_id)
    assert checkpoint is not None
    assert receipt.ledger_event_hash == checkpoint.event_hash
    assert receipt.ledger_event_type == "user_confirmed_fill"
    assert checkpoint.idempotency_key == receipt.ledger_idempotency_key
    assert ledger.project("confirmed").by_symbol["DEMO_EQ"].quantity == Decimal("11")


def test_record_replacement_requires_external_modeled_child_id(tmp_path: Path) -> None:
    replacement = _body()
    replacement["payload"].update(
        {
            "modeled_dca_replacement": True,
            "plan_id": "synthetic-daily-base",
            "plan_version": "v1",
        }
    )
    config, paths, _request, approval, _ = _approve(tmp_path, replacement)
    ledger = _initialized_ledger(paths, config)

    with pytest.raises(ManualOwnerEventError, match="replacement_target_required"):
        record_approved_event(approval, ledger, config, clock=lambda: NOW)


def test_skip_dca_records_only_current_plan(tmp_path: Path) -> None:
    body = _body(
        kind="skip_dca",
        occurred_at=None,
        payload={
            "plan_id": "synthetic-daily-base",
            "plan_version": "v1",
            "reason": "owner decision",
        },
    )
    config, paths, _request, approval, _ = _approve(tmp_path, body)
    ledger = _initialized_ledger(paths, config)

    receipt = record_approved_event(approval, ledger, config, clock=lambda: NOW)

    assert receipt.ledger_event_type == "dca_override"
    assert ledger.session_audit(SESSION).has_owner_skip is True


def test_receipt_publication_is_canonical_and_idempotent(tmp_path: Path) -> None:
    config, paths, _request, approval, _ = _approve(tmp_path)
    ledger = _initialized_ledger(paths, config)
    receipt = record_approved_event(approval, ledger, config, clock=lambda: NOW)

    first = publish_manual_event_receipt(paths, receipt)
    second = publish_manual_event_receipt(paths, receipt)

    assert first.receipt_sha256 == second.receipt_sha256
    document = json.loads(
        (paths.manual_event_receipt_directory / f"{receipt.event_nonce}.json").read_text()
    )
    assert document["contract_version"] == RECEIPT_CONTRACT_VERSION


def test_queue_recovers_receipt_crash_before_final_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _request, approval, _ = _approve(tmp_path)
    ledger = _initialized_ledger(paths, config)
    receipt = record_approved_event(approval, ledger, config, clock=lambda: NOW)
    original_write_temp = manual_owner_event._write_temp

    def crash_after_temp(path: Path, payload: bytes) -> Path:
        original_write_temp(path, payload)
        raise SystemExit("simulated process death before hardlink")

    monkeypatch.setattr(manual_owner_event, "_write_temp", crash_after_temp)
    with pytest.raises(ManualOwnerEventError, match="receipt_persistence_failed"):
        publish_manual_event_receipt(paths, receipt)
    monkeypatch.setattr(manual_owner_event, "_write_temp", original_write_temp)

    assert load_manual_event_queue(config, paths, ledger.event_checkpoint, None) == ()
    assert not tuple(paths.manual_event_receipt_directory.glob(".*.tmp"))
    assert (
        paths.manual_event_receipt_directory / f"{receipt.event_nonce}.json"
    ).is_file()


def test_queue_requires_request_confirmation(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    _make_manual_directories(paths)
    _write_request(paths, _body())

    with pytest.raises(ManualOwnerEventError, match="requires_confirmation"):
        load_manual_event_queue(config, paths, lambda _event_id: None, None)


def test_queue_without_request_or_control_directories_is_empty(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)

    assert load_manual_event_queue(config, paths, lambda _event_id: None, None) == ()


def test_queue_returns_pending_then_validates_consumed_receipt(tmp_path: Path) -> None:
    config, paths, _request, approval, _ = _approve(tmp_path)
    ledger = _initialized_ledger(paths, config)

    pending = load_manual_event_queue(config, paths, ledger.event_checkpoint, None)
    assert [item.event_nonce for item in pending] == [approval.event_nonce]

    receipt = record_approved_event(approval, ledger, config, clock=lambda: NOW)
    publish_manual_event_receipt(paths, receipt)
    assert load_manual_event_queue(config, paths, ledger.event_checkpoint, None) == ()


def test_consumed_history_survives_dca_config_change_but_pending_does_not(
    tmp_path: Path,
) -> None:
    config, paths, _request, approval, _ = _approve(tmp_path / "consumed")
    ledger = _initialized_ledger(paths, config)
    receipt = record_approved_event(approval, ledger, config, clock=lambda: NOW)
    publish_manual_event_receipt(paths, receipt)
    changed = replace(
        config,
        dca_plan=DcaPlan(
            plan_id=config.dca_plan.plan_id,
            version="v2",
            currency=config.dca_plan.currency,
            funding_mode=config.dca_plan.funding_mode,
            share_scale=config.dca_plan.share_scale,
            base_amounts={
                "DEMO_BOND": Decimal("11"),
                "DEMO_EQ": Decimal("15"),
            },
        ),
    )

    # The fixed request still points at the consumed v1 approval.  Its exact
    # digest/body and receipt-to-ledger binding remain audited, while mutable
    # current-plan compatibility is intentionally irrelevant to history.
    assert load_manual_event_queue(
        changed,
        paths,
        ledger.event_checkpoint,
        None,
    ) == ()

    _old, pending_paths, _request, _pending, _ = _approve(tmp_path / "pending")
    with pytest.raises(ManualOwnerEventError, match="approval_config_mismatch"):
        load_manual_event_queue(changed, pending_paths, lambda _event_id: None, None)


def test_queue_fails_on_receipt_binding_and_valuation_finality(tmp_path: Path) -> None:
    config, paths, _request, approval, _ = _approve(tmp_path)
    ledger = _initialized_ledger(paths, config)
    with pytest.raises(ManualOwnerEventError, match="after_valuation_finality"):
        load_manual_event_queue(
            config,
            paths,
            ledger.event_checkpoint,
            dt.date.fromisoformat(SESSION),
        )

    receipt = record_approved_event(approval, ledger, config, clock=lambda: NOW)
    bad = replace(receipt, ledger_event_hash="f" * 64, receipt_sha256="")
    publish_manual_event_receipt(paths, bad)
    with pytest.raises(ManualOwnerEventError, match="receipt_binding_failed"):
        load_manual_event_queue(config, paths, ledger.event_checkpoint, None)


def test_queue_order_is_session_phase_time_nonce(tmp_path: Path) -> None:
    config, paths, _request, first, _ = _approve(
        tmp_path,
        _body(nonce="b" * 32, session="2026-01-06"),
    )
    second_body = _body(nonce="a" * 32, session="2026-01-05")
    raw = _write_request(paths, second_body)
    request = load_manual_event_request(config, paths)
    input_stream, output_stream = _tty(
        f"CONFIRM 23456789AB {hashlib.sha256(raw).hexdigest()[:8]}\n"
    )
    proof = interactive_manual_event_presence(
        request,
        input_stream,
        output_stream,
        challenge_factory=lambda: "23456789AB",
    )
    second = approve_manual_event(config, paths, request, proof, lambda: NOW)

    pending = load_manual_event_queue(config, paths, lambda _event_id: None, None)

    assert [item.event_nonce for item in pending] == [second.event_nonce, first.event_nonce]


def test_approval_tamper_and_unknown_directory_entry_fail_closed(tmp_path: Path) -> None:
    config, paths, request, _approval, _ = _approve(tmp_path)
    approved_path = paths.manual_event_approved_directory / f"{request.event_nonce}.json"
    document = json.loads(approved_path.read_text())
    document["approved_at"] = "2026-01-06T01:01:00Z"
    approved_path.write_text(json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n")
    if os.name != "nt":
        approved_path.chmod(0o600)
    with pytest.raises(ManualOwnerEventError, match="self_hash_mismatch"):
        load_manual_event_queue(config, paths, lambda _event_id: None, None)

    approved_path.unlink()
    unknown = paths.manual_event_approved_directory / "notes.txt"
    unknown.write_text("private")
    if os.name != "nt":
        unknown.chmod(0o600)
    with pytest.raises(ManualOwnerEventError, match="unknown_entry"):
        load_manual_event_queue(config, paths, lambda _event_id: None, None)
