from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from serenity_monitor.private_runtime_config import (
    CONFIG_SCHEMA_VERSION,
    PUBLIC_EXAMPLE_NAME,
    PrivateRuntimeConfigError,
    load_private_daily_runtime_config,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config" / PUBLIC_EXAMPLE_NAME


def _document() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def _write(path: Path, document: dict) -> Path:
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def test_synthetic_example_loads_only_with_explicit_test_opt_in() -> None:
    with pytest.raises(PrivateRuntimeConfigError, match="synthetic_runtime_not_live"):
        load_private_daily_runtime_config(EXAMPLE)

    config = load_private_daily_runtime_config(EXAMPLE, allow_synthetic=True)
    assert config.simulation is True
    assert config.classification == "synthetic_example"
    assert config.providers == ("twelve_data", "alpha_vantage")
    assert tuple(config.by_symbol) == ("DEMO_BOND", "DEMO_EQ")
    assert tuple(config.dca_plan.base_amounts) == ("DEMO_BOND", "DEMO_EQ")
    assert config.opening.session.isoformat() == "2026-01-02"
    assert config.close_policy.block_bps.as_tuple().exponent == 0
    assert "DEMO_EQ" not in repr(config)


def test_private_config_requires_private_suffix_and_live_opt_in(tmp_path: Path) -> None:
    document = _document()
    document["runtime"] = {
        "data_classification": "private",
        "allow_live_report": True,
        "execution_mode": "modeled_manual_only",
    }
    wrong_name = _write(tmp_path / "owner.yaml", document)
    with pytest.raises(PrivateRuntimeConfigError, match="private_configuration_name_invalid"):
        load_private_daily_runtime_config(wrong_name)

    document["runtime"]["allow_live_report"] = False
    private_path = _write(tmp_path / "owner.private.yaml", document)
    with pytest.raises(PrivateRuntimeConfigError, match="private_live_reporting_not_enabled"):
        load_private_daily_runtime_config(private_path)


def test_unknown_fields_binary_float_and_provider_downgrade_fail_closed(tmp_path: Path) -> None:
    document = _document()
    document["private_daily_runtime"]["unexpected"] = True
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="contains_unknown_field"):
        load_private_daily_runtime_config(path, allow_synthetic=True)

    document = _document()
    document["private_daily_runtime"]["dca_plan"]["base_amounts"]["DEMO_EQ"] = 10.25
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="dca_amount_invalid"):
        load_private_daily_runtime_config(path, allow_synthetic=True)

    document = _document()
    document["private_daily_runtime"]["providers"] = ["twelve_data"]
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="required_provider_pair_invalid"):
        load_private_daily_runtime_config(path, allow_synthetic=True)


def test_corporate_action_attestations_never_default_to_clear(tmp_path: Path) -> None:
    document = _document()
    document["private_daily_runtime"]["corporate_actions"]["attestations"] = []
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    config = load_private_daily_runtime_config(path, allow_synthetic=True)
    session = config.opening.session
    as_of = config.opening.session.strftime("%Y-%m-%d") + "T23:00:00+00:00"
    import datetime as dt

    assert config.corporate_action_statuses(
        session,
        as_of=dt.datetime.fromisoformat(as_of),
    ) is None


def test_future_and_overlapping_attestations_are_rejected_or_not_usable(tmp_path: Path) -> None:
    document = _document()
    first = document["private_daily_runtime"]["corporate_actions"]["attestations"][0]
    overlapping = copy.deepcopy(first)
    overlapping["valid_from_session"] = "2026-06-01"
    overlapping["valid_through_session"] = "2027-01-01"
    document["private_daily_runtime"]["corporate_actions"]["attestations"].append(overlapping)
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="overlapping"):
        load_private_daily_runtime_config(path, allow_synthetic=True)

    document = _document()
    for row in document["private_daily_runtime"]["corporate_actions"]["attestations"]:
        row["attested_at"] = "2027-01-01T00:00:00Z"
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    config = load_private_daily_runtime_config(path, allow_synthetic=True)
    import datetime as dt

    assert config.corporate_action_statuses(
        dt.date(2026, 1, 5),
        as_of=dt.datetime(2026, 1, 6, tzinfo=dt.timezone.utc),
    ) is None


