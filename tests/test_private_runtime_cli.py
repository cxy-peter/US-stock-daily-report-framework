from __future__ import annotations

import io
import os
import subprocess
import sys
import datetime as dt
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from serenity_monitor import private_runtime_cli
import serenity_monitor.opening_owner_attestation as opening_attestation
from serenity_monitor.private_runtime_config import PrivateRuntimeConfigError
from serenity_monitor.private_runtime_paths import PrivateRuntimePathError
from scripts import attest_private_opening as attestation_script
from scripts import attest_private_event as manual_event_script
from scripts import publish_private_research as research_script


ROOT = Path(__file__).resolve().parents[1]


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def _capture(callable_):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = callable_()
    return code, stdout.getvalue(), stderr.getvalue()


def test_missing_configuration_environment_is_fixed_and_silent() -> None:
    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.run_private_daily_main(environ={})
    )

    assert code == private_runtime_cli.EXIT_CONFIG_OR_PRIVACY
    assert stdout == ""
    assert stderr == "PRIVATE_DAILY_RUNTIME:CONFIG_OR_PRIVACY_REJECTED\n"


def test_research_snapshot_publisher_uses_fixed_request_and_fixed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = object()
    paths = SimpleNamespace(root=tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        private_runtime_cli,
        "_load_live_config",
        lambda _environment: config,
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "resolve_private_runtime_paths",
        lambda _config, _environment: paths,
    )
    monkeypatch.setattr(private_runtime_cli, "ensure_private_storage", lambda _p: None)
    monkeypatch.setattr(
        private_runtime_cli,
        "publish_private_research_snapshot_request",
        lambda value, **kwargs: captured.update(paths=value, kwargs=kwargs),
    )

    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.publish_private_research_main(
            environ={private_runtime_cli.PRIVATE_CONFIG_ENV: "private-sentinel"}
        )
    )

    assert code == private_runtime_cli.EXIT_OK
    assert stdout == ""
    assert stderr == "PRIVATE_RESEARCH_SNAPSHOT:PUBLISHED\n"
    assert captured["paths"] is paths
    assert "prepared_at" in captured["kwargs"]


def test_research_snapshot_commit_unknown_maps_to_persistence_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = SimpleNamespace(root=tmp_path)
    monkeypatch.setattr(private_runtime_cli, "_load_live_config", lambda _env: object())
    monkeypatch.setattr(
        private_runtime_cli,
        "resolve_private_runtime_paths",
        lambda _config, _environment: paths,
    )
    monkeypatch.setattr(private_runtime_cli, "ensure_private_storage", lambda _p: None)

    def fail_publish(*_args, **_kwargs):
        raise private_runtime_cli.PrivateResearchStoreCommitUnknown(
            "research_snapshot_commit_state_unknown"
        )

    monkeypatch.setattr(
        private_runtime_cli,
        "publish_private_research_snapshot_request",
        fail_publish,
    )

    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.publish_private_research_main(
            environ={private_runtime_cli.PRIVATE_CONFIG_ENV: "private-sentinel"}
        )
    )

    assert code == private_runtime_cli.EXIT_PERSISTENCE
    assert stdout == ""
    assert stderr == "PRIVATE_RESEARCH_SNAPSHOT:PERSISTENCE_FAILED\n"


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_line"),
    [
        (
            PrivateRuntimeConfigError("sentinel-private-config"),
            private_runtime_cli.EXIT_CONFIG_OR_PRIVACY,
            "PRIVATE_DAILY_RUNTIME:CONFIG_OR_PRIVACY_REJECTED\n",
        ),
        (
            PrivateRuntimePathError("sentinel-private-path"),
            private_runtime_cli.EXIT_CONFIG_OR_PRIVACY,
            "PRIVATE_DAILY_RUNTIME:CONFIG_OR_PRIVACY_REJECTED\n",
        ),
        (
            RuntimeError("sentinel-api-key-target-ticker-amount"),
            private_runtime_cli.EXIT_INTERNAL,
            "PRIVATE_DAILY_RUNTIME:INTERNAL_FAILURE\n",
        ),
        (
            KeyboardInterrupt("sentinel-interrupt"),
            private_runtime_cli.EXIT_INTERRUPTED,
            "PRIVATE_DAILY_RUNTIME:INTERRUPTED\n",
        ),
    ],
)
def test_exception_text_never_reaches_stdout_or_stderr(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    expected_code: int,
    expected_line: str,
) -> None:
    def fail(_environ):
        raise error

    monkeypatch.setattr(private_runtime_cli, "_load_live_config_bundle", fail)

    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.run_private_daily_main(
            environ={private_runtime_cli.PRIVATE_CONFIG_ENV: "sentinel-path"}
        )
    )

    assert code == expected_code
    assert stdout == ""
    assert stderr == expected_line
    combined = stdout + stderr
    assert "sentinel" not in combined
    assert "Traceback" not in combined


