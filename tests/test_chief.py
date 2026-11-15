"""End-to-end tests for the Chief Engineer runtime (all agents on FakeModel)."""

import json

import pytest
import yaml

from forge.chief import ChiefEngineer, Roadmap, TaskSpec, load_roadmap
from forge.config import load_config
from forge.journal import DecisionJournal
from forge.ledger import (
    APPROVED,
    BACKLOG,
    BLOCKED_HUMAN,
    TESTING,
    TaskLedger,
)
from forge.model.fake import FakeModel
from forge.permissions import FsGrants, PermissionChecker, RoleGrant
from forge.prompting import APPROVED_FIRST_PASS, REVISED_ONCE, PromptRegistry

WS = "proj"


def build_grants():
    return {
        "backend": RoleGrant(
            role="backend", mission="implement",
            fs=FsGrants(read=[f"{WS}:"], write=[f"{WS}:src/**"]),
            shell_allow=["git status"], tools=["fs", "shell", "ledger.read"],
        ),
        "qa": RoleGrant(
            role="qa", mission="review independently",
            fs=FsGrants(read=[f"{WS}:"], write=[f"{WS}:tests/**"]),
            shell_allow=[], tools=["fs"],
        ),
        "prompt_architect": RoleGrant(
            role="prompt_architect", mission="curate context",
            fs=FsGrants(read=[f"{WS}:"], write=[f"{WS}:drafts/**"]),
            shell_allow=[], tools=["fs", "repo.search"],
        ),
    }


def pa_script():
    search = json.dumps({"thought": "search", "action": {
        "tool": "repo.search", "args": {"keywords": ["feat"]}}})
    final = json.dumps({"thought": "curated", "final": {
        "summary": "curated", "files_changed": [], "tests_run": [],
        "self_assessment": "ok", "open_issues": [],
        "artifacts": {"context_selected": [f"{WS}:docs/SPEC.md"],
                      "instructions": "implement add in src/calc.py",
                      "uncertainties": [], "conflicts": []}}})
    return [search, final]


def specialist_script():
    write = json.dumps({"thought": "write", "action": {"tool": "fs.write", "args": {
        "path": f"{WS}:src/calc.py", "content": "def add(a, b):\n    return a + b\n"}}})
    final = json.dumps({"thought": "done", "final": {
        "summary": "implemented add", "files_changed": [f"{WS}:src/calc.py"],
        "tests_run": ["pytest"], "self_assessment": "ok", "open_issues": []}})
    return [write, final]


def reviewer_script(verdict="CLEAN", findings=None):
    read = json.dumps({"thought": "checking", "action": {"tool": "fs.read", "args": {
        "path": f"{WS}:src/calc.py"}}})
    final = json.dumps({"thought": "verdict", "final": {
        "summary": "reviewed", "files_changed": [], "tests_run": [],
        "self_assessment": "ok", "open_issues": [],
        "artifacts": {"verdict": verdict, "findings": findings or []}}})
    return [read, final]


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / WS
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "docs" / "SPEC.md").write_text("feat: calc add", encoding="utf-8")
    config_path = tmp_path / "forge.yaml"
    config_path.write_text(yaml.safe_dump({
        "org": "T", "version": "0.0.0", "workspaces": {WS: str(root)},
        "model": {"default": "m", "lightweight": "m"},
        "limits": {"max_steps_per_run": 12, "max_revisions_per_task": 1,
                   "max_retries_per_run": 1},
        "gates": {"proj": [{"name": "echo", "command": 'python -c "print(1)"',
                            "workspace": WS}]},
    }), encoding="utf-8")
    config = load_config(config_path)
    checker = PermissionChecker(build_grants(), {WS: str(root)})
    ledger = TaskLedger(tmp_path / "forge.sqlite3", max_revisions=1)
    registry = PromptRegistry(tmp_path / "prompts.db", max_revisions=2)
    journal = DecisionJournal(tmp_path / "journal.db")
    models = {"prompt_architect": FakeModel(pa_script())}
    chief = ChiefEngineer(config, ledger, registry, journal, checker, models,
                          transcript_dir=tmp_path / "runs", max_steps_per_run=12)
    return chief, ledger, registry, journal, checker, config, models, root, tmp_path


def attach_specialist(env, script):
    env[6]["backend"] = FakeModel(script)


