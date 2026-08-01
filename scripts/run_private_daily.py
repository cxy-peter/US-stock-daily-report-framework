"""Prepare one owner-only daily report without command-line arguments."""
from __future__ import annotations

from serenity_monitor.private_runtime_cli import run_private_daily_main


if __name__ == "__main__":
    raise SystemExit(run_private_daily_main())