def test_initialize_entrypoint_uses_same_fixed_privacy_boundary() -> None:
    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.initialize_private_daily_main(environ={})
    )

    assert code == private_runtime_cli.EXIT_CONFIG_OR_PRIVACY
    assert stdout == ""
    assert stderr == "PRIVATE_DAILY_RUNTIME:CONFIG_OR_PRIVACY_REJECTED\n"


def test_attestation_requires_live_config_before_any_prompt() -> None:
    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.attest_private_opening_main(
            environ={},
            input_stream=TTYBuffer(),
            output_stream=TTYBuffer(),
        )
    )

    assert code == private_runtime_cli.EXIT_CONFIG_OR_PRIVACY
    assert stdout == ""
    assert stderr == "PRIVATE_OPENING_ATTESTATION:CONFIG_OR_PRIVACY_REJECTED\n"
    assert "Type CONFIRM" not in stderr


def test_manual_event_attestation_requires_live_config_before_any_prompt() -> None:
    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.attest_private_event_main(
            environ={},
            input_stream=TTYBuffer(),
            output_stream=TTYBuffer(),
        )
    )

    assert code == private_runtime_cli.EXIT_CONFIG_OR_PRIVACY
    assert stdout == ""
    assert stderr == "PRIVATE_MANUAL_EVENT:CONFIG_OR_PRIVACY_REJECTED\n"


def test_manual_event_attestation_approves_only_inside_the_guarded_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime(2026, 1, 6, 5, tzinfo=dt.timezone.utc)
    config = SimpleNamespace(ledger_policy=object(), primary_mic="XNAS")
    ledger_path = tmp_path / "ledger.sqlite3"
    ledger_path.touch()
    paths = SimpleNamespace(
        ledger_database=ledger_path,
        outbox_database=tmp_path / "missing-outbox.sqlite3",
        lock_file=tmp_path / "private.lock",
    )
    checkpoint = SimpleNamespace(
        opening_event_id="1" * 64,
        opening_event_hash="2" * 64,
        idempotency_key="opening-key",
        created_at=now - dt.timedelta(days=4),
    )

    class Ledger:
        def opening_checkpoint(self):
            return checkpoint

        def latest_common_valuation(self):
            return object()

        def latest_valuation_watermark(self):
            return dt.date(2026, 1, 2)

        def last_event_hash(self):
            return "3" * 64

    request = SimpleNamespace(
        session=dt.date(2026, 1, 5),
        occurred_at=now - dt.timedelta(hours=10),
    )
    proof = object()
    approved: dict[str, object] = {}
    monkeypatch.setattr(
        private_runtime_cli,
        "_load_live_config_bundle",
        lambda _environment: (config, "a" * 64),
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "resolve_private_runtime_paths",
        lambda _config, _environment: paths,
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "private_runtime_lock",
        lambda _path: nullcontext(),
    )
    monkeypatch.setattr(private_runtime_cli, "PortfolioLedger", lambda *_args, **_kwargs: Ledger())
    monkeypatch.setattr(
        private_runtime_cli,
        "audit_opening_owner_attestation",
        lambda *_args, **_kwargs: SimpleNamespace(state="consumed_verified"),
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "load_manual_event_request",
        lambda *_args: request,
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "interactive_manual_event_presence",
        lambda request_arg, input_arg, output_arg: proof,
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "approve_manual_event",
        lambda *args: approved.update(request=args[2], proof=args[3]),
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "ExchangeSessionResolver",
        lambda: SimpleNamespace(session_close=lambda *_args: now),
    )
    monkeypatch.setattr(private_runtime_cli, "_clock", lambda: now)
    owner_output = TTYBuffer()

    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.attest_private_event_main(
            environ={private_runtime_cli.PRIVATE_CONFIG_ENV: "private-sentinel"},
            input_stream=TTYBuffer(),
            output_stream=owner_output,
        )
    )

    assert code == private_runtime_cli.EXIT_OK
    assert stdout == stderr == ""
    assert owner_output.getvalue() == "PRIVATE_MANUAL_EVENT:APPROVED\n"
    assert approved == {"request": request, "proof": proof}


