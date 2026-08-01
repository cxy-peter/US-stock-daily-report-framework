from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

import serenity_monitor.opening_owner_attestation as attestation
from serenity_monitor.daily_outbox import DailyReportOutbox
from serenity_monitor.opening_owner_attestation import (
    OpeningLedgerBinding,
    OpeningOwnerAttestationError,
    audit_opening_owner_attestation,
    create_opening_owner_claim,
    interactive_owner_presence,
    opening_ledger_idempotency_key,
    opening_snapshot_sha256,
    publish_opening_intent,
    publish_opening_receipt,
    validate_opening_commit_time,
)
from serenity_monitor.portfolio_ledger import OpeningPosition, PortfolioLedger
from serenity_monitor.private_runtime_config import (
    PUBLIC_EXAMPLE_NAME,
    load_private_daily_runtime_config,
)
from serenity_monitor.private_runtime_paths import (
    PrivateRuntimePaths,
    ensure_private_storage,
)
from serenity_monitor.trading_calendar import ExchangeSessionResolver


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / PUBLIC_EXAMPLE_NAME
CONFIG_BYTES = CONFIG_PATH.read_bytes()
CONFIG_DIGEST = hashlib.sha256(CONFIG_BYTES).hexdigest()
NOW = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class MutableClock:
    def __init__(self, value: dt.datetime) -> None:
        self.value = value

    def __call__(self) -> dt.datetime:
        return self.value


@pytest.fixture(autouse=True)
def _allow_synthetic_temp_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        attestation,
        "validate_existing_private_storage_root",
        lambda paths: paths.root,
    )
    monkeypatch.setattr(
        attestation,
        "validate_existing_private_runtime_file",
        lambda _paths, path: Path(path),
    )


def _config():
    return load_private_daily_runtime_config(CONFIG_PATH, allow_synthetic=True)


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


def _presence(code: str = "23456789AB"):
    return interactive_owner_presence(
        TTYBuffer(f"CONFIRM {code}\n"),
        TTYBuffer(),
        challenge_factory=lambda: code,
    )


def _claim(config, paths, clock):
    create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        owner_presence=_presence(),
        clock=clock,
    )
    audit = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=clock(),
        ledger_binding=None,
    )
    assert audit.state == "pending_verified"
    assert audit.claim is not None
    return audit.claim


def _binding(ledger: PortfolioLedger) -> OpeningLedgerBinding:
    checkpoint = ledger.opening_checkpoint()
    return OpeningLedgerBinding(
        opening_event_id=checkpoint.opening_event_id,
        opening_event_hash=checkpoint.opening_event_hash,
        idempotency_key=checkpoint.idempotency_key,
        created_at=checkpoint.created_at,
    )


def test_owner_presence_requires_tty_and_exact_random_challenge() -> None:
    with pytest.raises(OpeningOwnerAttestationError, match="tty"):
        interactive_owner_presence(
            io.StringIO("CONFIRM 23456789AB\n"),
            TTYBuffer(),
            challenge_factory=lambda: "23456789AB",
        )
    with pytest.raises(OpeningOwnerAttestationError, match="rejected"):
        interactive_owner_presence(
            TTYBuffer("CONFIRM 23456789AC\n"),
            TTYBuffer(),
            challenge_factory=lambda: "23456789AB",
        )

    proof = _presence()

    assert proof is not None


