from __future__ import annotations

from pathlib import Path

import pytest

from serenity_monitor.private_runtime_lock import (
    PrivateRuntimeBusy,
    PrivateRuntimeLockError,
    private_runtime_lock,
)


def test_lock_requires_prevalidated_existing_private_root(tmp_path: Path) -> None:
    with pytest.raises(PrivateRuntimeLockError, match="root_missing"):
        with private_runtime_lock(tmp_path / "missing" / "runtime.lock"):
            pass


def test_lock_is_os_exclusive_and_reusable_after_release(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"
    with private_runtime_lock(path):
        assert path.is_file()
        with pytest.raises(PrivateRuntimeBusy, match="private_runtime_busy"):
            with private_runtime_lock(path):
                pass

    with private_runtime_lock(path):
        assert path.stat().st_size == 1
    assert path.read_bytes() == b"\0"


def test_existing_hardlinked_lock_is_rejected(tmp_path: Path) -> None:
    original = tmp_path / "original.lock"
    original.write_bytes(b"\0")
    linked = tmp_path / "runtime.lock"
    linked.hardlink_to(original)
    with pytest.raises((PrivateRuntimeLockError, ValueError), match="hardlink"):
        with private_runtime_lock(linked):
            pass
