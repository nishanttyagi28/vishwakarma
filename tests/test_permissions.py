"""Security tests for the Forge permission engine.

These are attack tests: every test simulates an agent trying to do something
its role does not grant, and asserts the engine refuses. If any of these fail,
no agent in the organisation can be trusted with file or shell access.
"""


import pytest

from forge.permissions import (
    FsGrants,
    PermissionChecker,
    PermissionDenied,
    RoleGrant,
    normalize_rel,
)

EXPECTED_ROLES = {
    "chief",
    "software_architect",
    "prompt_architect",
    "backend",
    "frontend",
    "data_engineer",
    "ml_engineer",
    "genai_engineer",
    "qa",
    "security",
    "devops",
}


# --- role registry -----------------------------------------------------------


def test_all_org_roles_are_declared(checker):
    assert set(checker.grants) == EXPECTED_ROLES


def test_every_role_has_a_mission(checker):
    for grant in checker.grants.values():
        assert grant.mission.strip(), f"role {grant.role!r} has no mission"


def test_every_role_has_fs_read_scope(checker):
    for grant in checker.grants.values():
        assert grant.fs.read, f"role {grant.role!r} cannot read anything"


# --- filesystem grants -------------------------------------------------------


def test_backend_can_write_forge_module(checker):
    path = checker.check_fs("backend", "write", "vishwakarma:forge/permissions.py")
    assert path == checker.workspace_root("vishwakarma") / "forge" / "permissions.py"


def test_backend_cannot_write_frontend(checker):
    with pytest.raises(PermissionDenied):
        checker.check_fs("backend", "write", "returnproof:frontend/src/App.tsx")


def test_frontend_cannot_write_backend(checker):
    with pytest.raises(PermissionDenied):
        checker.check_fs("frontend", "write", "vishwakarma:forge/permissions.py")


def test_qa_cannot_write_product_code(checker):
    with pytest.raises(PermissionDenied):
        checker.check_fs("qa", "write", "vishwakarma:forge/ledger.py")


def test_qa_can_write_tests(checker):
    path = checker.check_fs("qa", "write", "vishwakarma:tests/test_extra.py")
    assert path.name == "test_extra.py"


def test_chief_cannot_write_source_code(checker):
    with pytest.raises(PermissionDenied):
        checker.check_fs("chief", "write", "vishwakarma:forge/permissions.py")


def test_chief_can_write_docs_and_reports(checker):
    checker.check_fs("chief", "write", "vishwakarma:docs/adr/ADR-0009-x.md")
    checker.check_fs("chief", "write", "vishwakarma:reports/daily/2026-11-16.md")


def test_security_is_read_only_on_product_code(checker):
    checker.check_fs("security", "read", "returnproof:backend/app/api/routes.py")
    with pytest.raises(PermissionDenied):
        checker.check_fs("security", "write", "returnproof:backend/app/api/routes.py")


def test_read_all_roles_can_read_any_workspace(checker):
    for role in ("chief", "backend", "qa", "security"):
        checker.check_fs(role, "read", "returnproof:datasets/synthetic/orders.csv")


def test_unknown_role_is_denied(checker):
    with pytest.raises(PermissionDenied):
        checker.check_fs("intern", "write", "vishwakarma:forge/permissions.py")


def test_unknown_workspace_is_denied(checker):
    with pytest.raises(PermissionDenied):
        checker.check_fs("backend", "write", "galaxy:forge/x.py")


# --- path traversal ----------------------------------------------------------


def test_traversal_escaping_workspace_is_denied(checker):
    with pytest.raises(PermissionDenied):
        checker.check_fs("backend", "write", "vishwakarma:../../Documents/evil.py")


def test_traversal_at_root_is_denied(checker):
    with pytest.raises(PermissionDenied):
        checker.check_fs("backend", "write", "vishwakarma:../returnproof/README.md")


def test_internal_dotdot_is_normalised_not_an_escape(checker):
    """a/../b stays inside the workspace and resolves to b."""
    assert normalize_rel("forge/../forge/config.py") == "forge/config.py"


def test_spec_without_workspace_is_invalid(checker):
    with pytest.raises(ValueError):
        checker.check_fs("backend", "write", "forge/permissions.py")


# --- shell allowlist ---------------------------------------------------------


def test_backend_can_run_pytest(checker):
    checker.check_shell("backend", "pytest -q tests/test_permissions.py")


def test_backend_cannot_install_packages(checker):
    with pytest.raises(PermissionDenied):
        checker.check_shell("backend", "pip install requests")


def test_backend_cannot_git_reset(checker):
    with pytest.raises(PermissionDenied):
        checker.check_shell("backend", "git reset --hard HEAD~1")


def test_no_role_can_run_destructive_commands(checker):
    for role in checker.grants:
        with pytest.raises(PermissionDenied):
            checker.check_shell(role, "rm -rf build")


def test_no_role_can_force_push(checker):
    for role in checker.grants:
        with pytest.raises(PermissionDenied):
            checker.check_shell(role, "git push --force origin main")


def test_frontend_can_build_and_cannot_pytest(checker):
    checker.check_shell("frontend", "npm run build")
    with pytest.raises(PermissionDenied):
        checker.check_shell("frontend", "pytest -q")


def test_devops_can_install_declared_dependencies(checker):
    checker.check_shell("devops", "python -m pip install -e .")


def test_empty_command_is_denied(checker):
    with pytest.raises(PermissionDenied):
        checker.check_shell("backend", "   ")


# --- deny overrides allow ----------------------------------------------------


def test_deny_overrides_allow(tmp_path):
    grant = RoleGrant(
        role="temp",
        fs=FsGrants(read=["w:"], write=["w:docs/**"]),
        shell_allow=["git"],
        shell_deny=["git push"],
    )
    checker = PermissionChecker({"temp": grant}, {"w": str(tmp_path)})
    checker.check_fs("temp", "write", "w:docs/a.md")
    with pytest.raises(PermissionDenied):
        checker.check_fs("temp", "write", "w:src/a.py")
    checker.check_shell("temp", "git status")
    with pytest.raises(PermissionDenied):
        checker.check_shell("temp", "git push origin main")


# --- named tools -------------------------------------------------------------


def test_prompt_architect_tools_are_limited(checker):
    checker.check_tool("prompt_architect", "repo.search")
    checker.check_tool("prompt_architect", "ledger.query")
    with pytest.raises(PermissionDenied):
        checker.check_tool("prompt_architect", "dispatch.task")


def test_chief_has_dispatch_but_agents_do_not(checker):
    checker.check_tool("chief", "dispatch.request")
    for role in EXPECTED_ROLES - {"chief"}:
        with pytest.raises(PermissionDenied):
            checker.check_tool(role, "dispatch.request")
