"""Tests for the Prompt Architect runtime: secrets, retrieval, package
validation, the registry, and the end-to-end PA -> specialist mission."""

from pathlib import Path

import pytest
import yaml

from forge.config import load_config
from forge.ledger import TaskRecord
from forge.permissions import FsGrants, PermissionChecker, RoleGrant
from forge.prompting import (
    APPROVED_FIRST_PASS,
    Curation,
    PackageValidationError,
    PromptRegistry,
    RegistryError,
    assemble,
    contains_secret,
    gather_candidates,
    is_sensitive_path,
    pa_brief,
    redact_text,
    render,
    validate_package,
)
from forge.runtime.agent import COMPLETED, AgentRuntime
from forge.runtime.tools import ToolExecutor
from tests.test_runtime import report, turn  # helpers only; grants built locally

WS = "proj"


def local_grant(role: str, write: tuple[str, ...], shell: tuple[str, ...],
                tools: list[str]) -> RoleGrant:
    return RoleGrant(
        role=role,
        mission="test mission",
        fs=FsGrants(read=[f"{WS}:"], write=[f"{WS}:{p}" for p in write]),
        shell_allow=list(shell),
        tools=tools,
    )


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / WS
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "docs" / "SPEC.md").write_text(
        "calc add feature: calc.add must return the sum of two numbers.",
        encoding="utf-8",
    )
    (root / "src" / "utils.py").write_text("def mul(a, b):\n    return a * b\n", encoding="utf-8")
    grants = {
        "backend": local_grant("backend", ("src/**",),
                               ("git status", "git diff", "git add", "git commit"),
                               ["fs", "shell", "ledger.read"]),
        "prompt_architect": RoleGrant(
            role="prompt_architect",
            mission="curate context",
            fs=FsGrants(read=[f"{WS}:"], write=[f"{WS}:drafts/**"]),
            shell_allow=[],
            tools=["fs", "repo.search"],
        ),
        "auditor": RoleGrant(
            role="auditor",
            mission="test mission",
            fs=FsGrants(read=[f"{WS}:docs/**"], write=[f"{WS}:reports/**"]),
            shell_allow=[],
            tools=["fs"],
        ),
    }
    checker = PermissionChecker(grants, {WS: str(root)})
    config_path = tmp_path / "forge.yaml"
    config_path.write_text(yaml.safe_dump({
        "org": "T", "version": "0.0.0", "workspaces": {WS: str(root)},
        "model": {"default": "m", "lightweight": "m"},
        "limits": {"max_steps_per_run": 10, "max_revisions_per_task": 2,
                   "max_retries_per_run": 2},
        "gates": {"proj": [{"name": "echo", "command": 'python -c "print(1)"',
                            "workspace": WS}]},
    }), encoding="utf-8")
    config = load_config(config_path)
    task = TaskRecord(
        id="T-10", title="implement calc add feature", assignee_role="backend",
        status="BACKLOG", scope="src/", exclusions="no frontend files",
        acceptance="add(a,b) returns a+b; tests pass", created_at="", updated_at="",
    )
    return checker, config, task, root


# --- secrets -------------------------------------------------------------------


def test_sensitive_paths_detected(tmp_path):
    assert is_sensitive_path(tmp_path / ".env")
    assert is_sensitive_path(tmp_path / "server.key")
    assert not is_sensitive_path(tmp_path / "main.py")


def test_secret_patterns_detected_and_redacted():
    text = "api_key = supersecret12345\nsafe line\npassword=hunter222222"
    assert contains_secret(text)
    red = redact_text(text)
    assert "supersecret12345" not in red and "[REDACTED]" in red
    assert "safe line" in red


def test_private_key_shape_detected():
    assert contains_secret("-----BEGIN RSA PRIVATE KEY-----")


# --- retrieval ------------------------------------------------------------------


def test_search_ranks_and_skips_venv(env):
    checker, config, task, root = env
    (root / ".venv" / "x").mkdir(parents=True)
    (root / ".venv" / "x" / "calc_note.md").write_text("calc add calc add", encoding="utf-8")
    results = gather_candidates(task, config)
    rels = [r.split(":", 1)[1] for r in results]
    assert "docs/SPEC.md" in rels
    assert all(not r.startswith(".venv") for r in rels)


