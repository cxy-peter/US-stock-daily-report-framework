from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from serenity_monitor.private_runtime_config import (
    PUBLIC_EXAMPLE_NAME,
    load_private_daily_runtime_config,
)
from serenity_monitor.private_runtime_paths import (
    PrivateRuntimePathError,
    ensure_private_storage,
    missing_provider_environment,
    require_delivery_target,
    resolve_private_runtime_paths,
    validate_live_private_config_path,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_private_daily_runtime_config(
    ROOT / "config" / PUBLIC_EXAMPLE_NAME,
    allow_synthetic=True,
)


def test_private_storage_requires_external_absolute_non_cloud_root(tmp_path: Path) -> None:
    with pytest.raises(PrivateRuntimePathError, match="environment_missing"):
        resolve_private_runtime_paths(CONFIG, {})
    with pytest.raises(PrivateRuntimePathError, match="must_be_absolute"):
        resolve_private_runtime_paths(
            CONFIG,
            {CONFIG.storage_root_env: "relative/private"},
        )
    with pytest.raises(PrivateRuntimePathError, match="network_share"):
        resolve_private_runtime_paths(
            CONFIG,
            {
                CONFIG.storage_root_env: (
                    "\\" * 2 + "server" + "\\" + "share" + "\\" + "owner"
                )
            },
        )

    cloud = tmp_path / "BaiduNetdisk" / "owner"
    with pytest.raises(PrivateRuntimePathError, match="cloud_synced"):
        resolve_private_runtime_paths(
            CONFIG,
            {CONFIG.storage_root_env: str(cloud)},
        )

    for folder in ("BaiduNetdiskDownload", "OneDrive - Company", "Dropbox (Personal)"):
        with pytest.raises(PrivateRuntimePathError, match="cloud_synced"):
            resolve_private_runtime_paths(
                CONFIG,
                {CONFIG.storage_root_env: str(tmp_path / folder / "owner")},
            )


def test_private_storage_inside_any_git_worktree_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
    with pytest.raises(PrivateRuntimePathError, match="git_worktree"):
        resolve_private_runtime_paths(
            CONFIG,
            {CONFIG.storage_root_env: str(repository / "private")},
        )


def test_external_root_is_derived_without_retaining_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This Windows profile happens to be the root of an unrelated dotfiles Git
    # worktree, so isolate path derivation from the separately tested Git gate.
    monkeypatch.setattr(
        "serenity_monitor.private_runtime_paths._inside_git_worktree",
        lambda path: False,
    )
    root = tmp_path / "owner-runtime"
    paths = resolve_private_runtime_paths(
        CONFIG,
        {CONFIG.storage_root_env: str(root)},
    )
    assert paths.root == root
    assert paths.ledger_database.parent == root
    assert paths.outbox_database.parent == root
    assert paths.report_directory.parent == root
    assert paths.manual_event_request_file.parent == root
    assert paths.manual_event_directory.parent == root
    assert paths.manual_event_approved_directory.parent == paths.manual_event_directory
    assert paths.manual_event_receipt_directory.parent == paths.manual_event_directory
    assert "owner-target" not in repr(paths)
    ensure_private_storage(paths)
    assert paths.root.is_dir()
    assert paths.report_directory.is_dir()
    if os.name != "nt":
        assert paths.root.stat().st_mode & 0o077 == 0


def test_target_and_provider_environment_checks_never_return_secret_values() -> None:
    with pytest.raises(PrivateRuntimePathError, match="target_environment_missing"):
        require_delivery_target(CONFIG, {})
    target = require_delivery_target(
        CONFIG,
        {CONFIG.delivery_target_env: "owner-target"},
    )
    assert target == "owner-target"
    missing = missing_provider_environment({"TWELVE_DATA_API_KEY": "secret"})
    assert missing == ("ALPHA_VANTAGE_API_KEY",)
    assert "secret" not in repr(missing)
    for invalid in ("owner\ntarget", "owner target", "x" * 257):
        with pytest.raises(PrivateRuntimePathError, match="target_(format|too_long)"):
            require_delivery_target(
                CONFIG,
                {CONFIG.delivery_target_env: invalid},
            )


def test_live_configuration_location_rejects_repo_cloud_and_links(tmp_path: Path) -> None:
    with pytest.raises(PrivateRuntimePathError, match="git_worktree"):
        validate_live_private_config_path(ROOT / "config" / PUBLIC_EXAMPLE_NAME)

    cloud_file = tmp_path / "OneDrive" / "owner.private.yaml"
    cloud_file.parent.mkdir()
    cloud_file.write_text("private", encoding="utf-8")
    with pytest.raises(PrivateRuntimePathError, match="cloud_synced"):
        validate_live_private_config_path(cloud_file)

    hardlink_source = tmp_path / "source.private.yaml"
    hardlink_source.write_text("private", encoding="utf-8")
    hardlink = tmp_path / "hardlink.private.yaml"
    os.link(hardlink_source, hardlink)
    with pytest.raises(PrivateRuntimePathError, match="hardlink"):
        validate_live_private_config_path(hardlink)

    target = tmp_path / "owner.private.yaml"
    target.write_text("private", encoding="utf-8")
    link = tmp_path / "linked.private.yaml"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    with pytest.raises(PrivateRuntimePathError, match="symlink_or_junction"):
        validate_live_private_config_path(link)
