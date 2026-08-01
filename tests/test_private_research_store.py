from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path

import pytest

import serenity_monitor.private_research_store as research_store
from serenity_monitor.private_research_adapter import PrivateResearchInput
from serenity_monitor.private_research_store import (
    PrivateResearchSnapshot,
    PrivateResearchStoreCommitUnknown,
    PrivateResearchStoreError,
    REQUEST_SCHEMA_VERSION,
    build_private_research_snapshot,
    load_private_research_snapshot,
    persist_private_research_snapshot,
    publish_private_research_snapshot_request,
)
from serenity_monitor.private_runtime_paths import PrivateRuntimePaths


NOW = dt.datetime(2026, 8, 1, 12, tzinfo=dt.timezone.utc)


def _paths(root: Path) -> PrivateRuntimePaths:
    return PrivateRuntimePaths(
        root=root,
        ledger_database=root / "portfolio-ledger.sqlite3",
        outbox_database=root / "daily-outbox.sqlite3",
        report_directory=root / "reports",
        lock_file=root / "private-daily-runtime.lock",
    )


@pytest.fixture(autouse=True)
def _local_owner_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        research_store,
        "validate_existing_private_storage_root",
        lambda paths: paths.root.absolute(),
    )
    monkeypatch.setattr(
        research_store,
        "tighten_private_file",
        lambda path: os.chmod(path, 0o600),
    )
    monkeypatch.setattr(
        research_store,
        "_read_owner_only",
        lambda path: Path(path).read_bytes(),
    )


