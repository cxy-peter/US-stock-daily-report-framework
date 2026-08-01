"""Explicitly initialize private daily accounting without CLI arguments."""
from __future__ import annotations

from serenity_monitor.private_runtime_cli import initialize_private_daily_main


if __name__ == "__main__":
    raise SystemExit(initialize_private_daily_main())
