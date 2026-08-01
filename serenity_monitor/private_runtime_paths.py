"""Filesystem and environment boundary for owner-only runtime state."""
from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .private_runtime_config import PrivateDailyRuntimeConfig
from .private_windows_security import (
    PrivateWindowsSecurityError,
    read_owner_only_file,
    secure_create_owner_only_directory,
    tighten_and_verify_owner_only,
    verify_owner_only_dacl,
)


_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_CLOUD_COMPONENTS = frozenset(
    {
        "baidunetdisk",
        "baiduyun",
        "dropbox",
        "googledrive",
        "icloud",
        "icloud drive",
        "jianguoyun",
        "nutstore",
        "onedrive",
        "tencentdrive",
        "weiyun",
    }
)
_PROVIDER_KEY_ENV = ("TWELVE_DATA_API_KEY", "ALPHA_VANTAGE_API_KEY")
_MAX_PRIVATE_CONFIG_BYTES = 1_000_000
_KNOWN_CLOUD_ROOT_ENVS = (
    "OneDrive",
    "OneDriveCommercial",
    "OneDriveConsumer",
    "Dropbox",
    "GOOGLE_DRIVE",
    "ICLOUD_DRIVE",
)


class PrivateRuntimePathError(ValueError):
    """Path/environment validation failed without echoing a private value."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().lower()
        if not _SAFE_ERROR_CODE.fullmatch(normalized):
            normalized = "private_runtime_path_invalid"
        self.code = normalized
        super().__init__(normalized)


def _fail(code: str) -> None:
    raise PrivateRuntimePathError(code)


@dataclass(frozen=True, repr=False)
class PrivateRuntimePaths:
    root: Path
    ledger_database: Path
    outbox_database: Path
    report_directory: Path
    lock_file: Path

    @property
    def opening_claim_file(self) -> Path:
        """Fixed owner-presence claim path with no private value in its name."""

        return self.root / "opening-owner-attestation.claim.json"

    @property
    def opening_intent_file(self) -> Path:
        """Fixed pre-commit intent path for the opening transaction."""

        return self.root / "opening-owner-attestation.intent.json"

    @property
    def opening_receipt_file(self) -> Path:
        """Fixed post-commit receipt path bound to the opening event."""

        return self.root / "opening-owner-attestation.receipt.json"

    @property
    def manual_event_request_file(self) -> Path:
        """Fixed untrusted request reviewed by the owner from an interactive TTY."""

        return self.root / "manual-event.request.json"

    @property
    def manual_event_directory(self) -> Path:
        """Owner-only control directory for approved manual events."""

        return self.root / "manual-events"

    @property
    def manual_event_approved_directory(self) -> Path:
        """Immutable approved envelopes; filenames contain only opaque nonces."""

        return self.manual_event_directory / "approved"

    @property
    def manual_event_receipt_directory(self) -> Path:
        """Immutable ledger-binding receipts for consumed approved envelopes."""

        return self.manual_event_directory / "receipts"


def _normalized_component(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _looks_cloud_synced(path: Path) -> bool:
    normalized = {_normalized_component(part) for part in path.parts}
    tokens = {_normalized_component(token) for token in _CLOUD_COMPONENTS}
    if any(component.startswith(token) for component in normalized for token in tokens):
        return True
    candidate = os.path.normcase(os.path.abspath(str(path)))
    for variable in _KNOWN_CLOUD_ROOT_ENVS:
        raw_root = str(os.environ.get(variable, "")).strip()
        if not raw_root:
            continue
        cloud_root = os.path.normcase(os.path.abspath(os.path.expanduser(raw_root)))
        try:
            if os.path.commonpath((candidate, cloud_root)) == cloud_root:
                return True
        except ValueError:
            continue
    return False


def _nearest_cloud_component_distance(path: Path) -> int | None:
    tokens = {_normalized_component(token) for token in _CLOUD_COMPONENTS}
    parts = tuple(_normalized_component(part) for part in path.parts)
    matches = [
        len(parts) - index - 1
        for index, component in enumerate(parts)
        if any(component.startswith(token) for token in tokens)
    ]
    return min(matches) if matches else None


def _nearest_git_marker_distance(path: Path) -> int | None:
    candidate = path if path.is_dir() else path.parent
    for distance, ancestor in enumerate((candidate, *candidate.parents)):
        if os.path.lexists(str(ancestor / ".git")):
            return distance
    return None


def _existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _reject_link_or_junction(path: Path) -> None:
    absolute = path.absolute()
    candidates = [absolute, *absolute.parents]
    for candidate in candidates:
        if not os.path.lexists(str(candidate)):
            continue
        is_junction = getattr(candidate, "is_junction", lambda: False)
        attributes = int(getattr(candidate.lstat(), "st_file_attributes", 0))
        reparse_point = bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))
        if candidate.is_symlink() or bool(is_junction()) or reparse_point:
            _fail("symlink_or_junction_forbidden")


def _reject_network_path(path: Path) -> None:
    text = str(path)
    if text.startswith(("\\\\", "//")):
        _fail("network_share_private_path_forbidden")


def _inside_git_worktree(path: Path) -> bool:
    probe = _existing_ancestor(path)
    if probe.is_file():
        probe = probe.parent
    for ancestor in (probe, *probe.parents):
        if os.path.lexists(str(ancestor / ".git")):
            return True
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(probe),
                "rev-parse",
                "--is-inside-work-tree",
                "--is-bare-repository",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # A missing/broken Git executable cannot prove that a location is safe.
        return True
    if result.returncode == 0:
        values = {item.strip().casefold() for item in result.stdout.splitlines()}
        return "true" in values
    if result.returncode == 128:
        return False
    return True


def _reject_unsafe_windows_drive(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(str(Path(path.anchor))))
    except Exception:
        _fail("private_drive_identity_unavailable")
    # DRIVE_FIXED == 3.  Reject remote, removable, optical, RAM and unknown roots.
    if drive_type != 3:
        _fail("private_storage_requires_fixed_drive")


def _validate_external_private_path(path: Path, *, config_file: bool = False) -> Path:
    _reject_network_path(path)
    absolute = path.expanduser().absolute()
    if not absolute.is_absolute():
        _fail("private_path_must_be_absolute")
    _reject_link_or_junction(absolute)
    _reject_unsafe_windows_drive(absolute)
    if config_file:
        if not absolute.is_file():
            _fail("private_configuration_missing")
        if absolute.stat().st_nlink != 1:
            _fail("private_configuration_hardlink_forbidden")
    git_distance = _nearest_git_marker_distance(absolute)
    cloud_distance = _nearest_cloud_component_distance(absolute)
    inside_git = _inside_git_worktree(absolute)
    if inside_git and git_distance is not None and (
        cloud_distance is None or git_distance < cloud_distance
    ):
        _fail("git_worktree_private_path_forbidden")
    if _looks_cloud_synced(absolute):
        _fail("cloud_synced_private_path_forbidden")
    if inside_git:
        _fail("git_worktree_private_path_forbidden")
    if absolute == Path(absolute.anchor) or absolute == Path.home().absolute():
        _fail("private_storage_scope_too_broad")
    if config_file:
        if os.name == "nt":
            try:
                verify_owner_only_dacl(absolute, require_protected=True)
            except PrivateWindowsSecurityError as exc:
                raise PrivateRuntimePathError("private_configuration_acl_invalid") from exc
        else:
            if absolute.stat().st_uid != os.geteuid():
                _fail("private_configuration_owner_mismatch")
            mode = stat.S_IMODE(absolute.stat().st_mode)
            if mode & 0o077:
                _fail("private_configuration_permissions_too_broad")
    return absolute


def validate_live_private_config_path(path: str | Path) -> Path:
    """Require a live configuration outside Git, sync folders and links."""

    return _validate_external_private_path(Path(path), config_file=True)


def read_validated_live_private_config(path: str | Path) -> tuple[Path, bytes]:
    """Validate and read one live config from the same no-follow handle.

    POSIX ownership, mode, type, size and link count are checked with ``fstat``
    on the descriptor used for the read.  The Windows implementation is wired
    through the owner-only security module before live activation.
    """

    config_path = _validate_external_private_path(Path(path), config_file=False)
    if os.name == "nt":
        try:
            payload = read_owner_only_file(
                config_path,
                _MAX_PRIVATE_CONFIG_BYTES,
            )
        except PrivateWindowsSecurityError as exc:
            raise PrivateRuntimePathError("private_configuration_secure_read_failed") from exc
        return config_path, payload
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(config_path, flags)
    except OSError as exc:
        raise PrivateRuntimePathError("private_configuration_open_failed") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("private_configuration_not_regular_file")
        if metadata.st_nlink != 1:
            _fail("private_configuration_hardlink_forbidden")
        if metadata.st_size > _MAX_PRIVATE_CONFIG_BYTES:
            _fail("private_configuration_too_large")
        if metadata.st_uid != os.geteuid():
            _fail("private_configuration_owner_mismatch")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            _fail("private_configuration_permissions_too_broad")
        chunks: list[bytes] = []
        remaining = _MAX_PRIVATE_CONFIG_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > _MAX_PRIVATE_CONFIG_BYTES:
            _fail("private_configuration_too_large")
        return config_path, payload
    finally:
        os.close(descriptor)


def resolve_private_runtime_paths(
    config: PrivateDailyRuntimeConfig,
    environ: Mapping[str, str],
) -> PrivateRuntimePaths:
    """Resolve the private storage root without retaining its raw env value."""

    raw = str(environ.get(config.storage_root_env, "")).strip()
    if not raw:
        _fail("private_storage_environment_missing")
    supplied = Path(raw).expanduser()
    _reject_network_path(supplied)
    if not supplied.is_absolute():
        _fail("private_storage_must_be_absolute")
    root = _validate_external_private_path(supplied)
    return PrivateRuntimePaths(
        root=root,
        ledger_database=root / "portfolio-ledger.sqlite3",
        outbox_database=root / "daily-outbox.sqlite3",
        report_directory=root / "reports",
        lock_file=root / "private-daily-runtime.lock",
    )


def validate_existing_private_storage_root(paths: PrivateRuntimePaths) -> Path:
    """Validate an existing runtime root without creating or tightening it."""

    if not isinstance(paths, PrivateRuntimePaths):
        _fail("private_runtime_paths_invalid")
    root = _validate_external_private_path(paths.root)
    if root != paths.root.absolute() or not root.is_dir():
        _fail("private_storage_root_missing")
    metadata = root.stat()
    if os.name == "nt":
        try:
            verify_owner_only_dacl(root, require_protected=True)
        except PrivateWindowsSecurityError as exc:
            raise PrivateRuntimePathError("private_storage_root_acl_invalid") from exc
    elif metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        _fail("private_storage_root_permissions_invalid")
    return root


def validate_existing_private_runtime_file(
    paths: PrivateRuntimePaths,
    path: str | Path,
) -> Path:
    """Validate one known runtime database for a strictly read-only audit."""

    root = validate_existing_private_storage_root(paths)
    candidate = Path(path).expanduser().absolute()
    allowed = {
        paths.ledger_database.absolute(),
        paths.outbox_database.absolute(),
    }
    if candidate not in allowed or candidate.parent != root:
        _fail("private_runtime_file_identity_mismatch")
    validated = _validate_external_private_path(candidate)
    if not validated.is_file():
        _fail("private_runtime_file_missing")
    metadata = validated.stat()
    if metadata.st_nlink != 1:
        _fail("private_runtime_file_hardlink_forbidden")
    if os.name == "nt":
        try:
            verify_owner_only_dacl(validated, require_protected=True)
        except PrivateWindowsSecurityError as exc:
            raise PrivateRuntimePathError("private_runtime_file_acl_invalid") from exc
    elif metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        _fail("private_runtime_file_permissions_invalid")
    return validated


def require_delivery_target(
    config: PrivateDailyRuntimeConfig,
    environ: Mapping[str, str],
) -> str:
    """Return the raw target only to the immediate caller; never persist it."""

    target = str(environ.get(config.delivery_target_env, "")).strip()
    if not target:
        _fail("delivery_target_environment_missing")
    if len(target) > 8_192:
        _fail("delivery_target_too_long")
    if len(target) > 256 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", target):
        _fail("delivery_target_format_invalid")
    return target


def missing_provider_environment(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Return credential variable names only, never their values."""

    return tuple(name for name in _PROVIDER_KEY_ENV if not str(environ.get(name, "")).strip())


