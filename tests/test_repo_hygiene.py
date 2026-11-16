"""Repository hygiene: license, CI matrix and strict quality configuration."""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_mit_license_names_the_copyright_holder():
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026 Nishant Tyagi" in text


def test_ci_runs_quality_checks_on_both_platforms():
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for expected in (
        "ubuntu-latest", "windows-latest", 'python: ["3.12"]',
        "ruff check .", "mypy forge", "--cov=forge", "--cov-fail-under=88",
    ):
        assert expected in text


def test_project_enables_strict_mypy_and_coverage_floor():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["mypy"]["disallow_untyped_defs"] is True
    assert data["tool"]["coverage"]["report"]["fail_under"] >= 88
    assert data["project"]["scripts"]["forge"] == "forge.cli:main"
