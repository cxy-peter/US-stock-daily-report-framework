from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from run_report import (
    _report_output_paths,
    _validate_external_input_privacy,
    _validate_private_repo_path,
    _validate_output_privacy,
    _validate_runtime_privacy,
    main as run_report_main,
)
from serenity_monitor.external_views import ExternalSettings
from scripts.check_public_privacy import (
    PUBLIC_TEXT_SUFFIXES,
    XHS_EXAMPLE_HEADER,
    _validate_approved_public_config,
    _validate_public_markdown,
    _validate_public_provenance,
    _validate_xhs_example,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = ROOT / "config" / "portfolio.example.yaml"


def _example_config() -> dict:
    return yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8"))


def test_public_example_is_synthetic_and_cannot_run_live():
    config = _example_config()
    assert config["runtime"]["example_only"] is True
    assert _validate_runtime_privacy(
        EXAMPLE_PATH,
        config,
        mock=True,
        no_external=True,
    ) == "synthetic_example"
    with pytest.raises(ValueError, match="--mock --no-external"):
        _validate_runtime_privacy(
            EXAMPLE_PATH,
            config,
            mock=False,
            no_external=True,
        )


def test_unclassified_configuration_is_rejected(tmp_path):
    path = tmp_path / "portfolio.private.yaml"
    with pytest.raises(ValueError, match="data_classification"):
        _validate_runtime_privacy(path, {}, mock=True, no_external=True)


def test_synthetic_classification_cannot_be_used_by_another_file(tmp_path):
    path = tmp_path / "lookalike.yaml"
    with pytest.raises(ValueError, match="reserved"):
        _validate_runtime_privacy(
            path,
            _example_config(),
            mock=True,
            no_external=True,
        )


def test_private_runtime_paths_are_ignored_and_public_output_is_rejected():
    for relative in (
        "config/portfolio.private.yaml",
        "config/strategy-profile.private.yaml",
        "config/xiaohongshu_authorized.csv",
        "private/reports/latest.md",
    ):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", relative],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, relative
    _validate_output_privacy(ROOT / "private" / "reports", "private")
    with pytest.raises(ValueError, match="not ignored"):
        _validate_output_privacy(ROOT / "public-report-output", "private")


def test_public_workflow_has_no_private_report_sink():
    workflow = (ROOT / ".github" / "workflows" / "public-framework-ci.yml").read_text(
        encoding="utf-8"
    ).casefold()
    for forbidden in (
        "schedule:",
        "github_step_summary",
        "upload-artifact",
        "download-artifact",
        "secrets.",
        "git add reports",
        "git push",
        "portfolio.private",
    ):
        assert forbidden not in workflow
    assert "portfolio.example.yaml" in workflow
    assert "--mock --no-external" in workflow


def test_private_report_body_is_not_written_to_stdout(
    tmp_path,
    monkeypatch,
    capsys,
):
    sentinel = "PRIVATE_RUNTIME_SENTINEL"
    config = _example_config()
    config["runtime"] = {
        "data_classification": "private",
        "allow_live_report": True,
    }
    config["holdings"][0]["name"] = sentinel
    config_path = tmp_path / "portfolio.private.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    out_dir = tmp_path / "private-report"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_report.py",
            "--mock",
            "--no-external",
            "--config",
            str(config_path),
            "--out-dir",
            str(out_dir),
            "--date",
            dt.date.today().isoformat(),
        ],
    )
    assert run_report_main() == 0
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err
    report_text = (out_dir / "latest.md").read_text(encoding="utf-8")
    assert sentinel in report_text
    assert "SIMULATION ONLY" in report_text
    assert not (out_dir / "state.json").exists()


