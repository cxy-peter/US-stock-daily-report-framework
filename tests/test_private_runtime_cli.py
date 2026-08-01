from __future__ import annotations

import io
import os
import subprocess
import sys
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from serenity_monitor import private_runtime_cli
import serenity_monitor.opening_owner_attestation as opening_attestation
from serenity_monitor.private_runtime_config import PrivateRuntimeConfigError
from serenity_monitor.private_runtime_paths import PrivateRuntimePathError
from scripts import attest_private_opening as attestation_script


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


@pytest.mark.parametrize(
    ("script_name", "expected_line"),
    [
        ("attest_private_opening.py", "PRIVATE_OPENING_ATTESTATION:CONFIG_OR_PRIVACY_REJECTED\n"),
        ("initialize_private_daily.py", "PRIVATE_DAILY_RUNTIME:CONFIG_OR_PRIVACY_REJECTED\n"),
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