def test_attestation_rejects_non_tty_with_fixed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        private_runtime_cli,
        "_load_live_config_bundle",
        lambda _environment: (object(), "a" * 64),
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "resolve_private_runtime_paths",
        lambda _config, _environment: object(),
    )

    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.attest_private_opening_main(
            environ={private_runtime_cli.PRIVATE_CONFIG_ENV: "private-sentinel"},
            input_stream=io.StringIO("CONFIRM AAAAAAAAAA\n"),
            output_stream=io.StringIO(),
        )
    )

    assert code == private_runtime_cli.EXIT_INTEGRITY
    assert stdout == ""
    assert stderr == "PRIVATE_OPENING_ATTESTATION:INTEGRITY_REJECTED\n"
    assert "private-sentinel" not in stderr


def test_attestation_success_is_interactive_redacted_and_records_exact_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = object()
    paths = SimpleNamespace(lock_file=tmp_path / "private.lock")
    calls = {}
    monkeypatch.setattr(
        private_runtime_cli,
        "_load_live_config_bundle",
        lambda _environment: (config, "b" * 64),
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "resolve_private_runtime_paths",
        lambda _config, _environment: paths,
    )
    monkeypatch.setattr(private_runtime_cli, "ensure_private_storage", lambda _paths: None)
    monkeypatch.setattr(
        private_runtime_cli,
        "private_runtime_lock",
        lambda _path: nullcontext(),
    )
    monkeypatch.setattr(opening_attestation.secrets, "choice", lambda _alphabet: "A")

    def record(config_arg, paths_arg, **kwargs):
        calls.update(config=config_arg, paths=paths_arg, **kwargs)

    monkeypatch.setattr(private_runtime_cli, "create_opening_owner_claim", record)
    owner_output = TTYBuffer()
    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.attest_private_opening_main(
            environ={private_runtime_cli.PRIVATE_CONFIG_ENV: "private-sentinel"},
            input_stream=TTYBuffer("CONFIRM AAAAAAAAAA\n"),
            output_stream=owner_output,
        )
    )

    assert code == private_runtime_cli.EXIT_OK
    assert stdout == ""
    assert stderr == ""
    assert owner_output.getvalue() == (
        "Review the owner-only opening snapshot before confirming.\n"
        "Type CONFIRM AAAAAAAAAA to attest it: "
        "PRIVATE_OPENING_ATTESTATION:RECORDED\n"
    )
    assert calls["config"] is config
    assert calls["paths"] is paths
    assert calls["config_bytes_sha256"] == "b" * 64
    assert "private-sentinel" not in owner_output.getvalue()


