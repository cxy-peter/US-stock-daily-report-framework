"""Owner-only transport for sanitized aggregate research projections.

The store persists only the output of :mod:`private_research_adapter`.  Raw
posts, author identifiers, URLs, credentials and portfolio mutations are not
part of the contract.  The fixed latest pointer is atomic, self-hashed and
revalidated before it can enter a daily report.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .private_daily_report import canonical_json
from .private_research_adapter import (
    PrivateResearchAdapterError,
    PrivateResearchInput,
    PrivateResearchProjection,
    build_private_research_projection,
    validate_private_research_projection,
)
from .private_runtime_paths import (
    PrivateRuntimePaths,
    tighten_private_file,
    validate_existing_private_storage_root,
)
from .private_runtime_lock import private_runtime_lock
from .private_windows_security import (
    PrivateWindowsSecurityError,
    read_owner_only_file,
)


SNAPSHOT_SCHEMA_VERSION = "private_research_snapshot/v1.0.0"
REQUEST_SCHEMA_VERSION = "private_research_snapshot_request/v1.0.0"
_MAX_SNAPSHOT_BYTES = 2_000_000


class PrivateResearchStoreError(RuntimeError):
    """The sanitized private research snapshot is unsafe or ambiguous."""


class PrivateResearchStoreCommitUnknown(PrivateResearchStoreError):
    """The atomic pointer may have committed but could not be verified."""


@dataclass(frozen=True, repr=False)
class PrivateResearchSnapshot:
    snapshot_id: str
    as_of: dt.datetime
    created_at: dt.datetime
    projection: PrivateResearchProjection
    stored_projection: PrivateResearchProjection | None = field(
        default=None,
        repr=False,
        compare=False,
    )


def _aware_utc(value: dt.datetime, field_name: str) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PrivateResearchStoreError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(dt.timezone.utc)


def _utc_text(value: dt.datetime) -> str:
    return _aware_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _parse_utc(value: object, field_name: str) -> dt.datetime:
    if not isinstance(value, str):
        raise PrivateResearchStoreError(f"{field_name}_must_be_date_time")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrivateResearchStoreError(f"{field_name}_must_be_date_time") from exc
    return _aware_utc(parsed, field_name)


def _snapshot_body(
    projection: PrivateResearchProjection,
    *,
    as_of: dt.datetime,
    created_at: dt.datetime,
) -> dict[str, object]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "as_of": _utc_text(as_of),
        "created_at": _utc_text(created_at),
        "research": projection.research,
        "source_health": list(projection.source_health),
    }


def _snapshot_id(body: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _require_snapshot_as_of_identity(
    projection: PrivateResearchProjection,
    as_of: dt.datetime,
) -> None:
    expected = _utc_text(as_of)
    snapshot_rows = [
        item
        for item in projection.source_health
        if item.get("source_id") == "research.snapshot"
    ]
    if len(snapshot_rows) != 1 or snapshot_rows[0].get("observed_at") != expected:
        raise PrivateResearchStoreError("research_snapshot_as_of_identity_mismatch")


def build_private_research_snapshot(
    value: PrivateResearchInput,
    *,
    created_at: dt.datetime,
) -> PrivateResearchSnapshot:
    created = _aware_utc(created_at, "created_at")
    if value.as_of > created:
        raise PrivateResearchStoreError("research_snapshot_may_not_be_from_the_future")
    try:
        projection = validate_private_research_projection(
            build_private_research_projection(value, prepared_at=created),
            prepared_at=created,
        )
    except PrivateResearchAdapterError as exc:
        raise PrivateResearchStoreError("research_snapshot_projection_invalid") from exc
    body = _snapshot_body(projection, as_of=value.as_of, created_at=created)
    return PrivateResearchSnapshot(
        snapshot_id=_snapshot_id(body),
        as_of=value.as_of,
        created_at=created,
        projection=projection,
    )


def build_private_research_snapshot_from_projection(
    value: PrivateResearchProjection,
    *,
    as_of: dt.datetime,
    created_at: dt.datetime,
) -> PrivateResearchSnapshot:
    """Build a snapshot from an already sanitized aggregate projection."""

    observed = _aware_utc(as_of, "as_of")
    created = _aware_utc(created_at, "created_at")
    if observed > created:
        raise PrivateResearchStoreError("research_snapshot_may_not_be_from_the_future")
    try:
        projection = validate_private_research_projection(
            value,
            prepared_at=created,
        )
    except PrivateResearchAdapterError as exc:
        raise PrivateResearchStoreError("research_snapshot_projection_invalid") from exc
    _require_snapshot_as_of_identity(projection, observed)
    body = _snapshot_body(projection, as_of=observed, created_at=created)
    return PrivateResearchSnapshot(
        snapshot_id=_snapshot_id(body),
        as_of=observed,
        created_at=created,
        projection=projection,
    )


def _payload(snapshot: PrivateResearchSnapshot) -> bytes:
    persisted_projection = snapshot.stored_projection or snapshot.projection
    body = _snapshot_body(
        persisted_projection,
        as_of=snapshot.as_of,
        created_at=snapshot.created_at,
    )
    if _snapshot_id(body) != snapshot.snapshot_id:
        raise PrivateResearchStoreError("research_snapshot_identity_mismatch")
    return (
        canonical_json({**body, "snapshot_id": snapshot.snapshot_id}) + "\n"
    ).encode("utf-8")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("private research snapshot write made no progress")
        offset += written
    os.fsync(descriptor)


def _read_posix_owner_only(path: Path) -> bytes:
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise PrivateResearchStoreError("research_snapshot_permissions_invalid")
        if metadata.st_size > _MAX_SNAPSHOT_BYTES:
            raise PrivateResearchStoreError("research_snapshot_too_large")
        chunks: list[bytes] = []
        remaining = _MAX_SNAPSHOT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > _MAX_SNAPSHOT_BYTES:
            raise PrivateResearchStoreError("research_snapshot_too_large")
        return payload
    finally:
        os.close(descriptor)


def _read_owner_only(path: Path) -> bytes:
    if os.name == "nt":
        try:
            return read_owner_only_file(path, _MAX_SNAPSHOT_BYTES)
        except PrivateWindowsSecurityError as exc:
            raise PrivateResearchStoreError("research_snapshot_secure_read_failed") from exc
    return _read_posix_owner_only(path)


def _parse_snapshot_payload(
    payload: bytes,
) -> tuple[dict[str, object], str, dt.datetime, dt.datetime]:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateResearchStoreError("research_snapshot_parse_failed") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "snapshot_id",
        "as_of",
        "created_at",
        "research",
        "source_health",
    }:
        raise PrivateResearchStoreError("research_snapshot_envelope_invalid")
    if raw["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
        raise PrivateResearchStoreError("research_snapshot_schema_unsupported")
    snapshot_id = raw["snapshot_id"]
    if (
        not isinstance(snapshot_id, str)
        or len(snapshot_id) != 64
        or any(character not in "0123456789abcdef" for character in snapshot_id)
    ):
        raise PrivateResearchStoreError("research_snapshot_identity_invalid")
    body = {key: raw[key] for key in raw if key != "snapshot_id"}
    if _snapshot_id(body) != snapshot_id:
        raise PrivateResearchStoreError("research_snapshot_identity_mismatch")
    as_of = _parse_utc(raw["as_of"], "as_of")
    created_at = _parse_utc(raw["created_at"], "created_at")
    if as_of > created_at:
        raise PrivateResearchStoreError("research_snapshot_may_not_be_from_the_future")
    return raw, snapshot_id, as_of, created_at


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def persist_private_research_snapshot(
    snapshot: PrivateResearchSnapshot,
    paths: PrivateRuntimePaths,
) -> Path:
    if not isinstance(snapshot, PrivateResearchSnapshot):
        raise PrivateResearchStoreError("value_must_be_private_research_snapshot")
    observed = _aware_utc(snapshot.as_of, "as_of")
    created = _aware_utc(snapshot.created_at, "created_at")
    if observed > created:
        raise PrivateResearchStoreError("research_snapshot_may_not_be_from_the_future")
    persisted_projection = snapshot.stored_projection or snapshot.projection
    try:
        validated_projection = validate_private_research_projection(
            persisted_projection,
            prepared_at=created,
        )
    except PrivateResearchAdapterError as exc:
        raise PrivateResearchStoreError("research_snapshot_projection_invalid") from exc
    _require_snapshot_as_of_identity(validated_projection, observed)
    root = validate_existing_private_storage_root(paths)
    path = paths.research_snapshot_file.absolute()
    if path.parent != root or path.name != "research-snapshot.latest.json":
        raise PrivateResearchStoreError("research_snapshot_path_identity_mismatch")
    payload = _payload(snapshot)
    if len(payload) > _MAX_SNAPSHOT_BYTES:
        raise PrivateResearchStoreError("research_snapshot_too_large")
    with private_runtime_lock(paths.research_snapshot_lock_file):
        if os.path.lexists(path):
            _, existing_id, existing_as_of, existing_created_at = (
                _parse_snapshot_payload(_read_owner_only(path))
            )
            if existing_id == snapshot.snapshot_id:
                return path
            if (
                snapshot.as_of < existing_as_of
                or snapshot.created_at < existing_created_at
            ):
                raise PrivateResearchStoreError(
                    "research_snapshot_rollback_forbidden"
                )
            if (
                snapshot.as_of == existing_as_of
                and snapshot.created_at == existing_created_at
            ):
                raise PrivateResearchStoreError(
                    "research_snapshot_same_version_conflict"
                )

        temporary = root / f".{path.name}.{secrets.token_hex(16)}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0))
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        replaced = False
        try:
            _write_all(descriptor, payload)
            os.close(descriptor)
            descriptor = -1
            tighten_private_file(temporary)
            os.replace(temporary, path)
            replaced = True
            tighten_private_file(path)
            _fsync_directory(root)
            if _read_owner_only(path) != payload:
                raise PrivateResearchStoreError("research_snapshot_pointer_conflict")
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            if replaced:
                raise PrivateResearchStoreCommitUnknown(
                    "research_snapshot_commit_state_unknown"
                ) from None
            raise
    return path


def load_private_research_snapshot(
    paths: PrivateRuntimePaths,
    *,
    prepared_at: dt.datetime,
) -> PrivateResearchSnapshot | None:
    prepared = _aware_utc(prepared_at, "prepared_at")
    root = validate_existing_private_storage_root(paths)
    path = paths.research_snapshot_file.absolute()
    if path.parent != root or path.name != "research-snapshot.latest.json":
        raise PrivateResearchStoreError("research_snapshot_path_identity_mismatch")
    if not os.path.lexists(path):
        return None
    try:
        raw, snapshot_id, as_of, created_at = _parse_snapshot_payload(
            _read_owner_only(path)
        )
    except OSError as exc:
        raise PrivateResearchStoreError("research_snapshot_parse_failed") from exc
    if created_at > prepared:
        raise PrivateResearchStoreError("research_snapshot_may_not_be_from_the_future")
    raw_projection = PrivateResearchProjection(
        research=raw["research"],
        source_health=tuple(raw["source_health"]),
    )
    _require_snapshot_as_of_identity(raw_projection, as_of)
    try:
        projection = validate_private_research_projection(
            raw_projection,
            prepared_at=prepared,
        )
    except PrivateResearchAdapterError as exc:
        raise PrivateResearchStoreError("research_snapshot_projection_invalid") from exc
    return PrivateResearchSnapshot(
        snapshot_id=snapshot_id,
        as_of=as_of,
        created_at=created_at,
        projection=projection,
        stored_projection=raw_projection,
    )


def publish_private_research_snapshot_request(
    paths: PrivateRuntimePaths,
    *,
    prepared_at: dt.datetime,
) -> PrivateResearchSnapshot:
    """Validate and publish the fixed owner-only sanitized request file."""

    prepared = _aware_utc(prepared_at, "prepared_at")
    root = validate_existing_private_storage_root(paths)
    request_path = paths.research_snapshot_request_file.absolute()
    if (
        request_path.parent != root
        or request_path.name != "research-snapshot.request.json"
    ):
        raise PrivateResearchStoreError("research_snapshot_request_path_identity_mismatch")
    if not os.path.lexists(request_path):
        raise PrivateResearchStoreError("research_snapshot_request_missing")
    try:
        raw = json.loads(_read_owner_only(request_path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateResearchStoreError("research_snapshot_request_parse_failed") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "as_of",
        "created_at",
        "research",
        "source_health",
    }:
        raise PrivateResearchStoreError("research_snapshot_request_envelope_invalid")
    if raw["schema_version"] != REQUEST_SCHEMA_VERSION:
        raise PrivateResearchStoreError("research_snapshot_request_schema_unsupported")
    as_of = _parse_utc(raw["as_of"], "request.as_of")
    created_at = _parse_utc(raw["created_at"], "request.created_at")
    if created_at > prepared:
        raise PrivateResearchStoreError("research_snapshot_request_from_the_future")
    snapshot = build_private_research_snapshot_from_projection(
        PrivateResearchProjection(
            research=raw["research"],
            source_health=tuple(raw["source_health"]),
        ),
        as_of=as_of,
        created_at=created_at,
    )
    persist_private_research_snapshot(snapshot, paths)
    return snapshot


__all__ = [
    "PrivateResearchSnapshot",
    "PrivateResearchStoreCommitUnknown",
    "PrivateResearchStoreError",
    "REQUEST_SCHEMA_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "build_private_research_snapshot",
    "build_private_research_snapshot_from_projection",
    "load_private_research_snapshot",
    "persist_private_research_snapshot",
    "publish_private_research_snapshot_request",
]