def test_public_privacy_scanner_passes_for_tracked_tree():
    result = subprocess.run(
        [sys.executable, "scripts/check_public_privacy.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_privacy_scanner_covers_all_ignored_private_roots():
    scanner = (ROOT / "scripts" / "check_public_privacy.py").read_text(
        encoding="utf-8"
    )
    for protected in (
        '"private"',
        '".private-runtime"',
        '"private_runtime"',
        '"state"',
        'path.name == ".env"',
        'path.name.startswith(".env.")',
    ):
        assert protected in scanner


def _init_temp_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
    (path / ".gitignore").write_text(
        "*.private.yaml\nprivate/\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "--", ".gitignore"], cwd=path, check=True)


def test_force_tracked_private_input_is_rejected(tmp_path):
    _init_temp_git_repo(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    private_config = config_dir / "portfolio.private.yaml"
    private_config.write_text("runtime: private\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-f", "--", "config/portfolio.private.yaml"],
        cwd=tmp_path,
        check=True,
    )
    with pytest.raises(ValueError, match="tracked"):
        _validate_private_repo_path(private_config, tmp_path, "Private input")


@pytest.mark.parametrize("input_kind", ["profiles", "manual"])
def test_force_tracked_external_private_input_is_rejected(tmp_path, input_kind):
    _init_temp_git_repo(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    profiles = config_dir / "source_profiles.private.yaml"
    manual = config_dir / "manual_external_views.private.yaml"
    profiles.write_text("profiles: {}\n", encoding="utf-8")
    manual.write_text("items: []\n", encoding="utf-8")
    selected = profiles if input_kind == "profiles" else manual
    subprocess.run(
        ["git", "add", "-f", "--", selected.relative_to(tmp_path).as_posix()],
        cwd=tmp_path,
        check=True,
    )
    settings = ExternalSettings(
        enabled=True,
        source_profiles_path=str(profiles),
        manual_kol_enabled=True,
        manual_kol_path=str(manual),
    )
    with pytest.raises(ValueError, match="tracked"):
        _validate_external_input_privacy(
            settings,
            "private",
            repo_root=tmp_path,
        )


@pytest.mark.parametrize("input_kind", ["profiles", "manual"])
def test_external_private_input_symlink_to_tracked_file_is_rejected(
    tmp_path,
    input_kind,
):
    _init_temp_git_repo(tmp_path)
    public_fixture = tmp_path / "public-fixture.yaml"
    public_fixture.write_text("public: fixture\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", "public-fixture.yaml"],
        cwd=tmp_path,
        check=True,
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    profiles = config_dir / "source_profiles.private.yaml"
    manual = config_dir / "manual_external_views.private.yaml"
    try:
        if input_kind == "profiles":
            os.symlink(public_fixture, profiles)
            manual.write_text("items: []\n", encoding="utf-8")
        else:
            profiles.write_text("profiles: {}\n", encoding="utf-8")
            os.symlink(public_fixture, manual)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    settings = ExternalSettings(
        enabled=True,
        source_profiles_path=str(profiles),
        manual_kol_enabled=input_kind == "manual",
        manual_kol_path=str(manual),
    )
    with pytest.raises(ValueError, match="tracked"):
        _validate_external_input_privacy(
            settings,
            "private",
            repo_root=tmp_path,
        )


def test_force_tracked_private_output_is_rejected(tmp_path):
    _init_temp_git_repo(tmp_path)
    out_dir = tmp_path / "private" / "reports"
    out_dir.mkdir(parents=True)
    tracked_output = out_dir / "latest.md"
    tracked_output.write_text("synthetic fixture", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-f", "--", "private/reports/latest.md"],
        cwd=tmp_path,
        check=True,
    )
    output_paths = _report_output_paths(out_dir, dt.date(2026, 1, 2))
    with pytest.raises(ValueError, match="tracked"):
        _validate_output_privacy(
            out_dir,
            "private",
            output_paths,
            repo_root=tmp_path,
        )


def test_private_output_symlink_to_tracked_file_is_rejected(tmp_path):
    _init_temp_git_repo(tmp_path)
    tracked = tmp_path / "README.md"
    tracked.write_text("tracked public file", encoding="utf-8")
    subprocess.run(["git", "add", "--", "README.md"], cwd=tmp_path, check=True)
    out_dir = tmp_path / "private" / "reports"
    out_dir.mkdir(parents=True)
    link = out_dir / "latest.md"
    try:
        os.symlink(tracked, link)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    output_paths = _report_output_paths(out_dir, dt.date(2026, 1, 2))
    with pytest.raises(ValueError, match="tracked"):
        _validate_output_privacy(
            out_dir,
            "private",
            output_paths,
            repo_root=tmp_path,
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text.replace("account_value_usd: 100000", "account_value_usd: 100001"),
        lambda text: text.replace("shares: 100", "shares: 101", 1),
        lambda text: text.replace(
            "Synthetic Broad US Equity Example",
            "Changed Synthetic Name",
        ),
        lambda text: text + "\nprivate_snapshot: forbidden\n",
        lambda text: text + "\n# private comment sentinel\n",
    ],
)
def test_public_portfolio_golden_rejects_content_mutations(mutate):
    relative = "config/portfolio.example.yaml"
    original = (ROOT / relative).read_text(encoding="utf-8-sig")
    changed = mutate(original)
    assert changed != original
    with pytest.raises(RuntimeError, match="fixture content changed"):
        _validate_approved_public_config(relative, changed)


def test_source_profile_golden_rejects_same_ids_with_changed_content():
    relative = "config/source_profiles.example.yaml"
    original = (ROOT / relative).read_text(encoding="utf-8-sig")
    changed = original.replace(
        "Financial news / publisher",
        "Changed source label",
    )
    with pytest.raises(RuntimeError, match="fixture content changed"):
        _validate_approved_public_config(relative, changed)


def test_xhs_example_must_be_header_only():
    header = ",".join(XHS_EXAMPLE_HEADER)
    _validate_xhs_example(header + "\n")
    with pytest.raises(RuntimeError, match="header and no records"):
        _validate_xhs_example(
            header
            + "\nxiaohongshu,example-author,example text,2026-01-02,1,false,export,row-1\n"
        )


@pytest.mark.parametrize(
    "markdown",
    [
        "# Current Portfolio Snapshot\n",
        "Account value: $12345\n",
        "| Ticker | Shares |\n|---|---:|\n| DEMO | 10 |\n",
        "## 当前持仓\n",
    ],
)
def test_markdown_private_snapshot_patterns_are_rejected(markdown):
    with pytest.raises(RuntimeError):
        _validate_public_markdown(markdown)


@pytest.mark.parametrize(
    "private_text",
    [
        "D" + ":" + chr(92) + "private" + chr(92) + "research",
        "<" + "C" + ":" + chr(92) + "private" + chr(92) + "research>",
        "path=" + "C" + ":" + chr(92) + "private" + chr(92) + "research",
        chr(92) * 2 + "server" + chr(92) + "private" + chr(92) + "research",
        "/" + "Users/owner/research",
        "/" + "home/owner/research",
        "owner" + "@" + "mail.invalid",
        "local " + "resume and interview materials",
        "95 " + "P" + "DFs covering 4,150 P" + "DF pages",
        "95 " + "P" + "DFs\ncovering 4,150 P" + "DF pages",
        "a" * 40,
    ],
)
def test_public_provenance_rejects_private_source_fingerprints(private_text):
    with pytest.raises(RuntimeError):
        _validate_public_provenance(private_text)


def test_public_provenance_allows_documentation_example_email():
    _validate_public_provenance("research-agent test@example.com")


def test_privacy_scanner_covers_common_public_text_formats():
    assert {".json", ".ini", ".toml", ".cfg", ".ps1", ".sh"}.issubset(
        PUBLIC_TEXT_SUFFIXES
    )