def test_opening_digest_is_context_free_and_binds_instrument_identity() -> None:
    config = _config()
    original_position = config.opening.positions[0]
    first = replace(
        original_position,
        quantity=Decimal("1234567890123456789012345678.123456789"),
    )
    second = replace(
        original_position,
        quantity=Decimal("1234567890123456789012345678.123456788"),
    )
    first_config = replace(
        config,
        opening=replace(config.opening, positions=(first, *config.opening.positions[1:])),
    )
    second_config = replace(
        config,
        opening=replace(config.opening, positions=(second, *config.opening.positions[1:])),
    )
    with localcontext() as context:
        context.prec = 3
        low_first = opening_snapshot_sha256(first_config)
        low_second = opening_snapshot_sha256(second_config)
    with localcontext() as context:
        context.prec = 50
        high_first = opening_snapshot_sha256(first_config)
        high_second = opening_snapshot_sha256(second_config)

    assert low_first == high_first
    assert low_second == high_second
    assert low_first != low_second

    changed_instrument = replace(config.instruments[0], asset_type="stock")
    changed_config = replace(
        config,
        instruments=(changed_instrument, *config.instruments[1:]),
    )
    assert opening_snapshot_sha256(changed_config) != opening_snapshot_sha256(config)

    rerouted_instrument = replace(
        config.instruments[0],
        provider_symbols={
            **config.instruments[0].provider_symbols,
            "twelve_data": "UPDATED_PROVIDER_ALIAS",
        },
    )
    rerouted_config = replace(
        config,
        instruments=(rerouted_instrument, *config.instruments[1:]),
    )
    assert opening_snapshot_sha256(rerouted_config) == opening_snapshot_sha256(config)


def test_claim_is_redacted_canonical_and_exact_config_bound(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    clock = MutableClock(NOW)
    receipt = create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        owner_presence=_presence(),
        clock=clock,
    )
    payload = paths.opening_claim_file.read_bytes()
    document = json.loads(payload)

    assert receipt.status == "created"
    assert payload.endswith(b"\n")
    assert payload == (
        json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    assert set(document) == {
        "attestation_id",
        "attested_at",
        "claim_sha256",
        "config_bytes_sha256",
        "config_schema_version",
        "confirmation_method",
        "contract_version",
        "expires_at",
        "opening_identity_version",
        "opening_snapshot_sha256",
    }
    assert not {
        "cash",
        "positions",
        "quantity",
        "average_economic_cost",
        "path",
    } & set(document)
    for private_value in ("DEMO_EQ", "DEMO_BOND", str(paths.root)):
        assert private_value.encode() not in payload

    mismatch = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256="f" * 64,
        now=clock(),
        ledger_binding=None,
    )
    assert mismatch.state == "config_mismatch"
    assert "sha256" not in mismatch.reason_code


def test_fresh_owner_presence_always_renews_the_claim_ttl(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    clock = MutableClock(NOW)
    _claim(config, paths, clock)

    clock.value = NOW + attestation.CLAIM_TTL - dt.timedelta(seconds=1)
    renewed = create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        owner_presence=_presence(),
        clock=clock,
    )
    assert renewed.status == "renewed"
    assert len(tuple(paths.root.glob("opening-owner-attestation.claim.*.expired.json"))) == 1
    audit = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=clock(),
        ledger_binding=None,
    )
    assert audit.state == "pending_verified"
    assert audit.claim is not None
    assert audit.claim.expires_at == clock.value + attestation.CLAIM_TTL


def test_intent_without_ledger_is_explicitly_resumable(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    clock = MutableClock(NOW)
    claim = _claim(config, paths, clock)
    clock.value += dt.timedelta(seconds=1)
    publish_opening_intent(claim, paths, clock=clock)

    audit = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=clock(),
        ledger_binding=None,
    )

    assert audit.state == "resume_available"
    assert audit.intent is not None

    clock.value = claim.expires_at
    stale = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=clock(),
        ledger_binding=None,
    )
    assert stale.state == "resume_requires_owner_reconfirmation"

    renewed = create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        owner_presence=_presence(),
        clock=clock,
    )
    assert renewed.status == "renewed"
    assert not paths.opening_intent_file.exists()
    assert len(
        tuple(
            paths.root.glob(
                "opening-owner-attestation.intent.*.aborted.json"
            )
        )
    ) == 1
    pending = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=clock(),
        ledger_binding=None,
    )
    assert pending.state == "pending_verified"


@pytest.mark.parametrize("suffix", ("-journal", "-shm", "-wal"))
def test_missing_ledger_with_any_sidecar_rejects_claim(
    tmp_path: Path,
    suffix: str,
) -> None:
    config = _config()
    paths = _paths(tmp_path)
    Path(str(paths.ledger_database) + suffix).write_bytes(b"stale")

    with pytest.raises(OpeningOwnerAttestationError, match="already_started"):
        create_opening_owner_claim(
            config,
            paths,
            config_bytes_sha256=CONFIG_DIGEST,
            owner_presence=_presence(),
            clock=lambda: NOW,
        )


