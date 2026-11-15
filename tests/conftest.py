"""Shared fixtures for Forge tests."""

from pathlib import Path

import pytest

from forge.config import load_config
from forge.permissions import PermissionChecker, load_roles

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def checker() -> PermissionChecker:
    cfg = load_config(REPO_ROOT / "forge.yaml")
    grants = load_roles(REPO_ROOT / "forge" / "roles.yaml")
    return PermissionChecker(grants, cfg.workspaces)
