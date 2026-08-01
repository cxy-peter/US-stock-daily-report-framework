"""Silent production entrypoints for the owner-only private runtime."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import sys
import warnings
from pathlib import Path
from typing import Mapping, TextIO

from .daily_outbox import (
    DailyOutboxError,
    DailyReportOutbox,
    OutboxLedgerMutationBlocked,
)
from .opening_owner_attestation import (
    OpeningLedgerBinding,
    OpeningOwnerAttestationError,
    audit_opening_owner_attestation,
    create_opening_owner_claim,
    interactive_owner_presence,
)
from .manual_owner_event import (
    ManualOwnerEventError,
    approve_manual_event,
    interactive_manual_event_presence,
    load_manual_event_request,
)
from .portfolio_ledger import (
    LedgerNotInitializedError,
    PortfolioLedger,
    PortfolioLedgerError,
)
from .private_daily_report import PrivateDailyReportError
from .private_daily_runtime import (
    PrivateDailyIntegrityError,
    PrivateDailyNotInitialized,
    PrivateDailyRuntime,
    PrivateDailyRuntimeError,
    initialize_private_ledger,
)
from .private_report_store import PrivateReportStoreError
from .private_runtime_config import (
    PrivateRuntimeConfigError,
    load_private_daily_runtime_config,
)
from .private_runtime_lock import (
    PrivateRuntimeBusy,
    PrivateRuntimeLockError,
    private_runtime_lock,
)
from .private_runtime_paths import (
    PrivateRuntimePathError,
    ensure_private_storage,
    missing_provider_environment,
    read_validated_live_private_config,
    require_delivery_target,
    resolve_private_runtime_paths,
    tighten_private_file,
)
from .provider_registry import (
    AlphaVantageCloseProvider,
    ProviderRegistry,
    TwelveDataCloseProvider,
)
from .trading_calendar import ExchangeSessionError, ExchangeSessionResolver


PRIVATE_CONFIG_ENV = "SERENITY_PRIVATE_CONFIG"

EXIT_OK = 0
EXIT_BUSY = 10
EXIT_CONFIG_OR_PRIVACY = 20
EXIT_NOT_INITIALIZED = 21
EXIT_INTEGRITY = 30
EXIT_PERSISTENCE = 40
EXIT_DELIVERY_PENDING = 50
EXIT_INTERNAL = 70
EXIT_INTERRUPTED = 130

_ERROR_LINES = {
    EXIT_BUSY: "PRIVATE_DAILY_RUNTIME:BUSY",
    EXIT_CONFIG_OR_PRIVACY: "PRIVATE_DAILY_RUNTIME:CONFIG_OR_PRIVACY_REJECTED",
    EXIT_NOT_INITIALIZED: "PRIVATE_DAILY_RUNTIME:NOT_INITIALIZED",
    EXIT_INTEGRITY: "PRIVATE_DAILY_RUNTIME:INTEGRITY_REJECTED",
    EXIT_PERSISTENCE: "PRIVATE_DAILY_RUNTIME:PERSISTENCE_FAILED",
    EXIT_DELIVERY_PENDING: "PRIVATE_DAILY_RUNTIME:PRIOR_DELIVERY_PENDING",
    EXIT_INTERNAL: "PRIVATE_DAILY_RUNTIME:INTERNAL_FAILURE",
    EXIT_INTERRUPTED: "PRIVATE_DAILY_RUNTIME:INTERRUPTED",
}

_ATTESTATION_ERROR_LINES = {
    EXIT_BUSY: "PRIVATE_OPENING_ATTESTATION:BUSY",
    EXIT_CONFIG_OR_PRIVACY: "PRIVATE_OPENING_ATTESTATION:CONFIG_OR_PRIVACY_REJECTED",
    EXIT_INTEGRITY: "PRIVATE_OPENING_ATTESTATION:INTEGRITY_REJECTED",
    EXIT_PERSISTENCE: "PRIVATE_OPENING_ATTESTATION:PERSISTENCE_FAILED",
    EXIT_INTERNAL: "PRIVATE_OPENING_ATTESTATION:INTERNAL_FAILURE",
    EXIT_INTERRUPTED: "PRIVATE_OPENING_ATTESTATION:INTERRUPTED",
}

_MANUAL_EVENT_ERROR_LINES = {
    EXIT_BUSY: "PRIVATE_MANUAL_EVENT:BUSY",
    EXIT_CONFIG_OR_PRIVACY: "PRIVATE_MANUAL_EVENT:CONFIG_OR_PRIVACY_REJECTED",
    EXIT_NOT_INITIALIZED: "PRIVATE_MANUAL_EVENT:NOT_INITIALIZED",
    EXIT_INTEGRITY: "PRIVATE_MANUAL_EVENT:INTEGRITY_REJECTED",
    EXIT_PERSISTENCE: "PRIVATE_MANUAL_EVENT:PERSISTENCE_FAILED",
    EXIT_DELIVERY_PENDING: "PRIVATE_MANUAL_EVENT:PRIOR_DELIVERY_PENDING",
    EXIT_INTERNAL: "PRIVATE_MANUAL_EVENT:INTERNAL_FAILURE",
    EXIT_INTERRUPTED: "PRIVATE_MANUAL_EVENT:INTERRUPTED",
}


def _clock() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _fixed_error(exit_code: int) -> int:
    line = _ERROR_LINES[exit_code]
    sys.stderr.write(line + "\n")
    return exit_code


def _load_live_config(environ: Mapping[str, str]):
    config, _digest = _load_live_config_bundle(environ)
    return config


def _load_live_config_bundle(environ: Mapping[str, str]):
    raw_path = str(environ.get(PRIVATE_CONFIG_ENV, "")).strip()
    if not raw_path:
        raise PrivateRuntimePathError("private_configuration_environment_missing")
    config_path, payload = read_validated_live_private_config(Path(raw_path))
    config = load_private_daily_runtime_config(
        config_path,
        allow_synthetic=False,
        _validated_bytes=payload,
    )
    return config, hashlib.sha256(payload).hexdigest()


def _registry(config, environ: Mapping[str, str], clock):
    providers = (
        TwelveDataCloseProvider(environ=environ, clock=clock),
        AlphaVantageCloseProvider(environ=environ, clock=clock),
    )
    return ProviderRegistry(providers, policy=config.close_policy, clock=clock)


def _tighten_runtime_files(paths) -> None:
    for base in (paths.ledger_database, paths.outbox_database, paths.lock_file):
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(base) + suffix)
            if candidate.exists():
                tighten_private_file(candidate)


def _map_exception(exc: BaseException) -> int:
    if isinstance(exc, KeyboardInterrupt):
        return EXIT_INTERRUPTED
    if isinstance(exc, PrivateRuntimeBusy):
        return EXIT_BUSY
    if isinstance(exc, (PrivateRuntimeConfigError, PrivateRuntimePathError)):
        return EXIT_CONFIG_OR_PRIVACY
    if isinstance(exc, OpeningOwnerAttestationError):
        return EXIT_INTEGRITY
    if isinstance(exc, ManualOwnerEventError):
        return EXIT_INTEGRITY
    if isinstance(exc, PrivateDailyNotInitialized):
        return EXIT_NOT_INITIALIZED
    if isinstance(exc, OutboxLedgerMutationBlocked):
        return EXIT_DELIVERY_PENDING
    if isinstance(
        exc,
        (
            PrivateDailyIntegrityError,
            PortfolioLedgerError,
            PrivateDailyReportError,
        ),
    ):
        return EXIT_INTEGRITY
    if isinstance(
        exc,
        (
            DailyOutboxError,
            PrivateReportStoreError,
            PrivateRuntimeLockError,
        ),
    ):
        return EXIT_PERSISTENCE
    if isinstance(exc, PrivateDailyRuntimeError):
        return EXIT_INTEGRITY
    return EXIT_INTERNAL


def run_private_daily_main(
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Prepare today's report.  The function never writes normal stdout."""

    environment = os.environ if environ is None else environ
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            config, config_bytes_sha256 = _load_live_config_bundle(environment)
            paths = resolve_private_runtime_paths(config, environment)
            target_key = require_delivery_target(config, environment)
            ensure_private_storage(paths)
            with private_runtime_lock(paths.lock_file):
                if not os.path.lexists(str(paths.ledger_database)):
                    raise PrivateDailyNotInitialized("private_ledger_not_initialized")
                calendar = ExchangeSessionResolver()
                ledger = PortfolioLedger(
                    paths.ledger_database,
                    policy=config.ledger_policy,
                    calendar_resolver=calendar,
                )
                try:
                    checkpoint = ledger.opening_checkpoint()
                except LedgerNotInitializedError as exc:
                    raise PrivateDailyNotInitialized(
                        "private_ledger_not_initialized"
                    ) from exc
                opening_audit = audit_opening_owner_attestation(
                    config,
                    paths,
                    config_bytes_sha256=config_bytes_sha256,
                    now=_clock(),
                    ledger_binding=OpeningLedgerBinding(
                        opening_event_id=checkpoint.opening_event_id,
                        opening_event_hash=checkpoint.opening_event_hash,
                        idempotency_key=checkpoint.idempotency_key,
                        created_at=checkpoint.created_at,
                    ),
                )
                if opening_audit.state != "consumed_verified":
                    raise PrivateDailyIntegrityError(
                        "opening_owner_attestation_not_consumed"
                    )
                if ledger.latest_common_valuation() is None:
                    raise PrivateDailyNotInitialized(
                        "opening_valuations_not_initialized"
                    )
                outbox = DailyReportOutbox(paths.outbox_database)
                runtime = PrivateDailyRuntime(
                    config,
                    calendar=calendar,
                    close_registry=_registry(config, environment, _clock),
                    ledger=ledger,
                    outbox=outbox,
                    report_directory=paths.report_directory,
                    clock=_clock,
                    runtime_paths=paths,
                    config_bytes_sha256=config_bytes_sha256,
                )
                missing = missing_provider_environment(environment)
                result = runtime.prepare(
                    target_key,
                    preflight_block_reason=(
                        None if not missing else "provider_credentials_missing"
                    ),
                )
                _tighten_runtime_files(paths)
            if result.status == "pending_prior_delivery":
                return _fixed_error(EXIT_DELIVERY_PENDING)
            return EXIT_OK
    except BaseException as exc:
        return _fixed_error(_map_exception(exc))