def test_initialize_success_passes_exact_config_digest_and_lazy_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(ledger_policy=object())
    paths = SimpleNamespace(
        ledger_database=tmp_path / "ledger.sqlite3",
        lock_file=tmp_path / "private.lock",
    )
    captured = {}
    fake_ledger = object()
    monkeypatch.setattr(
        private_runtime_cli,
        "_load_live_config_bundle",
        lambda _environment: (config, "c" * 64),
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "resolve_private_runtime_paths",
        lambda _config, _environment: paths,
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "missing_provider_environment",
        lambda _environment: (),
    )
    monkeypatch.setattr(private_runtime_cli, "ensure_private_storage", lambda _paths: None)
    monkeypatch.setattr(
        private_runtime_cli,
        "private_runtime_lock",
        lambda _path: nullcontext(),
    )
    monkeypatch.setattr(private_runtime_cli, "ExchangeSessionResolver", object)
    monkeypatch.setattr(
        private_runtime_cli,
        "PortfolioLedger",
        lambda *_args, **_kwargs: fake_ledger,
    )
    monkeypatch.setattr(private_runtime_cli, "_registry", lambda *_args: object())
    monkeypatch.setattr(private_runtime_cli, "_tighten_runtime_files", lambda _paths: None)

    def initialize(config_arg, **kwargs):
        captured.update(config=config_arg, **kwargs)
        assert kwargs["ledger"] is None
        assert kwargs["ledger_factory"]() is fake_ledger

    monkeypatch.setattr(private_runtime_cli, "initialize_private_ledger", initialize)

    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.initialize_private_daily_main(
            environ={private_runtime_cli.PRIVATE_CONFIG_ENV: "private-sentinel"}
        )
    )

    assert code == private_runtime_cli.EXIT_OK
    assert stdout == ""
    assert stderr == ""
    assert captured["config"] is config
    assert captured["config_bytes_sha256"] == "c" * 64


def test_daily_missing_ledger_never_constructs_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = object()
    paths = SimpleNamespace(
        ledger_database=tmp_path / "missing.sqlite3",
        lock_file=tmp_path / "private.lock",
    )
    calls = {"ledger": 0, "outbox": 0}
    monkeypatch.setattr(
        private_runtime_cli,
        "_load_live_config_bundle",
        lambda _environment: (config, "d" * 64),
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "resolve_private_runtime_paths",
        lambda _config, _environment: paths,
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "require_delivery_target",
        lambda _config, _environment: "target",
    )
    monkeypatch.setattr(private_runtime_cli, "ensure_private_storage", lambda _paths: None)
    monkeypatch.setattr(
        private_runtime_cli,
        "private_runtime_lock",
        lambda _path: nullcontext(),
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "PortfolioLedger",
        lambda *_args, **_kwargs: calls.__setitem__("ledger", calls["ledger"] + 1),
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "DailyReportOutbox",
        lambda *_args, **_kwargs: calls.__setitem__("outbox", calls["outbox"] + 1),
    )

    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.run_private_daily_main(
            environ={private_runtime_cli.PRIVATE_CONFIG_ENV: "private-sentinel"}
        )
    )

    assert code == private_runtime_cli.EXIT_NOT_INITIALIZED
    assert stdout == ""
    assert stderr == "PRIVATE_DAILY_RUNTIME:NOT_INITIALIZED\n"
    assert calls == {"ledger": 0, "outbox": 0}


def test_daily_schema_only_ledger_never_constructs_outbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(ledger_policy=object())
    ledger_path = tmp_path / "schema-only.sqlite3"
    ledger_path.touch()
    paths = SimpleNamespace(
        ledger_database=ledger_path,
        lock_file=tmp_path / "private.lock",
    )
    outbox_calls = 0

    class EmptyLedger:
        def opening_checkpoint(self):
            raise private_runtime_cli.LedgerNotInitializedError("empty")

    def outbox(*_args, **_kwargs):
        nonlocal outbox_calls
        outbox_calls += 1

    monkeypatch.setattr(
        private_runtime_cli,
        "_load_live_config_bundle",
        lambda _environment: (config, "e" * 64),
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "resolve_private_runtime_paths",
        lambda _config, _environment: paths,
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "require_delivery_target",
        lambda _config, _environment: "target",
    )
    monkeypatch.setattr(private_runtime_cli, "ensure_private_storage", lambda _paths: None)
    monkeypatch.setattr(
        private_runtime_cli,
        "private_runtime_lock",
        lambda _path: nullcontext(),
    )
    monkeypatch.setattr(private_runtime_cli, "ExchangeSessionResolver", object)
    monkeypatch.setattr(
        private_runtime_cli,
        "PortfolioLedger",
        lambda *_args, **_kwargs: EmptyLedger(),
    )
    monkeypatch.setattr(private_runtime_cli, "DailyReportOutbox", outbox)

    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.run_private_daily_main(
            environ={private_runtime_cli.PRIVATE_CONFIG_ENV: "private-sentinel"}
        )
    )

    assert code == private_runtime_cli.EXIT_NOT_INITIALIZED
    assert stdout == ""
    assert stderr == "PRIVATE_DAILY_RUNTIME:NOT_INITIALIZED\n"
    assert outbox_calls == 0