def test_full_mission_happy_path(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("CLEAN"))

    roadmap = Roadmap(tasks=[TaskSpec(
        id="T-1", title="implement add feature", role="backend",
        scope="src/", exclusions="no frontend files",
        acceptance="add works; gates pass",
    )])
    outcomes = chief.run_roadmap(roadmap)

    assert outcomes[0].status == APPROVED
    assert outcomes[0].outcome_class == APPROVED_FIRST_PASS
    assert (root / "src" / "calc.py").exists()
    assert ledger.status_of("T-1") == APPROVED
    # independent review: reviewer is a different role than implementer
    transcripts = list((tmp_path / "runs").glob("rv-T-1-*.jsonl"))
    assert transcripts, "reviewer transcript missing"
    assert registry.first_pass_rate("p-T-1") == 1.0
    report = chief.daily_report()
    assert "T-1" in report and "Approved (1)" in report
    kinds = [d["kind"] for d in journal.recent(10)]
    assert "dispatch" in kinds and "approve" in kinds


def test_revision_loop_then_approval(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["backend"] = FakeModel(specialist_script() + specialist_script())
    models["qa"] = FakeModel(reviewer_script("ISSUES", ["add lacks docstring"])
                             + reviewer_script("CLEAN"))

    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="T-2", title="implement add feature", role="backend",
        scope="src/", exclusions="no frontend files", acceptance="gates pass",
    )]))
    assert outcomes[0].status == APPROVED
    assert outcomes[0].outcome_class == REVISED_ONCE
    assert ledger.get("T-2").revisions_used == 1


def test_revision_budget_exhaustion_escalates(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["backend"] = FakeModel(specialist_script() * 3)
    models["qa"] = FakeModel(reviewer_script("ISSUES", ["still bad"]) * 3)

    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="T-3", title="implement add feature", role="backend",
        scope="src/", exclusions="no frontend", acceptance="x",
    )]))
    assert outcomes[0].status == BLOCKED_HUMAN
    assert ledger.status_of("T-3") == BLOCKED_HUMAN
    report = chief.daily_report()
    assert "Blocked-human (1)" in report


def test_human_gate_awaits_founder(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("CLEAN"))

    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="T-4", title="sensitive change", role="backend", human_gate=True,
        scope="src/", exclusions="no frontend", acceptance="x",
    )]))
    assert outcomes[0].status == TESTING
    assert outcomes[0].detail.startswith("human-gated")
    assert ledger.status_of("T-4") == TESTING  # not APPROVED without founder
    kinds = [d["kind"] for d in journal.recent(10)]
    assert "human_gate" in kinds
    # founder approval completes it through the governed path
    ledger.approve("T-4", "founder", "founder accepts")
    assert ledger.status_of("T-4") == APPROVED


def test_dependencies_gate_execution_order(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("CLEAN"))

    roadmap = Roadmap(tasks=[
        TaskSpec(id="T-5", title="first", role="backend", scope="src/",
                 exclusions="", acceptance="x"),
        TaskSpec(id="T-6", title="second", role="backend", depends=["T-5"],
                 scope="src/", exclusions="", acceptance="x"),
        TaskSpec(id="T-7", title="third", role="backend", depends=["T-6"],
                 scope="src/", exclusions="", acceptance="x"),
    ])
    outcomes = chief.run_roadmap(roadmap, max_tasks=1)
    assert outcomes[0].task_id == "T-5"
    assert ledger.status_of("T-5") == APPROVED
    assert ledger.status_of("T-6") == BACKLOG  # not yet executed, waiting correctly


def test_pa_failure_escalates_cleanly(env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = env
    models["prompt_architect"] = FakeModel(["garbage", "garbage", "garbage", "garbage"])
    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("CLEAN"))

    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="T-8", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="x",
    )]))
    assert outcomes[0].status == BLOCKED_HUMAN
    assert "PA pipeline failed" in journal.recent(1)[0]["summary"]


def test_roadmap_yaml_loading(tmp_path):
    path = tmp_path / "roadmap.json"
    path.write_text(json.dumps({"tasks": [{
        "id": "T-9", "title": "t", "role": "backend", "depends": [],
        "human_gate": False, "scope": "s", "exclusions": "e", "acceptance": "a",
    }]}), encoding="utf-8")
    roadmap = load_roadmap(path)
    assert roadmap.tasks[0].id == "T-9"
