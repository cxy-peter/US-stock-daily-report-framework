"""Crash-safe local persistence for validated private daily reports."""
from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .private_daily_markdown import render_private_daily_markdown
from .private_daily_report import canonical_json, validate_private_daily_report
from .private_runtime_paths import (
    PrivateRuntimePaths,
    tighten_private_file,
    validate_private_report_directory,
)


class PrivateReportStoreError(RuntimeError):
    """A private report could not be persisted without ambiguity."""


class PrivateReportStoreConflict(PrivateReportStoreError):
    """An immutable content-addressed path contains different bytes."""


class PrivateReportStoreCommitUnknown(PrivateReportStoreError):
    """A pointer replace may have committed but could not be verified."""


@dataclass(frozen=True, repr=False)
class PrivateReportFiles:
    report_id: str
    json_path: Path
    markdown_path: Path
    latest_pointer_path: Path


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("private report write did not make progress")
        offset += written
    os.fsync(descriptor)


def _temporary_path(parent: Path, name: str) -> Path:
    return parent / f".{name}.{secrets.token_hex(16)}.tmp"


def _fsync_directory(directory: Path) -> None:
    """Durably publish directory-entry changes where the OS supports it."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_temp(parent: Path, name: str, payload: bytes) -> Path:
    temporary = _temporary_path(parent, name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        _write_all(descriptor, payload)
    except Exception:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    tighten_private_file(temporary)
    return temporary


def _recover_orphaned_temporary_link(path: Path) -> None:
    """Remove the one publication temp link left by a crash after ``link``."""

    published = path.lstat()
    if not stat.S_ISREG(published.st_mode) or stat.S_ISLNK(published.st_mode):
        raise PrivateReportStoreConflict("immutable_private_report_conflict")
    if published.st_nlink == 1:
        return
    if published.st_nlink != 2:
        raise PrivateReportStoreConflict("immutable_private_report_conflict")
    matches: list[Path] = []
    for candidate in path.parent.glob(f".{path.name}.*.tmp"):
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(candidate_stat.st_mode) and os.path.samestat(
            published,
            candidate_stat,
        ):
            matches.append(candidate)
    if len(matches) != 1:
        raise PrivateReportStoreConflict("immutable_private_report_conflict")
    matches[0].unlink()
    _fsync_directory(path.parent)
    recovered = path.lstat()
    if recovered.st_nlink != 1 or not os.path.samestat(published, recovered):
        raise PrivateReportStoreConflict("immutable_private_report_conflict")


def _verify_immutable(path: Path, payload: bytes) -> None:
    _recover_orphaned_temporary_link(path)
    tighten_private_file(path)
    if path.read_bytes() != payload:
        raise PrivateReportStoreConflict("immutable_private_report_conflict")


def _publish_immutable(path: Path, payload: bytes) -> None:
    if os.path.lexists(path):
        _verify_immutable(path, payload)
        return
    temporary = _write_temp(path.parent, path.name, payload)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            _verify_immutable(path, payload)
        temporary.unlink(missing_ok=True)
        _verify_immutable(path, payload)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _replace_pointer(path: Path, payload: bytes) -> None:
    temporary = _write_temp(path.parent, path.name, payload)
    replaced = False
    try:
        os.replace(temporary, path)
        replaced = True
        tighten_private_file(path)
        if path.read_bytes() != payload:
            raise PrivateReportStoreConflict("private_report_pointer_conflict")
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        if replaced:
            raise PrivateReportStoreCommitUnknown(
                "private_report_pointer_commit_state_unknown"
            ) from None
        raise


def persist_private_daily_report(
    report: Mapping[str, Any],
    report_directory: str | Path,
    *,
    runtime_paths: PrivateRuntimePaths | None = None,
) -> PrivateReportFiles:
    """Publish immutable JSON/Markdown, then atomically advance a pointer."""

    normalized = validate_private_daily_report(report)
    markdown = render_private_daily_markdown(normalized)
    report_id = str(normalized["report_id"])
    directory = Path(report_directory)
    if runtime_paths is not None:
        directory = validate_private_report_directory(runtime_paths, directory)
    if not directory.is_dir():
        raise PrivateReportStoreError("private_report_directory_missing")
    json_path = directory / f"daily-report-{report_id}.json"
    markdown_path = directory / f"daily-report-{report_id}.md"
    pointer_path = directory / "latest.pointer.json"
    json_payload = (canonical_json(normalized) + "\n").encode("utf-8")
    markdown_payload = markdown.encode("utf-8")
    pointer_payload = (
        json.dumps(
            {
                "json_file": json_path.name,
                "markdown_file": markdown_path.name,
                "report_id": report_id,
                "schema_version": "private_daily_report_pointer/v1.0.0",
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    try:
        _publish_immutable(json_path, json_payload)
        _publish_immutable(markdown_path, markdown_payload)
        _replace_pointer(pointer_path, pointer_payload)
    except PrivateReportStoreError:
        raise
    except (OSError, ValueError) as exc:
        raise PrivateReportStoreError("private_report_persistence_failed") from exc
    return PrivateReportFiles(
        report_id=report_id,
        json_path=json_path,
        markdown_path=markdown_path,
        latest_pointer_path=pointer_path,
    )


__all__ = [
    "PrivateReportFiles",
    "PrivateReportStoreConflict",
    "PrivateReportStoreCommitUnknown",
    "PrivateReportStoreError",
    "persist_private_daily_report",
]
