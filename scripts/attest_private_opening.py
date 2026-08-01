"""Create one owner-only opening proof after a fixed explicit confirmation."""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_main():
    """Import behind a silent boundary so private dependency failures stay fixed."""

    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            from serenity_monitor.private_runtime_cli import attest_private_opening_main
    except KeyboardInterrupt:
        return None, 130, "PRIVATE_OPENING_ATTESTATION:INTERRUPTED"
    except BaseException:
        return None, 70, "PRIVATE_OPENING_ATTESTATION:INTERNAL_FAILURE"
    return attest_private_opening_main, 0, ""


def _run() -> int:
    if len(sys.argv) != 1:
        sys.stderr.write("PRIVATE_OPENING_ATTESTATION:CONFIG_OR_PRIVACY_REJECTED\n")
        return 20
    main, import_exit_code, import_error = _load_main()
    if main is None:
        sys.stderr.write(import_error + "\n")
        return import_exit_code
    try:
        return int(main())
    except KeyboardInterrupt:
        sys.stderr.write("PRIVATE_OPENING_ATTESTATION:INTERRUPTED\n")
        return 130
    except BaseException:
        sys.stderr.write("PRIVATE_OPENING_ATTESTATION:INTERNAL_FAILURE\n")
        return 70


if __name__ == "__main__":
    raise SystemExit(_run())