def test_snapshot_round_trip_is_canonical_owner_only_and_research_only(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    snapshot = build_private_research_snapshot(
        PrivateResearchInput(as_of=NOW),
        created_at=NOW,
    )

    persisted = persist_private_research_snapshot(snapshot, paths)
    loaded = load_private_research_snapshot(paths, prepared_at=NOW)

    assert persisted == paths.research_snapshot_file
    assert loaded is not None
    assert loaded.snapshot_id == snapshot.snapshot_id
    assert loaded.projection.can_change_ledger is False
    assert loaded.projection.can_change_dca is False
    assert loaded.projection.can_create_trade_action is False
    raw = persisted.read_text(encoding="utf-8")
    assert "research-snapshot.latest.json" not in raw
    assert "token" not in raw.casefold()
    assert "http://" not in raw and "https://" not in raw


def test_missing_snapshot_is_optional(tmp_path: Path) -> None:
    assert load_private_research_snapshot(
        _paths(tmp_path),
        prepared_at=NOW,
    ) is None


def test_snapshot_tampering_and_future_creation_fail_closed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    snapshot = build_private_research_snapshot(
        PrivateResearchInput(as_of=NOW),
        created_at=NOW,
    )
    path = persist_private_research_snapshot(snapshot, paths)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["research"]["overall_view"] = "tampered"
    path.write_text(json.dumps(raw), encoding="utf-8")
    os.chmod(path, 0o600)

    with pytest.raises(PrivateResearchStoreError, match="identity_mismatch"):
        load_private_research_snapshot(paths, prepared_at=NOW)

    future = build_private_research_snapshot(
        PrivateResearchInput(as_of=NOW),
        created_at=NOW + dt.timedelta(seconds=1),
    )
    with pytest.raises(PrivateResearchStoreError, match="identity_mismatch"):
        persist_private_research_snapshot(future, paths)

    future_root = tmp_path / "future"
    future_root.mkdir()
    future_paths = _paths(future_root)
    persist_private_research_snapshot(future, future_paths)
    with pytest.raises(PrivateResearchStoreError, match="future"):
        load_private_research_snapshot(future_paths, prepared_at=NOW)


def test_persist_revalidates_projection_and_binds_envelope_as_of(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    snapshot = build_private_research_snapshot(
        PrivateResearchInput(as_of=NOW),
        created_at=NOW,
    )
    forged_as_of = NOW + dt.timedelta(minutes=1)
    forged_body = research_store._snapshot_body(
        snapshot.projection,
        as_of=forged_as_of,
        created_at=forged_as_of,
    )
    forged = PrivateResearchSnapshot(
        snapshot_id=research_store._snapshot_id(forged_body),
        as_of=forged_as_of,
        created_at=forged_as_of,
        projection=snapshot.projection,
    )

    with pytest.raises(PrivateResearchStoreError, match="as_of_identity_mismatch"):
        persist_private_research_snapshot(forged, paths)


def test_load_rejects_self_hashed_envelope_with_mismatched_projection_time(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    snapshot = build_private_research_snapshot(
        PrivateResearchInput(as_of=NOW),
        created_at=NOW,
    )
    raw = json.loads(research_store._payload(snapshot).decode("utf-8"))
    raw["as_of"] = (NOW + dt.timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    raw["created_at"] = raw["as_of"]
    body = {key: value for key, value in raw.items() if key != "snapshot_id"}
    raw["snapshot_id"] = research_store._snapshot_id(body)
    paths.research_snapshot_file.write_text(json.dumps(raw), encoding="utf-8")
    os.chmod(paths.research_snapshot_file, 0o600)

    with pytest.raises(PrivateResearchStoreError, match="as_of_identity_mismatch"):
        load_private_research_snapshot(
            paths,
            prepared_at=NOW + dt.timedelta(minutes=1),
        )


def test_post_replace_verification_failure_reports_commit_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    snapshot = build_private_research_snapshot(
        PrivateResearchInput(as_of=NOW),
        created_at=NOW,
    )
    monkeypatch.setattr(research_store, "_read_owner_only", lambda _path: b"wrong")

    with pytest.raises(
        PrivateResearchStoreCommitUnknown,
        match="commit_state_unknown",
    ):
        persist_private_research_snapshot(snapshot, paths)


def test_newer_snapshot_cannot_be_replaced_by_delayed_older_snapshot(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    newer_time = NOW + dt.timedelta(minutes=1)
    newer = build_private_research_snapshot(
        PrivateResearchInput(as_of=newer_time),
        created_at=newer_time,
    )
    older = build_private_research_snapshot(
        PrivateResearchInput(as_of=NOW),
        created_at=NOW,
    )
    persist_private_research_snapshot(newer, paths)

    with pytest.raises(PrivateResearchStoreError, match="rollback_forbidden"):
        persist_private_research_snapshot(older, paths)

    loaded = load_private_research_snapshot(paths, prepared_at=newer_time)
    assert loaded is not None
    assert loaded.snapshot_id == newer.snapshot_id


def test_stale_runtime_view_preserves_persisted_snapshot_identity(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    snapshot = build_private_research_snapshot(
        PrivateResearchInput(as_of=NOW),
        created_at=NOW,
    )
    persist_private_research_snapshot(snapshot, paths)

    loaded = load_private_research_snapshot(
        paths,
        prepared_at=NOW + dt.timedelta(days=3),
    )
    assert loaded is not None
    assert loaded.snapshot_id == snapshot.snapshot_id
    assert "research_snapshot_stale_candidate_score_disabled" in (
        loaded.projection.research["notes"]
    )

    assert persist_private_research_snapshot(loaded, paths) == (
        paths.research_snapshot_file
    )


def test_fixed_owner_only_request_publishes_and_loads_end_to_end(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    snapshot = build_private_research_snapshot(
        PrivateResearchInput(as_of=NOW),
        created_at=NOW,
    )
    request = json.loads(research_store._payload(snapshot).decode("utf-8"))
    request.pop("snapshot_id")
    request["schema_version"] = REQUEST_SCHEMA_VERSION
    paths.research_snapshot_request_file.write_text(
        json.dumps(request),
        encoding="utf-8",
    )
    os.chmod(paths.research_snapshot_request_file, 0o600)

    published = publish_private_research_snapshot_request(
        paths,
        prepared_at=NOW,
    )
    loaded = load_private_research_snapshot(paths, prepared_at=NOW)

    assert loaded is not None
    assert published.snapshot_id == snapshot.snapshot_id
    assert loaded.snapshot_id == snapshot.snapshot_id
    assert loaded.projection.can_change_ledger is False


def test_fixed_request_rejects_schema_valid_free_text_and_risk_forgery(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    snapshot = build_private_research_snapshot(
        PrivateResearchInput(as_of=NOW),
        created_at=NOW,
    )
    request = json.loads(research_store._payload(snapshot).decode("utf-8"))
    request.pop("snapshot_id")
    request["schema_version"] = REQUEST_SCHEMA_VERSION
    request["research"] = copy.deepcopy(request["research"])
    request["research"]["overall_view"] = (
        "Bearer SECRET-TOKEN author=@private https://invalid"
    )
    request["research"]["risk_budget_multiplier"] = "999"
    paths.research_snapshot_request_file.write_text(
        json.dumps(request),
        encoding="utf-8",
    )
    os.chmod(paths.research_snapshot_request_file, 0o600)

    with pytest.raises(PrivateResearchStoreError, match="projection_invalid"):
        publish_private_research_snapshot_request(
            paths,
            prepared_at=NOW,
        )