# --- package assembly and validation ----------------------------------------------


def curated():
    return Curation(
        context_selected=[f"{WS}:docs/SPEC.md", f"{WS}:src/utils.py"],
        instructions="Add add(a, b) to src; keep mul untouched.",
    )


def test_assemble_produces_valid_package(env):
    checker, config, task, root = env
    package = assemble(task, curated(), checker, config, "pkg-1",
                       prompt_id="p-T-10", prompt_version=1)
    assert package.tool_grants == {
        "fs.read": [f"{WS}:"],
        "fs.write": [f"{WS}:src/**"],
        "shell_allow": ["git status", "git diff", "git add", "git commit"],
        "tools": ["fs", "shell", "ledger.read"],
    }
    assert 'python -c "print(1)"' in package.verification_commands
    assert package.estimated_tokens > 0
    validate_package(package, task, checker, config, token_cap=100000)


def test_package_rejects_nonexistent_context(env):
    checker, config, task, root = env
    bad = Curation(context_selected=[f"{WS}:docs/does_not_exist.md"])
    with pytest.raises(PackageValidationError) as excinfo:
        assemble(task, bad, checker, config, "pkg-2", token_cap=100000)
    assert any("does not exist" in e for e in excinfo.value.errors)


def test_package_rejects_role_without_read_grant(env):
    checker, config, task, root = env
    task.assignee_role = "auditor"  # exists, but may read only docs/**
    with pytest.raises(PackageValidationError) as excinfo:
        assemble(task, curated(), checker, config, "pkg-3", token_cap=100000)
    assert any("not readable by role" in e for e in excinfo.value.errors)


def test_package_enforces_exclusion_scope_fence(env):
    checker, config, task, root = env
    package = assemble(task, curated(), checker, config, "pkg-4", token_cap=100000)
    # out_of_scope copied from exclusions; a PA cannot enlarge scope
    assert "no frontend files" in package.out_of_scope[0]
    package.out_of_scope = []
    with pytest.raises(PackageValidationError) as excinfo:
        validate_package(package, task, checker, config, token_cap=100000)
    assert any("missing from out_of_scope" in e for e in excinfo.value.errors)


def test_package_cannot_alter_grants(env):
    checker, config, task, root = env
    package = assemble(task, curated(), checker, config, "pkg-5", token_cap=100000)
    package.tool_grants["fs.write"] = [f"{WS}:**"]
    with pytest.raises(PackageValidationError) as excinfo:
        validate_package(package, task, checker, config, token_cap=100000)
    assert any("tool_grants" in e for e in excinfo.value.errors)


def test_package_token_cap_enforced(env):
    checker, config, task, root = env
    package = assemble(task, curated(), checker, config, "pkg-6", token_cap=100000)
    with pytest.raises(PackageValidationError):
        validate_package(package, task, checker, config, token_cap=10)


def test_sensitive_file_content_never_enters_context(env):
    checker, config, task, root = env
    (root / ".env").write_text("API_KEY=supersecret123456", encoding="utf-8")
    package = assemble(
        task,
        Curation(context_selected=[f"{WS}:.env", f"{WS}:docs/SPEC.md"]),
        checker, config, "pkg-7", token_cap=100000,
    )
    rendered = render(package, checker)
    assert "supersecret123456" not in rendered


# --- registry ---------------------------------------------------------------------


def test_registry_versions_and_bounds_revisions(tmp_path):
    reg = PromptRegistry(tmp_path / "prompts.db", max_revisions=2)
    try:
        v1 = reg.register("p-1", "backend", "impl", "hash1")
        assert v1 == 1
        v2 = reg.register("p-1", "backend", "impl", "hash2")
        reg.supersede("p-1", v1, v2)
        v3 = reg.register("p-1", "backend", "impl", "hash3")
        reg.supersede("p-1", v2, v3)
        with pytest.raises(RegistryError):
            reg.register("p-1", "backend", "impl", "hash4")
        assert reg.active_version("p-1").version == 3
    finally:
        reg.close()