def initialize_private_daily_main(
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Explicitly initialize the private ledger; never run from the daily job."""

    environment = os.environ if environ is None else environ
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            config, config_bytes_sha256 = _load_live_config_bundle(environment)
            paths = resolve_private_runtime_paths(config, environment)
            if missing_provider_environment(environment):
                return _fixed_error(EXIT_NOT_INITIALIZED)
            ensure_private_storage(paths)
            with private_runtime_lock(paths.lock_file):
                calendar = ExchangeSessionResolver()
                def ledger_factory() -> PortfolioLedger:
                    return PortfolioLedger(
                        paths.ledger_database,
                        policy=config.ledger_policy,
                        calendar_resolver=calendar,
                    )

                ledger = (
                    ledger_factory()
                    if os.path.lexists(str(paths.ledger_database))
                    else None
                )
                initialize_private_ledger(
                    config,
                    runtime_paths=paths,
                    config_bytes_sha256=config_bytes_sha256,
                    ledger=ledger,
                    ledger_factory=ledger_factory,
                    close_registry=_registry(config, environment, _clock),
                    calendar=calendar,
                    clock=_clock,
                )
                _tighten_runtime_files(paths)
            return EXIT_OK
    except BaseException as exc:
        return _fixed_error(_map_exception(exc))


def attest_private_opening_main(
    *,
    environ: Mapping[str, str] | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    """Create the one-time opening proof; never called by the daily job."""

    environment = os.environ if environ is None else environ
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            config, config_bytes_sha256 = _load_live_config_bundle(environment)
            paths = resolve_private_runtime_paths(config, environment)
            owner_output = sys.stderr if output_stream is None else output_stream
            owner_presence = interactive_owner_presence(
                sys.stdin if input_stream is None else input_stream,
                owner_output,
            )
            ensure_private_storage(paths)
            with private_runtime_lock(paths.lock_file):
                create_opening_owner_claim(
                    config,
                    paths,
                    config_bytes_sha256=config_bytes_sha256,
                    owner_presence=owner_presence,
                    clock=_clock,
                )
            owner_output.write("PRIVATE_OPENING_ATTESTATION:RECORDED\n")
            owner_output.flush()
            return EXIT_OK
    except BaseException as exc:
        code = _map_exception(exc)
        line = _ATTESTATION_ERROR_LINES.get(
            code,
            _ATTESTATION_ERROR_LINES[EXIT_INTERNAL],
        )
        sys.stderr.write(line + "\n")
        return code if code in _ATTESTATION_ERROR_LINES else EXIT_INTERNAL


def attest_private_event_main(
    *,
    environ: Mapping[str, str] | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    """Approve one fixed owner-only request; never ingest prose or submit an order."""

    environment = os.environ if environ is None else environ
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            config, config_bytes_sha256 = _load_live_config_bundle(environment)
            paths = resolve_private_runtime_paths(config, environment)
            owner_input = sys.stdin if input_stream is None else input_stream
            owner_output = sys.stderr if output_stream is None else output_stream
            with private_runtime_lock(paths.lock_file):
                if not os.path.lexists(str(paths.ledger_database)):
                    raise PrivateDailyNotInitialized("private_ledger_not_initialized")
                calendar = ExchangeSessionResolver()
                ledger = PortfolioLedger(
                    paths.ledger_database,
                    policy=config.ledger_policy,
                    calendar_resolver=calendar,
                )
                try:
                    checkpoint = ledger.opening_checkpoint()
                except LedgerNotInitializedError as exc:
                    raise PrivateDailyNotInitialized(
                        "private_ledger_not_initialized"
                    ) from exc
                opening_audit = audit_opening_owner_attestation(
                    config,
                    paths,
                    config_bytes_sha256=config_bytes_sha256,
                    now=_clock(),
                    ledger_binding=OpeningLedgerBinding(
                        opening_event_id=checkpoint.opening_event_id,
                        opening_event_hash=checkpoint.opening_event_hash,
                        idempotency_key=checkpoint.idempotency_key,
                        created_at=checkpoint.created_at,
                    ),
                )
                if opening_audit.state != "consumed_verified":
                    raise PrivateDailyIntegrityError(
                        "opening_owner_attestation_not_consumed"
                    )
                if ledger.latest_common_valuation() is None:
                    raise PrivateDailyNotInitialized(
                        "opening_valuations_not_initialized"
                    )
                request = load_manual_event_request(config, paths)
                # Validate that the declared session exists without requiring
                # it to have closed yet; a future explicit DCA skip is valid.
                try:
                    calendar.session_close(request.session, config.primary_mic)
                except ExchangeSessionError as exc:
                    raise ManualOwnerEventError(
                        "manual_event_exchange_session_invalid"
                    ) from exc
                observed = _clock()
                if (
                    request.occurred_at is not None
                    and request.occurred_at > observed + dt.timedelta(seconds=30)
                ):
                    raise ManualOwnerEventError("manual_event_time_in_future")
                watermark = ledger.latest_valuation_watermark()
                if watermark is not None and request.session <= watermark:
                    raise ManualOwnerEventError(
                        "manual_event_after_valuation_finality"
                    )
                if os.path.lexists(str(paths.outbox_database)):
                    outbox = DailyReportOutbox(paths.outbox_database)
                    outbox.require_ledger_mutation_allowed(
                        ledger.contains_event_hash,
                    )
                request_head = ledger.last_event_hash()
                proof = interactive_manual_event_presence(
                    request,
                    owner_input,
                    owner_output,
                )
                # Re-read the exact config and all mutable gates while still
                # holding the shared runtime lock.  The approval function then
                # re-reads and digest-compares the fixed request itself.
                reloaded, reloaded_digest = _load_live_config_bundle(environment)
                if reloaded_digest != config_bytes_sha256 or reloaded != config:
                    raise ManualOwnerEventError(
                        "manual_event_config_changed_after_confirmation"
                    )
                if ledger.last_event_hash() != request_head:
                    raise ManualOwnerEventError(
                        "manual_event_ledger_changed_after_confirmation"
                    )
                if os.path.lexists(str(paths.outbox_database)):
                    outbox.require_ledger_mutation_allowed(
                        ledger.contains_event_hash,
                    )
                approve_manual_event(
                    config,
                    paths,
                    request,
                    proof,
                    _clock,
                )
            owner_output.write("PRIVATE_MANUAL_EVENT:APPROVED\n")
            owner_output.flush()
            return EXIT_OK
    except BaseException as exc:
        code = _map_exception(exc)
        line = _MANUAL_EVENT_ERROR_LINES.get(
            code,
            _MANUAL_EVENT_ERROR_LINES[EXIT_INTERNAL],
        )
        sys.stderr.write(line + "\n")
        return code if code in _MANUAL_EVENT_ERROR_LINES else EXIT_INTERNAL


__all__ = [
    "EXIT_BUSY",
    "EXIT_CONFIG_OR_PRIVACY",
    "EXIT_DELIVERY_PENDING",
    "EXIT_INTEGRITY",
    "EXIT_INTERNAL",
    "EXIT_INTERRUPTED",
    "EXIT_NOT_INITIALIZED",
    "EXIT_OK",
    "EXIT_PERSISTENCE",
    "PRIVATE_CONFIG_ENV",
    "attest_private_opening_main",
    "attest_private_event_main",
    "initialize_private_daily_main",
    "run_private_daily_main",
]
