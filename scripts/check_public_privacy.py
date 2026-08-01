#!/usr/bin/env python3
"""Fail CI when private runtime data can enter the tracked public tree."""
from __future__ import annotations

import csv
import hashlib
import io
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_TICKERS = {"DEMO_EQ", "DEMO_BOND", "DEMO_CASH"}
EXAMPLE_PROFILE_IDS = {
    "official_company_filing",
    "financial_news",
    "independent_research_template",
}
ALLOWED_PUBLIC_CONFIG = {
    "config/manual_external_views.example.yaml",
    "config/portfolio.example.yaml",
    "config/source_profiles.example.yaml",
    "config/xiaohongshu_authorized.example.csv",
}
APPROVED_PUBLIC_CONFIG_DIGESTS = {
    "config/portfolio.example.yaml": "1feb627ae98cf05c9b1a3aed3ecf570a35b6bbd03f34a8468284cc13973eb837",
    "config/manual_external_views.example.yaml": "dc9724d7ba3db161b987571036d0a2ea48ecad90877af6b1535b73ff82f0ff79",
    "config/source_profiles.example.yaml": "fec5e92c8f5c192a719e37cf48373226ee1a2adc2e2bcc50e71e2c90ef21c006",
    "config/xiaohongshu_authorized.example.csv": "6ddfea4316316098f5d533a16046db9efc837cd2a85ef9eaa96cc190bb6f9ec5",
}
XHS_EXAMPLE_HEADER = [
    "platform",
    "author_id",
    "text",
    "published_at",
    "engagement",
    "sponsored",
    "collection_method",
    "record_id",
]
PRIVATE_MARKDOWN_HEADING = re.compile(
    r"(?im)^\s{0,3}#{1,6}\s+.*(?:current\s+portfolio\s+snapshot|"
    r"account\s+snapshot|broker\s+account\s+snapshot|当前持仓|持仓快照|账户快照)"
)
PRIVATE_MARKDOWN_VALUE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:account\s+(?:value|p/?l)|cash|"
    r"buying\s+power|账户(?:总值|价值|盈亏)|现金|购买力)\s*[:|]\s*"
    r"(?:usd\s*)?\$?\s*[\d,]+(?:\.\d+)?(?:\s|$)"
)
LOCAL_ABSOLUTE_PATH = re.compile(
    r"(?i)(?:^|[\s`'\"(<=>])(?:[a-z]:[\\/]|\\\\[^\\/\s]+[\\/]|"
    r"/(?:users|home)/[^/\s]+/)"
)
EMAIL_ADDRESS = re.compile(
    r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b"
)
PRIVATE_PROVENANCE = re.compile(
    r"(?i)(?:local\s+(?:resume|résumé|interview\s+materials)|"
    r"\b\d+\s+PDFs?\b[\s\S]*?\b\d[\d,]*\s+(?:PDF\s+)?pages?\b|"
    r"prior\s+live\b[\s\S]*?\bprobe\b|live\s+probe\s+observed)"
)
GIT_OBJECT_ID = re.compile(r"(?i)\b[0-9a-f]{40}\b")
PUBLIC_TEXT_SUFFIXES = {
    ".cfg",
    ".csv",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".rst",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def _tracked_files() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return {
        value.decode("utf-8")
        for value in result.stdout.split(b"\0")
        if value
    }


def _index_text(relative: str) -> str:
    result = subprocess.run(
        ["git", "show", f":{relative}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8-sig")


def _text_variants(relative: str) -> tuple[str, ...]:
    """Return both staged and working-tree text so neither can hide the other."""

    variants = [_index_text(relative)]
    worktree_path = ROOT / relative
    if worktree_path.is_file():
        worktree_text = worktree_path.read_text(encoding="utf-8-sig")
        if worktree_text != variants[0]:
            variants.append(worktree_text)
    return tuple(variants)


def _fail(message: str) -> None:
    raise RuntimeError(message)


def _normalized_public_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _validate_approved_public_config(relative: str, text: str) -> None:
    expected = APPROVED_PUBLIC_CONFIG_DIGESTS.get(relative)
    if expected is None:
        _fail("public configuration is not in the approved fixture set")
    digest = hashlib.sha256(_normalized_public_text(text).encode("utf-8")).hexdigest()
    if digest != expected:
        _fail("approved public fixture content changed; review and update its digest")


def _validate_xhs_example(text: str) -> None:
    rows = list(csv.reader(io.StringIO(_normalized_public_text(text))))
    if rows != [XHS_EXAMPLE_HEADER]:
        _fail("public Xiaohongshu example must contain exactly its header and no records")


def _validate_public_markdown(text: str) -> None:
    if PRIVATE_MARKDOWN_HEADING.search(text) or PRIVATE_MARKDOWN_VALUE.search(text):
        _fail("tracked Markdown appears to contain a private account snapshot")
    for line in text.splitlines():
        lowered = line.casefold()
        if "|" not in line:
            continue
        has_symbol = any(token in lowered for token in ("ticker", "symbol", "标的"))
        has_shares = any(token in lowered for token in ("shares", "share count", "股数"))
        if has_symbol and has_shares:
            _fail("tracked Markdown appears to contain a position table")


def _validate_public_provenance(text: str) -> None:
    if LOCAL_ABSOLUTE_PATH.search(text):
        _fail("tracked public text contains a local absolute path")
    if PRIVATE_PROVENANCE.search(text):
        _fail("tracked public text contains private corpus provenance")
    if GIT_OBJECT_ID.search(text):
        _fail("tracked public text contains a historical Git object identifier")
    for address in EMAIL_ADDRESS.findall(text):
        if not address.casefold().endswith("@example.com"):
            _fail("tracked public text contains a non-example email address")


def check_public_tree() -> None:
    tracked = _tracked_files()
    forbidden_exact = {
        "config/portfolio.yaml",
        "config/manual_external_views.yaml",
        "config/source_profiles.yaml",
    }
    leaked_exact = sorted(tracked & forbidden_exact)
    if leaked_exact:
        _fail("legacy private configuration is still tracked")

    for raw in tracked:
        path = PurePosixPath(raw)
        if ".private." in path.name:
            _fail("a private-named file is tracked")
        if path.parts and path.parts[0] in {
            "private",
            ".private-runtime",
            "private_runtime",
            "state",
        }:
            _fail("a private runtime directory is tracked")
        if path.name == ".env" or path.name.startswith(".env."):
            _fail("an environment file is tracked")
        if path.parts and path.parts[0] == "reports":
            _fail("runtime reports must not be tracked")
        if path.parts and path.parts[0] == "config" and raw not in ALLOWED_PUBLIC_CONFIG:
            _fail("an unapproved configuration file is tracked")

    required = {
        "config/portfolio.example.yaml",
        "config/manual_external_views.example.yaml",
        "config/source_profiles.example.yaml",
        "config/xiaohongshu_authorized.example.csv",
    }
    if not required.issubset(tracked):
        _fail("public example configuration is incomplete")

    for relative in sorted(APPROVED_PUBLIC_CONFIG_DIGESTS):
        for text in _text_variants(relative):
            _validate_approved_public_config(relative, text)

    portfolio_text = (ROOT / "config" / "portfolio.example.yaml").read_text(
        encoding="utf-8-sig"
    )
    portfolio = yaml.safe_load(portfolio_text) or {}
    runtime = portfolio.get("runtime", {}) or {}
    if runtime.get("data_classification") != "synthetic_example":
        _fail("public portfolio example is not classified as synthetic")
    if runtime.get("allow_live_report") is not False:
        _fail("public portfolio example may not allow live reporting")
    if runtime.get("example_only") is not True:
        _fail("public portfolio example is missing its example-only sentinel")
    if "broker_snapshot" in portfolio:
        _fail("public portfolio example may not contain a broker snapshot")
    if set(row.get("ticker") for row in portfolio.get("holdings", [])) != EXAMPLE_TICKERS:
        _fail("public portfolio example holdings differ from the synthetic fixture")
    if any(
        key in row
        for row in portfolio.get("holdings", [])
        for key in ("entry_price", "broker_pnl_usd", "broker_pnl_pct")
    ):
        _fail("public portfolio example contains cost or P/L fields")

    manual = yaml.safe_load(
        (ROOT / "config" / "manual_external_views.example.yaml").read_text(
            encoding="utf-8"
        )
    ) or {}
    if manual.get("items") != []:
        _fail("public manual evidence example must stay empty")

    profiles = yaml.safe_load(
        (ROOT / "config" / "source_profiles.example.yaml").read_text(encoding="utf-8")
    ) or {}
    if set((profiles.get("profiles") or {}).keys()) != EXAMPLE_PROFILE_IDS:
        _fail("public source-profile IDs differ from the generic fixture")

    xhs_text = (ROOT / "config" / "xiaohongshu_authorized.example.csv").read_text(
        encoding="utf-8-sig"
    )
    _validate_xhs_example(xhs_text)

    for raw in sorted(tracked):
        suffix = PurePosixPath(raw).suffix.casefold()
        if suffix in PUBLIC_TEXT_SUFFIXES:
            for tracked_text in _text_variants(raw):
                try:
                    _validate_public_provenance(tracked_text)
                    if suffix == ".md":
                        _validate_public_markdown(tracked_text)
                except RuntimeError as exc:
                    _fail(f"{raw}: {exc}")

    forbidden_workflow_terms = (
        "schedule:",
        "github_step_summary",
        "upload-artifact",
        "download-artifact",
        "secrets.",
        "git add reports",
        "portfolio.private",
    )
    for workflow_text in _text_variants(
        ".github/workflows/public-framework-ci.yml"
    ):
        workflow = workflow_text.casefold()
        if any(term in workflow for term in forbidden_workflow_terms):
            _fail("public workflow contains a private-report exfiltration sink")

    for readme_text in _text_variants("README.md"):
        readme = readme_text.casefold()
        if "current portfolio snapshot" in readme or "account p/l" in readme:
            _fail("README still describes a real account snapshot")


def main() -> int:
    try:
        check_public_tree()
    except (OSError, subprocess.SubprocessError, RuntimeError, yaml.YAMLError) as exc:
        print(f"public privacy check failed: {exc}", file=sys.stderr)
        return 1
    print("public privacy check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
