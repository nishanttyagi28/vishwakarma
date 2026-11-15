"""F-007 acceptance: end-to-end mission verification on the FakeModel adapter.

Deterministic coverage of every scenario the live smoke run surfaced —
success, revision, failure -> BLOCKED_HUMAN -> unblock -> recovery -> success,
PA deterministic-correction loop, uncommitted-change handling. No Ollama is
touched; live smoke is an opt-in manual command (tests/test_smoke_live.py and
README).
"""
from __future__ import annotations

from forge.chief import Roadmap, TaskSpec
from forge.ledger import APPROVED, BLOCKED_HUMAN, READY
from forge.model.fake import FakeModel
from tests.test_chief import (  # noqa: F401  (fixture import)
    env,
    pa_script,
    reviewer_script,
    specialist_script,
)

# single-shot PA replies (one JSON object, no ReAct wrapper — ADR-0004)
GOOD_CURATE = (
    '{"context_selected": ["proj:docs/SPEC.md"], '
    '"instructions": "implement add in src/calc.py", '
    '"uncertainties": [], "conflicts": []}'
)
INVENTED_PATH_CURATE = (
    '{"context_selected": ["proj:NOPE.md"], '
    '"instructions": "implement add in src/calc.py", '
    '"uncertainties": [], "conflicts": []}'
)


def _chief(mission_env, pa_script_=None, backend=None, qa=None):
    chief, ledger, registry, journal, checker, config, models, root, tmp_path = mission_env
    models["prompt_architect"] = FakeModel(pa_script_ or pa_script())
    models["backend"] = FakeModel(backend or specialist_script())
    models["qa"] = FakeModel(qa or reviewer_script("CLEAN"))
    return chief, ledger, registry, journal, config, models, root, tmp_path


def test_mission_success_with_full_audit_trail(env):
    chief, ledger, registry, journal, config, models, root, tmp_path = _chief(env)
    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="S-1", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="add works; gates pass")]))
    assert outcomes[0].status == APPROVED
    # audit trail: transcripts, registry outcome, journal kinds
    assert list((tmp_path / "runs").glob("sp-S-1-*.jsonl"))
    assert list((tmp_path / "runs").glob("rv-S-1-*.jsonl"))
    kinds = [d["kind"] for d in journal.recent(10)]
    assert "dispatch" in kinds and "approve" in kinds


def test_revision_scenario(env):
    chief, ledger, registry, journal, config, models, root, tmp_path = _chief(
        env,
        backend=specialist_script() + specialist_script(),
        qa=reviewer_script("ISSUES", ["add lacks docstring"])
        + reviewer_script("CLEAN"),
    )
    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="S-2", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="gates pass")]))
    assert outcomes[0].status == APPROVED
    assert ledger.get("S-2").revisions_used == 1


def test_failure_unblock_recovery_success_cycle(env):
    """Budget exhaustion -> BLOCKED_HUMAN; chief unblock counts the retry;
    recovery passes the task through READY; a fresh mission run APPROVES it."""
    chief, ledger, registry, journal, config, models, root, tmp_path = _chief(
        env,
        backend=specialist_script() * 3,
        qa=reviewer_script("ISSUES", ["still bad"]) * 3,
    )
    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="S-3", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="x")]))
    assert outcomes[0].status == BLOCKED_HUMAN

    ledger.unblock("S-3", "chief", "bounded retry after budget exhaustion")
    assert ledger.status_of("S-3") == READY
    assert ledger.get("S-3").retries_used == 1

    models["backend"] = FakeModel(specialist_script())
    models["qa"] = FakeModel(reviewer_script("CLEAN"))
    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="S-3", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="x")]))
    assert outcomes[0].status == APPROVED
    assert ledger.get("S-3").retries_used == 1


def test_pa_correction_loop_recovers_from_invented_path(env):
    """PA curates a nonexistent path first; deterministic validation feeds the
    error back and the second single-shot reply curates correctly."""
    chief, ledger, registry, journal, config, models, root, tmp_path = _chief(
        env, pa_script_=[INVENTED_PATH_CURATE, GOOD_CURATE]
    )
    outcomes = chief.run_roadmap(Roadmap(tasks=[TaskSpec(
        id="S-4", title="implement add feature", role="backend",
        scope="src/", exclusions="", acceptance="x")]))
    assert outcomes[0].status == APPROVED


def test_uncommitted_changes_reported_not_reverted(env, tmp_path):
    chief, ledger, registry, journal, config, models, root, tmp_path = _chief(env)
    scratch = root / "scratch.txt"
    scratch.write_text("uncommitted", encoding="utf-8")
    chief.run_roadmap(Roadmap(tasks=[]))
    assert scratch.read_text(encoding="utf-8") == "uncommitted"
