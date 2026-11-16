"""Recovery tests: interruption detection, retry budget, git-report safety."""

from pathlib import Path

import pytest
import yaml

from forge.chief import Roadmap, TaskSpec
from forge.ledger import (
    APPROVED,
    BLOCKED_HUMAN,
    READY,
    LedgerError,
    TaskLedger,
)
from forge.recovery import plan_recovery, revert_files


@pytest.fixture()
def chief_env(tmp_path):
    from forge.chief import ChiefEngineer
    from forge.config import load_config
    from forge.journal import DecisionJournal
    from forge.model.fake import FakeModel
    from forge.permissions import PermissionChecker
    from forge.prompting import PromptRegistry
    from tests.test_chief import build_grants, pa_script

    root = tmp_path / "proj"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "docs" / "SPEC.md").write_text("feat: calc add", encoding="utf-8")
    config_path = tmp_path / "forge.yaml"
    config_path.write_text(yaml.safe_dump({
        "org": "T", "version": "0.0.0", "workspaces": {"proj": str(root)},
        "model": {"default": "m", "lightweight": "m"},
        "limits": {"max_steps_per_run": 12, "max_revisions_per_task": 1,
                   "max_retries_per_run": 1},
        "gates": {"proj": [{"name": "echo", "command": '{python} -c "print(1)"',
                            "workspace": "proj"}]},
    }), encoding="utf-8")
    config = load_config(config_path)
    checker = PermissionChecker(build_grants(), {"proj": str(root)})
    ledger = TaskLedger(tmp_path / "forge.sqlite3", max_revisions=1)
    registry = PromptRegistry(tmp_path / "prompts.db", max_revisions=2)
    journal = DecisionJournal(tmp_path / "journal.db")
    models = {"prompt_architect": FakeModel(pa_script())}
    chief = ChiefEngineer(config, ledger, registry, journal, checker, models,
                          transcript_dir=tmp_path / "runs", max_steps_per_run=12)
    return chief, ledger, registry, journal, checker, config, models, root, tmp_path


def test_recover_returns_claimed_task_to_ready():
    ledger = TaskLedger(Path(":memory:"))
    try:
        ledger.create("T-1", "t", "backend")
        ledger.promote("T-1")
        ledger.claim("T-1", "run-died")
        task = ledger.recover_task("T-1", max_retries=2)
        assert task.status == READY
        assert task.retries_used == 1
        reasons = [t.reason for t in ledger.history("T-1")]
        assert any("recovered after interruption" in r for r in reasons)
    finally:
        ledger.close()


def test_retry_budget_exhaustion_escalates_to_blocked_human():
    ledger = TaskLedger(Path(":memory:"))
    try:
        ledger.create("T-1", "t", "backend")
        ledger.promote("T-1")
        ledger.claim("T-1", "run-1")
        ledger.recover_task("T-1", max_retries=1)  # retry 1 -> READY
        ledger.claim("T-1", "run-2")
        task = ledger.recover_task("T-1", max_retries=1)  # retry 2 -> over budget
        assert task.status == BLOCKED_HUMAN
        assert task.retries_used == 2
    finally:
        ledger.close()


def test_recovery_is_noop_for_clean_states():
    ledger = TaskLedger(Path(":memory:"))
    try:
        ledger.create("T-1", "t", "backend")
        ledger.promote("T-1")
        ledger.claim("T-1", "run-1")
        ledger.submit_for_review("T-1", "backend", "d")
        ledger.request_gates("T-1")
        ledger.approve("T-1", "chief")
        # APPROVED is terminal: direct recovery must refuse to touch it
        with pytest.raises(LedgerError):
            ledger.recover_task("T-1", max_retries=2)
        assert ledger.status_of("T-1") == APPROVED
    finally:
        ledger.close()


def test_plan_recovery_reports_uncommitted_but_never_reverts(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    (root / "tracked.txt").write_text("original", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=root, check=True,
    )
    (root / "tracked.txt").write_text("modified-by-dead-run", encoding="utf-8")

    class FakeLimits:
        max_retries_per_run = 2

    class FakeConfig:
        workspaces = {"proj": str(root)}
        limits = FakeLimits()

    ledger = TaskLedger(tmp_path / "l.db")
    try:
        ledger.create("T-9", "t", "backend")
        ledger.promote("T-9")
        ledger.claim("T-9", "run-dead")
        report = plan_recovery(ledger, FakeConfig(), max_retries=2)
        assert report.interrupted[0].task_id == "T-9"
        assert report.uncommitted["proj"] == ["tracked.txt"]
        # the file is untouched by planning recovery
        assert (root / "tracked.txt").read_text() == "modified-by-dead-run"
        # only an explicit human invocation reverts it
        revert_files(root, ["tracked.txt"])
        assert (root / "tracked.txt").read_text() == "original"
    finally:
        ledger.close()


def test_chief_resumes_after_interruption(chief_env):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = chief_env
    from forge.model.fake import FakeModel
    from tests.test_chief import reviewer_script, specialist_script

    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("CLEAN"))

    roadmap = Roadmap(tasks=[TaskSpec(
        id="T-1", title="implement add feature", role="backend",
        scope="src/", exclusions="no frontend", acceptance="x",
    )])
    chief.sync_roadmap(roadmap)
    ledger.promote("T-1")
    ledger.claim("T-1", "run-that-died")  # simulate interruption

    # fresh session: recovery returns the task to READY, then the mission runs
    outcomes = chief.run_roadmap(roadmap)
    assert outcomes[0].task_id == "T-1"
    assert ledger.status_of("T-1") == APPROVED
    assert ledger.get("T-1").retries_used == 1
    kinds = [d["kind"] for d in journal.recent(20)]
    assert "recovery" in kinds