def test_v1_claim_reader_does_not_depend_on_current_module_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    paths = _paths(tmp_path)
    _claim(config, paths, MutableClock(NOW))

    monkeypatch.setattr(attestation, "CONFIG_SCHEMA_VERSION", "future-config/v2")
    monkeypatch.setattr(attestation, "OPENING_IDENTITY_VERSION", "future-identity/v2")
    monkeypatch.setattr(attestation, "CLAIM_CONTRACT_VERSION", "future-claim/v2")
    monkeypatch.setattr(attestation, "INTENT_CONTRACT_VERSION", "future-intent/v2")
    monkeypatch.setattr(attestation, "RECEIPT_CONTRACT_VERSION", "future-receipt/v2")
    monkeypatch.setattr(attestation, "CONFIRMATION_METHOD", "future-method/v2")
    audit = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=NOW,
        ledger_binding=None,
    )

    assert audit.state == "pending_verified"
    assert audit.claim is not None
    assert audit.claim.opening_identity_version == (
        "opening_snapshot_identity/v1.0.0"
    )


def test_expired_claim_can_renew_over_exact_empty_schema_database(
    tmp_path: Path,
) -> None:
    config = _config()
    paths = _paths(tmp_path)
    clock = MutableClock(NOW)
    _claim(config, paths, clock)
    PortfolioLedger(paths.ledger_database, policy=config.ledger_policy)
    clock.value = NOW + attestation.CLAIM_TTL + dt.timedelta(seconds=1)

    renewed = create_opening_owner_claim(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        owner_presence=_presence(),
        clock=clock,
    )

    assert renewed.status == "renewed"
    with sqlite3.connect(paths.ledger_database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ledger_events").fetchone()[0] == 0


def test_exact_empty_legacy_outbox_is_allowed_but_any_row_is_rejected(
    tmp_path: Path,
) -> None:
    config = _config()
    empty_paths = _paths(tmp_path / "empty")
    DailyReportOutbox(empty_paths.outbox_database)
    created = create_opening_owner_claim(
        config,
        empty_paths,
        config_bytes_sha256=CONFIG_DIGEST,
        owner_presence=_presence(),
        clock=lambda: NOW,
    )
    assert created.status == "created"

    nonempty_paths = _paths(tmp_path / "nonempty")
    DailyReportOutbox(nonempty_paths.outbox_database)
    with sqlite3.connect(nonempty_paths.outbox_database) as connection:
        connection.execute(
            "INSERT INTO daily_report_outbox ("
            "report_id, delivery_id, delivery_date, timezone, channel, "
            "target_key_sha256, ledger_last_event_hash, report_json, markdown, "
            "content_sha256, status, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "1" * 64,
                "2" * 64,
                "2026-08-01",
                "Asia/Shanghai",
                "codex",
                "3" * 64,
                None,
                "{}",
                "private",
                "4" * 64,
                "prepared",
                "2026-08-01T00:00:00Z",
                "2026-08-01T00:00:00Z",
            ),
        )

    with pytest.raises(OpeningOwnerAttestationError, match="outbox_not_pristine"):
        create_opening_owner_claim(
            config,
            nonempty_paths,
            config_bytes_sha256=CONFIG_DIGEST,
            owner_presence=_presence(),
            clock=lambda: NOW,
        )


