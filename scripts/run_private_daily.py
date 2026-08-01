"""Prepare one owner-only daily report without command-line arguments."""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_main():
    """Import behind a silent boundary so optional dependencies cannot leak paths."""

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            from serenity_monitor.private_runtime_cli import run_private_daily_main
    except KeyboardInterrupt:
        return None, 130, "PRIVATE_DAILY_RUNTIME:INTERRUPTED"
    except BaseException:
        return None, 70, "PRIVATE_DAILY_RUNTIME:INTERNAL_FAILURE"
    return run_private_daily_main, 0, ""


if __name__ == "__main__":
    main, import_exit_code, import_error = _load_main()
    if main is None:
        sys.stderr.write(import_error + "\n")
        raise SystemExit(import_exit_code)
    raise SystemExit(main())