def test_schema_version_and_environment_variable_names_are_fixed(tmp_path: Path) -> None:
    document = _document()
    assert document["schema_version"] == CONFIG_SCHEMA_VERSION
    document["private_daily_runtime"]["storage_root_env"] = "bad-env"
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="storage_root_env_invalid"):
        load_private_daily_runtime_config(path, allow_synthetic=True)

    document = _document()
    document["private_daily_runtime"]["storage_root_env"] = "OTHER_PRIVATE_ROOT"
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="must_be_fixed"):
        load_private_daily_runtime_config(path, allow_synthetic=True)

    document = _document()
    document["private_daily_runtime"]["delivery"]["target_env"] = "TWELVE_DATA_API_KEY"
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="must_be_fixed"):
        load_private_daily_runtime_config(path, allow_synthetic=True)

    document = _document()
    document["private_daily_runtime"]["delivery"]["channel"] = "email"
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="must_be_codex"):
        load_private_daily_runtime_config(path, allow_synthetic=True)


def test_duplicate_keys_aliases_and_embedded_secret_fields_are_rejected(
    tmp_path: Path,
) -> None:
    original = EXAMPLE.read_text(encoding="utf-8")
    duplicate = original.replace(
        "  allow_live_report: false",
        "  allow_live_report: false\n  allow_live_report: false",
        1,
    )
    path = tmp_path / PUBLIC_EXAMPLE_NAME
    path.write_text(duplicate, encoding="utf-8")
    with pytest.raises(PrivateRuntimeConfigError, match="yaml_duplicate_key"):
        load_private_daily_runtime_config(path, allow_synthetic=True)

    anchored = original.replace(
        "  report_timezone: Asia/Shanghai",
        "  report_timezone: &zone Asia/Shanghai",
        1,
    )
    path.write_text(anchored, encoding="utf-8")
    with pytest.raises(PrivateRuntimeConfigError, match="yaml_anchor_forbidden"):
        load_private_daily_runtime_config(path, allow_synthetic=True)

    document = _document()
    document["private_daily_runtime"]["delivery"]["target"] = "private-value"
    _write(path, document)
    with pytest.raises(PrivateRuntimeConfigError, match="embedded_secret"):
        load_private_daily_runtime_config(path, allow_synthetic=True)


def test_provider_symbols_are_unique_per_provider(tmp_path: Path) -> None:
    document = _document()
    rows = document["private_daily_runtime"]["instruments"]
    rows[1]["provider_symbols"]["twelve_data"] = rows[0]["provider_symbols"][
        "twelve_data"
    ]
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="duplicate_provider_symbol"):
        load_private_daily_runtime_config(path, allow_synthetic=True)

    document = _document()
    del document["private_daily_runtime"]["instruments"][0]["provider_symbols"][
        "alpha_vantage"
    ]
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="required_provider_symbol"):
        load_private_daily_runtime_config(path, allow_synthetic=True)


def test_corporate_action_coverage_rejects_naive_as_of() -> None:
    import datetime as dt

    config = load_private_daily_runtime_config(EXAMPLE, allow_synthetic=True)
    assert config.corporate_action_statuses(
        dt.date(2026, 1, 5),
        as_of=dt.datetime(2026, 1, 6),
    ) is None


def test_calendar_identity_and_warning_settlement_cannot_be_weakened(
    tmp_path: Path,
) -> None:
    document = _document()
    document["private_daily_runtime"]["instruments"][0]["calendar_id"] = "XNAS"
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="calendar_identity_mismatch"):
        load_private_daily_runtime_config(path, allow_synthetic=True)

    document = _document()
    document["private_daily_runtime"]["close_policy"][
        "allow_warning_settlement"
    ] = True
    path = _write(tmp_path / PUBLIC_EXAMPLE_NAME, document)
    with pytest.raises(PrivateRuntimeConfigError, match="warning_settlement_forbidden"):
        load_private_daily_runtime_config(path, allow_synthetic=True)