def test_nonempty_or_schema_modified_ledger_rejects_claim(tmp_path: Path) -> None:
    config = _config()
    initialized_paths = _paths(tmp_path / "initialized")
    ledger = PortfolioLedger(initialized_paths.ledger_database, policy=config.ledger_policy)
    ledger.initialize(
        config.opening.session,
        config.opening.cash,
        config.opening.positions,
    )
    with pytest.raises(OpeningOwnerAttestationError, match="already_started"):
        create_opening_owner_claim(
            config,
            initialized_paths,
            config_bytes_sha256=CONFIG_DIGEST,
            owner_presence=_presence(),
            clock=lambda: NOW,
        )

    modified_paths = _paths(tmp_path / "modified")
    PortfolioLedger(modified_paths.ledger_database, policy=config.ledger_policy)
    with sqlite3.connect(modified_paths.ledger_database) as connection:
        connection.execute("CREATE TABLE unexpected_private_table (value TEXT)")
    with pytest.raises(OpeningOwnerAttestationError, match="already_started"):
        create_opening_owner_claim(
            config,
            modified_paths,
            config_bytes_sha256=CONFIG_DIGEST,
            owner_presence=_presence(),
            clock=lambda: NOW,
        )


def test_pristine_audit_detects_base_or_wal_created_during_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_paths = _paths(tmp_path / "missing")
    original_directory_fingerprint = attestation._directory_fingerprint
    directory_calls = 0

    def create_base_on_second_directory_check(path: Path):
        nonlocal directory_calls
        directory_calls += 1
        if directory_calls == 2:
            missing_paths.ledger_database.write_bytes(b"raced")
        return original_directory_fingerprint(path)

    monkeypatch.setattr(
        attestation,
        "_directory_fingerprint",
        create_base_on_second_directory_check,
    )
    assert attestation._is_pristine_empty_ledger(missing_paths) is False

    monkeypatch.setattr(
        attestation,
        "_directory_fingerprint",
        original_directory_fingerprint,
    )
    wal_paths = _paths(tmp_path / "wal")
    PortfolioLedger(wal_paths.ledger_database, policy=_config().ledger_policy)
    original_file_fingerprint = attestation._file_fingerprint
    file_calls = 0

    def create_wal_on_second_file_check(path: Path):
        nonlocal file_calls
        file_calls += 1
        if file_calls == 2:
            Path(str(wal_paths.ledger_database) + "-wal").write_bytes(b"raced")
        return original_file_fingerprint(path)

    monkeypatch.setattr(
        attestation,
        "_file_fingerprint",
        create_wal_on_second_file_check,
    )
    assert attestation._is_pristine_empty_ledger(wal_paths) is False


def test_opening_event_binding_recovers_receipt_and_detects_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    paths = _paths(tmp_path)
    clock = MutableClock(NOW.replace(microsecond=800_000))
    claim = _claim(config, paths, clock)
    intent = publish_opening_intent(claim, paths, clock=clock)
    ledger = PortfolioLedger(
        paths.ledger_database,
        policy=config.ledger_policy,
        calendar_resolver=ExchangeSessionResolver(),
    )
    clock.value = clock.value.replace(microsecond=900_000)
    ledger.initialize(
        config.opening.session,
        config.opening.cash,
        config.opening.positions,
        idempotency_key=opening_ledger_idempotency_key(claim, intent),
        recorded_at=clock(),
    )
    binding = _binding(ledger)
    recovery = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256="0" * 64,
        now=clock(),
        ledger_binding=binding,
    )
    assert recovery.state == "recovery_available"

    clock.value = clock.value.replace(microsecond=950_000)
    receipt = publish_opening_receipt(claim, intent, binding, paths, clock=clock)
    assert publish_opening_receipt(claim, intent, binding, paths, clock=clock) == receipt
    consumed = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256="0" * 64,
        now=clock(),
        ledger_binding=binding,
    )
    assert consumed.state == "consumed_verified"

    monkeypatch.setattr(attestation, "CLAIM_CONTRACT_VERSION", "future-claim/v2")
    monkeypatch.setattr(attestation, "INTENT_CONTRACT_VERSION", "future-intent/v2")
    monkeypatch.setattr(attestation, "RECEIPT_CONTRACT_VERSION", "future-receipt/v2")
    monkeypatch.setattr(attestation, "CONFIRMATION_METHOD", "future-method/v2")
    legacy_consumed = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=clock(),
        ledger_binding=binding,
    )
    assert legacy_consumed.state == "consumed_verified"

    clock_rollback = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=NOW - dt.timedelta(days=1),
        ledger_binding=binding,
    )
    assert clock_rollback.state == "unsafe"
    assert clock_rollback.reason_code == "opening_attestation_future_control"

    rollback = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=clock(),
        ledger_binding=None,
    )
    assert rollback.state == "replay_or_rollback"


