from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from serenity_monitor import private_runtime_cli
from serenity_monitor.private_runtime_config import PrivateRuntimeConfigError
from serenity_monitor.private_runtime_paths import PrivateRuntimePathError


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

    monkeypatch.setattr(private_runtime_cli, "_load_live_config", fail)

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