def ensure_private_storage(paths: PrivateRuntimePaths) -> None:
    """Create private directories and apply owner-only POSIX permissions."""

    if os.name == "nt":
        try:
            secure_create_owner_only_directory(paths.root, parents=True)
            secure_create_owner_only_directory(paths.report_directory, parents=False)
        except PrivateWindowsSecurityError as exc:
            raise PrivateRuntimePathError("private_storage_acl_failed") from exc
    else:
        paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths.report_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_link_or_junction(paths.root)
    _reject_link_or_junction(paths.report_directory)
    if os.name != "nt":
        os.chmod(paths.root, 0o700)
        os.chmod(paths.report_directory, 0o700)
        if paths.root.stat().st_uid != os.geteuid() or paths.report_directory.stat().st_uid != os.geteuid():
            _fail("private_storage_owner_mismatch")


def tighten_private_file(path: str | Path) -> None:
    """Apply owner-only POSIX permissions after creating a runtime file."""

    private_file = Path(path)
    _reject_link_or_junction(private_file)
    if private_file.exists() and private_file.is_file() and private_file.stat().st_nlink != 1:
        _fail("private_runtime_hardlink_forbidden")
    if private_file.exists():
        if os.name == "nt":
            try:
                tighten_and_verify_owner_only(private_file)
            except PrivateWindowsSecurityError as exc:
                raise PrivateRuntimePathError("private_runtime_acl_failed") from exc
        else:
            if private_file.stat().st_uid != os.geteuid():
                _fail("private_runtime_owner_mismatch")
            os.chmod(private_file, 0o600)


def validate_private_report_directory(
    paths: PrivateRuntimePaths,
    directory: str | Path,
) -> Path:
    """Revalidate the exact report directory represented by trusted paths."""

    candidate = Path(directory).expanduser().absolute()
    if candidate != paths.report_directory.absolute() or candidate.parent != paths.root.absolute():
        _fail("private_report_directory_identity_mismatch")
    validated = _validate_external_private_path(candidate)
    if not validated.is_dir():
        _fail("private_report_directory_missing")
    if os.name == "nt":
        try:
            verify_owner_only_dacl(validated, require_protected=True)
        except PrivateWindowsSecurityError as exc:
            raise PrivateRuntimePathError("private_report_directory_acl_invalid") from exc
    else:
        metadata = validated.stat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            _fail("private_report_directory_permissions_invalid")
    return validated


__all__ = [
    "PrivateRuntimePathError",
    "PrivateRuntimePaths",
    "ensure_private_storage",
    "missing_provider_environment",
    "read_validated_live_private_config",
    "require_delivery_target",
    "resolve_private_runtime_paths",
    "tighten_private_file",
    "validate_existing_private_runtime_file",
    "validate_existing_private_storage_root",
    "validate_private_report_directory",
    "validate_live_private_config_path",
]
