from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from serenity_monitor.private_daily_markdown import render_private_daily_markdown
from serenity_monitor.private_daily_report import canonical_json
from serenity_monitor.private_report_store import (
    PrivateReportStoreCommitUnknown,
    PrivateReportStoreConflict,
    PrivateReportStoreError,
    _publish_immutable,
    _write_temp,
    persist_private_daily_report,
)
from test_private_daily_report_schema import finalized_report


def test_content_addressed_report_and_latest_pointer_are_deterministic(
    tmp_path: Path,
) -> None:
    report = finalized_report()
    first = persist_private_daily_report(report, tmp_path)
    second = persist_private_daily_report(report, tmp_path)
    assert first == second
    assert first.json_path.read_text(encoding="utf-8") == canonical_json(report) + "\n"
    assert first.markdown_path.read_text(encoding="utf-8") == render_private_daily_markdown(report)
    pointer = json.loads(first.latest_pointer_path.read_text(encoding="utf-8"))
    assert pointer == {
        "json_file": first.json_path.name,
        "markdown_file": first.markdown_path.name,
        "report_id": report["report_id"],
        "schema_version": "private_daily_report_pointer/v1.0.0",
    }
    assert "delivery target" not in first.latest_pointer_path.read_text(encoding="utf-8")


def test_existing_content_addressed_path_can_never_be_overwritten(tmp_path: Path) -> None:
    report = finalized_report()
    files = persist_private_daily_report(report, tmp_path)
    files.markdown_path.write_text("tampered", encoding="utf-8")
    with pytest.raises(PrivateReportStoreConflict, match="immutable_private_report_conflict"):
        persist_private_daily_report(report, tmp_path)
    assert files.markdown_path.read_text(encoding="utf-8") == "tampered"


def test_missing_directory_does_not_create_an_unreviewed_private_root(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(PrivateReportStoreError, match="directory_missing"):
        persist_private_daily_report(finalized_report(), missing)
    assert not missing.exists()


def test_pointer_replace_failure_keeps_immutable_report_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = finalized_report()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("private sentinel must not escape")

    monkeypatch.setattr("serenity_monitor.private_report_store.os.replace", fail_replace)
    with pytest.raises(PrivateReportStoreError, match="persistence_failed") as captured:
        persist_private_daily_report(report, tmp_path)
    assert "sentinel" not in str(captured.value)
    assert len(list(tmp_path.glob("daily-report-*.json"))) == 1
    assert len(list(tmp_path.glob("daily-report-*.md"))) == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_pointer_commit_is_reported_unknown_when_post_replace_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = finalized_report()
    original = __import__(
        "serenity_monitor.private_report_store",
        fromlist=["tighten_private_file"],
    ).tighten_private_file

    def fail_after_pointer_replace(path: Path) -> None:
        if Path(path).name == "latest.pointer.json":
            raise OSError("private sentinel must not escape")
        original(path)

    monkeypatch.setattr(
        "serenity_monitor.private_report_store.tighten_private_file",
        fail_after_pointer_replace,
    )
    with pytest.raises(
        PrivateReportStoreCommitUnknown,
        match="commit_state_unknown",
    ) as captured:
        persist_private_daily_report(report, tmp_path)
    assert "sentinel" not in str(captured.value)
    pointer = json.loads((tmp_path / "latest.pointer.json").read_text(encoding="utf-8"))
    assert pointer["report_id"] == report["report_id"]


def test_pointer_commit_is_unknown_even_when_post_replace_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = finalized_report()
    original_read_bytes = Path.read_bytes

    def fail_pointer_read(path: Path) -> bytes:
        if path.name == "latest.pointer.json":
            raise OSError("private sentinel must not escape")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_pointer_read)
    with pytest.raises(
        PrivateReportStoreCommitUnknown,
        match="commit_state_unknown",
    ) as captured:
        persist_private_daily_report(report, tmp_path)
    assert "sentinel" not in str(captured.value)
    pointer_path = tmp_path / "latest.pointer.json"
    pointer = json.loads(original_read_bytes(pointer_path).decode("utf-8"))
    assert pointer["report_id"] == report["report_id"]


def test_orphaned_publication_temp_hardlink_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "daily-report-synthetic.json"
    payload = b'{"synthetic":true}\n'
    temporary = _write_temp(tmp_path, path.name, payload)
    os.link(temporary, path)
    assert path.stat().st_nlink == 2

    _publish_immutable(path, payload)

    assert path.read_bytes() == payload
    assert path.stat().st_nlink == 1
    assert not temporary.exists()


def test_directory_entries_are_fsynced_after_each_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(
        "serenity_monitor.private_report_store._fsync_directory",
        lambda path: calls.append(Path(path)),
    )

    persist_private_daily_report(finalized_report(), tmp_path)

    assert calls == [tmp_path, tmp_path, tmp_path]