def test_registry_outcomes_and_first_pass_rate(tmp_path):
    reg = PromptRegistry(tmp_path / "prompts.db")
    try:
        reg.register("p-1", "backend", "impl", "h")
        reg.record_outcome("p-1", 1, "r1", APPROVED_FIRST_PASS)
        reg.record_outcome("p-1", 1, "r2", "REVISED_ONCE")
        assert reg.outcome_count("p-1") == 2
        assert reg.first_pass_rate("p-1") == 0.5
        with pytest.raises(RegistryError):
            reg.record_outcome("p-1", 1, "r3", "MADE_UP")
    finally:
        reg.close()


# --- end-to-end PA mission (FakeModel) ---------------------------------------------


def test_e2e_pa_prepares_package_and_specialist_executes(env, tmp_path):
    checker, config, task, root = env

    # 1. deterministic candidate gathering
    candidates = gather_candidates(task, config)
    assert candidates

    # 2. PA agent (FakeModel) curates via repo.search then reports artifacts
    pa_script = [
        turn(action={"tool": "repo.search", "args": {"keywords": ["calc", "add"]}}),
        turn(final={
            "summary": "curated context",
            "files_changed": [], "tests_run": [], "self_assessment": "ok",
            "open_issues": [],
            "artifacts": {
                "context_selected": [f"{WS}:docs/SPEC.md", f"{WS}:src/utils.py"],
                "instructions": "Implement add(a, b) in a new module under src/.",
                "uncertainties": [], "conflicts": [],
            },
        }),
    ]
    pa_grant = checker.grants["prompt_architect"]
    named = {
        "repo.search": lambda args: _repo_search(root, args),
    }
    tools = ToolExecutor(checker, "prompt_architect", named_tools=named)
    pa = AgentRuntime(
        model=_FakeModel(pa_script), tools=tools, role="prompt_architect",
        mission=pa_grant.mission, max_steps=8, transcript_dir=tmp_path / "runs",
    )
    pa_result = pa.run("pa-run-1", pa_brief(task, candidates))
    assert pa_result.status == COMPLETED
    curation = Curation(**pa_result.report.artifacts)

    # 3. deterministic assembly + validation (code, not trust)
    package = assemble(task, curation, checker, config, "pkg-e2e",
                       prompt_id="p-T-10", prompt_version=1, token_cap=100000)
    validate_package(package, task, checker, config, token_cap=100000)

    # 4. specialist executes against the rendered package
    specialist_script = [
        turn(action={"tool": "fs.write", "args": {
            "path": f"{WS}:src/calc.py",
            "content": "def add(a, b):\n    return a + b\n"}}),
        turn(final=report(files=[f"{WS}:src/calc.py"], tests=["pytest src"])),
    ]
    sp_tools = ToolExecutor(checker, "backend")
    sp = AgentRuntime(
        model=_FakeModel(specialist_script), tools=sp_tools, role="backend",
        mission=checker.grants["backend"].mission, max_steps=8,
        transcript_dir=tmp_path / "runs",
    )
    sp_result = sp.run("sp-run-1", render(package, checker))
    assert sp_result.status == COMPLETED
    assert (root / "src" / "calc.py").read_text().startswith("def add")

    # 5. outcome recorded against the prompt version
    reg = PromptRegistry(tmp_path / "prompts.db")
    try:
        version = reg.register("p-T-10", "backend", "impl",
                               package.model_dump_json()[:64])
        reg.record_outcome("p-T-10", version, sp_result.run_id, APPROVED_FIRST_PASS)
        assert reg.first_pass_rate("p-T-10") == 1.0
    finally:
        reg.close()


def _repo_search(root: Path, args: dict) -> object:
    from forge.prompting.retrieval import search_workspace
    from forge.runtime.tools import ToolResult

    keywords = [str(k) for k in args.get("keywords", [])]
    hits = search_workspace(root, keywords)
    return ToolResult(True, "\n".join(f"{rel} (score {s})" for rel, s in hits) or "(no matches)")


class _FakeModel:
    """Adapter so tests can reuse the FakeModel from test_runtime without import cycles."""

    def __init__(self, script: list[str]) -> None:
        from forge.model.fake import FakeModel

        self._inner = FakeModel(script)

    def chat(self, messages, json_mode=False):
        return self._inner.chat(messages, json_mode)