def test_commit_time_is_checked_before_ledger_write(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    clock = MutableClock(NOW)
    claim = _claim(config, paths, clock)
    intent = publish_opening_intent(claim, paths, clock=clock)

    assert validate_opening_commit_time(claim, intent, NOW) == NOW
    with pytest.raises(OpeningOwnerAttestationError, match="outside_claim"):
        validate_opening_commit_time(claim, intent, claim.expires_at)
    assert not paths.ledger_database.exists()


def test_binding_mismatch_and_noncanonical_control_are_rejected(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    clock = MutableClock(NOW)
    _claim(config, paths, clock)
    original = paths.opening_claim_file.read_bytes()
    paths.opening_claim_file.write_bytes(b" {" + original[1:])
    attestation.tighten_private_file(paths.opening_claim_file)

    audit = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=clock(),
        ledger_binding=None,
    )

    assert audit.state == "unsafe"


def test_readonly_audit_does_not_change_control_files(tmp_path: Path) -> None:
    config = _config()
    paths = _paths(tmp_path)
    clock = MutableClock(NOW)
    _claim(config, paths, clock)
    before = (
        paths.opening_claim_file.stat().st_mtime_ns,
        hashlib.sha256(paths.opening_claim_file.read_bytes()).hexdigest(),
        tuple(paths.root.iterdir()),
    )

    result = audit_opening_owner_attestation(
        config,
        paths,
        config_bytes_sha256=CONFIG_DIGEST,
        now=clock(),
        ledger_binding=None,
    )
    after = (
        paths.opening_claim_file.stat().st_mtime_ns,
        hashlib.sha256(paths.opening_claim_file.read_bytes()).hexdigest(),
        tuple(paths.root.iterdir()),
    )

    assert result.state == "pending_verified"
    assert after == before


def test_hardlink_publication_recovery_is_no_overwrite_and_fail_closed(
    tmp_path: Path,
) -> None:
    config = _config()
    paths = _paths(tmp_path / "recover-temp")
    claim = _claim(config, paths, MutableClock(NOW))
    original = paths.opening_claim_file.read_bytes()

    with pytest.raises(FileExistsError):
        attestation._publish_new(paths.opening_claim_file, b"{}\n")
    assert paths.opening_claim_file.read_bytes() == original
    assert not tuple(paths.root.glob(f".{paths.opening_claim_file.name}.*.tmp"))

    temporary = paths.root / f".{paths.opening_claim_file.name}.{'a' * 32}.tmp"
    temporary.hardlink_to(paths.opening_claim_file)
    assert paths.opening_claim_file.stat().st_nlink == 2
    attestation.recover_opening_control_publications(paths)
    assert paths.opening_claim_file.exists()
    assert not temporary.exists()
    assert paths.opening_claim_file.stat().st_nlink == 1

    archive = paths.root / (
        "opening-owner-attestation.claim."
        f"{claim.claim_sha256}.expired.json"
    )
    archive.hardlink_to(paths.opening_claim_file)
    attestation.recover_opening_control_publications(paths)
    assert not paths.opening_claim_file.exists()
    assert archive.exists()
    assert archive.stat().st_nlink == 1

    ambiguous_paths = _paths(tmp_path / "ambiguous")
    ambiguous_claim = _claim(config, ambiguous_paths, MutableClock(NOW))
    ambiguous_temp = ambiguous_paths.root / (
        f".{ambiguous_paths.opening_claim_file.name}.{'b' * 32}.tmp"
    )
    ambiguous_archive = ambiguous_paths.root / (
        "opening-owner-attestation.claim."
        f"{ambiguous_claim.claim_sha256}.expired.json"
    )
    ambiguous_temp.hardlink_to(ambiguous_paths.opening_claim_file)
    ambiguous_archive.hardlink_to(ambiguous_paths.opening_claim_file)
    with pytest.raises(OpeningOwnerAttestationError, match="unsafe"):
        attestation.recover_opening_control_publications(ambiguous_paths)
