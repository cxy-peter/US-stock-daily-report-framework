"""Silent production entrypoints for the owner-only private runtime."""
from __future__ import annotations

import datetime as dt
import os
import sys
import warnings
from pathlib import Path
from typing import Mapping

from .daily_outbox import DailyOutboxError, DailyReportOutbox
from .portfolio_ledger import PortfolioLedger, PortfolioLedgerError
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
from .trading_calendar import ExchangeSessionResolver


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


def _clock() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _fixed_error(exit_code: int) -> int:
    line = _ERROR_LINES[exit_code]
    sys.stderr.write(line + "\n")
    return exit_code


def _load_live_config(environ: Mapping[str, str]):
    raw_path = str(environ.get(PRIVATE_CONFIG_ENV, "")).strip()
    if not raw_path:
        raise PrivateRuntimePathError("private_configuration_environment_missing")
    config_path, payload = read_validated_live_private_config(Path(raw_path))
    return load_private_daily_runtime_config(
        config_path,
        allow_synthetic=False,
        _validated_bytes=payload,
    )


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
    if isinstance(exc, PrivateDailyNotInitialized):
        return EXIT_NOT_INITIALIZED
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
            config = _load_live_config(environment)
            paths = resolve_private_runtime_paths(config, environment)
            target_key = require_delivery_target(config, environment)
            ensure_private_storage(paths)
            with private_runtime_lock(paths.lock_file):
                calendar = ExchangeSessionResolver()
                ledger = PortfolioLedger(
                    paths.ledger_database,
                    policy=config.ledger_policy,
                    calendar_resolver=calendar,
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
            config = _load_live_config(environment)
            paths = resolve_private_runtime_paths(config, environment)
            if missing_provider_environment(environment):
                return _fixed_error(EXIT_NOT_INITIALIZED)
            ensure_private_storage(paths)
            with private_runtime_lock(paths.lock_file):
                calendar = ExchangeSessionResolver()
                ledger = PortfolioLedger(
                    paths.ledger_database,
                    policy=config.ledger_policy,
                    calendar_resolver=calendar,
                )
                initialize_private_ledger(
                    config,
                    ledger=ledger,
                    close_registry=_registry(config, environment, _clock),
                    calendar=calendar,
                    as_of=_clock(),
                )
                _tighten_runtime_files(paths)
            return EXIT_OK
    except BaseException as exc:
        return _fixed_error(_map_exception(exc))


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
    "initialize_private_daily_main",
    "run_private_daily_main",
]