def test_daily_entrypoint_loads_owner_only_research_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(ledger_policy=object())
    ledger_path = tmp_path / "portfolio-ledger.sqlite3"
    ledger_path.touch()
    paths = SimpleNamespace(
        ledger_database=ledger_path,
        outbox_database=tmp_path / "daily-outbox.sqlite3",
        report_directory=tmp_path / "reports",
        lock_file=tmp_path / "private.lock",
        research_snapshot_file=tmp_path / "research-snapshot.latest.json",
    )
    checkpoint = SimpleNamespace(
        opening_event_id="1" * 64,
        opening_event_hash="2" * 64,
        idempotency_key="opening-key",
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    projection = object()
    captured: dict[str, object] = {}

    class Ledger:
        def opening_checkpoint(self):
            return checkpoint

        def latest_common_valuation(self):
            return object()

    class Runtime:
        def __init__(self, *args, **kwargs):
            captured["runtime_kwargs"] = kwargs

        def prepare(self, target_key, **kwargs):
            captured["target_key"] = target_key
            captured["prepare_kwargs"] = kwargs
            return SimpleNamespace(status="prepared")

    monkeypatch.setattr(
        private_runtime_cli,
        "_load_live_config_bundle",
        lambda _environment: (config, "f" * 64),
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "resolve_private_runtime_paths",
        lambda _config, _environment: paths,
    )
    monkeypatch.setattr(
        private_runtime_cli,
        "require_delivery_target",
        lambda _config, _environment: "target",
    )
    monkeypatch.setattr(private_runtime_cli, "ensure_private_storage", lambda _paths: None)
    monkeypatch.setattr(
        private_runtime_cli,
        "private_runtime_lock",
        lambda _path: nullcontext(),
    )
    monkeypatch.setattr(private_runtime_cli, "ExchangeSessionResolver", object)
    monkeypatch.setattr(private_runtime_cli, "PortfolioLedger", lambda *_a, **_k: Ledger())
    monkeypatch.setattr(
        private_runtime_cli,
        "audit_opening_owner_attestation",
        lambda *_a, **_k: SimpleNamespace(state="consumed_verified"),
    )
    monkeypatch.setattr(private_runtime_cli, "DailyReportOutbox", lambda *_a: object())
    monkeypatch.setattr(private_runtime_cli, "PrivateDailyRuntime", Runtime)
    monkeypatch.setattr(private_runtime_cli, "_registry", lambda *_a: object())
    monkeypatch.setattr(private_runtime_cli, "missing_provider_environment", lambda _e: ())
    monkeypatch.setattr(
        private_runtime_cli,
        "load_private_research_snapshot",
        lambda *_a, **_k: SimpleNamespace(projection=projection),
    )
    monkeypatch.setattr(private_runtime_cli, "_tighten_runtime_files", lambda _p: None)

    code, stdout, stderr = _capture(
        lambda: private_runtime_cli.run_private_daily_main(
            environ={private_runtime_cli.PRIVATE_CONFIG_ENV: "private-sentinel"}
        )
    )

    assert code == private_runtime_cli.EXIT_OK
    assert stdout == ""
    assert stderr == ""
    assert captured["target_key"] == "target"
    assert captured["prepare_kwargs"]["research_projection"] is projection


@pytest.mark.parametrize(
    ("script_name", "expected_line"),
    [
        ("attest_private_opening.py", "PRIVATE_OPENING_ATTESTATION:CONFIG_OR_PRIVACY_REJECTED\n"),
        ("attest_private_event.py", "PRIVATE_MANUAL_EVENT:CONFIG_OR_PRIVACY_REJECTED\n"),
        ("initialize_private_daily.py", "PRIVATE_DAILY_RUNTIME:CONFIG_OR_PRIVACY_REJECTED\n"),
        ("publish_private_research.py", "PRIVATE_RESEARCH_SNAPSHOT:CONFIG_OR_PRIVACY_REJECTED\n"),
        ("run_private_daily.py", "PRIVATE_DAILY_RUNTIME:CONFIG_OR_PRIVACY_REJECTED\n"),
    ],
)
def test_direct_script_entrypoint_imports_package_and_emits_only_fixed_error(
    tmp_path: Path,
    script_name: str,
    expected_line: str,
) -> None:
    environment = os.environ.copy()
    environment.pop("SERENITY_PRIVATE_CONFIG", None)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == private_runtime_cli.EXIT_CONFIG_OR_PRIVACY
    assert result.stdout == ""
    assert result.stderr == expected_line
    assert "Traceback" not in result.stderr
    assert str(ROOT) not in result.stderr


def test_attestation_script_rejects_arguments_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = False

    def load_main():
        nonlocal imported
        imported = True
        return None, 70, "PRIVATE_OPENING_ATTESTATION:INTERNAL_FAILURE"

    monkeypatch.setattr(attestation_script, "_load_main", load_main)
    monkeypatch.setattr(sys, "argv", ["attest_private_opening.py", "private-value"])

    code, stdout, stderr = _capture(attestation_script._run)

    assert code == private_runtime_cli.EXIT_CONFIG_OR_PRIVACY
    assert stdout == ""
    assert stderr == "PRIVATE_OPENING_ATTESTATION:CONFIG_OR_PRIVACY_REJECTED\n"
    assert imported is False
    assert "private-value" not in stderr


def test_manual_event_script_rejects_arguments_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = False

    def load_main():
        nonlocal imported
        imported = True
        return None, 70, "PRIVATE_MANUAL_EVENT:INTERNAL_FAILURE"

    monkeypatch.setattr(manual_event_script, "_load_main", load_main)
    monkeypatch.setattr(sys, "argv", ["attest_private_event.py", "private-payload"])

    code, stdout, stderr = _capture(manual_event_script._run)

    assert code == private_runtime_cli.EXIT_CONFIG_OR_PRIVACY
    assert stdout == ""
    assert stderr == "PRIVATE_MANUAL_EVENT:CONFIG_OR_PRIVACY_REJECTED\n"
    assert imported is False
    assert "private-payload" not in stderr


def test_research_snapshot_script_rejects_arguments_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported = False

    def load_main():
        nonlocal imported
        imported = True
        return None, 70, "PRIVATE_RESEARCH_SNAPSHOT:INTERNAL_FAILURE"

    monkeypatch.setattr(research_script, "_load_main", load_main)
    monkeypatch.setattr(sys, "argv", ["publish_private_research.py", "private-value"])

    code, stdout, stderr = _capture(research_script._run)

    assert code == private_runtime_cli.EXIT_CONFIG_OR_PRIVACY
    assert stdout == ""
    assert stderr == "PRIVATE_RESEARCH_SNAPSHOT:CONFIG_OR_PRIVACY_REJECTED\n"
    assert imported is False
    assert "private-value" not in stderr


def test_attestation_script_import_failure_is_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        attestation_script,
        "_load_main",
        lambda: (None, 70, "PRIVATE_OPENING_ATTESTATION:INTERNAL_FAILURE"),
    )
    monkeypatch.setattr(sys, "argv", ["attest_private_opening.py"])

    code, stdout, stderr = _capture(attestation_script._run)

    assert code == private_runtime_cli.EXIT_INTERNAL
    assert stdout == ""
    assert stderr == "PRIVATE_OPENING_ATTESTATION:INTERNAL_FAILURE\n"
