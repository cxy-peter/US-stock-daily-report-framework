"""Emit one redacted, read-only private-daily activation readiness object."""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_main():
    """Import behind the same silent boundary as the private runtime CLIs."""

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            from serenity_monitor.private_daily_readiness import (
                run_private_daily_readiness_main,
            )
    except KeyboardInterrupt:
        return None, 130, "PRIVATE_DAILY_READINESS:INTERRUPTED"
    except BaseException:
        return None, 70, "PRIVATE_DAILY_READINESS:INTERNAL_FAILURE"
    return run_private_daily_readiness_main, 0, ""


def _run() -> int:
    """Guard both import and execution without rendering an exception."""

    main, import_exit_code, import_error = _load_main()
    if main is None:
        sys.stderr.write(import_error + "\n")
        return import_exit_code
    try:
        return int(main())
    except KeyboardInterrupt:
        sys.stderr.write("PRIVATE_DAILY_READINESS:INTERRUPTED\n")
        return 130
    except BaseException:
        sys.stderr.write("PRIVATE_DAILY_READINESS:INTERNAL_FAILURE\n")
        return 70


if __name__ == "__main__":
    raise SystemExit(_run())
